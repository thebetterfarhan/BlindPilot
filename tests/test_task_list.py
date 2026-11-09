"""The agent's task list, as Claude Code and Codex report it.

A long task's plan was spoken one "Updating the task list" at a time, with no
way to ask where it stood. Task List (Ctrl+Shift+J) keeps the list itself:
each step and whether it is to do, in progress or done, as The Chat Place's
Tasks list does. Claude Code builds it with TaskCreate and TaskUpdate, or the
older TodoWrite; Codex sends its whole plan as ``turn/plan/updated``.
"""

from __future__ import annotations

import blindpilot_app as app
import pytest
from task_list import TaskList


def test_claude_task_tools_build_and_update_the_list():
    tasks = TaskList()
    tasks.apply_claude("TaskCreate", {"subject": "Write tests"}, "Task #1 created successfully")
    tasks.apply_claude("TaskCreate", {"subject": "Run suite"}, "Task #2 created successfully")

    finished = tasks.apply_claude("TaskUpdate", {"taskId": "1", "status": "completed"})
    tasks.apply_claude("TaskUpdate", {"taskId": "2", "status": "in_progress"})

    assert finished == ["Write tests"]
    assert tasks.lines() == ["1. Done: Write tests", "2. In progress: Run suite"]
    assert tasks.progress() == "Tasks: 1 of 2 done. In progress: Run suite."


def test_a_repeated_task_id_starts_a_new_list():
    tasks = TaskList()
    tasks.apply_claude("TaskCreate", {"subject": "Old"}, "Task #1 created successfully")
    tasks.apply_claude("TaskCreate", {"subject": "New"}, "Task #1 created successfully")

    assert tasks.lines() == ["1. To do: New"]


def test_a_deleted_or_unknown_task_changes_nothing_it_should_not():
    tasks = TaskList()
    tasks.apply_claude("TaskCreate", {"subject": "Keep"}, "Task #1 created successfully")
    tasks.apply_claude("TaskCreate", {"subject": "Drop"}, "Task #2 created successfully")

    tasks.apply_claude("TaskUpdate", {"taskId": "2", "status": "deleted"})
    tasks.apply_claude("TaskUpdate", {"taskId": "9", "status": "completed"})

    assert tasks.lines() == ["1. To do: Keep"]


def test_todowrite_replaces_the_list_and_reports_only_new_completions():
    tasks = TaskList()
    todos = [
        {"content": "Plan", "status": "completed"},
        {"content": "Build", "status": "in_progress"},
    ]
    assert tasks.apply_claude("TodoWrite", {"todos": todos}) == ["Plan"]

    todos[1]["status"] = "completed"
    assert tasks.apply_claude("TodoWrite", {"todos": todos}) == ["Build"]


def test_codex_plan_updates_use_its_own_status_names():
    tasks = TaskList()
    finished = tasks.apply_codex(
        [{"step": "Read code", "status": "completed"}, {"step": "Fix", "status": "inProgress"}]
    )

    assert finished == ["Read code"]
    assert tasks.lines() == ["1. Done: Read code", "2. In progress: Fix"]


def test_a_malformed_plan_is_ignored():
    tasks = TaskList()
    assert tasks.apply_codex("not a list") == []
    assert tasks.apply_codex([{"status": "completed"}, 3]) == []
    assert tasks.progress() == ""


def test_turn_status_includes_the_task_progress():
    stub = type("PanelStub", (), {})()
    spoken: list[str] = []
    stub._announce = lambda text, urgent=False: spoken.append(text)
    stub.task_list = TaskList()
    stub.task_list.apply_codex([{"step": "One", "status": "completed"}])

    app.SessionPanel.turn_status(stub)

    assert spoken == ["No turn is running. Tasks: 1 of 1 done."]


