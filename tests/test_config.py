import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from smolnugget.config import AppConfig, ConfigError, Store, name_from_url, token_var_for


def make(**kw):
    base = dict(name="bot", url="https://github.com/u/r.git", command=["python", "bot.py"])
    base.update(kw)
    return AppConfig(**base)


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip(self):
        app = make(env={"A": "1"}, setup=["echo hi"], token_env="GH", enabled=False)
        self.store.save({"bot": app})
        self.assertEqual(self.store.load()["bot"], app)

    def test_empty_home(self):
        self.assertEqual(self.store.load(), {})

    def test_names(self):
        make(name="ihor_bot").validate()
        make(name="2").validate()
        for bad in ("", "../x", "a/b", "-x", "a b"):
            with self.assertRaises(ConfigError):
                make(name=bad).validate()

    def test_name_from_url(self):
        self.assertEqual(name_from_url("https://github.com/u/my-bot.git"), "my-bot")
        self.assertEqual(name_from_url("https://github.com/u/my-bot/"), "my-bot")
        self.assertEqual(name_from_url("git@github.com:u/Bot.v2.git"), "Bot.v2")
        self.assertEqual(token_var_for("my-bot.v2"), "MY_BOT_V2_TOKEN")

    def test_credentials_in_url_rejected(self):
        with self.assertRaises(ConfigError):
            make(url="https://ghp_secret@github.com/u/r.git").validate()
        make(url="git@github.com:u/r.git").validate()

    def test_invalid_values(self):
        for kw in ({"restart": "sometimes"}, {"check_interval": 1}, {"command": []},
                   {"token_env": "bad var"}):
            with self.assertRaises(ConfigError):
                make(**kw).validate()

    def test_unknown_key_rejected(self):
        self.store.home.mkdir(parents=True, exist_ok=True)
        self.store.config_path.write_text(json.dumps(
            {"apps": {"bot": {"url": "x", "command": ["a"], "colour": "red"}}}))
        with self.assertRaises(ConfigError):
            self.store.load()

    def test_legacy_config_explained(self):
        self.store.home.mkdir(parents=True, exist_ok=True)
        self.store.config_path.write_text(json.dumps({"repos": {"2": {"url": "x"}}}))
        with self.assertRaisesRegex(ConfigError, "snt add"):
            self.store.load()

    def test_v1_inline_token_moves_to_secrets(self):
        self.store.home.mkdir(parents=True, exist_ok=True)
        self.store.config_path.write_text(json.dumps({"version": 1, "apps": {"bot": {
            "url": "https://github.com/u/r.git", "command": ["a"], "token": "ghp_x"}}}))
        app = self.store.load()["bot"]
        self.assertEqual(app.token_env, "BOT_TOKEN")
        self.assertEqual(self.store.resolve_token(app), "ghp_x")
        self.store.save({"bot": app})
        self.assertNotIn("ghp_x", self.store.config_path.read_text())

    def test_secrets(self):
        self.store.set_secret("A_TOKEN", "one")
        self.store.set_secret("B_TOKEN", "two")
        self.store.set_secret("A_TOKEN", None)
        self.assertEqual(self.store.secrets(), {"B_TOKEN": "two"})
        if sys.platform != "win32":
            self.assertEqual(self.store.secrets_path.stat().st_mode & 0o777, 0o600)

    def test_token_env_preferred_over_secrets(self):
        app = make(token_env="SNT_TEST_TOKEN")
        self.store.set_secret("SNT_TEST_TOKEN", "from-file")
        self.assertEqual(self.store.resolve_token(app), "from-file")
        os.environ["SNT_TEST_TOKEN"] = "from-env"
        try:
            self.assertEqual(self.store.resolve_token(app), "from-env")
        finally:
            del os.environ["SNT_TEST_TOKEN"]
        self.assertIsNone(self.store.resolve_token(make()))


if __name__ == "__main__":
    unittest.main()
