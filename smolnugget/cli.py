"""Command-line interface: `snt <command>`."""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import shlex
import sys
import time
from pathlib import Path

from smolnugget import __version__
from smolnugget.config import (
    PYTHON_NAMES, RESTART_POLICIES, AppConfig, ConfigError, Store, name_from_url, token_var_for,
    validate_name, validate_url,
)
from smolnugget.detect import detect
from smolnugget.git import GitError, GitRepo, rmtree
from smolnugget.locks import FileLock, is_locked
from smolnugget.state import is_managed, send_control, snapshot
from smolnugget.top import fmt_duration, tail

log = logging.getLogger("snt")

AUTH_HINTS = ("authentication failed", "could not read username", "terminal prompts disabled",
              "repository not found", "403", "401")


class CliError(Exception):
    pass


# --- helpers -----------------------------------------------------------------
def setup_logging(verbose: bool, log_file: str | None, timestamps: bool) -> None:
    if timestamps:
        fmt = logging.Formatter("%(asctime)s %(levelname)-7s [%(name)s] %(message)s",
                                "%Y-%m-%d %H:%M:%S")
    else:
        fmt = logging.Formatter("%(message)s")
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"))
    root = logging.getLogger()
    root.handlers.clear()
    for h in handlers:
        h.setFormatter(fmt)
        root.addHandler(h)
    root.setLevel(logging.DEBUG if verbose else logging.INFO)


def parse_env(pairs: list[str] | None) -> dict[str, str]:
    env = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise CliError(f"--env expects KEY=VALUE, got {pair!r}")
        env[key] = value
    return env


def parse_command(text: str) -> list[str]:
    try:
        cmd = shlex.split(text, posix=True)
    except ValueError as e:
        raise CliError(f"cannot parse --command: {e}") from e
    if not cmd:
        raise CliError("--command is empty")
    return cmd


def resolve_names(store: Store, names: list[str]) -> list[AppConfig]:
    apps = store.load()
    if not names:
        return list(apps.values())
    if missing := [n for n in names if n not in apps]:
        raise CliError(f"unknown app(s): {', '.join(missing)}")
    return [apps[n] for n in names]


def daemon_running(store: Store) -> bool:
    return is_locked(store.daemon_lock_path)


def wait_until(pred, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.2)
    return pred()


def interactive() -> bool:
    return sys.stdin.isatty() and sys.stderr.isatty()


def is_auth_error(e: GitError) -> bool:
    text = str(e).lower()
    return any(h in text for h in AUTH_HINTS)


def ask_token(url: str) -> str | None:
    import getpass
    print(f"\n{url} needs a token (private repo?).\n"
          "Create a fine-grained token with 'Contents: read-only' for this repo:\n"
          "  https://github.com/settings/personal-access-tokens/new", file=sys.stderr)
    try:
        token = getpass.getpass("token (input hidden, empty to cancel): ").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    return token or None


def no_daemon_hint() -> str:
    return "snt is not running - start it with 'snt run' (or install it: 'snt service install')"


# --- add / edit / remove -----------------------------------------------------------
def apply_token(store: Store, app: AppConfig, token: str | None, token_env: str | None) -> None:
    if token_env is not None:
        app.token_env = token_env or None
    if token:
        app.token_env = app.token_env or token_var_for(app.name)
        store.set_secret(app.token_env, token)


