"""Preparing a saved conversation off the GUI thread, and stopping when told to."""

from __future__ import annotations

import json
from pathlib import Path

import session_history
from session_history import HistoryEntry, prepare_conversation


def _block(role: str, **content) -> dict:
    return {"type": role, "message": {"role": role, "content": [content]}}


def _text(role: str, text: str) -> dict:
    return _block(role, type="text", text=text)


def _write(path: Path, records: list[dict]) -> HistoryEntry:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return HistoryEntry("claude", "s", "Demo", str(path), 0.0)


def _with_tasks() -> list[dict]:
    return [
        {"type": "user", "message": {"role": "user", "content": "Plan it"}},
        _text("assistant", "Planned."),
        _block("assistant", type="tool_use", id="a", name="TaskCreate", input={"subject": "Ship"}),
        _block("user", type="tool_result", tool_use_id="a", content="Task #1 created successfully"),
    ]


def test_a_prepared_conversation_carries_its_turns_and_its_task_list(tmp_path):
    entry = _write(tmp_path / "s.jsonl", _with_tasks())

    prepared = prepare_conversation(entry)

    assert [(t.prompt, t.response) for t in prepared.turns] == [("Plan it", "Planned.")]
    assert prepared.tasks.lines() == ["1. To do: Ship"]
    assert prepared.task_warning == ""


class _CancelAfter:
    """A cancel signal that turns on after being asked ``limit`` times."""

    def __init__(self, limit: int):
        self.limit = limit
        self.asked = 0

    def is_set(self) -> bool:
        self.asked += 1
        return self.asked > self.limit


def test_a_cancelled_read_stops_at_a_checkpoint_and_yields_nothing(tmp_path):
    records = [
        {"type": "user", "message": {"role": "user", "content": f"q{i}"}} for i in range(2000)
    ]
    entry = _write(tmp_path / "s.jsonl", records)
    cancel = _CancelAfter(3)

    assert prepare_conversation(entry, cancel) is None
    assert cancel.asked < 20, "the read kept consuming records after cancellation"


class _RunsOnCheck:
    """A cancel signal that never cancels, but runs ``action`` on its second check:
    after the text read has begun and before the task list is read."""

    def __init__(self, action):
        self.action = action
        self.asked = 0

    def is_set(self) -> bool:
        self.asked += 1
        if self.asked == 2:
            self.action()
        return False


def test_text_still_opens_with_one_warning_when_the_task_list_is_over_the_size_cap(
    tmp_path, monkeypatch
):
    entry = _write(tmp_path / "s.jsonl", _with_tasks())

    def tighten_the_cap():
        monkeypatch.setattr(session_history, "_MAX_TRANSCRIPT_BYTES", 1)

    prepared = prepare_conversation(entry, _RunsOnCheck(tighten_the_cap))

    assert [(t.prompt, t.response) for t in prepared.turns] == [("Plan it", "Planned.")]
    assert prepared.tasks.lines() == []
    assert prepared.task_warning != ""


def test_no_warning_for_an_unsupported_backend_or_a_genuinely_empty_task_list(tmp_path):
    entry = _write(tmp_path / "s.jsonl", _with_tasks()[:2])

    empty = prepare_conversation(entry)
    unsupported = prepare_conversation(
        HistoryEntry("codex", "s", "Demo", entry.path, 0.0),
    )

    assert empty.tasks.lines() == [] and empty.task_warning == ""
    assert unsupported.tasks.lines() == [] and unsupported.task_warning == ""
