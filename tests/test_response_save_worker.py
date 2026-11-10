"""Stop and response-save retries must stay responsive under SQLite contention."""

import sqlite3
import threading

import pytest
import wx

from accessible_ai.models import Message
from test_ponytail_chat_regressions import chat as chat_fixture, _begin, _wait

chat = chat_fixture


@pytest.mark.parametrize("regenerating", [False, True])
def test_stop_returns_while_database_is_locked_and_saves_after_unlock(chat, regenerating):
    panel, db, first, _ = chat
    cid = _begin(panel, first)
    if regenerating:
        previous = Message(conversation_id=cid, role="assistant", content="Previous answer")
        db.add_message(previous)
        panel._regeneration_history = db.list_messages(cid)
        panel.regenerating_message_id = previous.id
    panel._append_assistant_text(
        "A partial replacement" if regenerating else "Keep this partial answer"
    )
    lock = sqlite3.connect(db.path, check_same_thread=False)
    lock.execute("BEGIN EXCLUSIVE")
    unlocked = threading.Event()
    mutex = threading.Lock()

    def unlock():
        with mutex:
            if not unlocked.is_set():
                lock.rollback()
                unlocked.set()

    fallback = threading.Timer(0.5, unlock)
    fallback.start()
    try:
        panel.on_stop(None)
        assert not unlocked.is_set(), "Stop waited for the database lock"
        assert not panel.generating
        assert not panel.stop_button.IsEnabled()
        heartbeat = []
        wx.CallAfter(heartbeat.append, True)
        _wait(lambda: bool(heartbeat))
        assert not unlocked.is_set(), "GUI callbacks waited for the database lock"
    finally:
        unlock()
        fallback.cancel()
        fallback.join(2)
        lock.close()
    if not regenerating:
        _wait(lambda: panel._unsaved_response is None)
        assert (
            db.list_messages(cid)[-1].content
            == "Keep this partial answer\n\n[Response interrupted]"
        )
    else:
        assert db.list_messages(cid)[-1].content == "Previous answer"
        assert "Previous answer" in panel.transcript.GetValue()


def test_pending_save_keeps_actions_explicit_without_duplicate_writes(chat, monkeypatch):
    panel, db, first, _ = chat
    cid = _begin(panel, first)
    panel._append_assistant_text("An answer worth saving")
    entered, release = threading.Event(), threading.Event()
    writes = []
    original_add = db.add_message

    def add(message):
        writes.append(threading.get_ident())
        entered.set()
        release.wait(3)
        return original_add(message)

    monkeypatch.setattr(db, "add_message", add)
    try:
        panel.on_stop(None)
        assert entered.wait(1)
        panel.on_new_conversation(None)
        panel.on_new_conversation(None)
        assert panel.current_conversation_id == cid
        assert writes and all(thread != threading.get_ident() for thread in writes)
    finally:
        release.set()
        _wait(lambda: not panel._response_save_running)
    assert panel.current_conversation_id == cid
    panel.on_new_conversation(None)
    assert panel.current_conversation_id is None
    assert len(writes) == 1
    assert len([m for m in db.list_messages(cid) if m.role == "assistant"]) == 1


def test_save_finishes_after_shutdown_without_touching_the_closed_view(chat, monkeypatch):
    panel, db, first, _ = chat
    cid = _begin(panel, first)
    panel._append_assistant_text("Keep it after close")
    entered, release, committed = threading.Event(), threading.Event(), threading.Event()
    original_add = db.add_message

    def add(message):
        entered.set()
        release.wait(3)
        try:
            return original_add(message)
        finally:
            committed.set()

    monkeypatch.setattr(db, "add_message", add)
    try:
        panel.on_stop(None)
        assert entered.wait(1)
        pending = panel._unsaved_response
        panel.shutdown()
        speech = list(panel.audit_speech)
    finally:
        release.set()
        assert committed.wait(3)
        wx.GetApp().Yield()
    assert db.list_messages(cid)[-1].content.startswith("Keep it after close")
    assert pending.id is None  # the worker changes only its own snapshot
    assert panel.audit_speech == speech


def test_successful_regeneration_renders_the_saved_snapshot_without_gui_database_reads(
    chat, monkeypatch
):
    panel, db, first, _ = chat
    cid = _begin(panel, first)
    previous = Message(conversation_id=cid, role="assistant", content="Old answer")
    db.add_message(previous)
    panel._regeneration_history = db.list_messages(cid)
    panel.regenerating_message_id = previous.id
    panel._append_assistant_text("New answer")
    original_list = db.list_messages

    def list_messages(conversation_id):
        assert threading.get_ident() != gui_thread, "Completion queried SQLite on GUI"
        return original_list(conversation_id)

    gui_thread = threading.get_ident()
    with monkeypatch.context() as patch:
        patch.setattr(db, "list_messages", list_messages)
        panel._generation_finished(None)
        _wait(lambda: not panel._response_save_running)
    assert db.list_messages(cid)[-1].id == previous.id
    assert db.list_messages(cid)[-1].content == "New answer"
    assert "New answer" in panel.transcript.GetValue()
    assert "Old answer" not in panel.transcript.GetValue()


def test_save_completion_does_not_clear_a_draft_edited_after_a_blocked_action(chat, monkeypatch):
    panel, db, first, _ = chat
    _begin(panel, first)
    panel._append_assistant_text("Response")
    entered, release = threading.Event(), threading.Event()
    original_add = db.add_message

    def add(message):
        entered.set()
        release.wait(3)
        return original_add(message)

    monkeypatch.setattr(db, "add_message", add)
    try:
        panel.on_stop(None)
        assert entered.wait(1)
        panel.on_new_conversation(None)
        panel.message_input.SetValue("A newer draft")
    finally:
        release.set()
        _wait(lambda: not panel._response_save_running)
    assert panel.message_input.GetValue() == "A newer draft"
    assert panel.current_conversation_id is not None


def test_stream_canceled_before_reading_is_still_closed():
    import httpx
    from accessible_ai.providers.base import iter_sse_json

    response = httpx.Response(200, stream=httpx.ByteStream(b"data: [DONE]\n\n"))
    assert not response.is_closed
    cancel = threading.Event()
    cancel.set()
    assert list(iter_sse_json(response, cancel)) == []
    assert response.is_closed
