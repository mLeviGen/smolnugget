"""Host metrics for the web UI's system page.

Uses psutil when installed (the `[top]` extra); otherwise falls back to the stdlib and,
on Linux, /proc. Every field may be None when the platform can't provide it.
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import sys
import time
from pathlib import Path

from smolnugget import __version__

try:  # optional
    import psutil
except ImportError:  # pragma: no cover
    psutil = None


def _os_name() -> str:
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return platform.platform(terse=True)


def _proc_meminfo() -> dict | None:
    try:
        info = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, value = line.split(":", 1)
            info[key] = int(value.split()[0]) * 1024
        total, avail = info["MemTotal"], info.get("MemAvailable", info.get("MemFree", 0))
        return {"total": total, "used": total - avail, "available": avail}
    except (OSError, KeyError, ValueError):
        return None


def _proc_uptime() -> float | None:
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


class _ProcCpu:
    """CPU busy % from /proc/stat deltas between calls (Linux without psutil)."""

    def __init__(self):
        self.last = self._read()

    @staticmethod
    def _read():
        try:
            fields = [int(x) for x in Path("/proc/stat").read_text().splitlines()[0].split()[1:]]
            idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
            return sum(fields), idle
        except (OSError, ValueError, IndexError):
            return None

    def percent(self) -> float | None:
        now = self._read()
        prev, self.last = self.last, now
        if not now or not prev or now[0] == prev[0]:
            return None
        busy = 1 - (now[1] - prev[1]) / (now[0] - prev[0])
        return round(max(0.0, min(1.0, busy)) * 100, 1)


class SystemInfo:
    def __init__(self, disk_path: Path):
        self.disk_path = disk_path
        self._proc_cpu = None
        if psutil is not None:
            psutil.cpu_percent(None)  # prime: the first call always returns 0
        elif sys.platform.startswith("linux"):
            self._proc_cpu = _ProcCpu()

    def _cpu(self) -> float | None:
        if psutil is not None:
            return round(psutil.cpu_percent(None), 1)
        return self._proc_cpu.percent() if self._proc_cpu else None

    def _memory(self) -> dict | None:
        if psutil is not None:
            m = psutil.virtual_memory()
            mem = {"total": m.total, "used": m.total - m.available, "available": m.available}
        else:
            mem = _proc_meminfo()
        if mem:
            mem["percent"] = round(mem["used"] / mem["total"] * 100, 1) if mem["total"] else None
        return mem

    def _disk(self) -> dict | None:
        path = self.disk_path
        while not path.exists() and path.parent != path:
            path = path.parent
        try:
            d = shutil.disk_usage(path)
        except OSError:
            return None
        return {"path": str(path), "total": d.total, "used": d.used, "free": d.free,
                "percent": round(d.used / d.total * 100, 1) if d.total else None}

    def _uptime(self) -> float | None:
        if psutil is not None:
            return time.time() - psutil.boot_time()
        return _proc_uptime()

    def snapshot(self) -> dict:
        try:
            load = [round(x, 2) for x in os.getloadavg()]
        except (AttributeError, OSError):  # Windows
            load = None
        uptime = self._uptime()
        return {
            "hostname": socket.gethostname(),
            "os": _os_name(),
            "arch": platform.machine(),
            "python": platform.python_version(),
            "smolnugget": __version__,
            "cpu_count": os.cpu_count(),
            "cpu_percent": self._cpu(),
            "load_avg": load,
            "memory": self._memory(),
            "disk": self._disk(),
            "uptime_seconds": int(uptime) if uptime is not None else None,
            "psutil": psutil is not None,
        }
