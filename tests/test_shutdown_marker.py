"""A new copy waits, up to the cap, for an old one that is shutting down."""

from __future__ import annotations

import shutdown_marker


class _Clock:
    """Time that moves only when someone sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.slept = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.slept += seconds


def test_no_marker_means_no_wait(tmp_path):
    clock = _Clock()
    assert shutdown_marker.wait_for_shutdown(tmp_path, 8.0, clock.monotonic, clock.sleep)
    assert clock.slept == 0.0


def test_waits_until_the_old_copy_finishes(tmp_path):
    shutdown_marker.begin(tmp_path)
    clock = _Clock()

    def sleep(seconds: float) -> None:
        clock.sleep(seconds)
        if clock.now >= 2.0:
            shutdown_marker.end(tmp_path)

    assert shutdown_marker.wait_for_shutdown(tmp_path, 8.0, clock.monotonic, sleep)
    assert 2.0 <= clock.slept < 8.0


def test_gives_up_at_the_cap_and_lets_the_new_copy_start(tmp_path):
    shutdown_marker.begin(tmp_path)
    clock = _Clock()

    finished = shutdown_marker.wait_for_shutdown(tmp_path, 8.0, clock.monotonic, clock.sleep)

    assert finished is False
    assert 8.0 <= clock.slept < 9.0


def test_ending_twice_is_harmless(tmp_path):
    shutdown_marker.begin(tmp_path)
    shutdown_marker.end(tmp_path)
    shutdown_marker.end(tmp_path)
    assert not shutdown_marker.is_shutting_down(tmp_path)
