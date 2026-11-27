"""`snt doctor`: environment checks. Each check yields (level, title, fix).

`fix` is plain text, one line per row. A row starting with "$ " is a command to run;
every other row is an explanation. An empty string means there is nothing to fix.
"""

from __future__ import annotations

import datetime as dt
import getpass
import glob
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from smolnugget import service, term
from smolnugget.config import AppConfig, ConfigError, Store
from smolnugget.state import daemon_info

OK, WARN, FAIL = "OK", "WARN", "FAIL"
MIN_PYTHON = (3, 11)
MIN_GIT = (2, 31)
MIN_FREE_FRACTION = 0.10
MIN_FREE_BYTES = 1024 ** 3
CMD = "$ "
WALK_LIMIT = 200_000  # files counted per folder before the size is given up on

OS_RELEASE = Path("/etc/os-release")
SSH_ROOT = Path("/etc/ssh")
REBOOT_FILE = Path("/var/run/reboot-required")

# (distro id, VERSION_ID) -> end of security support
OS_EOL: dict[tuple[str, str], dt.date] = {
    ("debian", "9"): dt.date(2022, 6, 30),
    ("debian", "10"): dt.date(2024, 6, 30),
    ("debian", "11"): dt.date(2026, 8, 31),
    ("debian", "12"): dt.date(2028, 6, 30),
    ("ubuntu", "18.04"): dt.date(2023, 5, 31),
    ("ubuntu", "20.04"): dt.date(2025, 5, 31),
    ("ubuntu", "22.04"): dt.date(2027, 6, 1),
    ("ubuntu", "24.04"): dt.date(2029, 6, 1),
}

Check = tuple[str, str, str]


# --- fix text -------------------------------------------------------------------------
def fix(*lines: str) -> str:
    return "\n".join(lines)


def cmd(command: str) -> str:
    return CMD + command


def fix_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip()]


def is_command(line: str) -> bool:
    return line.startswith(CMD)


def command_text(line: str) -> str:
    """The command part of a command line, with a sudo marker when it needs one."""
    text = line[len(CMD):]
    # also `echo ... | sudo tee ...` and `a && sudo b`, not only commands starting with sudo
    needs = text.startswith("sudo ") or " sudo " in text
    return text + " (needs sudo)" if needs else text


def _linux() -> bool:
    return sys.platform.startswith("linux")


def _q(path: Path | str) -> str:
    """Quote a path for the shell the user is most likely running."""
    if sys.platform == "win32":
        return "'" + str(path).replace("'", "''") + "'"
    return shlex.quote(str(path))


def rm_command(path: Path) -> str:
    if sys.platform == "win32":
        return f"Remove-Item -Recurse -Force {_q(path)}"
    return f"rm -rf {_q(path)}"


def mv_command(src: Path, dst: Path) -> str:
    return f"{'Move-Item' if sys.platform == 'win32' else 'mv'} {_q(src)} {_q(dst)}"


def human_size(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.0f} MB"
    return f"{max(n, 1) / 1024:.0f} KB" if n else "0 KB"


def dir_size(path: Path, limit: int = WALK_LIMIT) -> int:
    """Total bytes below `path`; unreadable entries are skipped, huge trees are cut off."""
    total = seen = 0
    for root, _dirs, files in os.walk(path, onerror=lambda e: None):
        for name in files:
            seen += 1
            if seen > limit:
                return total
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


def _run(argv: list[str], timeout: float = 10, env: dict | None = None) -> str | None:
    """stdout of a command, or None when it is missing, fails or times out."""
    try:
        res = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout if res.returncode == 0 else None


# --- basic tools ----------------------------------------------------------------------
def check_python() -> Check:
    v = sys.version_info
    title = f"python {v.major}.{v.minor}.{v.micro}"
    if (v.major, v.minor) < MIN_PYTHON:
        how = {"win32": cmd("winget install Python.Python.3.12"),
               "linux": cmd("sudo apt install python3")}.get(sys.platform, "")
        return FAIL, title, fix("snt needs Python 3.11 or newer. Install a current Python"
                                " (then run snt with it):", *([how] if how else []))
    return OK, title, ""