def test_a_reopened_claude_conversation_gets_its_task_list_back(tmp_path):
    import json

    from session_history import HistoryEntry, restore_tasks

    def block(role, **content):
        return {"type": role, "message": {"role": role, "content": [content]}}

    records = [
        block("assistant", type="tool_use", id="a", name="TaskCreate", input={"subject": "Plan"}),
        block("user", type="tool_result", tool_use_id="a", content="Task #1 created successfully"),
        block("assistant", type="tool_use", id="b", name="TaskCreate", input={"subject": "Ship"}),
        block("user", type="tool_result", tool_use_id="b", content="Task #2 created successfully"),
        block(
            "assistant",
            type="tool_use",
            id="c",
            name="TaskUpdate",
            input={"taskId": "1", "status": "completed"},
        ),
        block(
            "user", type="tool_result", tool_use_id="c", content=[{"type": "text", "text": "ok"}]
        ),
        # A failed call changes nothing.
        block(
            "assistant",
            type="tool_use",
            id="d",
            name="TaskUpdate",
            input={"taskId": "2", "status": "completed"},
        ),
        block("user", type="tool_result", tool_use_id="d", content="boom", is_error=True),
    ]
    path = tmp_path / "s.jsonl"
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")

    tasks = restore_tasks(HistoryEntry("claude", "s", "t", str(path), 0.0))

    assert tasks.lines() == ["1. Done: Plan", "2. To do: Ship"]
    assert restore_tasks(HistoryEntry("codex", "s", "t", str(path), 0.0)).lines() == []


def test_prepared_history_contains_turns_and_replayed_tasks(tmp_path):
    import json

    from session_history import HistoryEntry, prepare_history_open

    records = [
        {"type": "user", "message": {"role": "user", "content": "Ship this"}},
        {"type": "assistant", "message": {"role": "assistant", "content": "Done."}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "task-1",
                        "name": "TaskCreate",
                        "input": {"subject": "Verify the release"},
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
    path = tmp_path / "session.jsonl"
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")

    prepared = prepare_history_open(HistoryEntry("claude", "s", "t", str(path), 0.0))

    assert [(turn.prompt, turn.response) for turn in prepared.turns] == [("Ship this", "Done.")]
    assert prepared.tasks.lines() == ["1. To do: Verify the release"]
    assert prepared.task_warning == ""
    assert [(row.kind, row.response_number) for row in prepared.rows] == [
        ("you", 1),
        ("header", 1),
        ("prose", 1),
    ]


def test_prepared_history_rows_preserve_empty_turn_numbering_and_cancel(monkeypatch):
    import session_history

    turns = [
        session_history.HistoryTurn("", "First"),
        session_history.HistoryTurn("Second", "Answer"),
    ]
    parsed = 0
    checks = 0
    import markdown_rows

    real_markdown_parse = markdown_rows._MD.parse

    def parse_markdown(*args, **kwargs):
        nonlocal parsed
        parsed += 1
        return real_markdown_parse(*args, **kwargs)

    def cancelled():
        nonlocal checks
        checks += 1
        return checks >= 8

    monkeypatch.setitem(
        session_history._READERS, session_history.BACKEND_CLAUDE, lambda *_args, **_kwargs: turns
    )
    monkeypatch.setattr(markdown_rows._MD, "parse", parse_markdown)
    prepared = session_history.prepare_history_open(
        session_history.HistoryEntry("claude", "s", "t", "", 0), cancelled=lambda: False
    )
    assert [row.response_number for row in prepared.rows] == [1, 1, 2, 2, 2]
    parsed = 0
    with pytest.raises(session_history.HistoryOpenCancelled):
        session_history.prepare_history_open(
            session_history.HistoryEntry("claude", "s", "t", "", 0), cancelled=cancelled
        )
    assert parsed == 1


def test_jsonl_reader_stops_on_cancellation_and_closes_file(monkeypatch, tmp_path):
    import session_history
    from pathlib import Path

    path = tmp_path / "records.jsonl"
    path.write_text('{"n": 1}\n\nnot-json\n{"n": 2}\n', encoding="utf-8")
    opened = []
    read_count = 0
    real_open = Path.open

    class TrackedFile:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            opened.append(self)
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def __iter__(self):
            return self.handle

        def readline(self):
            nonlocal read_count
            read_count += 1
            return self.handle.readline()

        @property
        def closed(self):
            return self.handle.closed

    monkeypatch.setattr(
        Path, "open", lambda chosen, *a, **kw: TrackedFile(real_open(chosen, *a, **kw))
    )
    cancelled = False
    records = session_history._iter_jsonl(path, cancelled=lambda: cancelled)
    assert next(records) == {"n": 1}
    cancelled = True
    with pytest.raises(session_history.HistoryOpenCancelled):
        next(records)
    assert len(opened) == 1
    assert opened[0].closed
    assert read_count == 1


def test_pre_cancelled_history_read_has_distinct_outcome(tmp_path):
    import session_history

    path = tmp_path / "records.jsonl"
    path.write_text('{"type":"user","message":{"content":"Hi"}}\n', encoding="utf-8")
    entry = session_history.HistoryEntry("claude", "s", "title", str(path), 0.0)
    with pytest.raises(session_history.HistoryOpenCancelled):
        session_history.prepare_history_open(entry, cancelled=lambda: True)


def test_task_replay_cancellation_is_not_reported_as_optional_failure(tmp_path, monkeypatch):
    import json

    import session_history

    records = [
        {"type": "user", "message": {"role": "user", "content": "Question"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "task-1",
                        "name": "TaskCreate",
                        "input": {"subject": "Check"},
                    }
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "task-1", "content": "Task #1 created"}
                ]
            },
        },
        {"type": "assistant", "message": {"content": "Answer"}},
    ]
    path = tmp_path / "tasks.jsonl"
    path.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
    entry = session_history.HistoryEntry("claude", "s", "title", str(path), 0.0)
    cancelled = False
    original_apply = TaskList.apply_claude

    def apply_and_cancel(self, *args, **kwargs):
        nonlocal cancelled
        result = original_apply(self, *args, **kwargs)
        cancelled = True
        return result

    monkeypatch.setattr(TaskList, "apply_claude", apply_and_cancel)
    with pytest.raises(session_history.HistoryOpenCancelled):
        session_history.prepare_history_open(entry, cancelled=lambda: cancelled)


