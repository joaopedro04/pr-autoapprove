import unittest

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
