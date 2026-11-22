import contextlib
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from smolnugget.cli import main
from smolnugget.config import Store
from smolnugget.state import is_managed
from smolnugget.supervisor import Daemon
from tests.helpers import Remote, wait_for

SLEEPER = "import time\nprint('up', flush=True)\ntime.sleep(120)\n"


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.h = ["--home", str(self.home)]
        self.store = Store(self.home)
        self.remote = Remote(self.root / "r")

    def snt(self, *argv):
        return run(*self.h, *argv)

    def daemon(self):
        d = Daemon(self.store, None, tick=0.1)
        t = threading.Thread(target=d.run)
        t.start()
        self.addCleanup(lambda: (d.request_stop(), t.join(30)))
        return d

    # --- add -------------------------------------------------------------------
    def test_add_detects_command_and_name(self):
        self.remote.commit({"bot.py": "print(1)", "requirements.txt": ""})
        code, out, err = self.snt("add", self.remote.url)
        self.assertEqual(code, 0, err)
        app = self.store.get("remote")  # name from the URL (remote.git)
        self.assertEqual(app.command, ["python", "bot.py"])
        self.assertTrue(app.venv)
        self.assertIn("detected from bot.py", out)
        self.assertTrue((self.store.repo_dir("remote") / "bot.py").exists())

    def test_add_explicit(self):
        self.remote.commit({"x.js": ""})
        code, out, err = self.snt("add", self.remote.url, "web", "-c", "node x.js",
                                  "--setup", "npm ci", "-e", "PORT=8080")
        self.assertEqual(code, 0, err)
        app = self.store.get("web")
        self.assertEqual((app.command, app.venv, app.setup, app.env),
                         (["node", "x.js"], False, ["npm ci"], {"PORT": "8080"}))

    def test_add_undetectable_cleans_up(self):
        self.remote.commit({"README.md": "hi"})
        code, _, err = self.snt("add", self.remote.url, "x")
        self.assertEqual(code, 1)
        self.assertIn("--command", err)
        self.assertFalse(self.store.app_dir("x").exists())
        self.assertEqual(self.store.load(), {})

    def test_add_bad_repo(self):
        code, _, err = self.snt("add", str(self.root / "missing.git"), "x")
        self.assertEqual(code, 1)
        self.assertFalse(self.store.app_dir("x").exists())

    def test_add_token_goes_to_secrets(self):
        code, _, err = self.snt("add", "https://github.com/u/r.git", "bot", "--no-clone",
                                "-c", "python bot.py", "--token", "ghp_secret")
        self.assertEqual(code, 0, err)
        self.assertNotIn("ghp_secret", self.store.config_path.read_text())
        self.assertEqual(self.store.secrets()["BOT_TOKEN"], "ghp_secret")
        _, out, _ = self.snt("show", "bot")
        self.assertNotIn("ghp_secret", out)
        self.assertIn("secrets.env", out)
        code, _, _ = self.snt("add", "https://github.com/u/r.git", "bot", "--no-clone", "-c", "x")
        self.assertEqual(code, 1)  # duplicate

    def test_add_rejects_credentials_in_url(self):
        code, _, err = self.snt("add", "https://tok@github.com/u/r.git", "--no-clone", "-c", "a")
        self.assertEqual(code, 1)
        self.assertIn("credentials", err)

    # --- edit / remove -----------------------------------------------------------
    def test_edit_and_remove(self):
        self.snt("add", "https://github.com/u/r.git", "bot", "--no-clone", "-c", "python bot.py",
                 "--token", "t", "-e", "A=1", "--setup", "echo hi")
        code, _, err = self.snt("edit", "bot", "--branch", "dev", "--no-venv", "--clear-token",
                                "--unset-env", "A", "--check-interval", "30")
        self.assertEqual(code, 0, err)
        app = self.store.get("bot")
        self.assertEqual((app.branch, app.venv, app.token_env, app.env, app.check_interval,
                          app.setup), ("dev", False, None, {}, 30, ["echo hi"]))
        self.assertNotIn("BOT_TOKEN", self.store.secrets())
        code, _, _ = self.snt("rm", "bot", "--purge")
        self.assertEqual(code, 0)
        self.assertEqual(self.store.load(), {})

    # --- status --------------------------------------------------------------------
    def test_status_text_and_json(self):
        head = self.remote.commit({"app.py": "print(1)"})
        self.snt("add", self.remote.url, "a", "--no-venv", "-e", "SECRET=hunter2")
        self.snt("add", "https://github.com/u/b.git", "b", "--no-clone", "-c", "x")
        self.snt("sync", "a")
        _, out, _ = self.snt("status")
        self.assertIn("stopped", out)
        self.assertIn("snt is not running", out)
        code, out, _ = self.snt("ls", "--json")
        self.assertNotIn("hunter2", out)
        data = json.loads(out)
        self.assertEqual(data["schema"], 1)
        self.assertFalse(data["daemon"]["running"])
        a, b = data["apps"]
        self.assertEqual((a["name"], a["status"], a["checkout"], a["pid"]), ("a", "stopped", head, None))
        self.assertTrue(a["last_check"].endswith("+00:00"))
        self.assertIsNone(b["checkout"])

    # --- live control -------------------------------------------------------------
    def test_control_commands_with_running_daemon(self):
        self.remote.commit({"app.py": SLEEPER})
        self.snt("add", self.remote.url, "a", "--no-venv")
        code, _, err = self.snt("restart", "a")
        self.assertEqual(code, 1)
        self.assertIn("not running", err)

        self.daemon()
        wait_for(lambda: self.store.read_state("a").get("pid"))
        data = json.loads(self.snt("status", "--json")[1])
        self.assertTrue(data["daemon"]["running"])
        app = data["apps"][0]
        self.assertEqual(app["status"], "running")
        self.assertEqual(app["commit"], app["checkout"])
        pid = app["pid"]

        code, out, err = self.snt("restart", "a")
        self.assertEqual(code, 0, err)
        wait_for(lambda: self.store.read_state("a").get("pid") not in (None, pid))

        code, out, _ = self.snt("stop", "a")
        self.assertIn("a: stopped", out)
        self.assertFalse(is_managed(self.store, "a"))
        self.assertEqual(json.loads(self.snt("status", "--json")[1])["apps"][0]["status"], "disabled")

        code, out, _ = self.snt("start", "a")
        self.assertIn("a: started", out)
        wait_for(lambda: self.store.read_state("a").get("pid"))

        code, out, _ = self.snt("update", "a")
        self.assertEqual(code, 0)

        code, out, _ = self.snt("logs", "a", "-n", "100")
        self.assertIn("up", out)
        self.assertIn("* info: started pid", out)

        self.snt("rm", "a")
        wait_for(lambda: not is_managed(self.store, "a"))

    def test_add_while_daemon_runs(self):
        self.daemon()
        self.remote.commit({"main.py": SLEEPER})
        code, out, _ = self.snt("add", self.remote.url, "late", "--no-venv")
        self.assertIn("snt is running and starts it now", out)
        wait_for(lambda: self.store.read_state("late").get("pid"))

    def test_logs_missing(self):
        self.snt("add", "https://github.com/u/b.git", "b", "--no-clone", "-c", "x")
        code, _, err = self.snt("logs", "b")
        self.assertEqual(code, 0)
        self.assertIn("no logs yet", err)

    def test_top_needs_terminal(self):
        code, _, err = self.snt("top")
        self.assertEqual(code, 1)
        self.assertIn("interactive terminal", err)

    def test_service_show(self):
        code, out, _ = self.snt("service", "show")
        self.assertEqual(code, 0)
        self.assertIn("-m smolnugget", out)
        self.assertIn(" run", out)
        self.assertIn("KillMode=mixed", out)
        self.assertIn("default.target", out)


if __name__ == "__main__":
    unittest.main()