def test_cancelled_reader_result_is_not_empty_success(monkeypatch, tmp_path):
    import session_history

    stopped = False

    def reader(_entry, *, cancelled=None):
        nonlocal stopped
        stopped = True
        return []

    monkeypatch.setitem(session_history._READERS, "claude", reader)
    entry = session_history.HistoryEntry("claude", "s", "title", str(tmp_path / "none"), 0.0)
    with pytest.raises(session_history.HistoryOpenCancelled):
        session_history.load_turns(entry, cancelled=lambda: stopped)


def test_prepared_history_keeps_text_when_task_transcript_disappears(tmp_path, monkeypatch):
    import json

    import session_history

    path = tmp_path / "session.jsonl"
    path.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "Question"}}),
        encoding="utf-8",
    )
    real_reader = session_history._READERS["claude"]

    def load_then_remove(entry):
        turns = real_reader(entry)
        path.unlink()
        return turns

    monkeypatch.setitem(session_history._READERS, "claude", load_then_remove)
    prepared = session_history.prepare_history_open(
        session_history.HistoryEntry("claude", "s", "t", str(path), 0.0)
    )

    assert [turn.prompt for turn in prepared.turns] == ["Question"]
    assert prepared.tasks.lines() == []
    assert prepared.task_warning == "Task list could not be restored."


def test_prepared_history_keeps_text_when_task_replay_raises(tmp_path, monkeypatch):
    import json

    import session_history

    path = tmp_path / "session.jsonl"
    path.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "Question"}}),
        encoding="utf-8",
    )

    def fail_task_replay(*_args, **_kwargs):
        raise ValueError("unexpected task payload")

    monkeypatch.setattr(session_history, "restore_tasks", fail_task_replay)
    prepared = session_history.prepare_history_open(
        session_history.HistoryEntry("claude", "s", "t", str(path), 0.0)
    )

    assert [turn.prompt for turn in prepared.turns] == ["Question"]
    assert prepared.tasks.lines() == []
    assert prepared.task_warning == "Task list could not be restored."


def test_prepared_history_skips_task_replay_when_no_turns_loaded(tmp_path, monkeypatch):
    import session_history

    def fail_task_replay(*_args, **_kwargs):
        raise AssertionError("task replay should follow valid conversation text")

    monkeypatch.setattr(session_history, "restore_tasks", fail_task_replay)
    prepared = session_history.prepare_history_open(
        session_history.HistoryEntry("claude", "s", "t", str(tmp_path / "gone"), 0.0)
    )

    assert prepared.turns == []
    assert prepared.tasks.lines() == []
    assert prepared.task_warning == ""


