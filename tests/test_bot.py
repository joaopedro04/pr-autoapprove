import logging
import threading
import time
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import pr_bot
from tests.test_gate import DIFF

H1, H2 = "1" * 40, "2" * 40
ANGULAR_PKG = '{"dependencies": {"@angular/core": "^16.2.0"}}'
CFG = {
    "org": "acme", "login": "my-login", "review_teams": ["front-approvers"], "repo_suffixes": ["-mfe", "-frontend", "-ui", "-front", "-container"],
    "extra_repos": [], "exclude_repos": [], "active_since": "2026-10-14", "max_reviews_per_run": 3, "max_failures": 3,
    "settle_minutes": 10, "review_debounce_minutes": 0, "require_checks": True, "ignore_checks": {}, "claude_path": "claude", "claude_model": "",
    "claude_timeout_minutes": 30, "claude_max_budget_usd": 20, "review_skill": "my-plugin:review",
    "review_plugin": "my-plugin@my-marketplace",
}


class FakeGh:
    def __init__(self):
        self.head = H1
        self.state = "OPEN"
        self.package = ANGULAR_PKG
        self.checks_list = [{"name": "Lint", "bucket": "pass"}]
        self.threads = []
        self.review_list = []
        self.compare_status = "ahead"
        self.posted, self.approved, self.resolved = [], [], []
        self.search_items = []
        self.queries = []
        self.requested = {"users": ["my-login"], "teams": []}
        self.repo_labels = set()
        self.pr_labels = []
        self.created_labels = []
        self.added_labels = []
        self.label_error = None

    def search_prs(self, query):
        self.queries.append(query)
        return self.search_items if "user-review-requested" in query else []

    def requested_reviewers(self, owner, repo, n):
        return self.requested

    def label_exists(self, owner, repo, name):
        return name in self.repo_labels

    def create_label(self, owner, repo, label):
        self.created_labels.append(label)
        self.repo_labels.add(label["name"])

    def add_labels(self, owner, repo, n, names):
        if self.label_error:
            raise self.label_error
        self.added_labels.append(list(names))
        self.pr_labels += [{"name": name} for name in names]

    def pr_view(self, owner, repo, n):
        return {"state": self.state, "isDraft": False, "headRefOid": self.head, "baseRefName": "main", "headRefName": "feature/x",
                "author": {"login": "colega"}, "url": f"https://github.com/{owner}/{repo}/pull/{n}", "title": "t",
                "labels": list(self.pr_labels)}

    def pr_files(self, owner, repo, n):
        return [{"filename": "src/app/a.component.ts"}, {"filename": "src/app/b.html"}]

    def pr_diff(self, owner, repo, n):
        return DIFF

    def file_at(self, owner, repo, path, ref):
        return self.package if path == "package.json" else None

    def tree(self, owner, repo, sha):
        return "src/main.single-spa.ts\n"

    def compare(self, owner, repo, base, head):
        return {"status": self.compare_status, "files": []}

    def compare_diff(self, owner, repo, base, head):
        return DIFF

    def checks(self, owner, repo, n):
        return self.checks_list

    def review_threads(self, owner, repo, n):
        return self.threads

    def reviews(self, owner, repo, n):
        return self.review_list

    def post_review(self, owner, repo, n, path):
        payload = __import__("json").loads(Path(path).read_text(encoding="utf-8"))
        self.posted.append(payload)
        rid = 101 + len(self.review_list)
        self.review_list.append({"id": rid, "user": "my-login", "state": "COMMENTED", "commit_id": payload["commit_id"], "body": payload["body"]})
        return {"id": rid}

    def approve(self, owner, repo, n, sha, body):
        self.approved.append(sha)

    def reply_and_resolve(self, thread_id, body):
        self.resolved.append(thread_id)


def thread(tid, review_id, resolved=False):
    return {"id": tid, "isResolved": resolved, "path": "src/app/a.component.ts", "line": 11,
            "comments": {"nodes": [{"body": "achado", "pullRequestReview": {"databaseId": review_id}}]}}


def payload(head, comments=(), fixed=(), counts=None):
    return {"commit_id": head, "event": "COMMENT", "body": "## 🤖 review", "comments": list(comments),
            "counts": counts or {"blocker": len(comments), "high": 0, "suggestion": 0, "learning": 0},
            "fixed_threads": list(fixed)}


