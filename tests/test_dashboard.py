import http.client
import json
import tempfile
import threading
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import dashboard

H1, H2 = "1" * 40, "2" * 40
CFG = {"mode": "live", "login": "my-login", "org": "acme", "schedule_interval_minutes": 3, "claude_timeout_minutes": 30}


def iso(delta_minutes=0):
    return (datetime.now(timezone.utc) + timedelta(minutes=delta_minutes)).isoformat(timespec="seconds")


def entry(**over):
    base = {"status": "REVIEWED", "head_sha": H1, "reviewed_sha": H1, "url": "https://github.com/acme/orders-mfe/pull/7"}
    base.update(over)
    return base


class PhaseTest(unittest.TestCase):
    def test_phases(self):
        self.assertEqual(dashboard.phase(entry(status="NEW")), "queued")
        self.assertEqual(dashboard.phase(entry(head_sha=H2)), "queued")
        self.assertEqual(dashboard.phase(entry(gate={"sha": H1, "open_threads": 2})), "waiting_author")
        self.assertEqual(dashboard.phase(entry(gate={"sha": H1, "open_threads": 0, "ci": "pending"})), "waiting_ci")
        self.assertEqual(dashboard.phase(entry(gate={"sha": H2, "open_threads": 3})), "waiting_ci")
        self.assertEqual(dashboard.phase(entry(status="APPROVED", approved_sha=H1)), "approved")
        self.assertEqual(dashboard.phase(entry(status="APPROVED", approved_sha=H2)), "queued")
        self.assertEqual(dashboard.phase(entry(status="SKIPPED")), "skipped")
        self.assertEqual(dashboard.phase(entry(status="ERROR")), "error")

    def test_reviewing_only_while_activity_is_alive(self):
        active = entry(activity={"kind": "full", "started_at": iso(-2)})
        self.assertEqual(dashboard.phase(active, {"finished_at": None}, CFG), "reviewing")
        self.assertEqual(dashboard.phase(active, {"finished_at": iso(-1)}, CFG), "waiting_ci")
        stuck = entry(activity={"kind": "full", "started_at": iso(-60)})
        self.assertEqual(dashboard.phase(stuck, {"finished_at": None}, CFG), "waiting_ci")


class SnapshotTest(unittest.TestCase):
    def test_snapshot_shapes_prs_done_and_cost(self):
        today = datetime.now().astimezone().date()
        old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat(timespec="seconds")
        data = {
            "run": {"started_at": iso(-3), "finished_at": iso(-1), "reviews": 1},
            "prs": {
                "acme/orders-mfe#7": entry(
                    title="</script><b>x</b>", author="dev", failures=0, last_error="velho",
                    reviews=[
                        {"kind": "full", "sha": H1, "at": iso(-30), "counts": {"blocker": 1}, "cost_usd": 2.5, "id": 99},
                        {"kind": "delta", "sha": H2, "at": old, "counts": None, "cost_usd": 4.0, "id": None},
                    ],
                ),
            },
            "done": [{"key": "acme/x-mfe#1", "reason": "merged", "left_at": iso(-5), "reviews": [{"at": iso(-10), "cost_usd": 1.0}]}],
        }
        snap = dashboard.snapshot(data, CFG, today=today)
        pr = snap["prs"][0]
        self.assertEqual(pr["title"], "</script><b>x</b>")
        self.assertIsNone(pr["last_error"])
        self.assertEqual(pr["reviews"][0]["url"], "https://github.com/acme/orders-mfe/pull/7#pullrequestreview-99")
        self.assertIsNone(pr["reviews"][1]["url"])
        self.assertEqual(snap["done"][0]["reviews"], 1)
        self.assertEqual(snap["cost_today"], 3.5)
        self.assertEqual(snap["interval_minutes"], 3)

    def test_empty_state(self):
        snap = dashboard.snapshot(None, CFG, today=date(2026, 1, 1))
        self.assertEqual((snap["prs"], snap["done"], snap["cost_today"]), ([], [], 0))


class LogTailTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "x.log"

    def test_incremental_reads_only_complete_lines(self):
        self.path.write_bytes("a\nb\nmeia".encode("utf-8"))
        lines, offset, reset = dashboard.read_new_lines(self.path, 0)
        self.assertEqual((lines, reset), (["a", "b"], False))
        with self.path.open("ab") as f:
            f.write(" linha\nc\n".encode("utf-8"))
        lines, offset, _ = dashboard.read_new_lines(self.path, offset)
        self.assertEqual(lines, ["meia linha", "c"])
        self.assertEqual(dashboard.read_new_lines(self.path, offset)[0], [])

    def test_truncation_resets(self):
        self.path.write_text("x\n" * 10, encoding="utf-8")
        _, offset, _ = dashboard.read_new_lines(self.path, 0)
        self.path.write_text("novo\n", encoding="utf-8")
        lines, _, reset = dashboard.read_new_lines(self.path, offset)
        self.assertEqual((lines, reset), (["novo"], True))

    def test_missing_file_is_quiet(self):
        self.assertEqual(dashboard.read_new_lines(self.path, 0), ([], 0, False))


class HelpersTest(unittest.TestCase):
    def test_allowed_host(self):
        self.assertTrue(dashboard.allowed_host("127.0.0.1:8765", 8765))
        self.assertTrue(dashboard.allowed_host("localhost:8765", 8765))
        self.assertFalse(dashboard.allowed_host("evil.example:8765", 8765))
        self.assertFalse(dashboard.allowed_host("127.0.0.1:9999", 8765))
        self.assertFalse(dashboard.allowed_host("", 8765))

    def test_sse_is_single_data_line(self):
        raw = dashboard.sse("log", ["a\nb", "ç"]).decode("utf-8")
        self.assertTrue(raw.startswith("event: log\ndata: "))
        self.assertTrue(raw.endswith("\n\n"))
        self.assertEqual(raw.count("\n"), 3)
        self.assertEqual(json.loads(raw.split("data: ", 1)[1]), ["a\nb", "ç"])


class FakeSource:
    def __init__(self):
        self.snap = {"prs": [], "mode": "live"}
        self.cfg = {"mode": "live", "org": "acme"}
        self.error = None

    def load(self):
        return self.cfg, self.error

    def snapshot(self):
        return self.snap

    def signature(self):
        return (1,)


class RequestReviewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for patcher in (
            mock.patch.object(dashboard, "BOT_DIR", Path(self.tmp.name)),
            mock.patch.object(dashboard, "spawn_bot"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.source = FakeSource()

    def test_valid_request_enqueues_and_spawns(self):
        status, payload = dashboard.request_review(self.source, "https://github.com/ACME/orders-mfe/pull/7/files")
        self.assertEqual(status, 202)
        self.assertEqual(payload["key"], "ACME/orders-mfe#7")
        self.assertEqual(len(list((Path(self.tmp.name) / "inbox").glob("*.json"))), 1)
        dashboard.spawn_bot.assert_called_once()

    def test_rejections(self):
        cases = [
            ("https://github.com/other/x-mfe/pull/1", 400),
            ("https://github.com/acme/x-mfe/issues/1", 400),
            ("not a url", 400),
        ]
        for url, expected in cases:
            self.assertEqual(dashboard.request_review(self.source, url)[0], expected, url)
        self.source.cfg = {"mode": "off", "org": "acme"}
        self.assertEqual(dashboard.request_review(self.source, "https://github.com/acme/x-mfe/pull/1")[0], 409)
        self.source.cfg, self.source.error = {"mode": "off"}, "faltam no .env: GITHUB_ORG"
        self.assertEqual(dashboard.request_review(self.source, "https://github.com/acme/x-mfe/pull/1")[0], 409)
        dashboard.spawn_bot.assert_not_called()
        self.assertFalse((Path(self.tmp.name) / "inbox").exists())


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        log = Path(self.tmp.name) / "today.log"
        log.write_text("2026-10-14 10:00:00,000 INFO início\n", encoding="utf-8")
        for patcher in (
            mock.patch.object(dashboard, "today_log", return_value=log),
            mock.patch.object(dashboard, "BOT_DIR", Path(self.tmp.name)),
            mock.patch.object(dashboard, "spawn_bot"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.server = dashboard.make_server(0, FakeSource())
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.stopping = True
        self.server.shutdown()
        self.server.server_close()

    def get(self, path, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.putrequest("GET", path, skip_host=True)
        conn.putheader("Host", host or f"127.0.0.1:{self.port}")
        conn.endheaders()
        return conn, conn.getresponse()

    def test_page_and_api(self):
        conn, res = self.get("/")
        self.assertEqual(res.status, 200)
        self.assertIn("text/html", res.getheader("Content-Type"))
        self.assertIn("default-src 'none'", res.getheader("Content-Security-Policy"))
        self.assertIn(b"EventSource", res.read())
        conn.close()
        conn, res = self.get("/api/state")
        self.assertEqual(json.loads(res.read()), {"prs": [], "mode": "live"})
        conn.close()

    def post(self, body, origin="default", token="default", content_type="application/json"):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": content_type}
        if origin:
            headers["Origin"] = f"http://127.0.0.1:{self.port}" if origin == "default" else origin
        if token:
            headers["X-PR-Token"] = self.server.token if token == "default" else token
        conn.request("POST", "/api/review", body=json.dumps(body), headers=headers)
        res = conn.getresponse()
        data = json.loads(res.read() or b"{}")
        conn.close()
        return res.status, data

    def test_page_embeds_token(self):
        conn, res = self.get("/")
        html = res.read().decode("utf-8")
        conn.close()
        self.assertIn(self.server.token, html)
        self.assertNotIn("__PR_TOKEN__", html)

    def test_manual_review_happy_path(self):
        status, data = self.post({"url": "https://github.com/acme/orders-mfe/pull/7"})
        self.assertEqual((status, data["key"], data["full"]), (202, "acme/orders-mfe#7", False))
        dashboard.spawn_bot.assert_called_once()

    def test_forced_full_review_request(self):
        status, data = self.post({"url": "https://github.com/acme/orders-mfe/pull/7", "full": True})
        self.assertEqual((status, data["full"]), (202, True))
        pending = dashboard.inbox.pending(Path(self.tmp.name))
        self.assertEqual([(p["key"], p["full"]) for p in pending], [("acme/orders-mfe#7", True)])

    def test_full_flag_must_be_boolean_true(self):
        status, data = self.post({"url": "https://github.com/acme/orders-mfe/pull/7", "full": "yes"})
        self.assertEqual((status, data["full"]), (202, False))

    def test_manual_review_csrf_guards(self):
        url = {"url": "https://github.com/acme/orders-mfe/pull/7"}
        self.assertEqual(self.post(url, origin="https://evil.example")[0], 403)
        self.assertEqual(self.post(url, origin=None)[0], 403)
        self.assertEqual(self.post(url, token=None)[0], 403)
        self.assertEqual(self.post(url, token="adivinhado")[0], 403)
        self.assertEqual(self.post(url, content_type="text/plain")[0], 415)
        dashboard.spawn_bot.assert_not_called()

    def test_foreign_host_is_refused(self):
        conn, res = self.get("/api/state", host="evil.example:8765")
        self.assertEqual(res.status, 403)
        conn.close()

    def test_unknown_path(self):
        conn, res = self.get("/../.env")
        self.assertEqual(res.status, 404)
        conn.close()

    def test_event_stream_sends_state_then_log(self):
        conn, res = self.get("/api/events")
        self.assertEqual(res.getheader("Content-Type"), "text/event-stream; charset=utf-8")
        first = res.readline() + res.readline() + res.readline()
        second = res.readline() + res.readline() + res.readline()
        conn.close()
        self.assertTrue(first.startswith(b"event: state\n"))
        self.assertTrue(second.startswith(b"event: log-reset\n"))
        self.assertIn("início".encode("utf-8"), second)


if __name__ == "__main__":
    unittest.main()
