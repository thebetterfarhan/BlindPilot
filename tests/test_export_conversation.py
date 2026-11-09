"""Saving a conversation to a file.

Copy Whole Conversation put the transcript on the clipboard and nowhere else,
so keeping a long conversation meant pasting it into an editor and saving it
from there. Export Conversation (Ctrl+E) writes the same text straight to a
Markdown or text file, as The Chat Place's Export does.
"""

from __future__ import annotations

import threading
import time

import pytest
import wx

import blindpilot_app as app
from markdown_rows import Row
import markdown_rows
from test_tab_strip_focus import _frame, _running_app


class _Dialog:
    """`wx.FileDialog` as `export_conversation` uses it: a context manager."""

    def __init__(self, path, accepted=True, kind=0):
        self._path = path
        self._accepted = accepted
        self._kind = kind

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def ShowModal(self):
        return app.wx.ID_OK if self._accepted else app.wx.ID_CANCEL

    def GetPath(self):
        return str(self._path)

    def GetFilterIndex(self):
        return self._kind


@pytest.fixture
def panel(monkeypatch, tmp_path):
    with _running_app():
        frame = _frame(monkeypatch, tmp_path)
        panel = frame.notebook.GetCurrentPage()
        panel._rows = [
            Row(kind="you", label="You: hi", payload="hi", response_number=1),
            Row(kind="header", label="Response 1", payload="Hello.", response_number=1),
            Row(kind="prose", label="Hello.", payload="Hello.", response_number=1),
        ]
        panel.tab_title = "Fix: the build?"
        panel.cwd = ""
        panel.spoken = []
        panel._announce = lambda text, urgent=False: panel.spoken.append(text)
        try:
            yield panel
        finally:
            if frame:
                frame.Destroy()


def _wait(predicate):
    deadline = time.monotonic() + 3
    while not predicate() and time.monotonic() < deadline:
        wx.GetApp().Yield()
        time.sleep(0.01)
    assert predicate()


def _export(panel, monkeypatch, path, accepted=True, kind=0, wait=True):
    offered: dict = {}

    def dialog(*args, **kwargs):
        offered.update(kwargs)
        return _Dialog(path, accepted, kind)

    monkeypatch.setattr(app.wx, "FileDialog", dialog)
    app.SessionPanel.export_conversation(panel)
    if wait:
        _wait(lambda: not getattr(panel, "_export_running", False))
    return offered


def test_the_whole_conversation_is_written_and_the_file_named(panel, monkeypatch, tmp_path):
    target = tmp_path / "out.md"

    offered = _export(panel, monkeypatch, target)

    text = target.read_text(encoding="utf-8")
    assert "Response 1" in text and "Hello." in text
    assert panel.spoken == ["Exported conversation to out.md"]
    # A title with characters no file name may hold still makes a name.
    assert offered["defaultFile"].startswith("Fix the build ")
    assert offered["defaultFile"].endswith(".md")


def test_markdown_gives_each_response_a_heading(panel, monkeypatch, tmp_path):
    target = tmp_path / "out.md"

    _export(panel, monkeypatch, target)

    assert "## Response 1" in target.read_text(encoding="utf-8")


def test_a_web_page_is_rendered_and_never_runs_html(panel, monkeypatch, tmp_path):
    panel._rows.append(
        Row(kind="prose", label="x", payload="<script>alert(1)</script>", response_number=1)
    )
    target = tmp_path / "out.html"

    _export(panel, monkeypatch, target)

    page = target.read_text(encoding="utf-8")
    assert "<h2>Response 1</h2>" in page
    assert "<script>" not in page and "&lt;script&gt;" in page


def test_plain_text_is_the_clipboard_text(panel, monkeypatch, tmp_path):
    target = tmp_path / "out.txt"

    _export(panel, monkeypatch, target)

    assert target.read_text(encoding="utf-8") == app.reassemble_all(panel._rows) + "\n"


def test_a_name_without_an_extension_takes_the_chosen_type(panel, monkeypatch, tmp_path):
    """GTK and macOS can return the name as typed; the Save as type decides."""
    _export(panel, monkeypatch, tmp_path / "notes", kind=1)

    assert "<h2>Response 1</h2>" in (tmp_path / "notes.html").read_text(encoding="utf-8")


def test_an_appended_name_that_exists_is_not_replaced_unasked(panel, monkeypatch, tmp_path):
    """The dialog's overwrite check only saw the name as typed."""
    (tmp_path / "notes.md").write_text("keep me", encoding="utf-8")
    monkeypatch.setattr(app.wx, "MessageBox", lambda *a, **k: app.wx.NO)

    _export(panel, monkeypatch, tmp_path / "notes", kind=0)

    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "keep me"


def test_code_holding_a_fence_gets_a_longer_one(panel, monkeypatch, tmp_path):
    panel._rows.append(Row(kind="code", label="code", payload="```\ninner\n```", response_number=1))
    target = tmp_path / "out.md"

    _export(panel, monkeypatch, target)

    assert "````\n```\ninner\n```\n````" in target.read_text(encoding="utf-8")


