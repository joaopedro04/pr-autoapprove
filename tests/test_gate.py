import unittest
from datetime import datetime, timedelta, timezone

import gate

CFG = {"repo_suffixes": ["-mfe", "-frontend", "-ui", "-front", "-container"], "extra_repos": [], "exclude_repos": []}

DIFF = """diff --git a/src/app/a.component.ts b/src/app/a.component.ts
index 1111111..2222222 100644
--- a/src/app/a.component.ts
+++ b/src/app/a.component.ts
@@ -10,4 +10,5 @@ export class A {
   one();
-  two();
+  dois();
+  tres();
   four();
@@ -40,2 +41,2 @@ export class A {
-  old();
+  novo();
   end();
diff --git a/src/app/b.html b/src/app/b.html
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/src/app/b.html
@@ -0,0 +1,2 @@
+<div>
+</div>
diff --git a/src/app/gone.ts b/src/app/gone.ts
deleted file mode 100644
index 4444444..0000000
--- a/src/app/gone.ts
+++ /dev/null
@@ -1,1 +0,0 @@
-x
"""

HEAD = "a" * 40


def thread(tid, review_id, resolved=False, path="src/app/a.component.ts", line=11):
    return {
        "id": tid, "isResolved": resolved, "path": path, "line": line,
        "comments": {"nodes": [{"body": f"achado {tid}", "pullRequestReview": {"databaseId": review_id}}]},
    }


class RepoScopeTest(unittest.TestCase):
    def test_suffixes(self):
        for name in ["orders-mfe", "checkout-frontend", "design-ui", "status-front", "shell-container"]:
            self.assertTrue(gate.repo_in_scope(name, CFG), name)
        for name in ["integration-api-test", "kiosk-attendance", "billing-backend", "mfe-tools"]:
            self.assertFalse(gate.repo_in_scope(name, CFG), name)

    def test_extra_and_exclude(self):
        cfg = {**CFG, "extra_repos": ["kiosk-attendance"], "exclude_repos": ["shell-container"]}
        self.assertTrue(gate.repo_in_scope("kiosk-attendance", cfg))
        self.assertFalse(gate.repo_in_scope("shell-container", cfg))


class FilesTest(unittest.TestCase):
    def test_reviewable(self):
        files = ["src/a.ts", "src/a.spec.ts", "package-lock.json", "README.md", ".npmrc", "src/x.scss", "node_modules/y.js"]
        self.assertEqual(gate.reviewable_files(files), ["src/a.ts", ".npmrc", "src/x.scss"])

    def test_angular_package(self):
        self.assertTrue(gate.is_angular_package('{"dependencies": {"@angular/core": "^16.2.0"}}'))
        self.assertFalse(gate.is_angular_package('{"dependencies": {"react": "18"}}'))
        self.assertFalse(gate.is_angular_package(None))


class CiVerdictTest(unittest.TestCase):
    def test_pass_with_skipping(self):
        checks = [{"name": "Lint", "bucket": "pass"}, {"name": "deploy-prod", "bucket": "skipping"}]
        self.assertEqual(gate.ci_verdict(checks), ("pass", []))

    def test_pending(self):
        checks = [{"name": "Lint", "bucket": "pass"}, {"name": "AI Review", "bucket": "pending"}]
        self.assertEqual(gate.ci_verdict(checks), ("pending", ["AI Review"]))

    def test_fail_and_cancel(self):
        checks = [{"name": "Sonar", "bucket": "fail"}, {"name": "Build", "bucket": "cancel"}, {"name": "x", "bucket": "pending"}]
        self.assertEqual(gate.ci_verdict(checks), ("fail", ["Sonar", "Build"]))

    def test_ignore_checks(self):
        checks = [{"name": "Pipeline / deploy-preview", "bucket": "fail"}, {"name": "Lint", "bucket": "pass"}]
        self.assertEqual(gate.ci_verdict(checks, ["Pipeline / deploy-preview"]), ("pass", []))

    def test_no_checks(self):
        self.assertEqual(gate.ci_verdict([])[0], "pending")
        self.assertEqual(gate.ci_verdict([], require_checks=False)[0], "pass")


class ThreadsTest(unittest.TestCase):
    def test_bot_threads_only_from_bot_reviews(self):
        threads = [thread("T1", 100), thread("T2", 200), thread("T3", 100, resolved=True), {"id": "T4", "comments": {"nodes": []}}]
        mine = gate.bot_threads(threads, [100, 300])
        self.assertEqual([t["id"] for t in mine], ["T1", "T3"])

    def test_open_summaries(self):
        summaries = gate.open_thread_summaries([thread("T1", 1), thread("T3", 1, resolved=True)])
        self.assertEqual(summaries, [{"thread_id": "T1", "path": "src/app/a.component.ts", "line": 11, "body": "achado T1"}])


