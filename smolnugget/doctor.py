"""`snt doctor`: environment checks. Each check yields (level, title, hint)."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys

from smolnugget import term
from smolnugget.config import AppConfig, ConfigError, Store
from smolnugget.state import daemon_info

OK, WARN, FAIL = "OK", "WARN", "FAIL"
MIN_PYTHON = (3, 11)
MIN_GIT = (2, 31)
MIN_FREE_FRACTION = 0.10
MIN_FREE_BYTES = 1024 ** 3

Check = tuple[str, str, str]


def check_python() -> Check:
    v = sys.version_info
    title = f"python {v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) < MIN_PYTHON:
        return FAIL, title, "snt needs Python 3.11 or newer"
    return OK, title, ""


def git_version() -> tuple[int, ...] | None:
    try:
        res = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", res.stdout)
    return tuple(int(x) for x in m.groups() if x is not None) if m else None


def check_git() -> Check:
    if not shutil.which("git"):
        return FAIL, "git not found", "install git and make sure it is on PATH"
    ver = git_version()
    if ver is None:
        return WARN, "git found, version unknown", "run 'git --version' to see what is wrong"
    title = "git " + ".".join(map(str, ver))
    if ver[:2] < MIN_GIT:
        return WARN, title, "tokens over HTTPS need git 2.31+; upgrade git"
    return OK, title, ""


def check_ssh() -> Check:
    if shutil.which("ssh"):
        return OK, "ssh found", ""
    return WARN, "ssh not found", "only needed for remote servers ('snt -r', 'snt hub')"


def check_home(store: Store) -> list[Check]:
    if not store.home.is_dir():
        return [(OK, f"home {store.home} (not created yet)", "")]
    out: list[Check] = [(OK, f"home {store.home}", "")]
    if sys.platform != "win32":
        for path, want in ((store.home, 0o700), (store.config_path, 0o600)):
            if not path.exists():
                continue
            mode = path.stat().st_mode & 0o777
            if mode & ~want & 0o777:
                out.append((WARN, f"{path.name or path} has mode {mode:o}",
                            f"run: chmod {want:o} {path}"))
    return out


def check_disk(store: Store) -> Check:
    path = store.home
    while not path.exists() and path.parent != path:
        path = path.parent
    try:
        usage = shutil.disk_usage(path)
    except OSError as e:
        return WARN, "disk space unknown", str(e)
    free_gb = usage.free / 1024 ** 3
    title = f"disk {free_gb:.1f} GB free ({usage.free / usage.total:.0%})"
    if usage.free < MIN_FREE_BYTES or usage.free / usage.total < MIN_FREE_FRACTION:
        return WARN, title, "low disk space: checkouts, venvs and logs live in the snt home"
    return OK, title, ""


def check_config(store: Store) -> tuple[Check, dict[str, AppConfig]]:
    try:
        apps = store.load()
    except ConfigError as e:
        return (FAIL, "config.json is not valid",
                f"{e}; fix or delete {store.config_path}"), {}
    n = len(apps)
    return (OK, f"config.json ({n} app{'' if n == 1 else 's'})", ""), apps


def check_daemon(store: Store, apps: dict[str, AppConfig]) -> Check:
    info = daemon_info(store)
    if info["running"]:
        return OK, f"snt is running (pid {info['pid'] or '?'})", ""
    if apps:
        return (WARN, "snt is not running",
                "start it with 'snt run' (or install it: 'snt service install')")
    return OK, "snt is not running (no apps yet)", ""


def check_app(store: Store, app: AppConfig) -> list[Check]:
    out: list[Check] = []
    git_dir = store.repo_dir(app.name) / ".git"
    if git_dir.exists():
        out.append((OK, f"{app.name}: checkout present", ""))
    else:
        out.append((WARN, f"{app.name}: not cloned yet",
                    f"snt sync {app.name} (or 'snt run' clones it)"))
    if app.token_env:
        if store.resolve_token(app):
            out.append((OK, f"{app.name}: token ${app.token_env} found", ""))
        else:
            out.append((FAIL, f"{app.name}: token ${app.token_env} is missing",
                        f"set it in the environment or in {store.secrets_path}, "
                        f"or: snt edit {app.name} --token ..."))
    error = store.read_state(app.name).get("last_error")
    if error:
        first = str(error).splitlines()[0] if str(error).strip() else "unknown error"
        first = term.clean(first)
        out.append((WARN, f"{app.name}: last error: {first}", f"see: snt logs {app.name}"))
    return out


def _safe(title: str, fn, *args):
    """Run one check; an unreadable path becomes a WARN line instead of a traceback."""
    try:
        return fn(*args)
    except OSError as e:
        return (WARN, f"{title}: {e.strerror or e}", "check the permissions of that path")


def run_checks(store: Store) -> list[Check]:
    results: list[Check] = [check_python(), check_git(), check_ssh()]
    if store.home.exists() and not store.home.is_dir():
        return results + [(FAIL, f"home {store.home} is not a directory",
                           "point --home / SNT_HOME at a folder")]
    home = _safe("home", check_home, store)
    results += home if isinstance(home, list) else [home]
    got = _safe("config.json", check_config, store)
    cfg, apps = got if isinstance(got, tuple) and len(got) == 2 and isinstance(got[1], dict) \
        else (got, {})
    results.append(cfg)
    results.append(_safe("daemon", check_daemon, store, apps))
    results.append(_safe("disk", check_disk, store))
    for app in apps.values():
        one = _safe(app.name, check_app, store, app)
        results += one if isinstance(one, list) else [one]
    return results
