"""Bounded shutdown (ADR 0005): the cap holds even when a backend hangs."""

from __future__ import annotations

import threading

import shutdown

FAST = {"join_seconds": 0.05, "drop_seconds": 0.05, "force_seconds": 0.05}


def _thread(work) -> threading.Thread:
    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    return thread


def test_clean_shutdown_never_forces():
    forced = []
    result = shutdown.run_shutdown(
        cancelling=[_thread(lambda: None)],
        drop=lambda: None,
        force_drop=lambda: forced.append(True),
        **FAST,
    )
    assert result == shutdown.CLEAN
    assert forced == []


def test_hung_cancellations_share_one_join_budget_then_drop_still_runs():
    release = threading.Event()
    dropped = threading.Event()
    hung = [_thread(release.wait) for _ in range(3)]

    def run():
        shutdown.run_shutdown(
            cancelling=hung,
            drop=dropped.set,
            force_drop=lambda: None,
            join_seconds=0.3,
            drop_seconds=0.05,
            force_seconds=0.05,
        )

    worker = _thread(run)
    # Three hung threads at 0.3 s each would be 0.9 s; one shared budget is 0.3.
    assert dropped.wait(timeout=0.6)
    worker.join(timeout=1)
    release.set()


def test_hung_drop_is_forced_at_the_cap_and_reports_it():
    release = threading.Event()
    forced = threading.Event()
    result = shutdown.run_shutdown(
        cancelling=[],
        drop=release.wait,
        force_drop=forced.set,
        **FAST,
    )
    release.set()
    assert result == shutdown.CAP_HIT
    assert forced.is_set()


def test_a_forced_drop_that_hangs_is_bounded_too():
    release = threading.Event()
    result_box: list[str] = []
    worker = _thread(
        lambda: result_box.append(
            shutdown.run_shutdown(
                cancelling=[],
                drop=release.wait,
                force_drop=release.wait,
                **FAST,
            )
        )
    )
    worker.join(timeout=1)
    release.set()
    assert result_box == [shutdown.CAP_HIT]
