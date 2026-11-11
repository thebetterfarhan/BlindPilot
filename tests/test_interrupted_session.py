"""A reopened session says when it ended mid-tool-call (ADR 0005)."""

from __future__ import annotations

import session_history


def _use(call_id: str) -> dict:
    return {"message": {"content": [{"type": "tool_use", "id": call_id, "name": "Bash"}]}}


def _result(call_id: str) -> dict:
    return {"message": {"content": [{"type": "tool_result", "tool_use_id": call_id}]}}


def _text(words: str) -> dict:
    return {"message": {"content": [{"type": "text", "text": words}]}}


def test_a_call_with_no_result_at_the_end_is_interrupted():
    assert session_history.ends_mid_tool_call([_text("hi"), _use("a")])


def test_a_call_that_got_its_result_is_not_interrupted():
    assert not session_history.ends_mid_tool_call([_use("a"), _result("a"), _text("done")])


def test_only_the_calls_still_open_at_the_end_count():
    records = [_use("a"), _use("b"), _result("a")]
    assert session_history.ends_mid_tool_call(records)
    assert not session_history.ends_mid_tool_call([*records, _result("b")])


def test_an_empty_or_plain_session_is_not_interrupted():
    assert not session_history.ends_mid_tool_call([])
    assert not session_history.ends_mid_tool_call([_text("hello")])
