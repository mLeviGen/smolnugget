"""Running apps: setup (venv, dependencies), process lifecycle and the daemon loop."""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from smolnugget import __version__
from smolnugget.config import PYTHON_NAMES, AppConfig, ConfigError, Store, write_atomic
from smolnugget.git import GitError, GitRepo
from smolnugget.locks import FileLock
from smolnugget.state import take_control

SETUP_TIMEOUT = 900
STABLE_UPTIME = 60  # a process that lived this long resets the crash backoff
MAX_BACKOFF = 60
LOG_MAX_BYTES = 1024 * 1024
LOG_BACKUPS = 3
IS_WINDOWS = sys.platform == "win32"


class SetupError(Exception):
    pass


class LockError(Exception):
    pass


# --- environment -------------------------------------------------------------
def venv_bin(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts" if IS_WINDOWS else "bin")


def venv_python(venv_dir: Path) -> Path:
    return venv_bin(venv_dir) / ("python.exe" if IS_WINDOWS else "python")


def build_env(app: AppConfig, store: Store) -> dict[str, str]:
    env = os.environ.copy()
    env.update(app.env)
    env["PYTHONUNBUFFERED"] = "1"
    if app.venv:
        vdir = store.venv_dir(app.name)
        env["VIRTUAL_ENV"] = str(vdir)
        env["PATH"] = str(venv_bin(vdir)) + os.pathsep + env.get("PATH", "")
        env.pop("PYTHONHOME", None)
    return env


def resolve_command(app: AppConfig, store: Store) -> list[str]:
    cmd = list(app.command)
    if cmd[0] in PYTHON_NAMES:
        cmd[0] = str(venv_python(store.venv_dir(app.name))) if app.venv else sys.executable
    return cmd


def _run_step(args, *, cwd: Path, env: dict, log: logging.Logger, shell: bool = False) -> None:
    shown = args if isinstance(args, str) else " ".join(map(str, args))
    log.info("setup: %s", shown)
    try:
        res = subprocess.run(
            args, cwd=cwd, env=env, shell=shell, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", timeout=SETUP_TIMEOUT,
        )
    except subprocess.TimeoutExpired as e:
        raise SetupError(f"'{shown}' timed out after {SETUP_TIMEOUT}s") from e
    except OSError as e:
        raise SetupError(f"'{shown}' could not be started: {e}") from e
    for line in res.stdout.splitlines():
        log.debug("setup | %s", line)
    if res.returncode != 0:
        tail = "\n".join(res.stdout.splitlines()[-20:])
        raise SetupError(f"'{shown}' exited with code {res.returncode}\n{tail}")


def run_setup(app: AppConfig, store: Store, log: logging.Logger) -> None:
    """Create/refresh the venv and run the app's setup commands."""
    repo = store.repo_dir(app.name)
    env = build_env(app, store)
    if app.venv:
        vdir = store.venv_dir(app.name)
        if not venv_python(vdir).exists():
            _run_step([sys.executable, "-m", "venv", str(vdir)], cwd=repo, env=env, log=log)
        if (repo / "requirements.txt").exists():
            _run_step(
                [str(venv_python(vdir)), "-m", "pip", "install", "--disable-pip-version-check",
                 "-q", "-r", "requirements.txt"],
                cwd=repo, env=env, log=log,
            )
    for cmd in app.setup:
        _run_step(cmd, cwd=repo, env=env, log=log, shell=True)


# --- a single child process -------------------------------------------------
class ManagedProcess:
    def __init__(self, cmd: list[str], cwd: Path, env: dict[str, str], output: logging.Logger):
        self.cmd = cmd
        self.cwd = cwd
        self.env = env
        self.output = output
        self.popen: subprocess.Popen | None = None
        self.started_at = 0.0

    @property
    def pid(self) -> int | None:
        return self.popen.pid if self.popen else None

    @property
    def returncode(self) -> int | None:
        return self.popen.returncode if self.popen else None

    def start(self, now: float) -> None:
        kwargs = {}
        if IS_WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True  # own process group -> we can kill grandchildren
        self.popen = subprocess.Popen(
            self.cmd, cwd=self.cwd, env=self.env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1, **kwargs,
        )
        self.started_at = now
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self) -> None:
        assert self.popen and self.popen.stdout
        try:
            for line in self.popen.stdout:
                self.output.info("%s", line.rstrip("\r\n"))
        except (OSError, ValueError):
            pass
        finally:
            self.popen.stdout.close()

    def poll(self) -> int | None:
        return self.popen.poll() if self.popen else 0

    def _signal(self, force: bool) -> None:
        p = self.popen
        assert p is not None
        try:
            if IS_WINDOWS:
                if force:
                    subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)],
                                   capture_output=True)
                    p.kill()
                else:
                    p.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(p.pid, signal.SIGKILL if force else signal.SIGTERM)
        except OSError:
            pass

    def stop(self, timeout: float) -> int | None:
        if self.popen is None or self.popen.poll() is not None:
            return self.returncode
        self._signal(force=False)
        try:
            return self.popen.wait(timeout)
        except subprocess.TimeoutExpired:
            self._signal(force=True)
            return self.popen.wait()


