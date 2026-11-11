# SPDX-License-Identifier: MIT
"""Bounded shutdown, as ADR 0005 decides it.

Runs on the shutdown worker, never on a GUI callback.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable, Iterable

import shutdown_marker
import shutdown_notice

CLEAN = "clean"
CAP_HIT = "cap_hit"

# The whole shutdown cap: three seconds to join, five to drop (ADR 0005).
CAP_SECONDS = 8.0


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


def finish(
    cancelling: Iterable[threading.Thread],
    drop: Callable[[], None],
    force_drop: Callable[[], None],
    *,
    data_dir: Path,
    end_process: Callable[[], None],
    join_seconds: float = 3.0,
    drop_seconds: float = 5.0,
    force_seconds: float = 2.0,
) -> None:
    """Run shutdown for a quitting window, and tell whoever comes next.

    The marker holds a relaunch back for the length of the shutdown. When the
    cap was hit, a note waits for the next launch and the process ends, since a
    hung stop would otherwise keep it alive with no window.
    """
    shutdown_marker.begin(data_dir)
    try:
        result = run_shutdown(
            cancelling,
            drop,
            force_drop,
            join_seconds=join_seconds,
            drop_seconds=drop_seconds,
            force_seconds=force_seconds,
        )
        if result == CAP_HIT:
            shutdown_notice.leave_notice(data_dir)
    finally:
        shutdown_marker.end(data_dir)
    if result == CAP_HIT:
        end_process()
