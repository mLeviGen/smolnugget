"""Cross-platform exclusive file locks (released automatically if the process dies)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"


class FileLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.fd: int | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            if IS_WINDOWS:
                import msvcrt
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self.fd = fd
        return True

    def release(self) -> None:
        if self.fd is None:
            return
        try:
            if IS_WINDOWS:
                import msvcrt
                os.lseek(self.fd, 0, os.SEEK_SET)
                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(self.fd)
        self.fd = None


def is_locked(path: Path) -> bool:
    if not Path(path).exists():
        return False
    lock = FileLock(path)
    if lock.acquire():
        lock.release()
        return False
    return True
