# SPDX-License-Identifier: MIT
"""A note for the next launch when shutdown reached its cap (ADR 0005).

There is no window at quit to say anything in, so the words wait in a small
file until the next launch announces them once.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

_NOTICE_FILE = "shutdown-cap-hit.txt"
_WORDS = "Last time BlindPilot closed, one backend did not stop in time and was ended by force."


def leave_notice(folder: Path) -> None:
    """Record that the last shutdown hit its cap."""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / _NOTICE_FILE).write_text(_WORDS, encoding="utf-8")
    except OSError:
        pass


def take_notice(folder: Path) -> Optional[str]:
    """The words to announce, once; None when there is nothing to say."""
    path = folder / _NOTICE_FILE
    try:
        words = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        path.unlink()
    except OSError:
        pass
    return words or None
