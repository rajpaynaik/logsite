"""Terminal colours for the CLI: on in a terminal, off when piped, ``NO_COLOR`` respected
(https://no-color.org), ``FORCE_COLOR`` or ``--color always`` to force them."""

from __future__ import annotations

import os
from functools import cache
from pathlib import Path
from typing import TextIO

_CODES = {
    "bold": "1",
    "dim": "2",
    "gray": "90",
    "red": "31",
    "green": "32",
    "yellow": "33",
    "blue": "34",
    "magenta": "35",
    "cyan": "36",
    "white": "97",
    "bold_white": "1;97",
    "bold_red": "1;31",
    "bold_cyan": "1;36",
    "bold_blue": "1;34",
    "bold_green": "1;32",
    "bold_yellow": "1;33",
    "bold_magenta": "1;35",
    "code": "92",  # bright green: source lines
    "yours": "1;4;36",  # bold underlined cyan: the project's own code
    "badge_red": "1;97;41",  # white on red: new
    "badge_yellow": "1;30;43",  # black on yellow: spiking
    "badge_gray": "30;47",  # black on light gray: steady
}


class Palette:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __getattr__(self, name: str):
        code = _CODES.get(name)
        if code is None:
            raise AttributeError(name)

        def paint(text: str) -> str:
            return f"\033[{code}m{text}\033[0m" if self.enabled and text else text

        return paint


def palette(mode: str, stream: TextIO) -> Palette:
    """``mode`` is ``auto``, ``always`` or ``never``."""
    if mode == "always":
        return Palette(True)
    if mode == "never" or os.environ.get("NO_COLOR"):
        return Palette(False)
    if os.environ.get("FORCE_COLOR"):
        return Palette(True)
    isatty = getattr(stream, "isatty", None)
    return Palette(bool(isatty and isatty()) and os.environ.get("TERM") != "dumb")


@cache
def _lines(path: Path) -> list[str] | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None


def source_line(root: Path, path: str, line: int) -> str | None:
    """The code at ``path:line`` in the project, stripped, or None."""
    lines = _lines(Path(root) / path)
    if not lines or not 0 < line <= len(lines):
        return None
    return lines[line - 1].strip() or None
