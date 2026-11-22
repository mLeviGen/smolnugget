"""Guess how to run a freshly cloned repository."""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path

PY_ENTRYPOINTS = ("main.py", "bot.py", "app.py", "run.py", "start.py", "server.py", "__main__.py")


@dataclass
class Detected:
    command: list[str]
    reason: str
    venv: bool = False
    setup: list[str] = field(default_factory=list)


def detect(repo: Path) -> Detected | None:
    repo = Path(repo)

    procfile = repo / "Procfile"
    if procfile.is_file():
        entries = {}
        for line in procfile.read_text(encoding="utf-8", errors="replace").splitlines():
            key, sep, value = line.partition(":")
            if sep and value.strip() and not key.startswith("#"):
                entries[key.strip()] = value.strip()
        for key in ("worker", "web", *entries):
            if key in entries:
                cmd = shlex.split(entries[key])
                return Detected(cmd, f"Procfile '{key}'", venv=cmd[0] in ("python", "python3"))

    package = repo / "package.json"
    if package.is_file():
        try:
            scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts", {})
        except ValueError:
            scripts = {}
        install = "npm ci" if (repo / "package-lock.json").exists() else "npm install"
        if "start" in scripts:
            return Detected(["npm", "start"], "package.json 'start' script", setup=[install])
        if (repo / "index.js").exists():
            return Detected(["node", "index.js"], "index.js", setup=[install])

    for name in PY_ENTRYPOINTS:
        if (repo / name).is_file():
            return Detected(["python", name], name, venv=True)
    py_files = [p.name for p in repo.glob("*.py") if p.name not in ("setup.py", "conftest.py")]
    if len(py_files) == 1:
        return Detected(["python", py_files[0]], "the only .py file", venv=True)
    return None
