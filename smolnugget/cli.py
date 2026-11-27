"""Command-line interface: `snt <command>`."""

from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path

from smolnugget import __version__, doctor, term
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


HAS_SERVICE = sys.platform.startswith("linux")  # `snt service` is systemd-only


def no_daemon_hint() -> str:
    if HAS_SERVICE:
        return "snt is not running - start it with 'snt run' (or install it: 'snt service install')"
    return "snt is not running - start it with 'snt run'"


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
        print("\nnext: snt run" + ("   (or keep it running as a service: snt service install)"
                                   if HAS_SERVICE else ""))
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
    print_table(snap)
    return 0


STATUS_STYLE = {"running": term.green, "waiting": term.yellow, "stopped": term.dim,
                "disabled": term.dim}


def print_table(snap: dict) -> None:
    rows = [("NAME", "STATUS", "PID", "UPTIME", "RESTARTS", "COMMIT", "REPO")]
    errors = []
    for s in snap["apps"]:
        rows.append((s["name"], s["status"], str(s["pid"] or "-"), fmt_duration(s["uptime_seconds"]),
                     str(s["restarts"]), (s["commit"] or s["checkout"] or "-")[:8],
                     s["repo"]["url"].removeprefix("https://github.com/").removesuffix(".git")))
        if s["last_error"]:
            errors.append(f"{s['name']}: {term.clean(s['last_error'].splitlines()[0])}")
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for n, r in enumerate(rows):
        cells = [c.ljust(w) for c, w in zip(r, widths)]
        if n and (style := STATUS_STYLE.get(r[1])):
            cells[1] = style(cells[1])
        print("  ".join(cells).rstrip())
    if errors:
        print("\nerrors:")
        for e in errors:
            print("  " + term.red(e))
    if not snap["daemon"]["running"] and not any(s["status"] in ("running", "waiting")
                                                 for s in snap["apps"]):
        print("\n" + term.dim(no_daemon_hint()))


def pick_log_app(store: Store, name: str | None) -> str:
    if name:
        store.get(name)
        return name
    apps = store.load()
    if len(apps) == 1:
        return next(iter(apps))
    if not apps:
        raise CliError("no apps yet - add one with: snt add <github url>")
    raise CliError("which app? use: snt logs NAME\napps: " + ", ".join(sorted(apps)))


def cmd_logs(args, store: Store) -> int:
    args.name = pick_log_app(store, args.name)
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


LEVEL_STYLE = {doctor.OK: term.green, doctor.WARN: term.yellow, doctor.FAIL: term.red}


def print_fixes(results, numbers: dict[int, int]) -> None:
    if not numbers:
        print("Nothing to fix.")
        return
    print("How to fix:")
    for index, number in sorted(numbers.items(), key=lambda kv: kv[1]):
        _, title, text = results[index]
        print(f"{number}. {term.clean(title)}")
        for line in doctor.fix_lines(text):
            if doctor.is_command(line):
                print("    " + term.bold(term.clean(doctor.command_text(line))))
            else:
                print("  " + term.clean(line))


def cmd_doctor(args, store: Store) -> int:
    results = doctor.run_checks(store)
    failed = sum(1 for r in results if r[0] == doctor.FAIL)
    if getattr(args, "json", False):
        print(json.dumps(doctor.to_json(results), indent=2))
        return 1 if failed else 0
    numbers = doctor.problem_numbers(results)
    for index, (level, title, _) in enumerate(results):
        line = f"{LEVEL_STYLE[level](f'{level:<4}')}  {term.clean(title)}"
        if index in numbers:
            line += "  " + term.dim(f"[{numbers[index]}]")
        print(line)
    warned = sum(1 for r in results if r[0] == doctor.WARN)
    print(f"\n{failed} failed, {warned} warning(s)\n")
    print_fixes(results, numbers)
    return 1 if failed else 0


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


