"""Installing `snt run` as a systemd service (Linux)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from smolnugget.config import Store

UNIT = "smolnugget.service"


class ServiceError(Exception):
    pass


def _check_platform() -> None:
    if not sys.platform.startswith("linux") or not shutil.which("systemctl"):
        raise ServiceError(
            "services are supported on Linux with systemd only; "
            "elsewhere keep 'snt run' running (e.g. Task Scheduler on Windows, launchd on macOS)"
        )


def unit_path(system: bool) -> Path:
    if system:
        return Path("/etc/systemd/system") / UNIT
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "systemd" / "user" / UNIT


def service_home(store: Store, explicit_home: bool, system: bool) -> tuple[Path, str | None]:
    """Home directory and user for a system service started via sudo."""
    run_as = os.environ.get("SUDO_USER") if system else None
    home = store.home
    if run_as and not explicit_home:
        import pwd
        home = Path(pwd.getpwnam(run_as).pw_dir) / ".smolnugget"
    return home, run_as


def unit_text(home: Path, system: bool, run_as: str | None = None) -> str:
    pkg_parent = Path(__file__).resolve().parent.parent
    lines = [
        "[Unit]",
        "Description=smolnugget - keeps GitHub repos and their processes running",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
    ]
    if system and run_as:
        lines.append(f"User={run_as}")
    lines += [
        f"WorkingDirectory={pkg_parent}",
        f"ExecStart={sys.executable} -m smolnugget --home {home} run",
        "Environment=PYTHONUNBUFFERED=1",
        "Restart=always",
        "RestartSec=5",
        "# SIGTERM goes to snt only; it stops the apps itself, then systemd kills leftovers",
        "KillMode=mixed",
        "TimeoutStopSec=90",
        "",
        "[Install]",
        f"WantedBy={'multi-user.target' if system else 'default.target'}",
        "",
    ]
    return "\n".join(lines)


def _systemctl(system: bool, *args: str) -> None:
    cmd = ["systemctl"] + ([] if system else ["--user"]) + list(args)
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise ServiceError(f"{' '.join(cmd)} failed: {(res.stderr or res.stdout).strip()}")


def install(store: Store, explicit_home: bool, system: bool) -> list[str]:
    _check_platform()
    if system and os.geteuid() != 0:
        raise ServiceError("a system service needs root: run with sudo, or drop --system")
    home, run_as = service_home(store, explicit_home, system)
    path = unit_path(system)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(unit_text(home, system, run_as), encoding="utf-8")
    _systemctl(system, "daemon-reload")
    _systemctl(system, "enable", UNIT)
    _systemctl(system, "restart", UNIT)
    ctl = "systemctl" if system else "systemctl --user"
    journal = "journalctl -u" if system else "journalctl --user -u"
    notes = [f"installed {path}", f"service is running: {ctl} status smolnugget",
             f"daemon logs: {journal} smolnugget -f"]
    if not system:
        user = os.environ.get("USER", "")
        res = subprocess.run(["loginctl", "show-user", user, "-p", "Linger"],
                             capture_output=True, text=True)
        if "Linger=yes" not in res.stdout:
            notes.append("to keep it running after you log out / start on boot, run once:\n"
                         f"  sudo loginctl enable-linger {user}")
    return notes


def uninstall(system: bool) -> list[str]:
    _check_platform()
    path = unit_path(system)
    if not path.exists():
        raise ServiceError(f"no service installed at {path}")
    try:
        _systemctl(system, "disable", "--now", UNIT)
    except ServiceError:
        pass
    path.unlink()
    _systemctl(system, "daemon-reload")
    return [f"removed {path} (apps and their files are kept)"]