def test_prepared_history_warns_when_task_transcript_grows_past_cap(tmp_path, monkeypatch):
    import json

    import session_history

    path = tmp_path / "session.jsonl"
    path.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "Question"}}),
        encoding="utf-8",
    )
    real_reader = session_history._READERS["claude"]

    def load_then_grow(entry):
        turns = real_reader(entry)
        with path.open("a", encoding="utf-8") as transcript:
            transcript.write(" " * 4)
        monkeypatch.setattr(session_history, "_MAX_TRANSCRIPT_BYTES", 1)
        return turns

    monkeypatch.setitem(session_history._READERS, "claude", load_then_grow)
    prepared = session_history.prepare_history_open(
        session_history.HistoryEntry("claude", "s", "t", str(path), 0.0)
    )

    assert [turn.prompt for turn in prepared.turns] == ["Question"]
    assert prepared.tasks.lines() == []
    assert prepared.task_warning == "Task list could not be restored."


def test_legacy_task_reader_keeps_silent_empty_behavior_for_missing_transcript(tmp_path):
    from session_history import HistoryEntry, restore_tasks

    assert restore_tasks(HistoryEntry("claude", "s", "t", str(tmp_path / "gone"), 0)).lines() == []


def test_prepared_history_treats_unsupported_backend_as_silent_empty(tmp_path):
    import json

    import session_history

    path = tmp_path / "conversation.jsonl"
    path.write_text(
        json.dumps({"type": "message", "message": {"role": "user", "content": "Question"}}),
        encoding="utf-8",
    )
    prepared = session_history.prepare_history_open(
        session_history.HistoryEntry("commandcode", "s", "t", str(path), 0)
    )

    assert prepared.turns
    assert prepared.tasks.lines() == []
    assert prepared.task_warning == ""


def test_a_reopened_copilot_conversation_gets_its_checklist_back(tmp_path):
    import json

    from session_history import HistoryEntry, restore_tasks

    event = {
        "type": "tool.execution_start",
        "data": {"toolName": "update_todo", "arguments": {"todos": "- [x] A\n- [ ] B"}},
    }
    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps(event), encoding="utf-8")

    tasks = restore_tasks(HistoryEntry("copilot", "s", "t", str(path), 0.0))

    assert tasks.progress() == "Tasks: 1 of 2 done."


def test_a_finished_step_is_announced_from_the_worker_event():
    stub = type("PanelStub", (), {})()
    spoken: list[str] = []
    stub._announce = lambda text, urgent=False: spoken.append(text)
    stub.task_list = TaskList()
    stub.task_list.apply_claude("TaskCreate", {"subject": "Ship"}, "Task #1 created")

    app.SessionPanel._on_tasks(
        stub, "claude", "TaskUpdate", {"taskId": "1", "status": "completed"}, ""
    )

    assert spoken == ["Task done: Ship"]


@pytest.mark.parametrize(
    ("saved_cwd", "available", "expected"),
    [
        ("/mnt/z/work", {"Z:\\work"}, "Z:\\work"),
        ("/missing-project", set(), ""),
        ("", set(), ""),
    ],
)
def test_prepared_history_resolves_saved_directory(saved_cwd, available, expected, monkeypatch):
    import session_history

    monkeypatch.setitem(
        session_history._READERS,
        "commandcode",
        lambda _entry: [session_history.HistoryTurn("Question", "Answer")],
    )
    real_isdir = session_history.os.path.isdir

    def probe(path):
        if path in {saved_cwd, "Z:\\work"}:
            return path in available
        return real_isdir(path)

    monkeypatch.setattr(session_history.os.path, "isdir", probe)
    entry = session_history.HistoryEntry("commandcode", "s", "Saved", "", 0, cwd=saved_cwd)

    prepared = session_history.prepare_history_open(entry)

    assert prepared.resolved_cwd == expected
    assert prepared.task_warning == ""


def test_cancellation_after_directory_probe_discards_prepared_opening(monkeypatch):
    import threading

    import session_history

    cancel = threading.Event()
    real_isdir = session_history.os.path.isdir
    monkeypatch.setitem(
        session_history._READERS,
        "commandcode",
        lambda _entry, **_kwargs: [session_history.HistoryTurn("Question", "Answer")],
    )

    def probe(_path):
        if _path == "/saved":
            cancel.set()
            return True
        return real_isdir(_path)

    monkeypatch.setattr(session_history.os.path, "isdir", probe)
    entry = session_history.HistoryEntry("commandcode", "s", "Saved", "", 0, cwd="/saved")

    with pytest.raises(session_history.HistoryOpenCancelled):
        session_history.prepare_history_open(entry, cancelled=cancel.is_set)
