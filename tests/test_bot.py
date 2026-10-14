import logging
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
    "settle_minutes": 10, "require_checks": True, "ignore_checks": {}, "claude_path": "claude", "claude_model": "",
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

    def search_prs(self, query):
        self.queries.append(query)
        return self.search_items if "user-review-requested" in query else []

    def requested_reviewers(self, owner, repo, n):
        return self.requested

    def pr_view(self, owner, repo, n):
        return {"state": self.state, "isDraft": False, "headRefOid": self.head, "baseRefName": "main", "headRefName": "feature/x",
                "author": {"login": "colega"}, "url": f"https://github.com/{owner}/{repo}/pull/{n}", "title": "t"}

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
        self.assertIn("rebase ou force-push", prompt)
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

    def test_closed_pr_leaves_queue(self):
        self.gh.state = "MERGED"
        self.bot().process_all()
        self.assertNotIn(KEY, self.state.prs)

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