class DiffTest(unittest.TestCase):
    def test_block_paths(self):
        self.assertEqual([gate.block_path(b) for b in gate.split_diff(DIFF)], ["src/app/a.component.ts", "src/app/b.html", "src/app/gone.ts"])

    def test_filter_drops_files_outside_pr(self):
        filtered = gate.filter_diff_to_files(DIFF, ["src/app/b.html"])
        self.assertTrue(filtered.startswith("diff --git a/src/app/b.html"))
        self.assertNotIn("a.component.ts", filtered)

    def test_right_lines(self):
        lines = gate.right_lines(DIFF)
        self.assertEqual(lines["src/app/a.component.ts"], {10, 11, 12, 13, 41, 42})
        self.assertEqual(lines["src/app/b.html"], {1, 2})
        self.assertEqual(lines["src/app/gone.ts"], set())


class PayloadTest(unittest.TestCase):
    def setUp(self):
        self.anchors = gate.right_lines(DIFF)

    def payload(self, **over):
        base = {"commit_id": HEAD, "event": "COMMENT", "body": "## review", "comments": []}
        base.update(over)
        return base

    def test_rejects_approve(self):
        with self.assertRaises(gate.PayloadError):
            gate.validate_payload(self.payload(event="APPROVE"), HEAD, self.anchors)

    def test_rejects_wrong_commit(self):
        with self.assertRaises(gate.PayloadError):
            gate.validate_payload(self.payload(commit_id="b" * 40), HEAD, self.anchors)

    def test_rejects_empty_body(self):
        with self.assertRaises(gate.PayloadError):
            gate.validate_payload(self.payload(body=" "), HEAD, self.anchors)

    def test_inline_routing(self):
        comments = [
            {"path": "src/app/a.component.ts", "line": 11, "body": "**🚨 BLOCKER** ok", "severity": "blocker", "confidence": "high"},
            {"path": "src/app/a.component.ts", "line": 13, "start_line": 11, "body": "range", "severity": "high"},
            {"path": "src/app/fora.ts", "line": 3, "body": "**⚠️ HIGH** fora do PR", "severity": "high"},
            {"path": "src/app/a.component.ts", "line": 30, "body": "linha fora do hunk", "severity": "high"},
            {"path": "src/app/b.html", "line": 1, "body": "**💡 SUGGESTION** nunca inline", "severity": "suggestion"},
        ]
        clean, moved = gate.validate_payload(self.payload(comments=comments), HEAD, self.anchors)
        self.assertEqual(moved, 3)
        self.assertEqual(clean["comments"][0], {"path": "src/app/a.component.ts", "side": "RIGHT", "line": 11, "body": "**🚨 BLOCKER** ok"})
        self.assertEqual(clean["comments"][1]["start_line"], 11)
        self.assertNotIn("severity", clean["comments"][1])
        self.assertIn("`src/app/fora.ts:3`", clean["body"])
        self.assertIn("nunca inline", clean["body"])

    def test_inline_cap(self):
        comments = [{"path": "src/app/a.component.ts", "line": 11, "body": f"c{i}", "severity": "high"} for i in range(35)]
        clean, moved = gate.validate_payload(self.payload(comments=comments), HEAD, self.anchors)
        self.assertEqual(len(clean["comments"]), 30)
        self.assertEqual(moved, 5)

    def test_marker_roundtrip(self):
        self.assertEqual(gate.marker_sha(gate.add_marker("body", HEAD)), HEAD)
        self.assertIsNone(gate.marker_sha(f"<!-- pr-autoapprove approve sha={HEAD} -->"))


class StateMachineTest(unittest.TestCase):
    def test_actions(self):
        self.assertEqual(gate.next_action({"status": "NEW"}, HEAD), "full_review")
        self.assertEqual(gate.next_action({"status": "REVIEWED", "reviewed_sha": HEAD}, HEAD), "gate")
        self.assertEqual(gate.next_action({"status": "REVIEWED", "reviewed_sha": "b" * 40}, HEAD), "delta_review")
        self.assertEqual(gate.next_action({"status": "APPROVED", "approved_sha": HEAD}, HEAD), "skip")
        self.assertEqual(gate.next_action({"status": "APPROVED", "approved_sha": "b" * 40}, HEAD), "delta_review")
        self.assertEqual(gate.next_action({"status": "SKIPPED", "skipped_sha": HEAD}, HEAD), "skip")
        self.assertEqual(gate.next_action({"status": "SKIPPED", "skipped_sha": "b" * 40}, HEAD), "full_review")
        self.assertEqual(gate.next_action({"status": "ERROR"}, HEAD), "skip")

    def test_settled(self):
        now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
        entry = {"head_seen_at": (now - timedelta(minutes=9)).isoformat()}
        self.assertFalse(gate.settled(entry, 10, now))
        entry["head_seen_at"] = (now - timedelta(minutes=10)).isoformat()
        self.assertTrue(gate.settled(entry, 10, now))
        self.assertFalse(gate.settled({}, 10, now))


if __name__ == "__main__":
    unittest.main()