def git_version() -> tuple[int, ...] | None:
    try:
        res = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", res.stdout)
    return tuple(int(x) for x in m.groups() if x is not None) if m else None


def _install_git_fix() -> list[str]:
    if sys.platform == "win32":
        return ["Install or update git:", cmd("winget install Git.Git")]
    if _linux():
        return ["Install or update git:", cmd("sudo apt update && sudo apt install git")]
    return ["Install or update git (https://git-scm.com/downloads)."]


def uses_https_tokens(apps: dict[str, AppConfig] | None) -> bool:
    return any(a.token_env and a.url.lower().startswith(("http://", "https://"))
               for a in (apps or {}).values())


def check_git(apps: dict[str, AppConfig] | None = None) -> Check:
    """`apps=None` means unknown: an old git then warns, as it may matter."""
    if not shutil.which("git"):
        return FAIL, "git not found", fix("snt clones apps with git and it is not on PATH.",
                                          *_install_git_fix())
    ver = git_version()
    if ver is None:
        return WARN, "git found, version unknown", fix("Run this and see what it says:",
                                                       cmd("git --version"))
    title = "git " + ".".join(map(str, ver))
    if ver[:2] < MIN_GIT:
        if apps is not None and not uses_https_tokens(apps):
            return OK, f"{title} (fine: no HTTPS tokens in use)", ""
        return WARN, title, fix("Tokens over HTTPS need git 2.31 or newer.", *_install_git_fix())
    return OK, title, ""


def check_ssh() -> Check:
    if shutil.which("ssh"):
        return OK, "ssh found", ""
    how = {"linux": [cmd("sudo apt install openssh-client")],
           "win32": ["Turn on Settings > System > Optional features > OpenSSH Client."]}
    return WARN, "ssh not found", fix("Only needed for remote servers ('snt -r', 'snt hub').",
                                      *how.get(sys.platform, []))


# --- home, config, disk ---------------------------------------------------------------
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
                            fix("Other users can read it. Tighten the permissions:",
                                cmd(f"chmod {want:o} {_q(path)}"))))
    return out


def pip_cache_dir() -> Path:
    return Path.home() / ".cache" / "pip"


def _leftover_fix_lines(leftovers: list[Path]) -> list[str]:
    lines = [cmd(rm_command(p)) for p in leftovers[:10]]
    if len(leftovers) > 10:
        lines.append(f"...and {len(leftovers) - 10} more folders in the same place.")
    return lines


def _cleanup_lines(store: Store, apps: dict[str, AppConfig] | None) -> list[str]:
    lines: list[str] = []
    leftovers = leftover_folders(store, apps)
    if leftovers:
        lines += ["Leftover app folders of apps that are no longer in config.json:"]
        lines += _leftover_fix_lines(leftovers)
    pip = pip_cache_dir()
    if pip.is_dir():
        lines += [f"pip's download cache ({human_size(dir_size(pip))}):",
                  cmd("python3 -m pip cache purge")]
    if _linux() and shutil.which("journalctl"):
        lines += ["Old system logs (keeps the newest 100 MB):",
                  cmd("sudo journalctl --vacuum-size=100M")]
    if _linux() and shutil.which("apt"):
        lines += ["Downloaded package files:", cmd("sudo apt-get clean")]
    return lines


def _disk_fix(store: Store, apps: dict[str, AppConfig] | None) -> str:
    if sys.platform == "win32":
        see = cmd("Get-ChildItem $HOME -Directory | ForEach-Object { '{0,10:N0} MB  {1}' -f "
                  "((Get-ChildItem $_.FullName -Recurse -File -ErrorAction SilentlyContinue | "
                  "Measure-Object Length -Sum).Sum / 1MB), $_.Name } | Sort-Object")
    else:
        see = cmd("du -xh ~ --max-depth=1 2>/dev/null | sort -h | tail -15")
    lines = ["snt cannot free space by itself: checkouts, venvs and logs live in its home.",
             "See what takes space:", see]
    cleanups = _cleanup_lines(store, apps)
    if cleanups:
        lines += ["Cleanups that are safe for snt:"] + cleanups
    return fix(*lines)