def cmd_add(args, store: Store) -> int:
    url = validate_url(args.url)
    name = validate_name(args.name or name_from_url(url))
    apps = store.load()
    if name in apps:
        raise CliError(f"app {name!r} already exists (pick another NAME or use 'snt edit {name}')")

    app = AppConfig(name=name, url=url, command=["?"], branch=args.branch,
                    check_interval=args.check_interval, restart=args.restart,
                    stop_timeout=args.stop_timeout, env=parse_env(args.env))
    apply_token(store, app, args.token, args.token_env)
    command = parse_command(args.command) if args.command else None
    detected = None

    if args.no_clone:
        if command is None:
            raise CliError("--no-clone needs --command (nothing to detect it from)")
    else:
        app_dir_existed = store.app_dir(name).exists()
        repo = GitRepo(store.repo_dir(name), url, args.branch, store.resolve_token(app))
        try:
            try:
                repo.sync()
            except GitError as e:
                if not (is_auth_error(e) and interactive() and url.startswith("http")
                        and not repo.token):
                    raise
                token = ask_token(url)
                if not token:
                    raise
                apply_token(store, app, token, None)
                repo.token = token
                repo.sync()
            if command is None:
                detected = detect(repo.path)
                if detected is None:
                    raise CliError("could not detect how to start this repo; "
                                   'pass it explicitly, e.g. --command "python main.py"')
        except (GitError, CliError) as e:
            if not app_dir_existed:
                rmtree(store.app_dir(name))
            if app.token_env == token_var_for(name) and app.token_env != args.token_env:
                store.set_secret(app.token_env, None)  # don't keep a token for an app not added
            if isinstance(e, GitError) and is_auth_error(e):
                raise CliError(f"{e}\nhint: private repo? pass --token (or --token-env VAR)") from e
            raise

    app.command = command or detected.command
    app.venv = args.venv if args.venv is not None else (
        detected.venv if detected else app.command[0] in PYTHON_NAMES)
    app.setup = args.setup if args.setup else (detected.setup if detected else [])
    app.validate()
    apps[name] = app
    store.save(apps)

    how = f"   (detected from {detected.reason})" if detected else ""
    print(f"added {name!r}")
    print(f"  repo     {url}" + (f"  [{app.branch}]" if app.branch else ""))
    print(f"  command  {shlex.join(app.command)}{how}")
    if app.venv:
        print("  venv     yes (requirements.txt is installed on every update)")
    if app.setup:
        print(f"  setup    {' && '.join(app.setup)}")
    if app.token_env:
        print(f"  token    ${app.token_env}")
    if detected:
        print(f"  wrong guess? snt edit {name} --command \"...\"")
    if daemon_running(store):
        print(f"\nsnt is running and starts it now. Follow it: snt logs {name} -f")
    else:
        print("\nnext: snt run   (or keep it running as a service: snt service install)")
    return 0


def cmd_edit(args, store: Store) -> int:
    apps = store.load()
    app = apps.get(args.name)
    if app is None:
        raise CliError(f"no app named {args.name!r}")
    if args.url is not None:
        app.url = args.url
    if args.command is not None:
        app.command = parse_command(args.command)
    if args.branch is not None:
        app.branch = args.branch or None
    if args.venv is not None:
        app.venv = args.venv
    if args.clear_setup:
        app.setup = []
    if args.setup:
        app.setup = list(args.setup)
    for key in ("check_interval", "restart", "stop_timeout"):
        if (value := getattr(args, key)) is not None:
            setattr(app, key, value)
    app.env.update(parse_env(args.env))
    for key in args.unset_env or []:
        app.env.pop(key, None)
    if args.clear_token:
        if app.token_env == token_var_for(app.name):
            store.set_secret(app.token_env, None)
        app.token_env = None
    apply_token(store, app, args.token, args.token_env)
    app.validate()
    store.save(apps)
    print(f"updated {app.name!r}" + (" - restarting it with the new settings"
                                      if is_managed(store, app.name) else ""))
    return 0


def cmd_remove(args, store: Store) -> int:
    apps = store.load()
    app = apps.pop(args.name, None)
    if app is None:
        raise CliError(f"no app named {args.name!r}")
    store.save(apps)
    if is_managed(store, app.name):
        print(f"stopping {app.name}...")
        if not wait_until(lambda: not is_managed(store, app.name), app.stop_timeout + 15):
            if args.purge:
                raise CliError(f"{app.name} is still running (was it started with 'snt run "
                               f"{app.name}' and a different config?); files kept")
    if args.purge:
        rmtree(store.app_dir(app.name))
        if app.token_env == token_var_for(app.name):
            store.set_secret(app.token_env, None)
        print(f"removed {app.name!r} and its files")
    else:
        print(f"removed {app.name!r} (files kept in {store.app_dir(app.name)}; --purge deletes them)")
    return 0


