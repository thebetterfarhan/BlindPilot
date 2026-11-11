"""The next launch says once that the last shutdown hit its cap (ADR 0005)."""

from __future__ import annotations

import blindpilot_app as app
import shutdown_notice
from blocking_contract import gui_thread_may_not_block


class _Frame:
    def __init__(self) -> None:
        self.said: list[str] = []

    def _announce(self, text: str, urgent: bool = False) -> None:
        self.said.append(text)


def test_a_left_notice_is_announced_once(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "blindpilot_data_dir", lambda: tmp_path)
    shutdown_notice.leave_notice(tmp_path)
    frame = _Frame()

    with gui_thread_may_not_block():
        app._announce_shutdown_notice(frame)
        app._announce_shutdown_notice(frame)

    assert len(frame.said) == 1
    assert "ended by force" in frame.said[0]


def test_no_notice_means_silence(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "blindpilot_data_dir", lambda: tmp_path)
    frame = _Frame()

    with gui_thread_may_not_block():
        app._announce_shutdown_notice(frame)

    assert frame.said == []
