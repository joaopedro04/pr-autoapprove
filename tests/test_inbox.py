import tempfile
import unittest
from pathlib import Path

import inbox


class ParseTest(unittest.TestCase):
    def test_accepts_pr_urls(self):
        for url, expected in [
            ("https://github.com/acme/orders-mfe/pull/7", ("acme", "orders-mfe", 7)),
            ("  https://github.com/acme/orders-mfe/pull/7/files  ", ("acme", "orders-mfe", 7)),
            ("https://github.com/acme/my.repo_x/pull/12#discussion_r1", ("acme", "my.repo_x", 12)),
            ("https://github.com/acme/orders-mfe/pull/7?w=1", ("acme", "orders-mfe", 7)),
        ]:
            self.assertEqual(inbox.parse_pr_url(url), expected, url)

    def test_rejects_everything_else(self):
        for url in [
            "", None, "http://github.com/acme/x/pull/1", "https://evil.example/acme/x/pull/1",
            "https://github.com/acme/x/issues/1", "https://github.com/acme/x/pull/", "https://github.com/acme/x/pull/1abc",
            "https://github.com.evil.example/acme/x/pull/1", "javascript:alert(1)",
        ]:
            self.assertIsNone(inbox.parse_pr_url(url), url)


class QueueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_enqueue_pending_drain(self):
        self.assertEqual(inbox.pending(self.dir), [])
        a = inbox.enqueue(self.dir, "https://github.com/acme/a-mfe/pull/1/files")
        inbox.enqueue(self.dir, "https://github.com/acme/b-mfe/pull/2")
        self.assertEqual(a["url"], "https://github.com/acme/a-mfe/pull/1")
        self.assertEqual(a["key"], "acme/a-mfe#1")
        self.assertEqual([p["key"] for p in inbox.pending(self.dir)], ["acme/a-mfe#1", "acme/b-mfe#2"])
        self.assertEqual(len(inbox.signature(self.dir)), 2)
        self.assertEqual([p["key"] for p in inbox.drain(self.dir)], ["acme/a-mfe#1", "acme/b-mfe#2"])
        self.assertEqual(inbox.pending(self.dir), [])
        self.assertEqual(inbox.signature(self.dir), ())

    def test_drain_discards_garbage(self):
        (self.dir / "inbox").mkdir()
        (self.dir / "inbox" / "1.json").write_text("{nope", encoding="utf-8")
        (self.dir / "inbox" / "2.json").write_text("[1, 2]", encoding="utf-8")
        self.assertEqual(inbox.drain(self.dir), [])
        self.assertEqual(list((self.dir / "inbox").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