class _AppLogFormatter(logging.Formatter):
    """Process output as-is, supervisor messages marked with '*'."""

    def format(self, record: logging.LogRecord) -> str:
        ts = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        if record.name.startswith("snt."):
            return f"{ts} * {record.levelname.lower()}: {record.getMessage()}"
        return f"{ts}   {record.getMessage()}"


# --- one app -----------------------------------------------------------------
class AppRunner:
    """Keeps one app's checkout up to date and its process running.

    Driven by tick(now); all decisions are made there so the logic is testable
    without threads.
    """

    def __init__(self, app: AppConfig, store: Store):
        self.app = app
        self.store = store
        self.log = logging.getLogger(f"snt.{app.name}")
        self.output = logging.getLogger(f"app.{app.name}")
        self.repo = GitRepo(store.repo_dir(app.name), app.url, app.branch, logger=self.log)
        self.state = store.read_state(app.name)
        self.proc: ManagedProcess | None = None
        self.proc_commit: str | None = None
        self.next_check = 0.0
        self.restart_at = 0.0
        self.crashes = 0
        self.exited_commit: str | None = None
        self.starts = 0
        self.last_error: str | None = None
        self._log_handler: logging.Handler | None = None

    # per-app log file -------------------------------------------------------
    def open_log(self) -> None:
        if self._log_handler:
            return
        path = self.store.log_path(self.app.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        h = logging.handlers.RotatingFileHandler(
            path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8", delay=True)
        h.setFormatter(_AppLogFormatter())
        h.setLevel(logging.INFO)
        for logger in (self.log, self.output):
            logger.addHandler(h)
            if logger.getEffectiveLevel() > logging.INFO:  # the file always gets INFO
                logger.setLevel(logging.INFO)
        self._log_handler = h

    def close_log(self) -> None:
        if h := self._log_handler:
            self.log.removeHandler(h)
            self.output.removeHandler(h)
            h.close()
            self._log_handler = None

    # main entry point -------------------------------------------------------
    def tick(self, now: float) -> None:
        self._handle_control(now)
        self._reap(now)
        if now >= self.next_check:
            self.next_check = now + self.app.check_interval
            self.check()
        self._reconcile(now)

    def _handle_control(self, now: float) -> None:
        action = take_control(self.store, self.app.name)
        if action == "update":
            self.log.info("update requested")
            self.next_check = 0.0
        elif action == "restart":
            self.log.info("restart requested")
            self.stop()
            self.crashes = 0
            self.restart_at = 0.0
            self.exited_commit = None

    def check(self) -> None:
        """Sync with the remote and prepare a new commit if there is one."""
        self.state["last_check"] = time.time()
        self.repo.token = self.store.resolve_token(self.app)
        try:
            if self.repo.sync():
                self.log.info("checked out %s", (self.repo.head() or "?")[:10])
            self.last_error = None
        except GitError as e:
            self.log.error("sync failed: %s", e)
            self.last_error = str(e)

        head = self.repo.head()
        self.state["checkout"] = head
        if head and head != self.state.get("setup_commit"):
            try:
                run_setup(self.app, self.store, self.log)
                self.state["setup_commit"] = head
                self.last_error = None
            except SetupError as e:
                self.log.error("setup failed (will retry on next check): %s", e)
                self.last_error = f"setup failed: {e}"
        self.save_state()

    def _reap(self, now: float) -> None:
        if self.proc is None or self.proc.poll() is None:
            return
        code = self.proc.returncode
        uptime = now - self.proc.started_at
        self.log.warning("process exited with code %s after %.0fs", code, uptime)
        exited_commit = self.proc_commit
        self.proc = None
        if self.app.restart == "always" or (self.app.restart == "on-failure" and code != 0):
            self._schedule_restart(now, stable=uptime >= STABLE_UPTIME)
        else:
            self.log.info("restart policy '%s': not restarting until next update", self.app.restart)
            self.exited_commit = exited_commit
        self.save_state()

    def _schedule_restart(self, now: float, stable: bool) -> None:
        self.crashes = 1 if stable else self.crashes + 1
        delay = min(MAX_BACKOFF, 2 ** (self.crashes - 1))
        self.restart_at = now + delay
        self.log.info("restarting in %ss", delay)

    def _reconcile(self, now: float) -> None:
        target = self.state.get("setup_commit")
        if not target:
            return  # nothing has been prepared yet
        if self.proc is not None and self.proc_commit != target:
            self.log.info("new version %s, restarting", target[:10])
            self.stop()
            self.crashes = 0
            self.restart_at = 0.0
        if self.proc is None and self.exited_commit != target and now >= self.restart_at:
            self._start(now, target)

    def _start(self, now: float, commit: str) -> None:
        self.exited_commit = None
        cmd = resolve_command(self.app, self.store)
        proc = ManagedProcess(cmd, self.store.repo_dir(self.app.name),
                              build_env(self.app, self.store), self.output)
        try:
            proc.start(now)
        except OSError as e:
            self.log.error("cannot start %s: %s", " ".join(cmd), e)
            self.last_error = f"cannot start: {e}"
            self._schedule_restart(now, stable=False)
            self.save_state()
            return
        self.proc = proc
        self.proc_commit = commit
        self.starts += 1
        self.log.info("started pid %s (%s)", proc.pid, commit[:10])
        self.save_state()

    def stop(self) -> None:
        if self.proc is None:
            return
        self.log.info("stopping pid %s", self.proc.pid)
        code = self.proc.stop(self.app.stop_timeout)
        self.log.info("stopped (exit code %s)", code)
        self.proc = None
        self.save_state()

    def save_state(self) -> None:
        started = None
        if self.proc:
            started = time.time() - (time.monotonic() - self.proc.started_at)
        self.state.update(
            daemon_pid=os.getpid(),
            pid=self.proc.pid if self.proc else None,
            commit=self.proc_commit if self.proc else None,
            started_at=started,
            restarts=max(0, self.starts - 1),
            last_error=self.last_error,
        )
        try:
            self.store.write_state(self.app.name, self.state)
        except OSError as e:
            self.log.warning("cannot write state: %s", e)


# --- daemon ------------------------------------------------------------------
@dataclass
class _Worker:
    runner: AppRunner
    lock: FileLock
    stop: threading.Event
    thread: threading.Thread | None = None


class Daemon:
    """Runs apps, each in its own thread, until SIGINT/SIGTERM.

    With names=None it follows the whole config: apps added, edited, enabled or
    disabled with the CLI are picked up within a second, no restart needed.
    """

    RETRY_BLOCKED = 5.0

    def __init__(self, store: Store, names: list[str] | None = None, tick: float = 0.5):
        self.store = store
        self.names = names
        self.tick = tick
        self.stop_event = threading.Event()
        self.workers: dict[str, _Worker] = {}
        self.blocked: set[str] = set()
        self.log = logging.getLogger("snt")
        self._daemon_lock: FileLock | None = None

    def request_stop(self, *_args) -> None:
        self.stop_event.set()

    # config -> desired set ----------------------------------------------------
    def _desired(self) -> dict[str, AppConfig]:
        apps = self.store.load()
        if self.names is not None:
            apps = {n: a for n, a in apps.items() if n in self.names}
        return {n: a for n, a in apps.items() if a.enabled}

    def _start_worker(self, app: AppConfig) -> bool:
        lock = FileLock(self.store.lock_path(app.name))
        if not lock.acquire():
            return False
        runner = AppRunner(app, self.store)
        runner.open_log()
        w = _Worker(runner, lock, threading.Event())
        w.thread = threading.Thread(target=self._loop, args=(w,), name=app.name, daemon=True)
        self.workers[app.name] = w
        w.thread.start()
        return True

    def _stop_worker(self, name: str) -> None:
        w = self.workers.pop(name)
        w.stop.set()
        if w.thread:
            w.thread.join()

    def _loop(self, w: _Worker) -> None:
        r = w.runner
        try:
            while not w.stop.is_set() and not self.stop_event.is_set():
                try:
                    r.tick(time.monotonic())
                except Exception:  # a bug in one app must not kill the daemon
                    r.log.exception("unexpected error")
                w.stop.wait(self.tick)
        finally:
            try:
                r.stop()
            except Exception:
                r.log.exception("error while stopping")
            r.close_log()
            w.lock.release()

    def reload(self) -> None:
        try:
            desired = self._desired()
        except ConfigError as e:
            self.log.error("config not reloaded: %s", e)
            return
        for name in list(self.workers):
            if name not in desired:
                self.log.info("%s: removed or disabled, stopping", name)
                self._stop_worker(name)
            elif self.workers[name].runner.app != desired[name]:
                self.log.info("%s: config changed, restarting", name)
                self._stop_worker(name)
        for name, app in desired.items():
            if name in self.workers:
                continue
            if self._start_worker(app):
                self.blocked.discard(name)
                self.log.info("%s: managing", name)
            elif name not in self.blocked:
                self.blocked.add(name)
                self.log.warning("%s: already run by another snt process, skipping", name)
        self.blocked &= set(desired)

    # lifecycle ------------------------------------------------------------------
    def _install_signals(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        signal.signal(signal.SIGINT, self.request_stop)
        signal.signal(signal.SIGTERM, self.request_stop)
        if IS_WINDOWS:
            signal.signal(signal.SIGBREAK, self.request_stop)
        else:
            signal.signal(signal.SIGHUP, self.request_stop)

    def _acquire_initial(self) -> None:
        if self.names is None:
            self._daemon_lock = FileLock(self.store.daemon_lock_path)
            if not self._daemon_lock.acquire():
                self._daemon_lock = None
                raise LockError("another 'snt run' is already running for this home directory")
            write_atomic(self.store.home / "daemon.json", json.dumps(
                {"pid": os.getpid(), "started_at": time.time(), "version": __version__}) + "\n")
        else:
            known = self.store.load()
            if missing := [n for n in self.names if n not in known]:
                raise ConfigError(f"unknown app(s): {', '.join(missing)}")
            if disabled := [n for n in self.names if not known[n].enabled]:
                self.log.warning("disabled, not starting: %s (enable with 'snt start')",
                                 ", ".join(disabled))
        for name, app in self._desired().items():
            if not self._start_worker(app):
                raise LockError(f"app {name!r} is already run by another snt process")

    def run(self) -> None:
        try:
            self._acquire_initial()
            self._install_signals()
            managed = ", ".join(self.workers) or "nothing yet (add apps with 'snt add')"
            self.log.info("snt %s started, managing: %s", __version__, managed)
            mtime = self.store.config_mtime()
            last_retry = time.monotonic()
            while not self.stop_event.wait(1.0 if self.tick >= 0.5 else self.tick):
                new_mtime = self.store.config_mtime()
                retry = self.blocked and time.monotonic() - last_retry >= self.RETRY_BLOCKED
                if new_mtime != mtime or retry:
                    mtime = new_mtime
                    last_retry = time.monotonic()
                    self.reload()
            self.log.info("shutting down")
        finally:
            self.stop_event.set()
            for name in list(self.workers):
                self._stop_worker(name)
            if self._daemon_lock:
                self._daemon_lock.release()