def cmd_show(args, store: Store) -> int:
    app = store.get(args.name)
    print(json.dumps({app.name: app.to_dict()}, indent=2))
    if app.token_env:
        if os.environ.get(app.token_env):
            source = "process environment"
        elif app.token_env in store.secrets():
            source = str(store.secrets_path)
        else:
            source = "MISSING"
        print(f"\ntoken    ${app.token_env} from {source}")
    print(f"files    {store.app_dir(app.name)}")
    return 0


# --- control ---------------------------------------------------------------------
def set_enabled(store: Store, names: list[str], enabled: bool) -> int:
    apps = store.load()
    if missing := [n for n in names if n not in apps]:
        raise CliError(f"unknown app(s): {', '.join(missing)}")
    for n in names:
        apps[n].enabled = enabled
    store.save(apps)
    running = daemon_running(store)
    for n in names:
        if enabled:
            if running and wait_until(lambda: is_managed(store, n), 5):
                print(f"{n}: started")
            else:
                print(f"{n}: enabled")
        else:
            if is_managed(store, n):
                ok = wait_until(lambda: not is_managed(store, n), apps[n].stop_timeout + 15)
                print(f"{n}: {'stopped' if ok else 'disabled, still stopping...'}")
            else:
                print(f"{n}: disabled")
    if enabled and not running:
        print(no_daemon_hint())
    return 0


def cmd_start(args, store: Store) -> int:
    return set_enabled(store, args.names, True)


def cmd_stop(args, store: Store) -> int:
    return set_enabled(store, args.names, False)


def send(store: Store, names: list[str], action: str) -> int:
    apps = resolve_names(store, names)
    if not names:
        raise CliError("name at least one app")
    for app in apps:
        if not is_managed(store, app.name):
            raise CliError(f"{app.name} is not running. " + (
                f"Enable it: snt start {app.name}" if not app.enabled else no_daemon_hint()))
    for app in apps:
        send_control(store, app.name, action)
    for app in apps:
        done = wait_until(lambda: not store.control_path(app.name).exists(), 5)
        verb = "restarting" if action == "restart" else "checking for updates"
        print(f"{app.name}: {verb}" + ("" if done else " (request queued)"))
    return 0


def cmd_restart(args, store: Store) -> int:
    return send(store, args.names, "restart")


def cmd_update(args, store: Store) -> int:
    return send(store, args.names, "update")