def serve(server, open_browser: bool, exit_on_eof: bool = False, what: str = "web UI",
          idle_exit: int = 0) -> int:
    """Run a WebServer until Ctrl+C (or, for tunnels, until stdin closes)."""
    print(f"smolnugget {what}: {server.login_url}  (Ctrl+C to stop)", flush=True)
    if open_browser:
        import webbrowser
        webbrowser.open(server.login_url)
    if exit_on_eof:  # started through an ssh tunnel: stop when the session closes
        import threading

        def watch_stdin():
            try:
                while sys.stdin.buffer.read(4096):
                    pass
            except (OSError, ValueError):
                pass
            server.shutdown()

        threading.Thread(target=watch_stdin, daemon=True).start()
    if idle_exit:  # a tunnel whose client vanished without closing the connection
        import threading

        def watch_idle():
            while True:
                time.sleep(min(5, idle_exit / 2))
                if time.monotonic() - server.last_request > idle_exit:
                    log.info("no requests for %ss, exiting", idle_exit)
                    server.shutdown()
                    return

        threading.Thread(target=watch_idle, daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def warn_public(host: str) -> None:
    if host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("serving on %s: anyone who can reach this address and has the link can "
                    "restart and stop your apps; prefer 127.0.0.1 and an ssh tunnel", host)


def cmd_web(args, store: Store) -> int:
    from smolnugget.webui import LocalSource, WebServer
    warn_public(args.host)
    token = None
    if args.token_stdin:  # `snt -r NAME web` / `snt hub` pass the token here, not in argv
        token = sys.stdin.buffer.readline().decode().strip() or None
        if not token:
            raise CliError("--token-stdin: no token on stdin")
    try:
        server = WebServer([LocalSource(store)], args.host, args.port, token=token)
    except OSError as e:
        raise CliError(f"cannot listen on {args.host}:{args.port}: {e} (try --port)") from e
    return serve(server, args.open, args.exit_on_eof, idle_exit=args.idle_exit)


def cmd_hub(args, store: Store) -> int:
    from smolnugget import remote as rm
    from smolnugget.hub import RemoteSource
    from smolnugget.webui import LocalSource, WebServer
    warn_public(args.host)
    try:
        remotes = rm.load_remotes(Store(None))
    except ConfigError as e:
        raise CliError(str(e)) from e
    if args.only:
        wanted = [n.strip() for n in args.only.split(",") if n.strip()]
        if missing := [n for n in wanted if n not in remotes and n != "local"]:
            raise CliError(f"unknown server(s): {', '.join(missing)} (see 'snt remote list')")
        remotes = {n: r for n, r in remotes.items() if n in wanted}
    sources = []
    has_local_apps = store.config_path.exists() and bool(store.load())
    if (has_local_apps and not args.only) or (args.only and "local" in args.only.split(",")):
        sources.append(LocalSource(store))
    sources += [RemoteSource(r) for r in remotes.values()]
    if not sources:
        raise CliError("nothing to show: add a server first - snt remote add NAME user@host")
    try:
        server = WebServer(sources, args.host, args.port, hub=True)
    except OSError as e:
        for s in sources:
            s.close()
        raise CliError(f"cannot listen on {args.host}:{args.port}: {e} (try --port)") from e
    names = ", ".join(s.name for s in sources)
    print(f"servers: {names}  (connecting through ssh in the background)", flush=True)
    return serve(server, args.open, what="hub")


def cmd_remote(args, store: Store) -> int:
    from smolnugget import remote as rm
    store = Store(None)  # saved servers live in the local default home
    try:
        remotes = rm.load_remotes(store)
        if args.action == "list":
            if not remotes:
                print("no saved servers - add one: snt remote add NAME user@host --setup-key")
            for r in remotes.values():
                extra = "".join([f" -p {r.port}" if r.port else "", f"  key {r.key}" if r.key else ""])
                print(f"{r.name:<16} {r.target}{extra}")
            return 0
        if args.action == "rm":
            if remotes.pop(args.name, None) is None:
                raise CliError(f"no saved server {args.name!r}")
            rm.save_remotes(store, remotes)
            print(f"removed {args.name!r} (its ssh key, if any, was left in place)")
            return 0
        # add
        rm.check_name(args.name)
        if args.target is None:
            raise CliError("usage: snt remote add NAME user@host [--key PATH] [--port N]")
        new = rm.Remote(args.name, args.target, args.port, args.key)
        if args.setup_key:
            new.key = rm.setup_key(new)
        if not args.no_check:
            res = subprocess.run(new.ssh_base() + [new.target, f"{rm.REMOTE_SNT} --version"],
                                 capture_output=True, text=True)
            if res.returncode != 0:
                raise CliError(f"cannot run snt on {new.target}: {rm.explain(res.returncode)}"
                               "\n(--no-check saves it anyway)")
            print(f"{new.target}: {res.stdout.strip()}")
        remotes[new.name] = new
        rm.save_remotes(store, remotes)
        print(f"saved {new.name!r}. Try:  snt -r {new.name} status   or   snt -r {new.name} web")
        return 0
    except rm.RemoteError as e:
        raise CliError(str(e)) from e


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


GETTING_STARTED = """smolnugget is installed. No apps yet.

  snt add <github url>        add a repo (it is cloned and the start command detected)
  snt run                     run it in the foreground{service}
  snt hub                     one dashboard for remote servers (snt remote add ...)

snt --help for all commands""".format(
    service=", or: snt service install" if HAS_SERVICE else "")


def cmd_default(store: Store) -> int:
    """`snt` without a command: status when there are apps, a short intro otherwise."""
    apps = list(store.load().values())
    if not apps:
        print(GETTING_STARTED)
        return 0
    print_table(snapshot(store, apps))
    print("\n" + term.dim("snt --help for all commands"))
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


HELP_DESCRIPTION = """Keep GitHub repositories and their processes running on a server.

Apps:
  add URL [NAME]      clone a repo, detect how to run it, save it
  edit NAME           change an app's settings (applied live)
  remove NAME (rm)    remove an app (--purge also deletes its files)
  show NAME           print an app's config

Run:
  run [NAME ...]      run snt in the foreground (all apps, follows config changes)
  service ACTION      run snt as a systemd service: install, uninstall, show
  sync NAME           clone/update and run setup once, without starting

Control:
  start NAME ...      enable apps (snt starts them)
  stop NAME ...       disable apps (snt stops them and keeps them stopped)
  restart NAME ...    restart running apps
  update NAME ...     check GitHub for updates right now

Watch:
  status (ls)         show all apps (--json for dashboards)
  logs [NAME]         print an app's output (-f to follow)
  top                 live monitor with logs and hotkeys
  web                 local web UI with status, logs and controls
  hub                 one dashboard for all saved servers
  doctor              check the environment and the apps

Servers:
  remote ACTION       saved servers: add, list, rm
  -r, --remote NAME   run any command on a saved server, e.g. snt -r prod status

Run 'snt COMMAND --help' for the options of a command."""

HELP_EPILOG = """examples:
  snt add https://github.com/you/bot       add and detect how to start it
  snt run                                  start everything in the foreground
  snt service install                      keep it running across reboots (Linux)
  snt logs bot -f                          follow an app's output
  snt -r prod status                       status of a saved server
  snt hub -o                               dashboard for all servers in the browser"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="snt", usage="snt [options] COMMAND [args]", description=HELP_DESCRIPTION,
        epilog=HELP_EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-V", "--version", action="version", version=f"smolnugget {__version__}")
    parser.add_argument("--home", type=Path,
                        help="data directory (default: $SNT_HOME or ~/.smolnugget)")
    parser.add_argument("--no-color", action="store_true",
                        help="plain output (also: NO_COLOR environment variable)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    parser.add_argument("--log-file", help="also write snt's own log to this file (rotated)")
    parser.add_argument("-r", "--remote", metavar="NAME",
                        help="run the command on a saved server (see 'snt remote'); "
                             "'snt -r NAME web' opens its dashboard here")
    # no help= on the subparsers: the command list lives in the hand-written description
    sub = parser.add_subparsers(dest="cmd", metavar="COMMAND", help=argparse.SUPPRESS)

    def cmd(name, func, help, aliases=()):
        p = sub.add_parser(name, aliases=list(aliases), description=help)
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

    p = cmd("web", cmd_web, "local web UI with status, logs and controls")
    p.add_argument("-p", "--port", type=int, default=8770, help="port (default 8770)")
    p.add_argument("--host", default="127.0.0.1",
                   help="address to listen on (default 127.0.0.1 - this computer only)")
    p.add_argument("-o", "--open", action="store_true", help="open it in the browser")
    p.add_argument("--exit-on-eof", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--token-stdin", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--idle-exit", type=int, default=0, help=argparse.SUPPRESS)

    p = cmd("hub", cmd_hub, "one dashboard for all saved servers (see 'snt remote')")
    p.add_argument("-p", "--port", type=int, default=8770, help="port (default 8770)")
    p.add_argument("--host", default="127.0.0.1", help="address to listen on (default 127.0.0.1)")
    p.add_argument("-o", "--open", action="store_true", help="open it in the browser")
    p.add_argument("--only", metavar="NAMES", help="comma-separated servers to show ('local' = this computer)")

    p = cmd("remote", cmd_remote, "saved servers for `snt -r NAME ...`")
    p.add_argument("action", choices=["add", "list", "rm"])
    p.add_argument("name", nargs="?")
    p.add_argument("target", nargs="?", help="user@host")
    p.add_argument("-k", "--key", help="private key to log in with (default: your ssh config)")
    p.add_argument("-p", "--port", type=int, help="ssh port (default 22)")
    p.add_argument("--setup-key", action="store_true",
                   help="create ~/.ssh/snt_NAME and install it on the server (asks the password once)")
    p.add_argument("--no-check", action="store_true", help="save without testing the connection")

    p = cmd("logs", cmd_logs, "print an app's output")
    p.add_argument("name", nargs="?", help="app name (optional when there is only one app)")
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

    p = cmd("doctor", cmd_doctor, "check the environment and list how to fix each problem")
    p.add_argument("--json", action="store_true", help="print the result as JSON")
    return parser


def run_on_remote(args, local: Store, argv: list[str]) -> int:
    """`snt -r NAME CMD ...`: CMD runs on the saved server; `web` is tunnelled to here."""
    from smolnugget import remote as rm
    if args.func in (cmd_remote, cmd_hub):
        raise CliError("'snt remote' and 'snt hub' work with the server list on this computer; "
                       "drop -r")
    try:
        server = rm.get_remote(local, args.remote)
        if args.func is cmd_web:
            return rm.open_dashboard(server)
        return rm.run_remote(server, rm.strip_remote_args(argv))
    except rm.RemoteError as e:
        raise CliError(str(e)) from e


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except KeyboardInterrupt:
        return 130


def _main(argv: list[str] | None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    term.set_disabled(args.no_color)
    if not getattr(args, "func", None):
        if args.remote:  # `snt -r NAME` alone: that server's status, like plain `snt` here
            argv_list = list(sys.argv[1:] if argv is None else argv)
            args.func = cmd_status
            try:
                return run_on_remote(args, Store(None), argv_list + ["status"])
            except (CliError, ConfigError) as e:
                print(f"{term.red('error:', sys.stderr)} {e}", file=sys.stderr)
                return 1
        setup_logging(args.verbose, args.log_file, timestamps=False)
        try:
            return cmd_default(Store(args.home))
        except (ConfigError, GitError) as e:
            print(f"{term.red('error:', sys.stderr)} {e}", file=sys.stderr)
            return 1
    setup_logging(args.verbose, args.log_file, timestamps=args.func in (cmd_run, cmd_hub))
    store = Store(args.home)
    try:
        if args.remote:
            return run_on_remote(args, Store(None), sys.argv[1:] if argv is None else argv)
        return args.func(args, store)
    except (CliError, ConfigError, GitError) as e:
        print(f"{term.red('error:', sys.stderr)} {e}", file=sys.stderr)
        return 1
