# SPDX-License-Identifier: MIT
"""Bounded shutdown, as ADR 0005 decides it.

Runs on the shutdown worker, never on a GUI callback.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Iterable

CLEAN = "clean"
CAP_HIT = "cap_hit"


def run_shutdown(
    cancelling: Iterable[threading.Thread],
    drop: Callable[[], None],
    force_drop: Callable[[], None],
    *,
    join_seconds: float = 3.0,
    drop_seconds: float = 5.0,
    force_seconds: float = 2.0,
) -> str:
    """Join cancellations, drop held processes, force a drop at the cap."""
    deadline = time.monotonic() + join_seconds
    for thread in cancelling:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    dropping = threading.Thread(target=drop, name="shutdown-drop", daemon=True)
    dropping.start()
    dropping.join(timeout=drop_seconds)
    if dropping.is_alive():
        forcing = threading.Thread(target=force_drop, name="shutdown-force", daemon=True)
        forcing.start()
        forcing.join(timeout=force_seconds)
        return CAP_HIT
    return CLEAN
