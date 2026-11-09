"""Slow history stores must not block the dialog or replace a newer scope."""

import threading
import time

import pytest
import wx

import blindpilot_app as app
import session_history
from session_history import HistoryEntry


def _wait(predicate):
    deadline = time.monotonic() + 3
    while not predicate() and time.monotonic() < deadline:
        wx.GetApp().Yield()
        time.sleep(0.01)
    assert predicate()


def _patch_load_turns(monkeypatch, loader):
    # blindpilot_app imported this by value from session_history.
    monkeypatch.setattr(app, "load_turns", loader)
    monkeypatch.setattr(session_history, "load_turns", loader)


def _capture_preview_dispatch(monkeypatch, dialog, completion_events):
    callbacks = []
    dispatch = app._call_after_alive

    def capture(callback, *args):
        if (
            getattr(callback, "__self__", None) is dialog
            and getattr(callback, "__func__", None) is app.HistoryDialog._show_preview
        ):
            callbacks.append((callback, args))
            completion_events[len(callbacks) - 1].set()
        else:
            dispatch(callback, *args)

    monkeypatch.setattr(app, "_call_after_alive", capture)
    return callbacks


def test_slow_history_opens_immediately_and_ignores_the_previous_scope(frame, monkeypatch):
    release = threading.Event()
    finished = threading.Event()
    threads = []
    folder = HistoryEntry("claude", "folder", "Old scope", "", 1)
    all_folders = HistoryEntry("claude", "all", "All folders", "", 2)

    def scan(backend, cwd):
        threads.append(threading.get_ident())
        if cwd:
            release.wait(3)
            finished.set()
            return [folder]
        return [all_folders]

    monkeypatch.setattr(app, "list_history", scan)
    monkeypatch.setattr(app, "announce", lambda *_a, **_k: None)
    dialog = app.HistoryDialog(frame, "claude", ".")
    delivered = []
    loaded = dialog._history_loaded

    def receive(*args):
        loaded(*args)
        delivered.append(args[0])

    monkeypatch.setattr(dialog, "_history_loaded", receive)
    try:
        assert not finished.is_set(), "constructing the dialog waited for the scan"
        assert not dialog.FindWindow(wx.ID_OK).IsEnabled()
        assert "Loading" in dialog.summary.GetLabel()
        dialog.filter_box.ChangeValue("All")
        dialog._refresh()
        assert "Loading" in dialog.summary.GetLabel()
        dialog.scope_picker.SetSelection(1)
        dialog._reload()
        _wait(lambda: dialog._shown == [all_folders])
        assert dialog.FindWindow(wx.ID_OK).IsEnabled()
        release.set()
        _wait(lambda: len(delivered) == 2)
        assert dialog._shown == [all_folders]
        assert all(t != threading.get_ident() for t in threads)
    finally:
        release.set()
        dialog._preview_timer.Stop()
        dialog.Destroy()


def test_history_loading_failure_is_spoken_and_leaves_no_openable_row(frame, monkeypatch):
    def scan(*_a):
        raise OSError("History unavailable")

    spoken = []
    monkeypatch.setattr(app, "list_history", scan)
    monkeypatch.setattr(app, "announce", lambda text, **_k: spoken.append(text))
    dialog = app.HistoryDialog(frame, "claude", ".")
    try:
        _wait(lambda: any("History unavailable" in text for text in spoken))
        assert dialog.list_box.GetCount() == 0
        assert not dialog.FindWindow(wx.ID_OK).IsEnabled()
        assert "History unavailable" in dialog.summary.GetLabel()
    finally:
        dialog._preview_timer.Stop()
        dialog.Destroy()


def test_closing_during_loading_discards_the_result(frame, monkeypatch):
    release = threading.Event()

    def scan(*_a):
        release.wait(3)
        return [HistoryEntry("claude", "late", "Late result", "", 1)]

    spoken = []
    monkeypatch.setattr(app, "list_history", scan)
    monkeypatch.setattr(app, "announce", lambda text, **_k: spoken.append(text))
    dialog = app.HistoryDialog(frame, "claude", ".")
    delivered = threading.Event()
    loaded = dialog._history_loaded

    def receive(*args):
        loaded(*args)
        delivered.set()

    monkeypatch.setattr(dialog, "_history_loaded", receive)
    dialog.Destroy()
    _wait(lambda: not dialog)
    release.set()
    _wait(delivered.is_set)
    # A callback on a destroyed wx object must not update controls or speak.
    assert spoken == []