def check_disk(store: Store, apps: dict[str, AppConfig] | None = None) -> Check:
    path = store.home
    while not path.exists() and path.parent != path:
        path = path.parent
    try:
        usage = shutil.disk_usage(path)
    except OSError as e:
        return WARN, "disk space unknown", fix(
            f"{e}. Check that the drive holding the snt home is mounted and readable.")
    free_gb = usage.free / 1024 ** 3
    title = f"disk {free_gb:.1f} GB free ({usage.free / usage.total:.0%})"
    if usage.free < MIN_FREE_BYTES or usage.free / usage.total < MIN_FREE_FRACTION:
        return WARN, title, _disk_fix(store, apps)
    return OK, title, ""


def check_config(store: Store) -> tuple[Check, dict[str, AppConfig]]:
    try:
        apps = store.load()
    except ConfigError as e:
        bak = store.config_path.with_name(store.config_path.name + ".bak")
        return (FAIL, "config.json is not valid",
                fix(str(e), f"Fix {store.config_path} by hand, or move it away and add"
                    " your apps again:", cmd(mv_command(store.config_path, bak)))), {}
    n = len(apps)
    return (OK, f"config.json ({n} app{'' if n == 1 else 's'})", ""), apps


def leftover_folders(store: Store, apps: dict[str, AppConfig] | None) -> list[Path]:
    """Folders in <home>/apps that no app of the config owns (None: config unknown)."""
    if apps is None:
        return []
    try:
        entries = sorted((store.home / "apps").iterdir())
    except OSError:
        return []
    return [p for p in entries if p.is_dir() and p.name not in apps]


def check_leftovers(store: Store, apps: dict[str, AppConfig] | None) -> Check | None:
    found = leftover_folders(store, apps)
    if not found:
        return None
    size = sum(dir_size(p) for p in found)
    names = ", ".join(p.name for p in found)
    return WARN, f"leftover app folders: {names} ({human_size(size)})", fix(
        "These belong to apps that are no longer in config.json. Delete what you do not need:",
        *_leftover_fix_lines(found))


# --- daemon and apps ------------------------------------------------------------------
def check_daemon(store: Store, apps: dict[str, AppConfig]) -> Check:
    info = daemon_info(store)
    if info["running"]:
        return OK, f"snt is running (pid {info['pid'] or '?'})", ""
    if apps:
        lines = ["Start it in the foreground:", cmd("snt run")]
        if _linux():
            lines += ["Or keep it running across reboots:", cmd("snt service install")]
        else:
            lines += ["Keep 'snt run' open, e.g. started by Task Scheduler or launchd."]
        return WARN, "snt is not running", fix(*lines)
    return OK, "snt is not running (no apps yet)", ""


def check_app(store: Store, app: AppConfig) -> list[Check]:
    out: list[Check] = []
    git_dir = store.repo_dir(app.name) / ".git"
    if git_dir.exists():
        out.append((OK, f"{app.name}: checkout present", ""))
    else:
        out.append((WARN, f"{app.name}: not cloned yet",
                    fix("Clone it and run its setup now ('snt run' does this too):",
                        cmd(f"snt sync {app.name}"))))
    if app.token_env:
        if store.resolve_token(app):
            out.append((OK, f"{app.name}: token ${app.token_env} found", ""))
        else:
            out.append((FAIL, f"{app.name}: token ${app.token_env} is missing",
                        fix(f"Put the token in the environment or in {store.secrets_path}"
                            f" as {app.token_env}=..., or give the app a new one:",
                            cmd(f"snt edit {app.name} --token ..."))))
    error = store.read_state(app.name).get("last_error")
    if error:
        first = str(error).splitlines()[0] if str(error).strip() else "unknown error"
        first = term.clean(first)
        out.append((WARN, f"{app.name}: last error: {first}",
                    fix("Read the app's output:", cmd(f"snt logs {app.name}"))))
    return out