# --- observing -------------------------------------------------------------------
def cmd_status(args, store: Store) -> int:
    apps = resolve_names(store, args.names)
    snap = snapshot(store, apps)
    if args.json:
        print(json.dumps(snap, indent=2))
        return 0
    if not apps:
        print("no apps yet - add one with: snt add <github url>")
        return 0
    rows = [("NAME", "STATUS", "PID", "UPTIME", "RESTARTS", "COMMIT", "REPO")]
    errors = []
    for s in snap["apps"]:
        rows.append((s["name"], s["status"], str(s["pid"] or "-"), fmt_duration(s["uptime_seconds"]),
                     str(s["restarts"]), (s["commit"] or s["checkout"] or "-")[:8],
                     s["repo"]["url"].removeprefix("https://github.com/").removesuffix(".git")))
        if s["last_error"]:
            errors.append(f"{s['name']}: {s['last_error'].splitlines()[0]}")
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for r in rows:
        print("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
    if errors:
        print("\nerrors:")
        for e in errors:
            print("  " + e)
    if not snap["daemon"]["running"] and not any(s["status"] in ("running", "waiting")
                                                 for s in snap["apps"]):
        print("\n" + no_daemon_hint())
    return 0


def cmd_logs(args, store: Store) -> int:
    store.get(args.name)
    path = store.log_path(args.name)
    for line in tail(path, args.lines):
        print(line)
    if not args.follow:
        if not path.exists():
            print(f"no logs yet for {args.name}", file=sys.stderr)
        return 0
    try:
        f = None
        pos = path.stat().st_size if path.exists() else 0
        while True:
            try:
                size = path.stat().st_size
            except OSError:
                size = None
            if size is not None:
                if f is None or size < pos:  # first open or log rotated
                    if f:
                        f.close()
                        pos = 0
                    f = open(path, "r", encoding="utf-8", errors="replace")
                    f.seek(pos)
                for line in f:
                    sys.stdout.write(line)
                sys.stdout.flush()
                pos = f.tell()
            time.sleep(0.3)
    except KeyboardInterrupt:
        return 0


def cmd_top(args, store: Store) -> int:
    from smolnugget.top import run_top
    try:
        run_top(store, args.interval)
    except RuntimeError as e:
        raise CliError(str(e)) from e
    return 0


# --- running -----------------------------------------------------------------------
def cmd_run(args, store: Store) -> int:
    from smolnugget.supervisor import Daemon, LockError
    names = args.names or None
    for app in resolve_names(store, args.names):
        if app.token_env and not store.resolve_token(app):
            log.warning("%s: token $%s is not set (environment or %s)",
                        app.name, app.token_env, store.secrets_path)
    try:
        Daemon(store, names).run()
    except LockError as e:
        raise CliError(f"{e}\nsee what it runs: snt status") from e
    return 0


def cmd_sync(args, store: Store) -> int:
    from smolnugget.supervisor import AppRunner
    app = store.get(args.name)
    lock = FileLock(store.lock_path(app.name))
    if not lock.acquire():
        raise CliError(f"{app.name} is running; use 'snt update {app.name}' instead")
    runner = AppRunner(app, store)
    runner.open_log()
    try:
        runner.check()
    finally:
        runner.close_log()
        lock.release()
    if runner.last_error:
        raise CliError(runner.last_error)
    print(f"{app.name}: at {(runner.repo.head() or '?')[:10]}, ready to run")
    return 0


def cmd_service(args, store: Store) -> int:
    from smolnugget import service
    try:
        if args.action == "install":
            notes = service.install(store, args.home is not None, args.system)
        elif args.action == "uninstall":
            notes = service.uninstall(args.system)
        else:
            home, run_as = service.service_home(store, args.home is not None, args.system) \
                if sys.platform.startswith("linux") else (store.home, None)
            print(service.unit_text(home, args.system, run_as), end="")
            print(f"# save as {service.unit_path(args.system)}", file=sys.stderr)
            return 0
    except service.ServiceError as e:
        raise CliError(str(e)) from e
    for n in notes:
        print(n)
    return 0


# --- parser ------------------------------------------------------------------
def add_app_options(p: argparse.ArgumentParser, editing: bool) -> None:
    d = (lambda v: None) if editing else (lambda v: v)
    p.add_argument("-c", "--command",
                   help='how to start it, e.g. "python bot.py" (auto-detected by add)')
    p.add_argument("-b", "--branch", help="branch to track (default: the repo's default branch)")
    p.add_argument("--venv", action=argparse.BooleanOptionalAction, default=None,
                   help="own virtualenv + requirements.txt (default: on for python commands)")
    p.add_argument("--setup", action="append", metavar="CMD",
                   help='shell command run after every update, e.g. "npm ci" (repeatable)')
    p.add_argument("--check-interval", type=int, default=d(60), metavar="SEC",
                   help="how often to check GitHub (default 60)")
    p.add_argument("--restart", choices=RESTART_POLICIES, default=d("always"),
                   help="restart when the process exits (default always)")
    p.add_argument("--stop-timeout", type=int, default=d(10), metavar="SEC",
                   help="grace period before killing on stop (default 10)")
    p.add_argument("-e", "--env", action="append", metavar="KEY=VALUE",
                   help="environment variable for the process (repeatable)")
    p.add_argument("--token", help="GitHub token; saved to secrets.env (asked for interactively "
                                    "if a private repo needs one)")
    p.add_argument("--token-env", metavar="VAR",
                   help="read the token from this environment variable / secrets.env key")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="snt",
        description="Keep GitHub repositories and their processes running on a server.",
        epilog="quick start:  snt add https://github.com/you/bot  &&  snt run",
    )
    parser.add_argument("-V", "--version", action="version", version=f"smolnugget {__version__}")
    parser.add_argument("--home", type=Path,
                        help="data directory (default: $SNT_HOME or ~/.smolnugget)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--log-file", help="also write snt's own log to this file (rotated)")
    sub = parser.add_subparsers(dest="cmd", metavar="COMMAND")

    def cmd(name, func, help, aliases=()):
        p = sub.add_parser(name, help=help, aliases=list(aliases), description=help)
        p.set_defaults(func=func)
        return p

    p = cmd("add", cmd_add, "add a repo: clone it, detect how to run it, save")
    p.add_argument("url", help="https://github.com/you/repo or git@github.com:you/repo.git")
    p.add_argument("name", nargs="?", help="short name (default: the repo name)")
    add_app_options(p, editing=False)
    p.add_argument("--no-clone", action="store_true", help="don't clone now (needs --command)")

    p = cmd("edit", cmd_edit, "change an app's settings (applied live)")
    p.add_argument("name")
    p.add_argument("--url")
    add_app_options(p, editing=True)
    p.add_argument("--clear-setup", action="store_true", help="remove all setup commands")
    p.add_argument("--unset-env", action="append", metavar="KEY")
    p.add_argument("--clear-token", action="store_true", help="stop using a token")

    p = cmd("remove", cmd_remove, "remove an app", aliases=["rm"])
    p.add_argument("name")
    p.add_argument("--purge", action="store_true", help="also delete its checkout, venv and logs")

    p = cmd("status", cmd_status, "show all apps", aliases=["ls", "list"])
    p.add_argument("names", nargs="*")
    p.add_argument("--json", action="store_true", help="machine-readable output (for dashboards)")

    p = cmd("top", cmd_top, "live monitor with logs and hotkeys")
    p.add_argument("-n", "--interval", type=float, default=1.0, help="refresh seconds (default 1)")

    p = cmd("logs", cmd_logs, "print an app's output")
    p.add_argument("name")
    p.add_argument("-f", "--follow", action="store_true", help="keep printing new lines")
    p.add_argument("-n", "--lines", type=int, default=50, help="how many lines (default 50)")

    p = cmd("show", cmd_show, "print an app's config")
    p.add_argument("name")

    for name, func, help in (
        ("start", cmd_start, "enable apps (snt starts them)"),
        ("stop", cmd_stop, "disable apps (snt stops them and keeps them stopped)"),
        ("restart", cmd_restart, "restart running apps"),
        ("update", cmd_update, "check GitHub for updates right now"),
    ):
        cmd(name, func, help).add_argument("names", nargs="+", metavar="name")

    p = cmd("run", cmd_run, "run snt in the foreground (all apps, follows config changes)")
    p.add_argument("names", nargs="*", help="only these apps")

    p = cmd("sync", cmd_sync, "clone/update and run setup once, without starting")
    p.add_argument("name")

    p = cmd("service", cmd_service, "run snt as a systemd service (Linux)")
    p.add_argument("action", choices=["install", "uninstall", "show"])
    p.add_argument("--system", action="store_true",
                   help="system-wide unit (needs sudo) instead of a user unit")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    setup_logging(args.verbose, args.log_file, timestamps=args.func is cmd_run)
    store = Store(args.home)
    try:
        return args.func(args, store)
    except (CliError, ConfigError, GitError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