def test_a_queued_result_is_ignored_while_native_destruction_is_deferred(frame, monkeypatch):
    release = threading.Event()

    def scan(*_a):
        release.wait(3)
        return []

    monkeypatch.setattr(app, "list_history", scan)
    spoken = []
    monkeypatch.setattr(app, "announce", lambda text, **_k: spoken.append(text))
    dialog = app.HistoryDialog(frame, "claude", ".")
    serial = dialog._load_serial
    try:
        with monkeypatch.context() as patch:
            # Cocoa can keep the C++ dialog alive until the next event-loop pass.
            patch.setattr(wx.Dialog, "Destroy", lambda _self: True)
            dialog.Destroy()
            dialog._history_loaded(serial, [], "")
        assert spoken == []
        assert dialog._loading
    finally:
        release.set()
        dialog.Destroy()


@pytest.mark.parametrize("old_result", ["success", "error"])
def test_preview_aba_ignores_late_old_result_and_cancels_obsolete_reads(
    frame, monkeypatch, old_result
):
    monkeypatch.setattr(app, "list_history", lambda *_a: [])
    dialog = app.HistoryDialog(frame, "claude", ".")
    release_old = threading.Event()
    release_middle = threading.Event()
    started = [threading.Event() for _ in range(3)]
    queued = [threading.Event() for _ in range(3)]
    reads = []
    launched = 0
    try:
        _wait(lambda: not dialog._loading)
        first = HistoryEntry("claude", "a", "A", "", 1)
        middle = HistoryEntry("claude", "b", "B", "", 2)
        dialog._shown = [first, middle]
        dialog.list_box.Set(["A", "B"])
        monkeypatch.setattr(app, "_last_exchange", lambda text: text)

        def load(entry, *, cancelled=None):
            index = len(reads)
            reads.append((entry, cancelled or (lambda: False)))
            started[index].set()
            if index == 0:
                assert release_old.wait(3)
                if old_result == "error":
                    raise OSError("old A failed")
                return "old A"
            if index == 1:
                assert release_middle.wait(3)
                return "B"
            return "new A"

        callbacks = _capture_preview_dispatch(monkeypatch, dialog, queued)
        _patch_load_turns(monkeypatch, load)

        dialog.list_box.SetSelection(0)
        dialog._schedule_preview()
        dialog._preview_timer.Stop()
        dialog._load_preview()
        launched += 1
        assert started[0].wait(3)

        dialog.list_box.SetSelection(1)
        dialog._schedule_preview()
        assert reads[0][1]()
        dialog._load_preview()
        launched += 1
        assert started[1].wait(3)

        dialog.list_box.SetSelection(0)
        dialog._schedule_preview()
        assert reads[1][1]()
        dialog._load_preview()
        launched += 1
        assert started[2].wait(3)
        assert queued[0].wait(3)
        callback, args = callbacks[0]
        callback(*args)
        assert dialog.preview.GetValue() == "new A"

        release_old.set()
        assert queued[1].wait(3)
        release_middle.set()
        assert queued[2].wait(3)
        for callback, args in callbacks[1:]:
            callback(*args)
        assert dialog.preview.GetValue() == "new A"
        assert not reads[2][1]()
    finally:
        release_old.set()
        release_middle.set()
        for index in range(launched):
            assert queued[index].wait(3)
        dialog.Destroy()


