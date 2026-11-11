"""The note left for the next launch when shutdown hit its cap (ADR 0005)."""

from __future__ import annotations

import shutdown_notice


def test_nothing_left_means_nothing_to_announce(tmp_path):
    assert shutdown_notice.take_notice(tmp_path) is None


def test_a_left_notice_is_taken_once_then_gone(tmp_path):
    shutdown_notice.leave_notice(tmp_path)

    first = shutdown_notice.take_notice(tmp_path)

    assert first == (
        "Last time BlindPilot closed, one backend did not stop in time and was ended by force."
    )
    assert shutdown_notice.take_notice(tmp_path) is None


def test_leaving_a_notice_in_a_missing_folder_creates_it(tmp_path):
    folder = tmp_path / "not" / "made" / "yet"
    shutdown_notice.leave_notice(folder)
    assert shutdown_notice.take_notice(folder) is not None
