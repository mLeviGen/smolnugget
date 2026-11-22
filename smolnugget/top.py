"""`snt top`: a small live terminal monitor. Stdlib only; psutil adds CPU/memory if installed."""

from __future__ import annotations

import os
import shutil
import sys
import time
from pathlib import Path

from smolnugget.config import ConfigError, Store
from smolnugget.state import is_managed, send_control, snapshot

try:  # optional
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

IS_WINDOWS = sys.platform == "win32"

RESET, BOLD, DIM, REV = "\x1b[0m", "\x1b[1m", "\x1b[2m", "\x1b[7m"
GREEN, YELLOW, RED, CYAN = "\x1b[32m", "\x1b[33m", "\x1b[31m", "\x1b[36m"
STATUS_COLOR = {"running": GREEN, "waiting": YELLOW, "stopped": DIM, "disabled": DIM}


# --- keyboard ----------------------------------------------------------------
class Keys:
    """Non-blocking single-key input in a cross-platform way."""

    def __enter__(self):
        if not IS_WINDOWS:
            import termios
            import tty
            self.fd = sys.stdin.fileno()
            self.old = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        return self

    def __exit__(self, *exc):
        if not IS_WINDOWS:
            import termios
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)

    def read(self, timeout: float) -> str | None:
        if IS_WINDOWS:
            import msvcrt
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if msvcrt.kbhit():
                    ch = msvcrt.getwch()
                    if ch in ("\x00", "\xe0"):
                        return {"H": "up", "P": "down"}.get(msvcrt.getwch())
                    return "esc" if ch == "\x1b" else ch
                time.sleep(0.03)
            return None
        import select
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return None
        data = os.read(self.fd, 8).decode(errors="ignore")
        if data.startswith("\x1b["):
            return {"A": "up", "B": "down"}.get(data[2:3])
        return "esc" if data == "\x1b" else data[:1]


# --- resource usage ------------------------------------------------------------
class Usage:
    def __init__(self):
        self.cache: dict[int, object] = {}

    def _proc(self, pid: int):
        p = self.cache.get(pid)
        if p is None:
            p = self.cache[pid] = psutil.Process(pid)
            p.cpu_percent(None)  # prime
        return p

    def get(self, pid: int | None) -> tuple[str, str]:
        if psutil is None or not pid:
            return "-", "-"
        try:
            root = self._proc(pid)
            procs = [root] + [self._proc(c.pid) for c in root.children(recursive=True)]
            cpu = sum(p.cpu_percent(None) for p in procs)
            rss = sum(p.memory_info().rss for p in procs)
        except (psutil.Error, OSError):
            self.cache.pop(pid, None)
            return "-", "-"
        return f"{cpu:.0f}%", fmt_bytes(rss)


def fmt_bytes(n: float) -> str:
    for unit in ("B", "K", "M", "G"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.1f}T"


def fmt_duration(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    d, rem = divmod(seconds, 86400)
    h, rem = divmod(rem, 3600)
    m, s = divmod(rem, 60)
    if d:
        return f"{d}d{h}h"
    if h:
        return f"{h}h{m}m"
    return f"{m}m{s}s" if m else f"{s}s"


def tail(path: Path, lines: int) -> list[str]:
    if lines <= 0:
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 64 * 1024))
            data = f.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    return data.splitlines()[-lines:]


# --- rendering -------------------------------------------------------------------
def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: max(0, width - 1)] + "…"