BLOCKER = {"path": "src/app/a.component.ts", "line": 11, "body": "**🚨 BLOCKER** x", "severity": "blocker", "confidence": "high"}
KEY = "acme/orders-mfe#7"


class BotFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(pr_bot, "BOT_DIR", Path(self.tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.gh = FakeGh()
        self.state = pr_bot.State(Path(self.tmp.name) / "state.json")
        self.state.prs[KEY] = pr_bot.new_entry("acme", "orders-mfe", 7, "https://github.com/acme/orders-mfe/pull/7")
        self.log = logging.getLogger("test-bot")
        self.log.addHandler(logging.NullHandler())
        self.log.propagate = False

    def bot(self, dry_run=False):
        return pr_bot.Bot(CFG, self.gh, self.state, self.log, dry_run)

    def entry(self):
        return self.state.prs[KEY]

    def age_head(self):
        self.entry()["head_seen_at"] = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()

    def test_full_cycle_review_then_approve_after_thread_resolved(self):
        with mock.patch("reviewer.run_claude", return_value=payload(H1, [BLOCKER])):
            self.bot().process_all()
        e = self.entry()
        self.assertEqual(e["status"], "REVIEWED")
        self.assertEqual(e["review_ids"], [101])
        self.assertEqual((e["title"], e["author"]), ("t", "colega"))
        self.assertEqual([(r["kind"], r["sha"], r["id"]) for r in e["reviews"]], [("full", H1, 101)])
        self.assertEqual(e["reviews"][0]["counts"]["blocker"], 1)
        self.assertNotIn("activity", e)
        self.assertIn("visto há menos", e["note"])
        self.assertEqual(self.gh.posted[0]["event"], "COMMENT")
        self.assertIn(f"pr-autoapprove sha={H1}", self.gh.posted[0]["body"])
        self.assertEqual(self.gh.approved, [])

        self.age_head()
        self.gh.threads = [thread("T1", 101), thread("OUTRO", 999)]
        self.bot().process_all()
        self.assertEqual(self.gh.approved, [])

        self.gh.checks_list = [{"name": "Lint", "bucket": "pending"}]
        self.gh.threads = [thread("T1", 101, resolved=True), thread("OUTRO", 999)]
        self.bot().process_all()
        self.assertEqual(self.gh.approved, [])

        self.gh.checks_list = [{"name": "Lint", "bucket": "pass"}]
        self.bot().process_all()
        self.assertEqual(self.gh.approved, [H1])
        self.assertEqual(self.entry()["status"], "APPROVED")
        self.assertEqual(self.entry()["gate"]["open_threads"], 0)
        self.assertEqual(self.entry()["gate"]["ci"], "pass")
        self.assertEqual(self.entry()["note"], f"APROVADO em {H1[:7]}")

    def test_gate_records_open_threads_even_with_ci_pending(self):
        e = self.entry()
        e.update(status="REVIEWED", reviewed_sha=H1, review_ids=[101], head_sha=H1)
        self.age_head()
        self.gh.checks_list = [{"name": "Lint", "bucket": "pending"}]
        self.gh.threads = [thread("T1", 101), thread("T2", 101, resolved=True)]
        self.bot().process_all()
        gate_info = self.entry()["gate"]
        self.assertEqual((gate_info["ci"], gate_info["open_threads"], gate_info["threads"]), ("pending", 1, 2))
        self.assertEqual(self.gh.approved, [])

    def test_activity_is_visible_while_claude_runs(self):
        seen = {}

        def fake_claude(cfg, prompt, run_dir, log):
            seen.update(pr_bot.json.loads(self.state.path.read_text(encoding="utf-8"))["prs"][KEY])
            return payload(H1)

        with mock.patch("reviewer.run_claude", side_effect=fake_claude):
            self.bot().process_all()
        self.assertEqual(seen["activity"]["kind"], "full")
        self.assertNotIn("activity", self.entry())

    def test_push_after_approval_runs_delta_and_resolves_only_bot_threads(self):
        e = self.entry()
        e.update(status="APPROVED", reviewed_sha=H1, approved_sha=H1, review_ids=[101], head_sha=H1)
        self.gh.head = H2
        self.gh.threads = [thread("T1", 101)]
        fixed = [{"thread_id": "T1", "evidence": "trocou"}, {"thread_id": "OUTRO", "evidence": "x"}]
        with mock.patch("reviewer.run_claude", return_value=payload(H2, fixed=fixed, counts={"blocker": 0})) as rc:
            self.bot().process_all()
        self.assertEqual(rc.call_count, 1)
        self.assertEqual(self.gh.posted, [])
        self.assertEqual(self.gh.resolved, ["T1"])
        self.assertEqual(self.entry()["status"], "REVIEWED")
        self.assertEqual(self.entry()["reviewed_sha"], H2)
        self.assertEqual(self.gh.approved, [])

    def test_delta_with_only_learning_posts_nothing(self):
        e = self.entry()
        e.update(status="REVIEWED", reviewed_sha=H1, review_ids=[101], head_sha=H1)
        self.gh.head = H2
        counts = {"blocker": 0, "high": 0, "suggestion": 0, "learning": 1}
        with mock.patch("reviewer.run_claude", return_value=payload(H2, counts=counts)):
            self.bot().process_all()
        self.assertEqual(self.gh.posted, [])
        self.assertEqual(self.entry()["reviewed_sha"], H2)

    def test_delta_with_new_high_is_posted(self):
        e = self.entry()
        e.update(status="REVIEWED", reviewed_sha=H1, review_ids=[101], head_sha=H1)
        self.gh.head = H2
        counts = {"blocker": 0, "high": 1, "suggestion": 0, "learning": 0}
        with mock.patch("reviewer.run_claude", return_value=payload(H2, counts=counts)):
            self.bot().process_all()
        self.assertEqual(len(self.gh.posted), 1)

    def test_debounce_waits_for_pushes_to_stop(self):
        cfg = {**CFG, "review_debounce_minutes": 5}
        e = self.entry()
        e.update(status="REVIEWED", reviewed_sha=H1, review_ids=[101], head_sha=H1)
        self.gh.head = H2
        with mock.patch("reviewer.run_claude", return_value=payload(H2, [BLOCKER])) as rc:
            pr_bot.Bot(cfg, self.gh, self.state, self.log, False).process_all()
            rc.assert_not_called()
            self.assertEqual(self.entry()["head_sha"], H2)
            self.age_head()
            pr_bot.Bot(cfg, self.gh, self.state, self.log, False).process_all()
            rc.assert_called_once()

    def test_rewritten_history_falls_back_to_full_review(self):
        e = self.entry()
        e.update(status="REVIEWED", reviewed_sha=H1, review_ids=[101], head_sha=H1)
        self.gh.head = H2
        self.gh.compare_status = "diverged"
        self.gh.threads = [thread("T1", 101)]
        self.gh.review_list = [{"id": 101, "user": "my-login", "state": "COMMENTED", "commit_id": H1, "body": f"<!-- pr-autoapprove sha={H1} -->"}]
        with mock.patch("reviewer.run_claude", return_value=payload(H2, [BLOCKER])) as rc:
            self.bot().process_all()
        prompt = rc.call_args.args[1]
        self.assertIn("open_threads.json", prompt)
        self.assertIn("histórico foi reescrito", prompt)
        self.assertEqual(self.entry()["review_ids"], [101, 102])

    def test_non_angular_is_skipped_without_claude(self):
        self.gh.package = '{"dependencies": {"react": "18"}}'
        with mock.patch("reviewer.run_claude") as rc:
            self.bot().process_all()
        rc.assert_not_called()
        self.assertEqual(self.entry()["status"], "SKIPPED")

    def test_dry_run_never_writes_to_github(self):
        with mock.patch("reviewer.run_claude", return_value=payload(H1)):
            self.bot(dry_run=True).process_all()
        self.age_head()
        self.bot(dry_run=True).process_all()
        self.assertEqual((self.gh.posted, self.gh.approved, self.gh.resolved), ([], [], []))
        self.assertEqual(self.entry()["status"], "APPROVED")

    def test_model_asking_to_approve_is_rejected(self):
        bad = {**payload(H1), "event": "APPROVE"}
        with mock.patch("reviewer.run_claude", return_value=bad):
            self.bot().process_all()
        self.assertEqual(self.gh.posted, [])
        self.assertEqual(self.entry()["failures"], 1)
        self.assertEqual(self.entry()["status"], "NEW")

    def test_three_failures_mark_error(self):
        bad = {**payload(H1), "event": "APPROVE"}
        with mock.patch("reviewer.run_claude", return_value=bad):
            for _ in range(3):
                self.bot().process_all()
        self.assertEqual(self.entry()["status"], "ERROR")

    def test_existing_bot_review_is_adopted(self):
        self.gh.review_list = [{"id": 55, "user": "my-login", "state": "COMMENTED", "commit_id": H1, "body": f"x <!-- pr-autoapprove sha={H1} -->"}]
        with mock.patch("reviewer.run_claude") as rc:
            self.bot().process_all()
        rc.assert_not_called()
        self.assertEqual(self.entry()["review_ids"], [55])
        self.assertEqual(self.entry()["status"], "REVIEWED")

    def drop_request(self, url):
        import inbox
        inbox.enqueue(Path(self.tmp.name), url)

    def test_panel_request_adds_pr_and_skips_debounce(self):
        self.state.prs.clear()
        self.drop_request("https://github.com/acme/new-mfe/pull/3/files")
        self.gh.requested = {"users": [], "teams": []}
        cfg = {**CFG, "review_debounce_minutes": 5}
        bot = pr_bot.Bot(cfg, self.gh, self.state, self.log, False)
        self.assertEqual(bot.ingest_inbox(), ["acme/new-mfe#3"])
        e = self.state.prs["acme/new-mfe#3"]
        self.assertEqual((e["source"], e["force"], e["note"]), ("manual", True, "adicionado pelo painel"))
        with mock.patch("reviewer.run_claude", return_value=payload(H1)) as rc:
            bot.process_all()
        rc.assert_called_once()
        self.assertEqual(e["status"], "REVIEWED")
        self.assertNotIn("force", e)
        self.assertEqual(list((Path(self.tmp.name) / "inbox").glob("*.json")), [])

    def test_panel_request_requeues_error(self):
        self.entry().update(status="ERROR", failures=3, last_error="boom")
        self.drop_request(self.entry()["url"])
        self.bot().ingest_inbox()
        e = self.entry()
        self.assertEqual((e["status"], e["failures"], e["force"]), ("NEW", 0, True))
        self.assertNotIn("last_error", e)

    def test_panel_request_on_reviewed_head_does_not_rereview(self):
        e = self.entry()
        e.update(status="REVIEWED", reviewed_sha=H1, review_ids=[101], head_sha=H1)
        self.age_head()
        self.gh.checks_list = [{"name": "Lint", "bucket": "pending"}]
        self.drop_request(e["url"])
        bot = self.bot()
        bot.ingest_inbox()
        with mock.patch("reviewer.run_claude") as rc:
            bot.process_all()
        rc.assert_not_called()
        self.assertNotIn("force", self.entry())
        self.assertEqual(self.gh.posted, [])

    def drop_full_request(self, url):
        import inbox
        inbox.enqueue(Path(self.tmp.name), url, full=True)

    def test_forced_full_review_on_reviewed_head(self):
        e = self.entry()
        e.update(status="APPROVED", reviewed_sha=H1, approved_sha=H1, review_ids=[101], head_sha=H1)
        self.age_head()
        self.gh.threads = [thread("T1", 101), thread("T2", 101, resolved=True)]
        self.gh.review_list = [{"id": 101, "user": "my-login", "state": "COMMENTED", "commit_id": H1, "body": ""}]
        self.drop_full_request(e["url"])
        bot = self.bot()
        bot.ingest_inbox()
        self.assertEqual(self.entry()["note"], "review completa forçada pelo painel")
        fixed = [{"thread_id": "T1", "evidence": "corrigido"}]
        with mock.patch("reviewer.run_claude", return_value=payload(H1, fixed=fixed)) as rc:
            bot.process_all()
        rc.assert_called_once()
        prompt = rc.call_args.args[1]
        self.assertIn("open_threads.json", prompt)
        open_threads = pr_bot.json.loads((Path(self.tmp.name) / "runs" / f"orders-mfe-7-{H1[:7]}-full" / "open_threads.json").read_text(encoding="utf-8"))
        self.assertEqual([t["thread_id"] for t in open_threads], ["T1"])
        self.assertEqual(self.gh.resolved, ["T1"])
        self.assertEqual(len(self.gh.posted), 1)
        self.assertNotIn("force", self.entry())
        self.assertNotIn("force_full", self.entry())
        self.assertEqual([r["kind"] for r in self.entry()["reviews"]], ["full"])

    def test_forced_full_review_waits_for_budget_instead_of_dropping(self):
        e = self.entry()
        e.update(status="REVIEWED", reviewed_sha=H1, review_ids=[101], head_sha=H1)
        self.drop_full_request(e["url"])
        bot = self.bot()
        bot.ingest_inbox()
        bot.reviews_left = 0
        with mock.patch("reviewer.run_claude") as rc:
            bot.process_all()
        rc.assert_not_called()
        self.assertTrue(self.entry().get("force_full"))
        self.assertIn("adiada", self.entry()["note"])
        self.age_head()
        bot.process_all()
        self.assertEqual(self.gh.approved, [])
        self.assertIn("adiada", self.entry()["note"])

    def test_forced_full_review_runs_even_when_bot_review_exists_for_new_entry(self):
        self.gh.review_list = [{"id": 55, "user": "my-login", "state": "COMMENTED", "commit_id": H1, "body": f"x <!-- pr-autoapprove sha={H1} -->"}]
        self.drop_full_request(self.entry()["url"])
        bot = self.bot()
        bot.ingest_inbox()
        with mock.patch("reviewer.run_claude", return_value=payload(H1)) as rc:
            bot.process_all()
        rc.assert_called_once()
        self.assertEqual(self.entry()["review_ids"][0], 55)

    def test_panel_requests_jump_the_queue(self):
        self.state.prs["acme/a-mfe#1"] = pr_bot.new_entry("acme", "a-mfe", 1, "https://github.com/acme/a-mfe/pull/1")
        self.drop_request("https://github.com/acme/z-mfe/pull/9")
        bot = self.bot()
        bot.ingest_inbox()
        order = []
        with mock.patch.object(bot, "process", side_effect=lambda key, entry: order.append(key)):
            bot.process_all()
        self.assertEqual(order[0], "acme/z-mfe#9")

    def add_prs(self, count):
        self.state.prs.clear()
        keys = []
        for i in range(count):
            key = f"acme/app{i}-mfe#{i + 1}"
            self.state.prs[key] = pr_bot.new_entry("acme", f"app{i}-mfe", i + 1, f"https://github.com/acme/app{i}-mfe/pull/{i + 1}")
            keys.append(key)
        self.gh.reviews = lambda owner, repo, n: []
        return keys

    def test_reviews_run_in_parallel_up_to_concurrency(self):
        keys = self.add_prs(5)
        cfg = {**CFG, "review_concurrency": 2, "max_reviews_per_run": 10}
        lock = threading.Lock()
        live = {"now": 0, "max": 0, "max_activity_on_disk": 0}

        def fake_claude(cfg_, prompt, run_dir, log):
            with lock:
                live["now"] += 1
                live["max"] = max(live["max"], live["now"])
                on_disk = pr_bot.json.loads(self.state.path.read_text(encoding="utf-8"))["prs"]
                live["max_activity_on_disk"] = max(live["max_activity_on_disk"], sum(1 for e in on_disk.values() if e.get("activity")))
            time.sleep(0.15)
            with lock:
                live["now"] -= 1
            return payload(H1)

        with mock.patch("reviewer.run_claude", side_effect=fake_claude) as rc:
            pr_bot.Bot(cfg, self.gh, self.state, self.log, False).process_all()
        self.assertEqual(rc.call_count, 5)
        self.assertEqual(live["max"], 2)
        self.assertLessEqual(live["max_activity_on_disk"], 2)
        self.assertEqual({self.state.prs[k]["status"] for k in keys}, {"REVIEWED"})
        self.assertFalse(any("activity" in self.state.prs[k] for k in keys))
        self.assertEqual(len(self.gh.posted), 5)

    def test_budget_still_caps_total_reviews(self):
        keys = self.add_prs(4)
        cfg = {**CFG, "review_concurrency": 3, "max_reviews_per_run": 2}
        with mock.patch("reviewer.run_claude", return_value=payload(H1)) as rc:
            pr_bot.Bot(cfg, self.gh, self.state, self.log, False).process_all()
        self.assertEqual(rc.call_count, 2)
        self.assertEqual(sorted(self.state.prs[k]["status"] for k in keys), ["NEW", "NEW", "REVIEWED", "REVIEWED"])

    def test_one_failing_review_does_not_stop_the_others(self):
        keys = self.add_prs(3)
        cfg = {**CFG, "review_concurrency": 3, "max_reviews_per_run": 10}

        def flaky(cfg_, prompt, run_dir, log):
            if "app1-mfe" in str(run_dir):
                raise pr_bot.reviewer.ReviewError("boom")
            return payload(H1)

        with mock.patch("reviewer.run_claude", side_effect=flaky):
            pr_bot.Bot(cfg, self.gh, self.state, self.log, False).process_all()
        statuses = {k: self.state.prs[k]["status"] for k in keys}
        self.assertEqual(statuses[keys[1]], "NEW")
        self.assertEqual(self.state.prs[keys[1]]["failures"], 1)
        self.assertEqual([statuses[keys[0]], statuses[keys[2]]], ["REVIEWED", "REVIEWED"])

    def test_head_moving_during_review_skips_gate(self):
        self.age_head()

        def moving(cfg_, prompt, run_dir, log):
            self.gh.head = H2
            return payload(H1)

        with mock.patch("reviewer.run_claude", side_effect=moving):
            self.bot().process_all()
        e = self.entry()
        self.assertEqual((e["status"], e["reviewed_sha"], e["head_sha"]), ("REVIEWED", H1, H2))
        self.assertIn("durante a review", e["note"])
        self.assertNotIn("gate", e)
        self.assertEqual(self.gh.approved, [])

    def test_save_retries_when_windows_locks_the_file(self):
        real_replace = pr_bot.os.replace
        calls = {"n": 0}

        def flaky_replace(src, dst):
            calls["n"] += 1
            if calls["n"] < 3:
                raise PermissionError(5, "Access is denied")
            return real_replace(src, dst)

        with mock.patch.object(pr_bot.os, "replace", side_effect=flaky_replace), mock.patch.object(pr_bot, "SAVE_RETRY_SECONDS", 0):
            self.state.save()
        self.assertEqual(calls["n"], 3)
        self.assertIn(KEY, pr_bot.json.loads(self.state.path.read_text(encoding="utf-8"))["prs"])

    def test_save_gives_up_after_many_attempts(self):
        with mock.patch.object(pr_bot.os, "replace", side_effect=PermissionError(5, "Access is denied")), \
                mock.patch.object(pr_bot, "SAVE_RETRY_SECONDS", 0), self.assertRaises(PermissionError):
            self.state.save()

    def label_bot(self, labels, dry_run=False):
        return pr_bot.Bot({**CFG, "pr_labels": labels}, self.gh, self.state, self.log, dry_run)

    GREENER = {"name": "ci-autofix", "color": "0E8A16", "description": "loop até verde"}

    def test_new_pr_gets_labels_and_missing_label_is_created(self):
        self.gh.repo_labels = {"other"}
        with mock.patch("reviewer.run_claude", return_value=payload(H1)):
            self.label_bot([self.GREENER, {"name": "other"}]).process_all()
        self.assertEqual(self.gh.created_labels, [self.GREENER])
        self.assertEqual(self.gh.added_labels, [["ci-autofix", "other"]])
        labels = self.entry()["labels"]
        self.assertEqual((labels["applied"], labels["created"]), (["ci-autofix", "other"], ["ci-autofix"]))
        self.assertNotIn("labels_pending", self.entry())
        self.assertEqual(self.entry()["status"], "REVIEWED")

    def test_labels_are_applied_only_once(self):
        bot = self.label_bot([self.GREENER])
        with mock.patch("reviewer.run_claude", return_value=payload(H1)):
            bot.process_all()
        self.gh.pr_labels = []
        self.label_bot([self.GREENER]).process_all()
        self.assertEqual(self.gh.added_labels, [["ci-autofix"]])

    def test_label_already_on_pr_is_not_added_again(self):
        self.gh.pr_labels = [{"name": "ci-autofix"}]
        with mock.patch("reviewer.run_claude", return_value=payload(H1)):
            self.label_bot([self.GREENER]).process_all()
        self.assertEqual((self.gh.added_labels, self.gh.created_labels), ([], []))
        self.assertEqual(self.entry()["labels"]["applied"], [])

    def test_existing_entries_are_not_labeled(self):
        self.entry().pop("labels_pending")
        with mock.patch("reviewer.run_claude", return_value=payload(H1)):
            self.label_bot([self.GREENER]).process_all()
        self.assertEqual(self.gh.added_labels, [])
        self.assertNotIn("labels", self.entry())

    def test_without_configured_labels_the_pr_stays_pending(self):
        with mock.patch("reviewer.run_claude", return_value=payload(H1)):
            self.label_bot([]).process_all()
        self.assertTrue(self.entry()["labels_pending"])
        self.label_bot([self.GREENER]).process_all()
        self.assertEqual(self.gh.added_labels, [["ci-autofix"]])

    def test_dry_run_does_not_touch_labels(self):
        with mock.patch("reviewer.run_claude", return_value=payload(H1)):
            self.label_bot([self.GREENER], dry_run=True).process_all()
        self.assertEqual((self.gh.added_labels, self.gh.created_labels), ([], []))
        self.assertEqual(self.entry()["labels"]["dry_run"], ["ci-autofix"])

    def test_label_failure_does_not_block_review_and_gives_up_after_3(self):
        self.gh.label_error = pr_bot.GhError(["api"], 1, "HTTP 403: Resource not accessible")
        with mock.patch("reviewer.run_claude", return_value=payload(H1)) as rc:
            self.label_bot([self.GREENER]).process_all()
        rc.assert_called_once()
        self.assertEqual(self.entry()["status"], "REVIEWED")
        self.assertEqual(self.entry()["failures"], 0)
        self.assertTrue(self.entry()["labels_pending"])
        for _ in range(2):
            self.label_bot([self.GREENER]).process_all()
        self.assertNotIn("labels_pending", self.entry())
        self.assertIn("403", self.entry()["labels"]["error"])

    def test_closed_pr_leaves_queue(self):
        self.gh.state = "MERGED"
        self.bot().process_all()
        self.assertNotIn(KEY, self.state.prs)
        done = self.state.data["done"][0]
        self.assertEqual((done["key"], done["reason"], done["title"]), (KEY, "merged", "t"))

    def test_dropped_when_review_no_longer_requested(self):
        self.gh.requested = {"users": ["outra-pessoa"], "teams": ["all-approvers"]}
        with mock.patch("reviewer.run_claude") as rc:
            self.bot().process_all()
        rc.assert_not_called()
        self.assertNotIn(KEY, self.state.prs)

    def test_team_request_keeps_pr(self):
        self.gh.requested = {"users": [], "teams": ["front-approvers"]}
        with mock.patch("reviewer.run_claude", return_value=payload(H1)) as rc:
            self.bot().process_all()
        rc.assert_called_once()
        self.assertEqual(self.entry()["status"], "REVIEWED")

    def test_manual_pr_skips_request_check(self):
        self.entry()["source"] = "manual"
        self.gh.requested = {"users": [], "teams": []}
        with mock.patch("reviewer.run_claude", return_value=payload(H1)) as rc:
            self.bot().process_all()
        rc.assert_called_once()

    def test_discover_queries_user_and_configured_teams_only(self):
        self.bot().discover()
        self.assertEqual(len(self.gh.queries), 2)
        self.assertIn("user-review-requested:my-login", self.gh.queries[0])
        self.assertIn("team-review-requested:acme/front-approvers", self.gh.queries[1])
        self.assertFalse(any(" review-requested:" in q or "all-approvers" in q for q in self.gh.queries))

    def test_discover_filters_by_suffix(self):
        self.state.prs.clear()
        self.gh.search_items = [
            {"url": "https://github.com/acme/status-front/pull/1", "number": 1, "repo_url": "https://api.github.com/repos/acme/status-front"},
            {"url": "https://github.com/acme/integration-api-test/pull/2", "number": 2, "repo_url": "https://api.github.com/repos/acme/integration-api-test"},
        ]
        self.bot().discover()
        self.assertEqual(list(self.state.prs), ["acme/status-front#1"])


if __name__ == "__main__":
    unittest.main()
