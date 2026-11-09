# SPDX-License-Identifier: MIT
"""Opening a saved conversation never loads its transcript in the GUI callback."""

import threading
import time
import json

import blindpilot_app as app
import session_history
import wx
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

            def slow_load(chosen):
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

            def slow_load(_entry):
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
            assert frame._history_open_cancel.is_set()
            release.set()
            _wait(lambda: frame._history_open_cancel is None)
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
            monkeypatch.setattr(app, "prepare_history_open", lambda _entry: prepared)

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

            def track_turn_reader(chosen):
                parser_threads.append(("turns", threading.get_ident()))
                return real_reader(chosen)

            def track_task_reader(chosen, *, strict_errors=False):
                parser_threads.append(("tasks", threading.get_ident()))
                return real_restore_tasks(chosen, strict_errors=strict_errors)

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
