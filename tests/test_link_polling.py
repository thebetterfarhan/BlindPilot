"""Session-link handoffs are read away from the GUI, with bounded reads."""

import os
import threading
import time
from pathlib import Path

import pytest
import wx

import blindpilot_app as app
from test_tab_strip_focus import _frame, _running_app


def _wait(predicate):
    deadline = time.monotonic() + 3
    while not predicate() and time.monotonic() < deadline:
        wx.GetApp().Yield()
        time.sleep(0.01)
    assert predicate()


def test_poll_reads_on_worker_and_dispatches_once_in_order_on_gui(monkeypatch, tmp_path):
    folder = tmp_path / "links"
    folder.mkdir()
    links = ["blindpilot://open/claude/first", "blindpilot://open/codex/second"]
    for index, link in enumerate(links):
        path = folder / f"{index}.link"
        path.write_text(link, encoding="utf-8")
        os.utime(path, (index + 1, index + 1))
    gui_thread = threading.get_ident()
    reads, opened = [], []

    def links_dir():
        reads.append(threading.get_ident())
        return folder

    monkeypatch.setattr(app, "_links_dir", links_dir)
    with _running_app():
        frame = _frame(monkeypatch, tmp_path)
        try:
            monkeypatch.setattr(
                frame,
                "open_session_link",
                lambda link: opened.append((threading.get_ident(), link)),
            )
            frame._take_links()
            _wait(lambda: len(opened) == 2)
            assert reads and all(thread != gui_thread for thread in reads)
            assert opened == [(gui_thread, link) for link in links]
            assert not list(folder.glob("*.link"))
            frame._take_links()
            _wait(lambda: not frame._links_loading)
            assert len(opened) == 2
        finally:
            frame.Destroy()


def test_handoff_read_stops_at_the_character_limit(monkeypatch, tmp_path):
    path = tmp_path / "large.link"
    path.write_text("λ" * 3000, encoding="utf-8")
    monkeypatch.setattr(app, "_links_dir", lambda: tmp_path)
    original_open = Path.open
    sizes = []

    class Reader:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def read(self, size=-1):
            sizes.append(size)
            return self.handle.read(size)

    def open_path(path, *args, **kwargs):
        handle = original_open(path, *args, **kwargs)
        return Reader(handle) if path.suffix == ".link" else handle

    monkeypatch.setattr(Path, "open", open_path)
    assert app.take_handed_links() == ["λ" * 2000]
    assert sizes == [2000]
    assert not path.exists()


@pytest.mark.parametrize("outcome", ["success", "failure", "closed"])
def test_slow_poll_does_not_overlap_or_block_gui_and_recovers(monkeypatch, tmp_path, outcome):
    folder = tmp_path / "links"
    folder.mkdir()
    (folder / "pending.link").write_text("blindpilot://open/claude/pending", encoding="utf-8")
    monkeypatch.setattr(app, "_links_dir", lambda: folder)
    original_poll = app.take_handed_links
    original_call_after = app._call_after_alive
    started, release, queued = threading.Event(), threading.Event(), threading.Event()
    polls, opened = [], []
    with _running_app():
        frame = _frame(monkeypatch, tmp_path)

        def poll():
            polls.append(threading.get_ident())
            if len(polls) == 1:
                started.set()
                release.wait(3)
                if outcome == "failure":
                    raise OSError("temporarily unavailable")
            return original_poll()

        def call_after(callback, *args):
            original_call_after(callback, *args)
            if (
                getattr(callback, "__self__", None) is frame
                and callback.__name__ == "_handed_links_loaded"
            ):
                queued.set()

        try:
            monkeypatch.setattr(app, "take_handed_links", poll)
            monkeypatch.setattr(app, "_call_after_alive", call_after)
            monkeypatch.setattr(frame, "open_session_link", opened.append)
            frame._take_links()
            assert started.wait(1)
            heartbeat = []
            wx.CallAfter(heartbeat.append, True)
            _wait(lambda: bool(heartbeat))
            frame._take_links()
            frame._take_links()
            assert len(polls) == 1
            if outcome == "closed":
                frame.Destroy()
                wx.GetApp().Yield()
            release.set()
            assert queued.wait(3)
            wx.GetApp().Yield()
            if outcome == "closed":
                assert opened == []
            else:
                _wait(lambda: not frame._links_loading)
                assert opened == (
                    [] if outcome == "failure" else ["blindpilot://open/claude/pending"]
                )
                frame._take_links()
                _wait(lambda: not frame._links_loading)
                assert len(polls) == 2
                assert opened == ["blindpilot://open/claude/pending"]
        finally:
            release.set()
            if started.is_set():
                assert queued.wait(3)
                wx.GetApp().Yield()
            if frame:
                frame.Destroy()


def test_invalid_utf8_handoff_does_not_block_other_links(monkeypatch, tmp_path):
    (tmp_path / "invalid.link").write_bytes(b"\xff")
    (tmp_path / "valid.link").write_text("blindpilot://open/codex/valid", encoding="utf-8")
    monkeypatch.setattr(app, "_links_dir", lambda: tmp_path)
    assert app.take_handed_links() == ["blindpilot://open/codex/valid"]
    assert not (tmp_path / "valid.link").exists()
