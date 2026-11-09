"""Formatted reading prepares HTML without holding the GUI event loop."""

import threading
import time

import pytest
import wx

import blindpilot_app as app
import markdown_rows
from test_tab_strip_focus import _frame, _running_app


def _wait(predicate):
    deadline = time.monotonic() + 3
    while not predicate() and time.monotonic() < deadline:
        wx.GetApp().Yield()
        time.sleep(0.01)
    assert predicate()


@pytest.mark.parametrize("web_available", [True, False])
def test_formatted_reading_renders_in_worker_and_opens_on_gui(monkeypatch, tmp_path, web_available):
    with _running_app():
        frame = _frame(monkeypatch, tmp_path)
        try:
            panel = frame.notebook.GetCurrentPage()
            panel._rows = markdown_rows.parse_response("# Heading\n\nA **response**.", 1)
            panel._refresh_list()
            gui_thread = threading.get_ident()
            render_threads, opened, modal, destroyed = [], [], [], []
            original_render = markdown_rows._MD_PAGE.render

            def render(text):
                render_threads.append(threading.get_ident())
                return original_render(text)

            class View:
                def ShowModal(self):
                    modal.append(threading.get_ident())

                def Destroy(self):
                    destroyed.append(True)

            def formatted(parent, html, title):
                opened.append((threading.get_ident(), html, title))
                return View() if web_available else None

            plain = []
            monkeypatch.setattr(markdown_rows._MD_PAGE, "render", render)
            monkeypatch.setattr(app.FormattedView, "open", staticmethod(formatted))
            monkeypatch.setattr(
                app, "ReadView", lambda parent, text, title: plain.append(text) or View()
            )
            panel._read_formatted(panel._rows[0])
            _wait(lambda: bool(destroyed))
            assert render_threads and all(thread != gui_thread for thread in render_threads)
            assert opened[0][0] == gui_thread and modal == [gui_thread]
            assert "<h1>Heading</h1>" in opened[0][1]
            assert opened[0][2] == "Response 1"
            assert plain == ([] if web_available else ["# Heading\n\nA **response**."])
        finally:
            frame.Destroy()


@pytest.mark.parametrize("change", ["clear", "close", "switch", "newer"])
def test_late_formatted_results_do_not_reopen_old_reading(monkeypatch, tmp_path, change):
    with _running_app():
        frame = _frame(monkeypatch, tmp_path)
        started, release, queued = threading.Event(), threading.Event(), threading.Event()
        panel = frame.notebook.GetCurrentPage()
        original_call_after = app._call_after_alive
        original_render = markdown_rows._MD_PAGE.render
        opened = []

        def render(text):
            if "Old response" in text:
                started.set()
                release.wait(3)
            return original_render(text)

        def call_after(callback, *args):
            original_call_after(callback, *args)
            if (
                getattr(callback, "__self__", None) is panel
                and callback.__name__ == "_formatted_response_loaded"
            ):
                if "Old response" in args[2]:
                    queued.set()

        class View:
            def ShowModal(self):
                pass

            def Destroy(self):
                pass

        def formatted(parent, page, title):
            opened.append(page)
            return View()

        try:
            monkeypatch.setattr(markdown_rows._MD_PAGE, "render", render)
            monkeypatch.setattr(app, "_call_after_alive", call_after)
            monkeypatch.setattr(app.FormattedView, "open", staticmethod(formatted))
            panel._rows = markdown_rows.parse_response("Old response", 1)
            panel._read_formatted(panel._rows[0])
            assert started.wait(1)
            # The event loop remains usable while the renderer is blocked.
            heartbeat = []
            wx.CallAfter(heartbeat.append, True)
            _wait(lambda: bool(heartbeat))
            if change == "clear":
                panel.clear_conversation()
            elif change == "close":
                frame.Destroy()
                wx.GetApp().Yield()
            elif change == "switch":
                frame._add_session(str(tmp_path))
            else:
                panel._rows.extend(markdown_rows.parse_response("New response", 2))
                panel._read_formatted(panel._rows[-1])
                _wait(lambda: bool(opened))
            release.set()
            assert queued.wait(3)
            wx.GetApp().Yield()
            assert len(opened) == (1 if change == "newer" else 0)
            if opened:
                assert "New response" in opened[0] and "Old response" not in opened[0]
        finally:
            release.set()
            if started.is_set():
                assert queued.wait(3)
                wx.GetApp().Yield()
            if frame:
                frame.Destroy()


def test_rendering_failure_keeps_plain_text_readable(monkeypatch, tmp_path):
    with _running_app():
        frame = _frame(monkeypatch, tmp_path)
        try:
            panel = frame.notebook.GetCurrentPage()
            panel._rows = markdown_rows.parse_response("Still readable", 1)
            plain = []

            def render(_text):
                raise ValueError("invalid renderer state")

            class View:
                def __init__(self, parent, text, title):
                    plain.append((text, title))

                def ShowModal(self):
                    pass

                def Destroy(self):
                    pass

            monkeypatch.setattr(markdown_rows._MD_PAGE, "render", render)
            monkeypatch.setattr(app, "ReadView", View)
            panel._read_formatted(panel._rows[0])
            _wait(lambda: bool(plain))
            assert plain == [("Still readable", "Response 1")]
        finally:
            frame.Destroy()
