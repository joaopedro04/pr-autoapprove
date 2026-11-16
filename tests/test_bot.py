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
    "extra_repos": [], "exclude_repos": [], "active_since": "2026-10-14", "max_reviews_per_run": 3, "max_failures": 3, "min_changed_files": 0,
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
        self.reviews_posted_before_label = []
        self.label_error = None
        self.pr_view_error = None
        self.diff_error = None
        self.changed_files = 30

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
        self.reviews_posted_before_label.append(len(self.posted))
        self.added_labels.append(list(names))
        self.pr_labels += [{"name": name} for name in names]

    def pr_view(self, owner, repo, n):
        if self.pr_view_error:
            raise self.pr_view_error
        return {"state": self.state, "isDraft": False, "headRefOid": self.head, "baseRefName": "main", "headRefName": "feature/x",
                "author": {"login": "colega"}, "url": f"https://github.com/{owner}/{repo}/pull/{n}", "title": "t",
                "labels": list(self.pr_labels),
                **({"changedFiles": self.changed_files} if self.changed_files is not None else {})}

    def pr_files(self, owner, repo, n):
        return [{"filename": "src/app/a.component.ts"}, {"filename": "src/app/b.html"}]

    def pr_diff(self, owner, repo, n):
        if self.diff_error:
            raise self.diff_error
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

    AUTOFIX_LABEL = {"name": "ci-autofix", "color": "0E8A16", "description": "loop até verde"}

    def test_new_pr_gets_labels_and_missing_label_is_created(self):
        self.gh.repo_labels = {"other"}
        with mock.patch("reviewer.run_claude", return_value=payload(H1)):
            self.label_bot([self.AUTOFIX_LABEL, {"name": "other"}]).process_all()
        self.assertEqual(self.gh.created_labels, [self.AUTOFIX_LABEL])
        self.assertEqual(self.gh.added_labels, [["ci-autofix", "other"]])
        labels = self.entry()["labels"]
        self.assertEqual((labels["applied"], labels["created"]), (["ci-autofix", "other"], ["ci-autofix"]))
        self.assertNotIn("labels_pending", self.entry())
        self.assertEqual(self.entry()["status"], "REVIEWED")

    def test_labels_wait_for_the_first_review(self):
        cfg = {**CFG, "review_debounce_minutes": 5, "pr_labels": [self.AUTOFIX_LABEL]}
        with mock.patch("reviewer.run_claude", return_value=payload(H1, [BLOCKER])) as rc:
            pr_bot.Bot(cfg, self.gh, self.state, self.log, False).process_all()
            rc.assert_not_called()
            self.assertEqual(self.gh.added_labels, [])
            self.assertTrue(self.entry()["labels_pending"])
            self.age_head()
            pr_bot.Bot(cfg, self.gh, self.state, self.log, False).process_all()
        self.assertEqual(self.gh.added_labels, [["ci-autofix"]])
        self.assertEqual(self.gh.reviews_posted_before_label, [1])

    def test_skipped_pr_is_not_labeled(self):
        self.gh.package = '{"dependencies": {"react": "18"}}'
        self.label_bot([self.AUTOFIX_LABEL]).process_all()
        self.assertEqual(self.entry()["status"], "SKIPPED")
        self.assertEqual(self.gh.added_labels, [])

    def test_labels_are_applied_only_once(self):
        bot = self.label_bot([self.AUTOFIX_LABEL])
        with mock.patch("reviewer.run_claude", return_value=payload(H1)):
            bot.process_all()
        self.gh.pr_labels = []
        self.label_bot([self.AUTOFIX_LABEL]).process_all()
        self.assertEqual(self.gh.added_labels, [["ci-autofix"]])

    def test_label_already_on_pr_is_not_added_again(self):
        self.gh.pr_labels = [{"name": "ci-autofix"}]
        with mock.patch("reviewer.run_claude", return_value=payload(H1)):
            self.label_bot([self.AUTOFIX_LABEL]).process_all()
        self.assertEqual((self.gh.added_labels, self.gh.created_labels), ([], []))
        self.assertEqual(self.entry()["labels"]["applied"], [])

    def test_existing_entries_are_not_labeled(self):
        self.entry().pop("labels_pending")
        with mock.patch("reviewer.run_claude", return_value=payload(H1)):
            self.label_bot([self.AUTOFIX_LABEL]).process_all()
        self.assertEqual(self.gh.added_labels, [])
        self.assertNotIn("labels", self.entry())

    def test_without_configured_labels_the_pr_stays_pending(self):
        with mock.patch("reviewer.run_claude", return_value=payload(H1)):
            self.label_bot([]).process_all()
        self.assertTrue(self.entry()["labels_pending"])
        self.label_bot([self.AUTOFIX_LABEL]).process_all()
        self.assertEqual(self.gh.added_labels, [["ci-autofix"]])

    def test_dry_run_does_not_touch_labels(self):
        with mock.patch("reviewer.run_claude", return_value=payload(H1)):
            self.label_bot([self.AUTOFIX_LABEL], dry_run=True).process_all()
        self.assertEqual((self.gh.added_labels, self.gh.created_labels), ([], []))
        self.assertEqual(self.entry()["labels"]["dry_run"], ["ci-autofix"])

    def test_label_failure_does_not_block_review_and_gives_up_after_3(self):
        self.gh.label_error = pr_bot.GhError(["api"], 1, "HTTP 403: Resource not accessible")
        with mock.patch("reviewer.run_claude", return_value=payload(H1)) as rc:
            self.label_bot([self.AUTOFIX_LABEL]).process_all()
        rc.assert_called_once()
        self.assertEqual(self.entry()["status"], "REVIEWED")
        self.assertEqual(self.entry()["failures"], 0)
        self.assertTrue(self.entry()["labels_pending"])
        for _ in range(2):
            self.label_bot([self.AUTOFIX_LABEL]).process_all()
        self.assertNotIn("labels_pending", self.entry())
        self.assertIn("403", self.entry()["labels"]["error"])

    def test_claude_unavailable_pauses_reviews_without_failing_prs(self):
        keys = self.add_prs(3)
        cfg = {**CFG, "review_concurrency": 1, "max_reviews_per_run": 10}
        down = pr_bot.reviewer.ClaudeUnavailable("Failed to authenticate: OAuth session expired")
        with mock.patch("reviewer.run_claude", side_effect=down) as rc:
            pr_bot.Bot(cfg, self.gh, self.state, self.log, False).process_all()
        rc.assert_called_once()
        for key in keys:
            e = self.state.prs[key]
            self.assertEqual((e["status"], e["failures"]), ("NEW", 0), key)
            self.assertEqual(e.get("reviews", []), [], key)
        self.assertIn("adiada", self.state.prs[keys[0]]["note"])
        self.assertIn("OAuth", self.state.data["claude_down"]["message"])
        with mock.patch("reviewer.run_claude", return_value=payload(H1)):
            pr_bot.Bot(cfg, self.gh, self.state, self.log, False).process_all()
        self.assertNotIn("claude_down", self.state.data)
        self.assertEqual({self.state.prs[k]["status"] for k in keys}, {"REVIEWED"})

    def test_diff_too_large_is_skipped_instead_of_failing(self):
        self.gh.diff_error = pr_bot.GhError(["pr", "diff"], 1, "could not find pull request diff: HTTP 406: Sorry, the diff exceeded the maximum number of lines (20000)")
        with mock.patch("reviewer.run_claude") as rc:
            self.bot().process_all()
        rc.assert_not_called()
        e = self.entry()
        self.assertEqual((e["status"], e["failures"]), ("SKIPPED", 0))
        self.assertIn("20 mil linhas", e["skip_reason"])

    def test_deleted_repo_leaves_the_queue(self):
        self.gh.pr_view_error = pr_bot.GhError(["pr", "view"], 1, "GraphQL: Could not resolve to a Repository with the name 'acme/x'. (repository)")
        self.bot().process_all()
        self.assertNotIn(KEY, self.state.prs)
        self.assertEqual(self.state.data["done"][0]["reason"], "not_found")

    def test_forced_review_does_not_loop_on_error_status(self):
        self.entry().update(status="ERROR", force=True, force_full=True, failures=3)
        with mock.patch("reviewer.run_claude") as rc:
            self.bot().process_all()
        rc.assert_not_called()
        self.assertEqual(self.entry()["status"], "ERROR")

    def test_retry_from_panel_clears_skip_and_error_leftovers(self):
        self.entry().update(status="SKIPPED", skipped_sha=H1, skip_reason="nenhum arquivo revisável", last_error="x")
        self.drop_request(self.entry()["url"])
        self.bot().ingest_inbox()
        e = self.entry()
        self.assertEqual((e["status"], e["force"]), ("NEW", True))
        for gone in ("skipped_sha", "skip_reason", "last_error"):
            self.assertNotIn(gone, e)

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

    def sized_bot(self, minimum=16, **extra):
        return pr_bot.Bot({**CFG, "min_changed_files": minimum, **extra}, self.gh, self.state, self.log, False)

    def run_sized(self, bot=None):
        with mock.patch("reviewer.run_claude", return_value=payload(self.gh.head)) as rc:
            with (
                mock.patch.object(self.gh, "pr_files", wraps=self.gh.pr_files) as files,
                mock.patch.object(self.gh, "pr_diff", wraps=self.gh.pr_diff) as diff,
                mock.patch.object(self.gh, "reviews", wraps=self.gh.reviews) as self.reviews_spy,
            ):
                (bot or self.sized_bot()).process_all()
        return rc, files, diff

    def test_below_minimum_is_skipped_without_touching_claude_diff_or_labels(self):
        self.gh.changed_files = 15
        rc, files, diff = self.run_sized(self.sized_bot(pr_labels=[self.AUTOFIX_LABEL]))
        e = self.entry()
        self.assertTrue(e["labels_pending"])
        self.assertEqual(e["status"], "SKIPPED")
        self.assertEqual(e["skipped_sha"], H1)
        self.assertEqual(e["skip_reason"], "15 arquivos alterados, abaixo do mínimo de 16")
        rc.assert_not_called()
        files.assert_not_called()
        diff.assert_not_called()
        self.assertEqual(self.gh.added_labels, [])

    def test_lowering_the_minimum_releases_a_size_skipped_pr_without_push(self):
        self.gh.changed_files = 12
        self.run_sized()
        e = self.entry()
        self.assertEqual((e["status"], e["skip_kind"]), ("SKIPPED", "min_changed_files"))
        rc, _, _ = self.run_sized(self.sized_bot(10))
        rc.assert_called_once()
        self.assertEqual(self.entry()["status"], "REVIEWED")
        self.assertNotIn("skip_kind", self.entry())

    def test_disabling_the_minimum_releases_a_size_skipped_pr_without_push(self):
        self.gh.changed_files = 12
        self.run_sized()
        rc, _, _ = self.run_sized(self.sized_bot(0))
        rc.assert_called_once()
        self.assertEqual(self.entry()["status"], "REVIEWED")

    def test_size_skipped_pr_stays_skipped_while_still_below_minimum(self):
        self.gh.changed_files = 12
        self.run_sized()
        rc, _, _ = self.run_sized(self.sized_bot(14))
        rc.assert_not_called()
        self.reviews_spy.assert_not_called()
        self.assertEqual(self.entry()["status"], "SKIPPED")

    def test_release_respects_debounce(self):
        self.gh.changed_files = 12
        self.run_sized()
        bot = pr_bot.Bot({**CFG, "min_changed_files": 10, "review_debounce_minutes": 5}, self.gh, self.state, self.log, False)
        self.entry()["head_seen_at"] = datetime.now(timezone.utc).isoformat()
        rc, _, _ = self.run_sized(bot)
        rc.assert_not_called()
        self.assertEqual(self.entry()["status"], "SKIPPED")
        self.assertIn("esperando novos pushes", self.entry()["note"])

    def test_other_skip_on_same_head_is_not_released_by_the_minimum(self):
        self.gh.package = '{"dependencies": {"react": "18"}}'
        self.run_sized(self.sized_bot(0))
        e = self.entry()
        self.assertEqual(e["status"], "SKIPPED")
        self.assertNotIn("skip_kind", e)
        rc, _, _ = self.run_sized(self.sized_bot(0))
        rc.assert_not_called()
        self.reviews_spy.assert_not_called()
        self.assertEqual(self.entry()["skip_reason"], "package.json sem @angular/core")

    def test_other_skip_replaces_size_kind(self):
        self.gh.changed_files = 12
        self.run_sized()
        self.gh.head, self.gh.package = H2, '{"dependencies": {"react": "18"}}'
        self.run_sized(self.sized_bot(10))
        e = self.entry()
        self.assertEqual((e["status"], e["skipped_sha"]), ("SKIPPED", H2))
        self.assertNotIn("skip_kind", e)

    def test_request_review_reopens_discovery_entry_skipped_by_size(self):
        self.gh.changed_files = 3
        self.run_sized()
        self.assertEqual(self.entry()["source"], "discovery")
        bot = self.sized_bot()
        self.assertEqual(bot.request_review("acme", "orders-mfe", 7), KEY)
        e = self.entry()
        self.assertEqual((e["status"], e["source"], e["force"], e["note"]), ("NEW", "manual", True, "reenfileirado pelo painel"))
        for stale in ("skipped_sha", "skip_reason", "skip_kind"):
            self.assertNotIn(stale, e)
        rc, _, _ = self.run_sized(bot)
        rc.assert_called_once()
        self.assertEqual(self.entry()["status"], "REVIEWED")

    def test_request_review_reopens_error(self):
        self.entry().update(status="ERROR", failures=3, last_error="boom")
        self.sized_bot().request_review("acme", "orders-mfe", 7)
        e = self.entry()
        self.assertEqual((e["status"], e["failures"], e["force"]), ("NEW", 0, True))
        self.assertNotIn("last_error", e)

    def test_request_review_creates_entry_and_full_flag(self):
        self.state.prs.clear()
        key = self.sized_bot().request_review("acme", "new-mfe", 3, full=True)
        e = self.state.prs[key]
        self.assertEqual((e["source"], e["force"], e["force_full"], e["note"]), ("manual", True, True, "review completa forçada pelo painel"))

    def test_request_review_skips_debounce(self):
        self.gh.changed_files = 3
        bot = pr_bot.Bot({**CFG, "min_changed_files": 16, "review_debounce_minutes": 5}, self.gh, self.state, self.log, False)
        bot.request_review("acme", "orders-mfe", 7)
        rc, _, _ = self.run_sized(bot)
        rc.assert_called_once()

    def test_existing_bot_review_is_adopted_instead_of_size_skip(self):
        self.gh.changed_files = 3
        self.gh.review_list = [{"id": 55, "user": "my-login", "state": "COMMENTED", "commit_id": H1, "body": f"x <!-- pr-autoapprove sha={H1} -->"}]
        rc, files, diff = self.run_sized()
        rc.assert_not_called()
        self.assertEqual((self.entry()["status"], self.entry()["review_ids"]), ("REVIEWED", [55]))

    def test_dry_run_does_not_adopt_before_size_skip(self):
        self.gh.changed_files = 3
        self.gh.review_list = [{"id": 55, "user": "my-login", "state": "COMMENTED", "commit_id": H1, "body": f"x <!-- pr-autoapprove sha={H1} -->"}]
        bot = pr_bot.Bot({**CFG, "min_changed_files": 16}, self.gh, self.state, self.log, True)
        self.run_sized(bot)
        self.assertEqual(self.entry()["status"], "SKIPPED")

    def test_released_discovery_pr_is_retired_when_request_is_gone(self):
        self.gh.changed_files = 12
        self.run_sized()
        self.gh.requested = {"users": [], "teams": []}
        rc, _, _ = self.run_sized(self.sized_bot(10))
        rc.assert_not_called()
        self.assertNotIn(KEY, self.state.prs)
        self.assertEqual(self.state.data["done"][0]["reason"], "not_requested")

    def test_grown_discovery_pr_is_retired_when_request_is_gone(self):
        self.gh.changed_files = 12
        self.run_sized()
        self.gh.requested = {"users": [], "teams": []}
        self.gh.head, self.gh.changed_files = H2, 20
        rc, _, _ = self.run_sized()
        rc.assert_not_called()
        self.assertNotIn(KEY, self.state.prs)

    def test_released_discovery_pr_with_request_still_present_is_reviewed(self):
        self.gh.changed_files = 12
        self.run_sized()
        rc, _, _ = self.run_sized(self.sized_bot(10))
        rc.assert_called_once()

    def test_other_skip_with_new_head_is_reviewed_without_rechecking_request(self):
        self.entry().update(status="SKIPPED", skipped_sha=H1, skip_reason="nenhum arquivo revisável", head_sha=H1)
        self.gh.requested = {"users": [], "teams": []}
        self.gh.head = H2
        with mock.patch.object(self.gh, "requested_reviewers", wraps=self.gh.requested_reviewers) as req:
            rc, _, _ = self.run_sized(self.sized_bot(0))
        rc.assert_called_once()
        req.assert_not_called()
        self.assertEqual(self.entry()["status"], "REVIEWED")

    def test_request_is_rechecked_only_after_debounce_window(self):
        self.gh.changed_files = 12
        self.run_sized()
        self.gh.head, self.gh.changed_files = H2, 20
        bot = pr_bot.Bot({**CFG, "min_changed_files": 16, "review_debounce_minutes": 5}, self.gh, self.state, self.log, False)
        with mock.patch.object(self.gh, "requested_reviewers", wraps=self.gh.requested_reviewers) as req:
            rc, _, _ = self.run_sized(bot)
            rc.assert_not_called()
            req.assert_not_called()
            self.age_head()
            rc, _, _ = self.run_sized(bot)
            req.assert_called_once()
        rc.assert_called_once()

    def test_still_small_pr_does_not_recheck_request(self):
        self.gh.changed_files = 12
        self.run_sized()
        self.gh.requested = {"users": [], "teams": []}
        rc, _, _ = self.run_sized()
        rc.assert_not_called()
        self.assertEqual(self.entry()["status"], "SKIPPED")

    def test_skipped_pr_already_reviewed_by_the_bot_is_not_cut_by_size(self):
        self.entry().update(status="SKIPPED", skipped_sha=H1, skip_reason="nenhum arquivo revisável", reviewed_sha=H1, review_ids=[101], head_sha=H1)
        self.gh.head, self.gh.changed_files = H2, 2
        rc, _, _ = self.run_sized()
        rc.assert_called_once()

    def test_command_line_pr_runs_through_run_and_reviews_small_pr(self):
        self.gh.changed_files = 3
        self.gh.login = lambda: "my-login"
        self.entry().update(status="SKIPPED", skipped_sha=H1, skip_reason="x", skip_kind="min_changed_files")
        self.state.save()
        cfg = {**CFG, "min_changed_files": 16, "gh_path": "gh"}
        with mock.patch.object(pr_bot, "Gh", return_value=self.gh), mock.patch("reviewer.run_claude", return_value=payload(H1)) as rc:
            code = pr_bot.run(cfg, self.log, "live", False, "https://github.com/acme/orders-mfe/pull/7")
        self.assertEqual(code, 0)
        rc.assert_called_once()
        saved = pr_bot.State(Path(self.tmp.name) / "state.json").prs[KEY]
        self.assertEqual((saved["status"], saved["source"]), ("REVIEWED", "manual"))
        self.assertEqual(saved["reviews"][0]["kind"], "full")

    def test_command_line_pr_note_does_not_mention_panel(self):
        self.gh.changed_files = 3
        bot = self.sized_bot()
        bot.request_review("acme", "orders-mfe", 7, via="pela linha de comando")
        self.assertEqual(self.entry()["note"], "review pedido pela linha de comando")

    def test_exactly_minimum_is_reviewed(self):
        self.gh.changed_files = 16
        rc, _, _ = self.run_sized()
        rc.assert_called_once()
        self.assertEqual(self.entry()["status"], "REVIEWED")

    def test_skipped_pr_is_reevaluated_when_head_grows_past_minimum(self):
        self.gh.changed_files = 15
        self.run_sized()
        self.assertEqual(self.entry()["status"], "SKIPPED")
        self.gh.head, self.gh.changed_files = H2, 20
        rc, _, _ = self.run_sized()
        rc.assert_called_once()
        self.assertEqual(self.entry()["status"], "REVIEWED")

    def test_skipped_pr_with_new_head_still_small_stays_skipped(self):
        self.gh.changed_files = 15
        self.run_sized()
        self.gh.head = H2
        rc, _, _ = self.run_sized()
        rc.assert_not_called()
        self.reviews_spy.assert_not_called()
        self.assertEqual(self.entry()["skipped_sha"], H2)

    def test_grown_pr_respects_debounce(self):
        self.gh.changed_files = 15
        self.run_sized()
        self.gh.head, self.gh.changed_files = H2, 20
        bot = pr_bot.Bot({**CFG, "min_changed_files": 16, "review_debounce_minutes": 5}, self.gh, self.state, self.log, False)
        rc, _, _ = self.run_sized(bot)
        rc.assert_not_called()
        self.assertEqual(self.entry()["status"], "SKIPPED")
        self.assertEqual(self.entry()["skipped_sha"], H1)
        self.assertIn("esperando novos pushes", self.entry()["note"])

    def test_panel_request_reviews_small_pr(self):
        self.gh.changed_files = 3
        self.drop_request(self.entry()["url"])
        bot = self.sized_bot()
        bot.ingest_inbox()
        rc, _, _ = self.run_sized(bot)
        rc.assert_called_once()
        self.assertEqual(self.entry()["status"], "REVIEWED")

    def test_panel_request_requeues_size_skipped_pr_and_reviews_it(self):
        self.gh.changed_files = 3
        self.run_sized()
        self.assertEqual(self.entry()["status"], "SKIPPED")
        self.drop_request(self.entry()["url"])
        bot = self.sized_bot()
        bot.ingest_inbox()
        rc, _, _ = self.run_sized(bot)
        rc.assert_called_once()

    def test_manual_source_without_force_reviews_small_pr(self):
        self.gh.changed_files = 3
        self.entry()["source"] = "manual"
        rc, _, _ = self.run_sized()
        rc.assert_called_once()

    def test_reviewed_small_pr_gets_delta_review_on_new_head(self):
        self.entry().update(status="REVIEWED", reviewed_sha=H1, review_ids=[101], head_sha=H1)
        self.gh.head, self.gh.changed_files = H2, 2
        rc, _, _ = self.run_sized()
        rc.assert_called_once()
        self.assertEqual(self.entry()["reviewed_sha"], H2)

    def test_approved_small_pr_gets_delta_review_on_new_head(self):
        self.entry().update(status="APPROVED", approved_sha=H1, reviewed_sha=H1, review_ids=[101], head_sha=H1)
        self.gh.head, self.gh.changed_files = H2, 2
        rc, _, _ = self.run_sized()
        rc.assert_called_once()

    def test_forced_full_review_on_reviewed_small_pr_is_not_cut(self):
        self.entry().update(status="REVIEWED", reviewed_sha=H1, review_ids=[101], head_sha=H1, force=True, force_full=True)
        self.gh.changed_files = 2
        rc, _, _ = self.run_sized()
        rc.assert_called_once()

    def test_zero_disables_the_filter(self):
        self.gh.changed_files = 1
        rc, _, _ = self.run_sized(self.sized_bot(0))
        rc.assert_called_once()

    def test_missing_or_invalid_count_does_not_cut(self):
        for value in (None, "15", True):
            with self.subTest(value=value):
                self.state.prs[KEY] = pr_bot.new_entry("acme", "orders-mfe", 7, "https://github.com/acme/orders-mfe/pull/7")
                self.gh.changed_files = value
                self.gh.review_list = []
                rc, _, _ = self.run_sized()
                rc.assert_called_once()


if __name__ == "__main__":
    unittest.main()
