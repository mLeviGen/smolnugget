import tempfile
import threading
import time
import unittest
from pathlib import Path

from smolnugget import supervisor
from smolnugget.config import AppConfig, Store
from smolnugget.locks import FileLock
from smolnugget.state import is_managed, send_control
from smolnugget.supervisor import AppRunner, Daemon, LockError
from tests.helpers import Remote, wait_for

# Appends "<version> <pid>" to $MARKER, then behaves according to $MODE.
APP = """\
import os, sys, time
VERSION = {version!r}
with open(os.environ["MARKER"], "a") as f:
    f.write(VERSION + " " + str(os.getpid()) + "\\n")
mode = os.environ.get("MODE", "sleep")
if mode == "crash":
    sys.exit(3)
if mode == "exit0":
    sys.exit(0)
time.sleep(120)
"""


class SupervisorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.remote = Remote(self.root / "remote")
        self.remote.commit({"app.py": APP.format(version="v1")})
        self.store = Store(self.root / "home")
        self.marker = self.root / "marker.txt"
        self.runners = []

    def tearDown(self):
        for r in self.runners:
            r.stop()
        self.tmp.cleanup()

    def runner(self, **kw) -> AppRunner:
        env = {"MARKER": str(self.marker)}
        env.update(kw.pop("env", {}))
        app = AppConfig(name="app", url=self.remote.url, command=["python", "app.py"],
                        env=env, stop_timeout=5, **kw).validate()
        r = AppRunner(app, self.store)
        self.runners.append(r)
        return r

    def lines(self):
        if not self.marker.exists():
            return []
        return [l.split() for l in self.marker.read_text().splitlines()]

    def test_start_and_update(self):
        r = self.runner()
        r.tick(time.monotonic())
        wait_for(lambda: len(self.lines()) == 1)
        self.assertEqual(self.lines()[0][0], "v1")
        first_pid = r.proc.pid

        self.remote.commit({"app.py": APP.format(version="v2")})
        r.next_check = 0
        r.tick(time.monotonic())
        wait_for(lambda: len(self.lines()) == 2)
        self.assertEqual(self.lines()[1][0], "v2")
        self.assertNotEqual(r.proc.pid, first_pid)
        state = self.store.read_state("app")
        self.assertEqual(state["pid"], r.proc.pid)
        self.assertEqual(state["restarts"], 1)

    def test_no_restart_without_changes(self):
        r = self.runner()
        r.tick(time.monotonic())
        wait_for(lambda: len(self.lines()) == 1)
        pid = r.proc.pid
        r.next_check = 0
        r.tick(time.monotonic())
        time.sleep(0.5)
        self.assertEqual(r.proc.pid, pid)
        self.assertEqual(len(self.lines()), 1)

    def test_crash_is_restarted_with_backoff(self):
        r = self.runner(env={"MODE": "crash"})
        wait_for(lambda: len(self.lines()) >= 3, timeout=20,
                 tick=lambda: r.tick(time.monotonic()))
        self.assertGreaterEqual(r.crashes, 2)

    def test_restart_policy_no(self):
        r = self.runner(env={"MODE": "exit0"}, restart="no")
        r.tick(time.monotonic())
        wait_for(lambda: r.proc is not None and r.proc.poll() is not None)
        for _ in range(10):
            r.tick(time.monotonic())
            time.sleep(0.1)
        self.assertEqual(len(self.lines()), 1)
        # a new commit starts it again
        self.remote.commit({"app.py": APP.format(version="v2")})
        r.next_check = 0
        r.tick(time.monotonic())
        wait_for(lambda: len(self.lines()) == 2)

    def test_on_failure_ignores_clean_exit(self):
        r = self.runner(env={"MODE": "exit0"}, restart="on-failure")
        r.tick(time.monotonic())
        wait_for(lambda: r.proc is not None and r.proc.poll() is not None)
        for _ in range(10):
            r.tick(time.monotonic())
            time.sleep(0.1)
        self.assertEqual(len(self.lines()), 1)

    def test_failed_setup_keeps_old_version(self):
        import sys
        r = self.runner(setup=[f'"{sys.executable}" -c "import os,sys; sys.exit(os.path.exists(\'broken\'))"'])
        r.tick(time.monotonic())
        wait_for(lambda: len(self.lines()) == 1)
        old_pid = r.proc.pid

        self.remote.commit({"app.py": APP.format(version="v2"), "broken": ""})
        r.next_check = 0
        r.tick(time.monotonic())
        self.assertIn("setup failed", r.last_error)
        self.assertEqual(r.proc.pid, old_pid)  # old process untouched

        self.remote.commit({"app.py": APP.format(version="v3")}, msg="fix")
        (self.remote.work / "broken").unlink()
        from tests.helpers import git
        git(self.remote.work, "add", "-A")
        git(self.remote.work, "commit", "-m", "rm")
        git(self.remote.work, "push", "origin", "HEAD:main")
        r.next_check = 0
        r.tick(time.monotonic())
        wait_for(lambda: len(self.lines()) == 2)
        self.assertEqual(self.lines()[1][0], "v3")
        self.assertIsNone(r.last_error)

    def test_bad_command_does_not_crash(self):
        app = AppConfig(name="app", url=self.remote.url, command=["definitely-not-a-binary"])
        r = AppRunner(app, self.store)
        r.tick(time.monotonic())
        self.assertIsNone(r.proc)
        self.assertIn("cannot start", r.last_error)

    def test_unreachable_remote_retries(self):
        app = AppConfig(name="app", url=str(self.root / "nope.git"), command=["python", "x.py"])
        r = AppRunner(app, self.store)
        r.tick(time.monotonic())
        self.assertIsNone(r.proc)
        self.assertIn("clone", r.last_error)

    def test_lock(self):
        a = FileLock(self.store.lock_path("app"))
        self.assertTrue(a.acquire())
        self.assertTrue(is_managed(self.store, "app"))
        b = FileLock(self.store.lock_path("app"))
        self.assertFalse(b.acquire())
        a.release()
        self.assertFalse(is_managed(self.store, "app"))

    def test_control_restart_and_update(self):
        r = self.runner()
        r.tick(time.monotonic())
        wait_for(lambda: len(self.lines()) == 1)
        send_control(self.store, "app", "restart")
        r.tick(time.monotonic())
        wait_for(lambda: len(self.lines()) == 2)
        self.assertEqual(self.lines()[1][0], "v1")
        self.assertFalse(self.store.control_path("app").exists())

        self.remote.commit({"app.py": APP.format(version="v2")})
        send_control(self.store, "app", "update")  # no waiting for check_interval
        r.tick(time.monotonic())
        wait_for(lambda: len(self.lines()) == 3)
        self.assertEqual(self.lines()[2][0], "v2")

    def test_output_goes_to_app_log(self):
        self.remote.commit({"app.py": "print('hello from app')\nimport time\ntime.sleep(60)\n"})
        r = self.runner()
        r.open_log()
        try:
            r.tick(time.monotonic())
            log = self.store.log_path("app")
            wait_for(lambda: log.exists() and "hello from app" in log.read_text())
            text = log.read_text()
            self.assertIn("* info: started pid", text)
        finally:
            r.stop()
            r.close_log()

    def test_venv_and_requirements(self):
        self.remote.commit({"requirements.txt": "# nothing to install\n",
                            "app.py": APP.format(version="venv")})
        r = self.runner(venv=True)
        r.tick(time.monotonic())
        vpy = supervisor.venv_python(self.store.venv_dir("app"))
        self.assertTrue(vpy.exists())
        self.assertEqual(r.proc.cmd[0], str(vpy))
        wait_for(lambda: len(self.lines()) == 1)



class DaemonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.remote = Remote(self.root / "remote")
        self.remote.commit({"app.py": APP.format(version="v1")})
        self.store = Store(self.root / "home")
        self.markers = {}
        self.addCleanup(self.tmp.cleanup)  # runs after the daemon-stopping cleanups

    def add(self, name, **kw):
        self.markers[name] = self.root / f"{name}.txt"
        apps = self.store.load()
        apps[name] = AppConfig(name=name, url=self.remote.url, command=["python", "app.py"],
                               env={"MARKER": str(self.markers[name])}, **kw).validate()
        self.store.save(apps)

    def count(self, name):
        m = self.markers.get(name)
        return len(m.read_text().splitlines()) if m and m.exists() else 0

    def start(self, names=None):
        d = Daemon(self.store, names, tick=0.1)
        t = threading.Thread(target=d.run)
        t.start()
        self.addCleanup(lambda: (d.request_stop(), t.join(30)))
        return d, t

    def test_runs_and_stops_children(self):
        self.add("a")
        d, t = self.start()
        wait_for(lambda: self.count("a") == 1)
        self.assertTrue(is_managed(self.store, "a"))
        proc = d.workers["a"].runner.proc.popen
        with self.assertRaises(LockError):
            Daemon(self.store).run()  # second daemon for the same home
        with self.assertRaises(LockError):
            Daemon(self.store, ["a"]).run()  # or for the same app
        d.request_stop()
        t.join(30)
        self.assertFalse(t.is_alive())
        self.assertIsNotNone(proc.poll())
        self.assertFalse(is_managed(self.store, "a"))

    def test_hot_reload(self):
        d, _ = self.start()  # empty config: idles
        self.add("a")
        wait_for(lambda: self.count("a") == 1)
        self.add("b")
        wait_for(lambda: self.count("b") == 1)

        apps = self.store.load()  # disable a
        apps["a"].enabled = False
        self.store.save(apps)
        wait_for(lambda: not is_managed(self.store, "a"))
        self.assertIn("b", d.workers)

        apps = self.store.load()  # edit b -> restarted with new env
        apps["b"].env["EXTRA"] = "1"
        self.store.save(apps)
        wait_for(lambda: self.count("b") == 2)

        apps = self.store.load()  # remove b
        del apps["b"]
        self.store.save(apps)
        wait_for(lambda: not is_managed(self.store, "b"))
        self.assertEqual(d.workers, {})

    def test_edit_during_startup_is_not_lost(self):
        # Regression: an edit landing right after the initial load used to be ignored.
        test = self

        class RacyDaemon(Daemon):
            def _acquire_initial(self):
                super()._acquire_initial()
                test.add("a")

        d = RacyDaemon(self.store, None, tick=0.1)
        t = threading.Thread(target=d.run)
        t.start()
        self.addCleanup(lambda: (d.request_stop(), t.join(30)))
        wait_for(lambda: self.count("a") == 1)

    def test_named_mode_ignores_other_apps(self):
        self.add("a")
        self.add("b")
        d, _ = self.start(["a"])
        wait_for(lambda: self.count("a") == 1)
        time.sleep(1)
        self.assertEqual(self.count("b"), 0)
        self.assertEqual(list(d.workers), ["a"])

    def test_bad_config_edit_keeps_running(self):
        self.add("a")
        d, _ = self.start()
        wait_for(lambda: self.count("a") == 1)
        self.store.config_path.write_text("{ broken")
        time.sleep(1.5)
        self.assertIn("a", d.workers)
        self.assertTrue(is_managed(self.store, "a"))


if __name__ == "__main__":
    unittest.main()