def test_preview_completion_after_selection_clear_is_ignored(frame, monkeypatch):
    monkeypatch.setattr(app, "list_history", lambda *_a: [])
    dialog = app.HistoryDialog(frame, "claude", ".")
    queued = threading.Event()
    callbacks = []
    try:
        _wait(lambda: not dialog._loading)
        entry = HistoryEntry("claude", "a", "A", "", 1)
        dialog._shown = [entry]
        dialog.list_box.Set(["A"])
        dialog.list_box.SetSelection(0)
        monkeypatch.setattr(app, "_last_exchange", lambda text: text)
        _patch_load_turns(monkeypatch, lambda *_a, **_k: "late A")

        callbacks = _capture_preview_dispatch(monkeypatch, dialog, [queued])
        dialog._schedule_preview()
        dialog._preview_timer.Stop()
        dialog._load_preview()
        assert queued.wait(3)
        dialog._shown = []
        dialog.list_box.Set([])
        dialog._schedule_preview()
        callback, args = callbacks[0]
        callback(*args)
        assert dialog.preview.GetValue() == ""
    finally:
        dialog.Destroy()


def test_preview_completion_queued_before_destroy_is_ignored(frame, monkeypatch):
    monkeypatch.setattr(app, "list_history", lambda *_a: [])
    dialog = app.HistoryDialog(frame, "claude", ".")
    queued = threading.Event()
    callbacks = []
    try:
        _wait(lambda: not dialog._loading)
        entry = HistoryEntry("claude", "a", "A", "", 1)
        dialog._shown = [entry]
        dialog.list_box.Set(["A"])
        dialog.list_box.SetSelection(0)
        monkeypatch.setattr(app, "_last_exchange", lambda text: text)
        _patch_load_turns(monkeypatch, lambda *_a, **_k: "late A")

        callbacks = _capture_preview_dispatch(monkeypatch, dialog, [queued])
        dialog._schedule_preview()
        dialog._preview_timer.Stop()
        dialog._load_preview()
        assert queued.wait(3)
        with monkeypatch.context() as patch:
            patch.setattr(wx.Dialog, "Destroy", lambda _self: True)
            dialog.Destroy()
            callback, args = callbacks[0]
            callback(*args)
            assert dialog.preview.GetValue() == "Reading…"
    finally:
        dialog.Destroy()


@pytest.mark.parametrize(
    ("turns", "expected"),
    [("current A", "current A"), (OSError("current failure"), "Could not read this conversation.")],
)
def test_current_preview_success_and_error_are_unchanged(frame, monkeypatch, turns, expected):
    monkeypatch.setattr(app, "list_history", lambda *_a: [])
    dialog = app.HistoryDialog(frame, "claude", ".")
    queued = threading.Event()
    callbacks = []
    try:
        _wait(lambda: not dialog._loading)
        entry = HistoryEntry("claude", "a", "A", "", 1)
        dialog._shown = [entry]
        dialog.list_box.Set(["A"])
        dialog.list_box.SetSelection(0)
        monkeypatch.setattr(app, "_last_exchange", lambda text: text)

        def load(*_a, **_k):
            if isinstance(turns, Exception):
                raise turns
            return turns

        callbacks = _capture_preview_dispatch(monkeypatch, dialog, [queued])
        _patch_load_turns(monkeypatch, load)
        dialog._schedule_preview()
        dialog._preview_timer.Stop()
        dialog._load_preview()
        assert queued.wait(3)
        callback, args = callbacks[0]
        callback(*args)
        assert dialog.preview.GetValue() == expected
    finally:
        dialog.Destroy()


def test_preview_reader_cancellation_is_silent(frame, monkeypatch):
    monkeypatch.setattr(app, "list_history", lambda *_a: [])
    dialog = app.HistoryDialog(frame, "claude", ".")
    finished = threading.Event()
    queued = []
    try:
        _wait(lambda: not dialog._loading)
        entry = HistoryEntry("claude", "a", "A", "", 1)
        dialog._shown = [entry]
        dialog.list_box.Set(["A"])
        dialog.list_box.SetSelection(0)

        def cancel_read(*_a, **_k):
            try:
                raise app.HistoryOpenCancelled("superseded")
            finally:
                finished.set()

        _patch_load_turns(monkeypatch, cancel_read)
        _capture_preview_dispatch(monkeypatch, dialog, [])
        dialog._schedule_preview()
        dialog._preview_timer.Stop()
        dialog._load_preview()
        assert finished.wait(3)
        assert queued == []
        assert dialog.preview.GetValue() == "Reading…"
    finally:
        dialog.Destroy()