# --- host checks (Linux) --------------------------------------------------------------
def parse_os_release(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and not key.startswith("#"):
            out[key] = value.strip().strip("\"'")
    return out


def check_os(release_path: Path = OS_RELEASE, today: dt.date | None = None) -> Check | None:
    try:
        info = parse_os_release(release_path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    name = info.get("PRETTY_NAME") or info.get("ID") or "unknown"
    eol = OS_EOL.get((info.get("ID", "").lower(), info.get("VERSION_ID", "")))
    if eol is None or (today or dt.date.today()) <= eol:
        return OK, f"os {name}", ""
    return WARN, f"{name}: no security updates since {eol.isoformat()}", fix(
        "Upgrade to the next release, or reinstall the server with a current one.",
        "Back up your app data first: snt only keeps code, config.json and secrets.env.",
        "Afterwards add the apps again with 'snt add'; snt clones them by itself.")


def count_upgradable(text: str) -> int:
    """Packages listed by `apt list --upgradable` (everything after the header line)."""
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith("Listing")), -1)
    return sum(1 for ln in lines[start + 1:] if ln.strip())


def check_updates() -> Check | None:
    if not shutil.which("apt"):
        return None
    env = dict(os.environ, LC_ALL="C")
    out = _run(["apt", "list", "--upgradable"], timeout=20, env=env)
    if out is None:
        return None
    n = count_upgradable(out)
    if n <= 0:
        return OK, "no package updates pending", ""
    return WARN, f"{n} package updates pending", fix(
        "Install the updates (security fixes included):",
        cmd("sudo apt update && sudo apt upgrade"))


def check_reboot(path: Path = REBOOT_FILE) -> Check | None:
    if not path.exists():
        return None
    return WARN, "reboot required (system updates are waiting for it)", fix(
        "Reboot when it suits you:", cmd("sudo reboot"),
        "If snt is installed as a service and linger is on, it starts the apps after boot.")


