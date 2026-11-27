"""Tiny ANSI color helpers. Plain text when piped, with NO_COLOR, or after --no-color."""

from __future__ import annotations

import os
import re
import sys
from typing import TextIO

RESET = "\x1b[0m"
CODES = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33"}

_disabled = False
_ansi_ready = False


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def clean(text: str) -> str:
    """Drop control characters (keeps tabs): text from app output must not reach the
    terminal as escape sequences."""
    return _CONTROL.sub("", text)


def set_disabled(flag: bool) -> None:
    """Force colors off for this process (`--no-color`)."""
    global _disabled
    _disabled = flag


def _enable_windows_ansi() -> None:
    global _ansi_ready
    if not _ansi_ready:
        _ansi_ready = True
        if sys.platform == "win32":
            os.system("")  # makes the console interpret ANSI escapes


def enabled(file: TextIO | None = None) -> bool:
    if _disabled or os.environ.get("NO_COLOR"):
        return False
    stream = file or sys.stdout
    try:
        tty = stream.isatty()
    except (AttributeError, ValueError):
        return False
    if tty:
        _enable_windows_ansi()
    return tty


def paint(style: str, text: str, file: TextIO | None = None) -> str:
    if not enabled(file):
        return text
    return f"\x1b[{CODES[style]}m{text}{RESET}"


def green(text: str, file: TextIO | None = None) -> str:
    return paint("green", text, file)


def yellow(text: str, file: TextIO | None = None) -> str:
    return paint("yellow", text, file)


def red(text: str, file: TextIO | None = None) -> str:
    return paint("red", text, file)


def dim(text: str, file: TextIO | None = None) -> str:
    return paint("dim", text, file)


def bold(text: str, file: TextIO | None = None) -> str:
    return paint("bold", text, file)
