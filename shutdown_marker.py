# SPDX-License-Identifier: MIT
"""Tells a newly launched copy that an older one is still shutting down.

The marker is written when shutdown begins and removed when it ends. A copy
that crashed mid-shutdown leaves it behind, which costs the next launch at most
the cap, never more (ADR 0005).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable

_MARKER_FILE = "shutting-down"
_POLL_SECONDS = 0.1


def begin(folder: Path) -> None:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / _MARKER_FILE).write_text("", encoding="utf-8")
    except OSError:
        pass


def end(folder: Path) -> None:
    try:
        (folder / _MARKER_FILE).unlink()
    except OSError:
        pass


def is_shutting_down(folder: Path) -> bool:
    return (folder / _MARKER_FILE).exists()


def wait_for_shutdown(
    folder: Path,
    cap_seconds: float,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Wait for an older copy to finish; False when the cap ran out first."""
    deadline = monotonic() + cap_seconds
    while is_shutting_down(folder):
        if monotonic() >= deadline:
            return False
        sleep(_POLL_SECONDS)
    return True