def test_only_real_response_headers_become_headings(panel, monkeypatch, tmp_path):
    panel._rows.append(
        Row(kind="code", label="code", payload="Response 7", response_number=1, language="text")
    )
    target = tmp_path / "out.md"

    _export(panel, monkeypatch, target)

    text = target.read_text(encoding="utf-8")
    assert "## Response 1" in text
    assert "## Response 7" not in text and "Response 7" in text


def test_nothing_to_export_says_so(panel, monkeypatch, tmp_path):
    panel._rows = []

    _export(panel, monkeypatch, tmp_path / "never.md")

    assert not (tmp_path / "never.md").exists()
    assert panel.spoken == ["Error: Nothing to export yet"]


def test_cancelling_writes_nothing(panel, monkeypatch, tmp_path):
    _export(panel, monkeypatch, tmp_path / "never.md", accepted=False)

    assert not (tmp_path / "never.md").exists()
    assert panel.spoken == []


def test_html_export_renders_off_the_gui_thread(panel, monkeypatch, tmp_path):
    gui_thread = threading.get_ident()
    threads = []
    original_render = markdown_rows._MD_PAGE.render

    def render(text):
        threads.append(threading.get_ident())
        return original_render(text)

    monkeypatch.setattr(markdown_rows._MD_PAGE, "render", render)
    target = tmp_path / "thread.html"
    _export(panel, monkeypatch, target)
    assert threads and all(thread != gui_thread for thread in threads)
    assert "Hello." in target.read_text(encoding="utf-8")


def test_slow_export_keeps_gui_live_and_saves_the_requested_snapshot(panel, monkeypatch, tmp_path):
    started, release = threading.Event(), threading.Event()
    original_render = markdown_rows._MD_PAGE.render
    original_replace = app.os.replace
    gui_thread = threading.get_ident()
    writes, announcements = [], []
    target = tmp_path / "snapshot.html"
    target.write_text("Previous export", encoding="utf-8")

    def render(text):
        started.set()
        release.wait(3)
        return original_render(text)

    def replace(source, destination):
        if destination == target:
            writes.append(threading.get_ident())
        return original_replace(source, destination)

    monkeypatch.setattr(markdown_rows._MD_PAGE, "render", render)
    monkeypatch.setattr(app.os, "replace", replace)
    panel._announce = lambda text: announcements.append((threading.get_ident(), text))
    try:
        _export(panel, monkeypatch, target, wait=False)
        assert started.wait(1)
        heartbeat = []
        wx.CallAfter(heartbeat.append, True)
        _wait(lambda: bool(heartbeat))
        assert target.read_text(encoding="utf-8") == "Previous export"
        panel._rows[-1].payload = "Changed after exporting"
        panel.clear_conversation()
        _export(panel, monkeypatch, tmp_path / "second.md", wait=False)
        assert not (tmp_path / "second.md").exists()
    finally:
        release.set()
        _wait(lambda: not panel._export_running)
    assert writes and all(thread != gui_thread for thread in writes)
    assert all(thread == gui_thread for thread, _text in announcements)
    assert any(text == "An export is already in progress" for _thread, text in announcements)
    page = target.read_text(encoding="utf-8")
    assert "Hello." in page and "Changed after exporting" not in page


@pytest.mark.parametrize("failure", [OSError("disk unavailable"), RuntimeError()])
def test_failed_export_preserves_existing_file_and_allows_retry(
    panel, monkeypatch, tmp_path, failure
):
    target = tmp_path / "existing.md"
    target.write_text("Keep this export", encoding="utf-8")
    original_replace = app.os.replace

    def replace(source, destination):
        if destination == target:
            raise failure
        return original_replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(app.os, "replace", replace)
        _export(panel, patch, target)
    assert target.read_text(encoding="utf-8") == "Keep this export"
    assert not list(tmp_path.glob(".existing.md.*.tmp"))
    assert len(panel.spoken) == 1 and panel.spoken[0].startswith("Error: Could not export")
    _export(panel, monkeypatch, target)
    assert "Hello." in target.read_text(encoding="utf-8")
    assert panel.spoken[-1] == "Exported conversation to existing.md"


def test_accepted_export_finishes_after_tab_closes_without_late_speech(
    panel, monkeypatch, tmp_path
):
    started, release, queued = threading.Event(), threading.Event(), threading.Event()
    original_render = markdown_rows._MD_PAGE.render
    original_call_after = app._call_after_alive
    target = tmp_path / "closed.html"

    def render(text):
        started.set()
        release.wait(3)
        return original_render(text)

    def call_after(callback, *args):
        original_call_after(callback, *args)
        if getattr(callback, "__self__", None) is panel and callback.__name__ == "_export_finished":
            queued.set()

    monkeypatch.setattr(markdown_rows._MD_PAGE, "render", render)
    monkeypatch.setattr(app, "_call_after_alive", call_after)
    try:
        _export(panel, monkeypatch, target, wait=False)
        assert started.wait(1)
        panel.GetTopLevelParent().Destroy()
        wx.GetApp().Yield()
    finally:
        release.set()
        if started.is_set():
            assert queued.wait(3)
            wx.GetApp().Yield()
    assert "Hello." in target.read_text(encoding="utf-8")
    assert panel.spoken == []