def _sshd_rows(path: Path, root: Path, depth: int = 0):
    """(keyword, arguments) rows of an sshd config in reading order, Includes expanded."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = re.split(r"\s*=\s*|\s+", line, maxsplit=1)
        key, args = parts[0].lower(), (parts[1] if len(parts) > 1 else "")
        if key == "include" and depth < 8:
            for pattern in args.split():
                full = pattern if os.path.isabs(pattern) else str(root / pattern)
                for name in sorted(glob.glob(full)):
                    yield from _sshd_rows(Path(name), root, depth + 1)
        else:
            yield key, args


def password_auth(config: Path, root: Path) -> str:
    """sshd's PasswordAuthentication: the first value wins; none means the default `yes`.
    Rows after a Match line only apply to some connections, so reading stops there."""
    for key, args in _sshd_rows(config, root):
        if key == "match":
            break
        if key == "passwordauthentication" and args.split():
            return args.split()[0].strip("\"'").lower()
    return "yes"


def check_ssh_password(root: Path = SSH_ROOT) -> Check | None:
    config = root / "sshd_config"
    try:
        config.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if password_auth(config, root) != "yes":
        return OK, "ssh server: password login is off", ""
    drop_in = (root / "sshd_config.d" / "00-no-password.conf").as_posix()
    return WARN, "SSH allows password login", fix(
        "Anyone on the internet can try passwords. First make sure key login works:",
        cmd("ssh -i <key> user@host"),
        "(run it from your PC; this sets up a key for you:)",
        cmd("snt remote add NAME user@host --setup-key"),
        "Then switch passwords off (keep your current session open while you test):",
        cmd(f"echo 'PasswordAuthentication no' | sudo tee {drop_in}"),
        cmd("sudo systemctl reload ssh"),
        "On some distros the service is called sshd: sudo systemctl reload sshd")


def _lingering(user: str) -> bool | None:
    out = _run(["loginctl", "show-user", user, "-p", "Linger"], timeout=5)
    if out is None:
        return None
    return out.strip().lower() == "linger=yes"


def check_service(apps: dict[str, AppConfig]) -> list[Check]:
    if not apps:
        return []
    user_unit = service.unit_path(False).exists()
    if not user_unit and not service.unit_path(True).exists():
        return [(WARN, "snt is not installed as a service: apps stop on reboot", fix(
            "Install the service (start at login/boot, restart after crashes):",
            cmd("snt service install"),
            "Add --system for a system-wide unit that needs no login (needs sudo)."))]
    if not user_unit or not shutil.which("loginctl"):
        return []
    try:
        user = getpass.getuser()
    except (OSError, KeyError, ImportError):
        return []
    if _lingering(user) is False:
        return [(WARN, "linger is off: the service stops when you log out", fix(
            "Let your user services run without a login session:",
            cmd("sudo loginctl enable-linger $USER")))]
    return []


def host_checks(apps: dict[str, AppConfig]) -> list[Check]:
    """Linux-only checks; each one that does not apply returns nothing."""
    if not _linux():
        return []
    found: list[Check | None] = [check_os(), check_updates(), check_reboot(),
                                 check_ssh_password()]
    if shutil.which("systemctl"):
        found += _safe_list("service", check_service, apps)
    return [c for c in found if c is not None]


def _safe_list(title: str, fn, *args) -> list[Check]:
    got = _safe(title, fn, *args)
    return got if isinstance(got, list) else [got]


def _safe(title: str, fn, *args):
    """Run one check; an unreadable path becomes a WARN line instead of a traceback."""
    try:
        return fn(*args)
    except OSError as e:
        return (WARN, f"{title}: {e.strerror or e}", "check the permissions of that path")


def run_checks(store: Store) -> list[Check]:
    base: list[Check] = [check_python()]
    tail: list[Check] = [check_ssh()]
    if store.home.exists() and not store.home.is_dir():
        return [*base, check_git(), *tail, (FAIL, f"home {store.home} is not a directory", fix(
            "Point --home / SNT_HOME at a folder, or move the file away:",
            cmd(mv_command(store.home, store.home.with_name(store.home.name + ".bak")))))]
    home = _safe("home", check_home, store)
    got = _safe("config.json", check_config, store)
    cfg, apps = got if isinstance(got, tuple) and len(got) == 2 and isinstance(got[1], dict) \
        else (got, {})
    known = apps if cfg[0] != FAIL else None  # an unreadable config must not mark folders as leftover
    results = [*base, check_git(known), *tail]
    results += home if isinstance(home, list) else [home]
    results.append(cfg)
    results.append(_safe("daemon", check_daemon, store, apps))
    results.append(_safe("disk", check_disk, store, known))
    left = _safe("leftover folders", check_leftovers, store, known)
    results += [left] if left else []
    for app in apps.values():
        results += _safe_list(app.name, check_app, store, app)
    results += host_checks(apps)
    return results


# --- report ---------------------------------------------------------------------------
def problem_numbers(results: list[Check]) -> dict[int, int]:
    """Index in `results` -> reference number of that problem (FAILs first, then WARNs)."""
    order = [i for lvl in (FAIL, WARN) for i, r in enumerate(results) if r[0] == lvl]
    return {i: n for n, i in enumerate(order, 1)}


def to_json(results: list[Check]) -> dict:
    return {
        "checks": [{"level": lvl, "title": title, "fix": fix_lines(text)}
                   for lvl, title, text in results],
        "failed": sum(1 for r in results if r[0] == FAIL),
        "warnings": sum(1 for r in results if r[0] == WARN),
    }
