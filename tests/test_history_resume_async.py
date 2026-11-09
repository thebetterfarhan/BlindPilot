# SPDX-License-Identifier: MIT
"""Opening a saved conversation never loads its transcript in the GUI callback."""

import threading
import time
import json

import blindpilot_app as app
import session_history
import wx
import pytest
from task_list import TaskList
from test_tab_strip_focus import _frame, _running_app


def _wait(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        wx.GetApp().Yield()
        if predicate():
            return
        time.sleep(0.01)
    assert predicate()


def test_resume_returns_before_slow_transcript_load_and_opens_when_ready(monkeypatch, tmp_path):
    with _running_app():
        frame = _frame(monkeypatch, tmp_path)
        try:
            entry = session_history.HistoryEntry("claude", "saved", "Saved chat", "", 1)
            started = threading.Event()
            release = threading.Event()

            def slow_load(chosen, *, cancelled=None):
                started.set()
                assert release.wait(3)
                tasks = TaskList()
                tasks.apply_codex([{"step": "Verify", "status": "in_progress"}])
                return session_history.HistoryOpenData(
                    [session_history.HistoryTurn("Question", "Answer")],
                    tasks,
                )

            monkeypatch.setattr(app, "prepare_history_open", slow_load)
            before = time.monotonic()
            result = frame._resume_history(entry)
            elapsed = time.monotonic() - before
            assert result is None
            assert elapsed < 0.05
            assert started.wait(1)
            assert frame.notebook.GetPageCount() == 1
            release.set()
            _wait(lambda: frame.notebook.GetPageCount() == 2)
            panel = frame.notebook.GetCurrentPage()
            assert panel._session_id == "saved"
            assert [(turn.prompt, turn.response) for turn in panel._turns] == [
                ("Question", "Answer")
            ]
            assert panel.task_list.lines() == ["1. In progress: Verify"]
        finally:
            if "release" in locals():
                release.set()
            progress = getattr(frame, "_history_open_progress", None)
            if progress:
                progress.Destroy()
            if getattr(frame, "_history_open_cancel", None) is not None:
                frame._history_open_cancel.set()
            frame.Destroy()


def test_cancelled_history_load_does_not_open_a_tab(monkeypatch, tmp_path):
    with _running_app():
        frame = _frame(monkeypatch, tmp_path)
        try:
            entry = session_history.HistoryEntry("claude", "saved", "Saved chat", "", 1)
            started = threading.Event()
            release = threading.Event()

            def slow_load(_entry, *, cancelled=None):
                started.set()
                assert release.wait(3)
                return session_history.HistoryOpenData(
                    [session_history.HistoryTurn("Question", "Answer")], TaskList()
                )

            monkeypatch.setattr(app, "prepare_history_open", slow_load)
            frame._resume_history(entry)
            _wait(started.is_set)
            progress = frame._history_open_progress
            assert progress is not None
            progress.Close()
            _wait(lambda: frame._history_open_progress is None)
            assert frame._history_open_cancel is None
            release.set()
            assert frame.notebook.GetPageCount() == 1
        finally:
            if "release" in locals():
                release.set()
            progress = getattr(frame, "_history_open_progress", None)
            if progress:
                progress.Destroy()
            if getattr(frame, "_history_open_cancel", None) is not None:
                frame._history_open_cancel.set()
            frame.Destroy()


def test_explicit_cancel_detaches_progress_before_blocked_worker_returns(monkeypatch, tmp_path):
    with _running_app():
        frame = _frame(monkeypatch, tmp_path)
        announcements = []
        monkeypatch.setattr(app, "announce", lambda text, urgent=False: announcements.append(text))
        started = threading.Event()
        release = threading.Event()
        late = threading.Event()
        entry = session_history.HistoryEntry("claude", "saved", "Saved chat", "", 1)

        def blocked(_entry, *, cancelled=None):
            started.set()
            assert release.wait(3)
            late.set()
            raise OSError("late reader failure")

        monkeypatch.setattr(app, "prepare_history_open", blocked)
        try:
            frame._resume_history(entry)
            _wait(started.is_set)
            serial = frame._history_open_serial
            progress = frame._history_open_progress
            assert progress is not None
            frame._cancel_history_open(serial)
            assert frame._history_open_progress is None
            assert frame._history_open_message is None
            assert frame._history_open_cancel_button is None
            assert frame._history_open_cancel is None
            assert announcements.count("Opening conversation cancelled") == 1
            assert frame.notebook.GetPageCount() == 1
            invalidated_serial = frame._history_open_serial
            frame._cancel_history_open(serial)
            assert frame._history_open_serial == invalidated_serial
            assert announcements.count("Opening conversation cancelled") == 1
            release.set()
            _wait(late.is_set)
            wx.GetApp().Yield()
            assert frame.notebook.GetPageCount() == 1
            assert announcements == [
                "Loading conversation Saved chat",
                "Opening conversation cancelled",
            ]
        finally:
            release.set()
            frame.Destroy()


@pytest.mark.parametrize("failure", [False, True])
def test_queued_completion_is_discarded_after_cancel(monkeypatch, tmp_path, failure):
    with _running_app():
        frame = _frame(monkeypatch, tmp_path)
        announcements = []
        queued = []
        done = threading.Event()
        opened = []
        real_call_after_alive = app._call_after_alive
        monkeypatch.setattr(app, "announce", lambda text, urgent=False: announcements.append(text))

        def hold_delivery(callback, *args):
            queued.append((callback, args))

        monkeypatch.setattr(app, "_call_after_alive", hold_delivery)
        entry = session_history.HistoryEntry("claude", "saved", "Saved chat", "", 1)

        def finish(_entry, *, cancelled=None):
            done.set()
            if failure:
                raise OSError("late read failure")
            return session_history.HistoryOpenData(
                [session_history.HistoryTurn("Question", "Answer")], TaskList()
            )

        monkeypatch.setattr(app, "prepare_history_open", finish)
        try:
            frame._resume_history(entry, on_open=opened.append)
            _wait(done.is_set)
            _wait(lambda: bool(queued))
            serial = frame._history_open_serial
            frame._cancel_history_open(serial)
            callback, args = queued.pop()
            callback(*args)
            assert frame.notebook.GetPageCount() == 1
            assert announcements.count("Opening conversation cancelled") == 1
            assert not any("late read failure" in text for text in announcements)
            assert opened == []
        finally:
            monkeypatch.setattr(app, "_call_after_alive", real_call_after_alive)
            frame.Destroy()


def test_owner_close_invalidates_queued_history_completion(monkeypatch, tmp_path):
    with _running_app():
        frame = _frame(monkeypatch, tmp_path)
        queued = []
        done = threading.Event()
        announcements = []
        opened = []
        monkeypatch.setattr(app, "announce", lambda text, urgent=False: announcements.append(text))
        monkeypatch.setattr(
            app, "_call_after_alive", lambda callback, *args: queued.append((callback, args))
        )
        monkeypatch.setattr(app, "_destroy_other_top_level_windows", lambda _owner: None)
        monkeypatch.setattr(app.backend_pool, "stop_all_held_processes", lambda: None)
        monkeypatch.setattr(wx, "CallAfter", lambda *_args, **_kwargs: None)
        entry = session_history.HistoryEntry("claude", "saved", "Saved chat", "", 1)
        prepared = session_history.HistoryOpenData(
            [session_history.HistoryTurn("Question", "Answer")], TaskList()
        )

        def finish(_entry, *, cancelled=None):
            done.set()
            return prepared

        monkeypatch.setattr(app, "prepare_history_open", finish)

        class CloseEvent:
            def Skip(self):
                pass

        try:
            frame._resume_history(entry, on_open=opened.append)
            _wait(done.is_set)
            _wait(lambda: bool(queued))
            serial = frame._history_open_serial
            frame._on_close(CloseEvent())
            assert frame._history_open_serial > serial
            callback, args = queued.pop()
            callback(*args)
            assert frame.notebook.GetPageCount() == 1
            assert opened == []
            assert announcements == ["Loading conversation Saved chat"]
        finally:
            frame.Destroy()


def test_queued_success_announcements_are_suppressed_by_newer_open(monkeypatch, tmp_path):
    with _running_app():
        frame = _frame(monkeypatch, tmp_path)
        announcements = []
        held_announcements = []
        monkeypatch.setattr(app, "announce", lambda text, urgent=False: announcements.append(text))
        real_call_after = wx.CallAfter

        def hold_opening_announcements(callback, *args):
            if getattr(callback, "__name__", "") == "_announce_history_open_if_current":
                held_announcements.append((callback, args))
            else:
                real_call_after(callback, *args)

        monkeypatch.setattr(wx, "CallAfter", hold_opening_announcements)
        entry = session_history.HistoryEntry("claude", "saved", "Saved chat", "", 1)
        prepared = session_history.HistoryOpenData(
            [session_history.HistoryTurn("Question", "Answer")],
            TaskList(),
            "Task list could not be restored.",
        )
        replacement_started = threading.Event()
        release_replacement = threading.Event()

        def block_replacement(chosen, *, cancelled=None):
            replacement_started.set()
            assert release_replacement.wait(3)
            return prepared

        monkeypatch.setattr(app, "prepare_history_open", block_replacement)
        try:
            frame._history_open_serial = 1
            cancel = threading.Event()
            frame._history_open_cancel = cancel
            frame._show_history_open_progress(1, entry.title)
            serial = 1
            frame._history_open_loaded(serial, cancel, entry, prepared, "", None)
            assert frame.notebook.GetPageCount() == 2
            assert len(held_announcements) == 2

            replacement = session_history.HistoryEntry("claude", "new", "New chat", "", 1)
            frame._resume_history(replacement)
            _wait(replacement_started.is_set)
            for callback, args in held_announcements:
                callback(*args)
            assert not any(text.startswith("Resumed Saved chat") for text in announcements)
            assert "Task list could not be restored." not in announcements
            assert frame._history_open_progress is not None
        finally:
            release_replacement.set()
            progress = getattr(frame, "_history_open_progress", None)
            if progress:
                progress.Destroy()
            if getattr(frame, "_history_open_cancel", None) is not None:
                frame._history_open_cancel.set()
            frame.Destroy()


def test_optional_task_warning_is_announced_once_and_text_still_opens(monkeypatch, tmp_path):
    with _running_app():
        frame = _frame(monkeypatch, tmp_path)
        announcements = []
        monkeypatch.setattr(app, "announce", lambda text, urgent=False: announcements.append(text))
        try:
            entry = session_history.HistoryEntry("claude", "saved", "Saved chat", "", 1)
            prepared = session_history.HistoryOpenData(
                [session_history.HistoryTurn("Question", "Answer")],
                TaskList(),
                "Task list could not be restored.",
            )
            monkeypatch.setattr(
                app, "prepare_history_open", lambda _entry, *, cancelled=None: prepared
            )

            frame._resume_history(entry)
            _wait(lambda: frame.notebook.GetPageCount() == 2)
            _wait(lambda: announcements.count("Task list could not be restored.") == 1)

            panel = frame.notebook.GetCurrentPage()
            assert [(turn.prompt, turn.response) for turn in panel._turns] == [
                ("Question", "Answer")
            ]
            assert panel.task_list.lines() == []
            assert announcements.count("Task list could not be restored.") == 1
        finally:
            progress = getattr(frame, "_history_open_progress", None)
            if progress:
                progress.Destroy()
            if getattr(frame, "_history_open_cancel", None) is not None:
                frame._history_open_cancel.set()
            frame.Destroy()


def test_real_turn_and_task_parsers_run_on_opening_worker(monkeypatch, tmp_path):
    with _running_app():
        frame = _frame(monkeypatch, tmp_path)
        try:
            path = tmp_path / "saved.jsonl"
            records = [
                {"type": "user", "message": {"role": "user", "content": "Question"}},
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "task-1",
                                "name": "TaskCreate",
                                "input": {"subject": "Check result"},
                            }
                        ],
                    },
                },
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "task-1",
                                "content": "Task #1 created successfully",
                            }
                        ],
                    },
                },
            ]
            path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
            entry = session_history.HistoryEntry("claude", "saved", "Saved chat", str(path), 1)
            gui_thread = threading.get_ident()
            parser_threads = []
            real_reader = session_history._READERS["claude"]
            real_restore_tasks = session_history.restore_tasks

            def track_turn_reader(chosen, *, cancelled=None):
                parser_threads.append(("turns", threading.get_ident()))
                return real_reader(chosen, cancelled=cancelled)

            def track_task_reader(chosen, *, strict_errors=False, cancelled=None):
                parser_threads.append(("tasks", threading.get_ident()))
                return real_restore_tasks(chosen, strict_errors=strict_errors, cancelled=cancelled)

            monkeypatch.setitem(session_history._READERS, "claude", track_turn_reader)
            monkeypatch.setattr(session_history, "restore_tasks", track_task_reader)

            frame._resume_history(entry)
            _wait(lambda: frame.notebook.GetPageCount() == 2)

            panel = frame.notebook.GetCurrentPage()
            assert [(turn.prompt, turn.response) for turn in panel._turns] == [("Question", "")]
            assert panel.task_list.lines() == ["1. To do: Check result"]
            assert {kind for kind, _thread_id in parser_threads} == {"turns", "tasks"}
            assert all(thread_id != gui_thread for _kind, thread_id in parser_threads)
        finally:
            progress = getattr(frame, "_history_open_progress", None)
            if progress:
                progress.Destroy()
            if getattr(frame, "_history_open_cancel", None) is not None:
                frame._history_open_cancel.set()
            frame.Destroy()
