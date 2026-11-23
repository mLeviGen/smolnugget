"""Thin wrapper around the git CLI.

The access token is never written to disk or put into the remote URL: it is
handed to git through GIT_CONFIG_* environment variables as an HTTP
Authorization header scoped to the repository host (requires git >= 2.31).
"""

from __future__ import annotations

import base64
import logging
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

CLONE_TIMEOUT = 600
GIT_TIMEOUT = 120


class GitError(Exception):
    pass


def rmtree(path: Path) -> None:
    """shutil.rmtree that also removes read-only files (git objects on Windows)."""

    def on_exc(func, p, _exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)

    if not path.exists():
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=on_exc)
    else:
        shutil.rmtree(path, onerror=on_exc)


class GitRepo:
    def __init__(self, path: Path, url: str, branch: str | None = None,
                 token: str | None = None, logger: logging.Logger | None = None):
        self.log = logger or log
        self.path = Path(path)
        self.url = url
        self.branch = branch
        self.token = token

    # --- plumbing --------------------------------------------------------
    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"  # never hang waiting for a password
        env["GCM_INTERACTIVE"] = "never"
        env["LC_ALL"] = "C"
        parts = urlsplit(self.url)
        if self.token and parts.scheme in ("http", "https"):
            creds = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
            env["GIT_CONFIG_COUNT"] = "1"
            env["GIT_CONFIG_KEY_0"] = f"http.{parts.scheme}://{parts.netloc}/.extraheader"
            env["GIT_CONFIG_VALUE_0"] = f"Authorization: Basic {creds}"
        return env

    def _redact(self, text: str) -> str:
        if self.token:
            text = text.replace(self.token, "***")
        return text

    def _run(self, *args: str, cwd: Path | None = None, timeout: int = GIT_TIMEOUT) -> str:
        cmd = ["git", *args]
        try:
            res = subprocess.run(
                cmd,
                cwd=cwd if cwd is not None else self.path,
                env=self._env(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except FileNotFoundError as e:
            raise GitError("git executable not found in PATH") from e
        except subprocess.TimeoutExpired as e:
            raise GitError(f"'git {args[0]}' timed out after {timeout}s") from e
        if res.returncode != 0:
            msg = (res.stderr or res.stdout).strip()
            raise GitError(self._redact(f"'git {args[0]}' failed: {msg}"))
        return res.stdout.strip()

    # --- queries ---------------------------------------------------------
    def exists(self) -> bool:
        return (self.path / ".git").exists()

    def head(self) -> str | None:
        if not self.exists():
            return None
        try:
            return self._run("rev-parse", "HEAD")
        except GitError:
            return None

    def resolve_branch(self) -> str:
        if self.branch:
            return self.branch
        try:
            ref = self._run("symbolic-ref", "--short", "refs/remotes/origin/HEAD")
            return ref.split("/", 1)[1]
        except GitError:
            pass
        current = self._run("rev-parse", "--abbrev-ref", "HEAD")
        if current and current != "HEAD":
            return current
        raise GitError("cannot determine branch; set it explicitly with --branch")

    # --- actions ---------------------------------------------------------
    def clone(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".partial")
        rmtree(tmp)
        args = ["clone", "--single-branch", "--no-tags"]
        if self.branch:
            args += ["--branch", self.branch]
        args += ["--", self.url, str(tmp)]
        self.log.info("cloning %s", self.url)
        try:
            self._run(*args, cwd=self.path.parent, timeout=CLONE_TIMEOUT)
        except GitError:
            rmtree(tmp)
            raise
        if self.path.exists():
            rmtree(self.path)
        os.replace(tmp, self.path)

    def sync(self) -> bool:
        """Bring the working tree to the tip of the remote branch.

        Returns True if the checked-out commit changed (including a fresh clone).
        Local modifications to tracked files and untracked files are discarded;
        git-ignored files (.env, data dirs, ...) are kept.
        """
        if not self.exists():
            self.clone()
            return True

        if self._run("remote", "get-url", "origin") != self.url:
            self.log.info("remote URL changed, updating origin")
            self._run("remote", "set-url", "origin", self.url)

        branch = self.resolve_branch()
        self._run(
            "fetch", "--prune", "--no-tags", "origin",
            f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
            timeout=CLONE_TIMEOUT,
        )
        remote = self._run("rev-parse", f"refs/remotes/origin/{branch}")
        local = self.head()
        current_branch = self._run("rev-parse", "--abbrev-ref", "HEAD")
        dirty = bool(self._run("status", "--porcelain"))

        if local == remote and current_branch == branch and not dirty:
            return False

        # checkout -B + clean also handles force-pushes, detached HEAD and branch switches
        self._run("checkout", "--force", "-B", branch, f"refs/remotes/origin/{branch}")
        self._run("clean", "-fd")
        return local != remote
