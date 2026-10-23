import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import reviewer


class ExtractPayloadTest(unittest.TestCase):
    def test_plain(self):
        text = 'Mode: PR\n<<<PAYLOAD\n{"event": "COMMENT", "body": "x"}\nPAYLOAD>>>\n'
        self.assertEqual(reviewer.extract_payload(text), {"event": "COMMENT", "body": "x"})

    def test_fenced_and_last_block_wins(self):
        text = '<<<PAYLOAD\n{"a": 1}\nPAYLOAD>>>\nrevisão final:\n<<<PAYLOAD\n```json\n{"a": 2}\n```\nPAYLOAD>>>'
        self.assertEqual(reviewer.extract_payload(text), {"a": 2})

    def test_missing_block(self):
        with self.assertRaises(reviewer.ReviewError):
            reviewer.extract_payload("sem payload")

    def test_invalid_json(self):
        with self.assertRaises(reviewer.ReviewError):
            reviewer.extract_payload("<<<PAYLOAD\n{nope}\nPAYLOAD>>>")


class PluginRootTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.plugins = self.home / ".claude" / "plugins"
        self.cache = self.plugins / "cache" / "mkt" / "plug"
        for version in ("5.7.1", "5.31.0", "5.33.0"):
            (self.cache / version).mkdir(parents=True)

    def write_installed(self, data):
        (self.plugins / "installed_plugins.json").write_text(json.dumps(data), encoding="utf-8")

    def test_uses_installed_path(self):
        self.write_installed({"plugins": {"plug@mkt": [{"installPath": str(self.cache / "5.31.0")}]}})
        self.assertEqual(reviewer.plugin_root("plug@mkt", self.home), self.cache / "5.31.0")

    def test_falls_back_to_newest_cached_version_while_plugin_updates(self):
        self.write_installed({"plugins": {"other@x": []}})
        self.assertEqual(reviewer.plugin_root("plug@mkt", self.home), self.cache / "5.33.0")
        (self.plugins / "installed_plugins.json").write_text("{corrompido", encoding="utf-8")
        self.assertEqual(reviewer.plugin_root("plug@mkt", self.home), self.cache / "5.33.0")

    def test_missing_plugin_is_global_unavailability(self):
        self.write_installed({"plugins": {}})
        with self.assertRaises(reviewer.ClaudeUnavailable):
            reviewer.plugin_root("ghost@mkt", self.home)


class RunClaudeErrorsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run_dir = Path(self.tmp.name)
        self.cfg = {"claude_path": "claude", "claude_max_budget_usd": 1, "claude_timeout_minutes": 1, "review_plugin": "p@m", "claude_model": ""}
        patcher = mock.patch.object(reviewer, "plugin_root", return_value=self.run_dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.log = mock.Mock()

    def run_with(self, output):
        done = subprocess.CompletedProcess([], 0, stdout=json.dumps(output), stderr="")
        with mock.patch.object(reviewer.subprocess, "run", return_value=done):
            return reviewer.run_claude(self.cfg, "prompt", self.run_dir, self.log)

    def test_expired_login_is_unavailability_not_review_error(self):
        output = {"subtype": "success", "is_error": True, "result": "Failed to authenticate: OAuth session expired and could not be refreshed"}
        with self.assertRaises(reviewer.ClaudeUnavailable):
            self.run_with(output)

    def test_rate_limit_status_is_unavailability(self):
        with self.assertRaises(reviewer.ClaudeUnavailable):
            self.run_with({"subtype": "error_during_execution", "is_error": True, "api_error_status": 429, "result": "x"})

    def test_other_errors_stay_review_errors(self):
        with self.assertRaises(reviewer.ReviewError):
            self.run_with({"subtype": "error_max_turns", "is_error": True, "result": "max turns"})


class RenderTest(unittest.TestCase):
    def test_templates_have_no_leftover_placeholders(self):
        common = {
            "SKILL": "my-plugin:review", "PR_URL": "u", "LOGIN": "l", "RUN_DIR": "d", "OWNER": "o", "REPO": "r", "NUMBER": "1", "HEAD_SHA": "h",
            "HEAD_SHA7": "h", "OPEN_THREADS_SECTION": "", "FIXED_THREADS_FIELD": "", "REVIEWED_SHA": "b", "REVIEWED_SHA7": "b",
        }
        for name in ("full_review.md", "delta_review.md"):
            text = reviewer.render(name, common)
            self.assertNotIn("{{", text, name)
            self.assertTrue(text.startswith("/my-plugin:review u"), name)

    def test_open_threads_section_formats(self):
        section = reviewer.OPEN_THREADS_SECTION.format(run_dir="C:/x")
        self.assertIn("C:/x/open_threads.json", section)
        self.assertIn('[{"thread_id", "path", "line", "body"}]', section)


if __name__ == "__main__":
    unittest.main()
