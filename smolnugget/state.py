"""Reading status and sending commands to a running daemon (no IPC beyond files)."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime

from smolnugget import __version__
from smolnugget.config import AppConfig, Store, write_atomic
from smolnugget.locks import is_locked

SCHEMA = 1
CONTROL_ACTIONS = ("restart", "update")


def iso(ts: float | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, UTC).isoformat(timespec="seconds")


def is_managed(store: Store, name: str) -> bool:
    """True while some snt process is running this app."""
    return is_locked(store.lock_path(name))


def daemon_info(store: Store) -> dict:
    running = is_locked(store.daemon_lock_path)
    info = {"running": running, "pid": None, "started_at": None}
    if running:
        try:
            data = json.loads((store.home / "daemon.json").read_text(encoding="utf-8"))
            info.update(pid=data.get("pid"), started_at=iso(data.get("started_at")))
        except (OSError, ValueError):
            pass
    return info


def app_status(app: AppConfig, store: Store) -> dict:
    """Machine-readable status of one app. Never includes tokens or env values."""
    st = store.read_state(app.name)
    managed = is_managed(store, app.name)
    pid = st.get("pid") if managed else None
    started = st.get("started_at") if pid else None
    if pid:
        status = "running"
    elif managed:
        status = "waiting"
    else:
        status = "stopped" if app.enabled else "disabled"
    return {
        "name": app.name,
        "status": status,
        "enabled": app.enabled,
        "pid": pid,
        "commit": st.get("commit") if pid else None,
        "checkout": st.get("checkout"),
        "started_at": iso(started),
        "uptime_seconds": int(time.time() - started) if started else None,
        "restarts": st.get("restarts", 0) if managed else 0,
        "last_check": iso(st.get("last_check")),
        "last_error": st.get("last_error"),
        "repo": {"url": app.url, "branch": app.branch},
        "command": app.command,
        "restart_policy": app.restart,
        "check_interval": app.check_interval,
    }


def snapshot(store: Store, apps: list[AppConfig]) -> dict:
    return {
        "schema": SCHEMA,
        "smolnugget": __version__,
        "generated_at": iso(time.time()),
        "daemon": daemon_info(store),
        "apps": [app_status(a, store) for a in apps],
    }


def send_control(store: Store, name: str, action: str) -> None:
    assert action in CONTROL_ACTIONS
    write_atomic(store.control_path(name), action + "\n")


def take_control(store: Store, name: str) -> str | None:
    path = store.control_path(name)
    try:
        action = path.read_text(encoding="utf-8").strip()
        path.unlink()
    except OSError:
        return None
    return action if action in CONTROL_ACTIONS else None
