"""The pool's forced drop (ADR 0005): owned processes only, and never twice."""

from __future__ import annotations

import threading

import backend_pool


class _Handle:
    def __init__(self) -> None:
        self.entered_stop = threading.Event()
        self.release = threading.Event()
        self.stops = 0
        self.forced = 0


def _held(handle: _Handle, *, can_force: bool = True, hangs: bool = True):
    def stop(h: _Handle) -> None:
        h.stops += 1
        h.entered_stop.set()
        if hangs:
            h.release.wait(timeout=5)

    def force(h: _Handle) -> None:
        h.forced += 1

    return backend_pool.HeldProcess(
        handle,
        backend_pool.Adapter(
            alive=lambda h: True,
            interrupt=lambda h, t: True,
            stop=stop,
            busy=lambda h: False,
            force_stop=force if can_force else None,
        ),
    )


def _keep(pool: backend_pool.BackendPool, held: backend_pool.HeldProcess) -> None:
    pool.keep(backend_pool.pool_key("codex"), held)


def _drop_in_background(pool: backend_pool.BackendPool) -> threading.Thread:
    thread = threading.Thread(target=pool.drop_all, daemon=True)
    thread.start()
    return thread


def test_a_process_whose_stop_hangs_gets_a_forced_drop():
    pool = backend_pool.BackendPool()
    handle = _Handle()
    _keep(pool, _held(handle))
    dropping = _drop_in_background(pool)
    assert handle.entered_stop.wait(timeout=2)

    pool.force_drop_owned()

    assert handle.forced == 1
    handle.release.set()
    dropping.join(timeout=2)


def test_a_process_with_no_way_to_verify_ownership_is_left_alone():
    pool = backend_pool.BackendPool()
    handle = _Handle()
    _keep(pool, _held(handle, can_force=False))
    dropping = _drop_in_background(pool)
    assert handle.entered_stop.wait(timeout=2)

    pool.force_drop_owned()

    assert handle.forced == 0
    handle.release.set()
    dropping.join(timeout=2)


def test_the_atexit_sweep_after_a_finished_shutdown_stops_nothing_again():
    pool = backend_pool.BackendPool()
    handle = _Handle()
    _keep(pool, _held(handle, hangs=False))

    pool.drop_all()
    pool.drop_all()

    assert handle.stops == 1
