import subprocess
import time
from pathlib import Path


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout.strip()


class Remote:
    """A bare repo plus a working copy used to push commits into it."""

    def __init__(self, root: Path):
        self.bare = root / "remote.git"
        self.work = root / "work"
        self.bare.mkdir(parents=True)
        self.work.mkdir()
        git(self.bare, "init", "--bare", "-b", "main")
        git(self.work, "init", "-b", "main")
        git(self.work, "remote", "add", "origin", str(self.bare))

    @property
    def url(self) -> str:
        return str(self.bare)

    def commit(self, files: dict[str, str], msg: str = "c", branch: str = "main") -> str:
        for name, content in files.items():
            p = self.work / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        git(self.work, "add", "-A")
        git(self.work, "commit", "-m", msg)
        git(self.work, "push", "-f", "origin", f"HEAD:{branch}")
        return git(self.work, "rev-parse", "HEAD")


def wait_for(pred, timeout: float = 15.0, step: float = 0.05, tick=None):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if tick:
            tick()
        if pred():
            return True
        time.sleep(step)
    raise AssertionError("condition not met in time")
