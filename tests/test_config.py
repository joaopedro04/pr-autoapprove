import tempfile
import unittest
from pathlib import Path

import config

ENV = """
# comentário
BOT_MODE=live
GITHUB_ORG=my-org
GITHUB_LOGIN="my-login"
REVIEW_TEAMS=team-a, team-b
ACTIVE_SINCE=2026-10-14
REQUIRE_CHECKS=false
IGNORE_CHECKS=repo-a:Pipeline / deploy-preview;repo-a:Wiz;repo-b:Sonar
REVIEW_SKILL=my-plugin:frontend-code-review
REVIEW_PLUGIN=my-plugin@my-marketplace
GH_PATH=C:\\Program Files\\GitHub CLI\\gh.exe
CLAUDE_PATH=~/bin/claude
"""


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env_file = Path(self.tmp.name) / ".env"

    def load(self, text, environ=None):
        self.env_file.write_text(text, encoding="utf-8")
        return config.load_config(self.env_file, environ or {})

    def test_parses_types_and_defaults(self):
        cfg = self.load(ENV)
        self.assertEqual(cfg["mode"], "live")
        self.assertEqual(cfg["login"], "my-login")
        self.assertEqual(cfg["review_teams"], ["team-a", "team-b"])
        self.assertFalse(cfg["require_checks"])
        self.assertEqual(cfg["ignore_checks"], {"repo-a": ["Pipeline / deploy-preview", "Wiz"], "repo-b": ["Sonar"]})
        self.assertEqual(cfg["gh_path"], r"C:\Program Files\GitHub CLI\gh.exe")
        self.assertNotIn("~", cfg["claude_path"])
        self.assertEqual(cfg["repo_suffixes"], ["-mfe", "-frontend", "-ui", "-front", "-container"])
        self.assertEqual(cfg["max_reviews_per_run"], 6)
        self.assertEqual(cfg["review_concurrency"], 3)
        self.assertEqual(cfg["claude_max_budget_usd"], 20.0)
        self.assertEqual(cfg["extra_repos"], [])

    def test_accepts_utf8_bom(self):
        self.env_file.write_bytes(b"\xef\xbb\xbf" + ENV.lstrip().encode("utf-8"))
        cfg = config.load_config(self.env_file, {})
        self.assertEqual(cfg["mode"], "live")

    def test_environment_overrides_dotenv(self):
        cfg = self.load(ENV, {"BOT_MODE": "off", "MAX_REVIEWS_PER_RUN": "1"})
        self.assertEqual(cfg["mode"], "off")
        self.assertEqual(cfg["max_reviews_per_run"], 1)

    def test_missing_required(self):
        with self.assertRaises(config.ConfigError) as ctx:
            self.load("BOT_MODE=live\nGITHUB_ORG=x\n")
        self.assertIn("GITHUB_LOGIN", str(ctx.exception))
        self.assertIn("REVIEW_SKILL", str(ctx.exception))

    def test_concurrency_must_be_positive(self):
        with self.assertRaises(config.ConfigError):
            self.load(ENV + "REVIEW_CONCURRENCY=0\n")

    def test_invalid_mode_and_number(self):
        with self.assertRaises(config.ConfigError):
            self.load(ENV.replace("BOT_MODE=live", "BOT_MODE=producao"))
        with self.assertRaises(config.ConfigError):
            self.load(ENV + "SETTLE_MINUTES=dez\n")

    def test_example_file_is_complete(self):
        cfg = config.load_config(config.BOT_DIR / ".env.example", {})
        self.assertEqual(cfg["mode"], "dry-run")
        self.assertEqual(cfg["review_teams"], [])


if __name__ == "__main__":
    unittest.main()