def render(store: Store, snap: dict, selected: int, message: str, usage: Usage) -> str:
    width, height = shutil.get_terminal_size((100, 30))
    apps = snap["apps"]
    d = snap["daemon"]
    daemon = (f"{GREEN}daemon running{RESET} (pid {d['pid']})" if d["running"]
              else f"{YELLOW}daemon not running{RESET} {DIM}- start: snt run / snt service install{RESET}")
    out = [f"{BOLD}smolnugget{RESET}  {daemon}", ""]

    cols = ["NAME", "STATUS", "PID", "UPTIME", "CPU", "MEM", "RESTARTS", "COMMIT"]
    rows = []
    for a in apps:
        cpu, mem = usage.get(a["pid"])
        rows.append([a["name"], a["status"], str(a["pid"] or "-"), fmt_duration(a["uptime_seconds"]),
                     cpu, mem, str(a["restarts"]), (a["commit"] or a["checkout"] or "-")[:8]])
    widths = [max(len(c), *(len(r[i]) for r in rows)) if rows else len(c)
              for i, c in enumerate(cols)]
    out.append(DIM + _clip("  ".join(c.ljust(w) for c, w in zip(cols, widths)), width) + RESET)
    for i, (r, a) in enumerate(zip(rows, apps)):
        line = _clip("  ".join(c.ljust(w) for c, w in zip(r, widths)), width - 2)
        color = STATUS_COLOR.get(a["status"], "")
        mark = f"{RED}!{RESET}" if a["last_error"] else " "
        if i == selected:
            out.append(f"{REV}{line.ljust(width - 2)}{RESET} {mark}")
        else:
            out.append(f"{color}{line}{RESET} {mark}")
    if not apps:
        out.append(f"{DIM}no apps yet - add one with: snt add <github url>{RESET}")

    if apps:
        a = apps[selected]
        out.append("")
        if a["last_error"]:
            out.append(RED + _clip("error: " + a["last_error"].splitlines()[0], width) + RESET)
        out.append(f"{CYAN}── {a['name']} log {'─' * max(0, width - len(a['name']) - 8)}{RESET}")
        room = height - len(out) - 3
        for line in tail(store.log_path(a["name"]), room):
            out.append(_clip(line, width))
    out += [""] * max(0, height - len(out) - 2)
    out.append(YELLOW + _clip(message, width) + RESET if message else "")
    out.append(DIM + _clip("↑/↓ select  r restart  u update now  s start/stop  q quit", width) + RESET)
    return "\x1b[H" + "\x1b[K\n".join(out[:height]) + "\x1b[K\x1b[J"


# --- main loop -------------------------------------------------------------------
def toggle(store: Store, name: str) -> str:
    apps = store.load()
    app = apps[name]
    app.enabled = not app.enabled
    store.save(apps)
    return f"{name}: {'enabled, starting' if app.enabled else 'disabled, stopping'}"


def run_top(store: Store, interval: float = 1.0) -> None:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise RuntimeError("snt top needs an interactive terminal (use 'snt status --json' in scripts)")
    if IS_WINDOWS:
        os.system("")  # enable ANSI escape sequences in the Windows console
    usage = Usage()
    selected, message, message_until = 0, "", 0.0
    if psutil is None:
        message, message_until = "tip: pip install psutil to see CPU and memory", time.monotonic() + 5
    sys.stdout.write("\x1b[?1049h\x1b[?25l")  # alternate screen, hide cursor
    try:
        with Keys() as keys:
            while True:
                try:
                    apps = list(store.load().values())
                except ConfigError as e:
                    apps, message, message_until = [], f"config error: {e}", time.monotonic() + 5
                snap = snapshot(store, apps)
                selected = min(selected, max(0, len(apps) - 1))
                if time.monotonic() > message_until:
                    message = ""
                sys.stdout.write(render(store, snap, selected, message, usage))
                sys.stdout.flush()

                key = keys.read(interval)
                if key in ("q", "esc", "\x03"):
                    return
                if not apps or key is None:
                    continue
                name = apps[selected].name
                if key in ("up", "k"):
                    selected = max(0, selected - 1)
                elif key in ("down", "j"):
                    selected = min(len(apps) - 1, selected + 1)
                elif key in ("r", "u"):
                    if is_managed(store, name):
                        send_control(store, name, "restart" if key == "r" else "update")
                        message = f"{name}: {'restart' if key == 'r' else 'update check'} requested"
                    else:
                        message = f"{name} is not managed by a running snt"
                    message_until = time.monotonic() + 3
                elif key == "s":
                    message, message_until = toggle(store, name), time.monotonic() + 3
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\x1b[?25h\x1b[?1049l")
        sys.stdout.flush()
