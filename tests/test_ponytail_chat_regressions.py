"""Chat must keep routing, saved answers and request ownership safe."""

import sqlite3
import threading
import time

import pytest
import wx

from accessible_ai.models import Message, StreamEvent
from accessible_ai.providers.base import ProviderError
from test_chat_conversations import _panel, _seeded, _started


def _wait(predicate):
    deadline = time.monotonic() + 3
    while not predicate() and time.monotonic() < deadline:
        wx.GetApp().Yield()
        time.sleep(0.01)
    assert predicate()


@pytest.fixture
def chat(frame, tmp_path, monkeypatch):
    db, first, other = _seeded(tmp_path)
    panel = _panel(frame, db)
    panel.audit_status, panel.audit_speech, panel.audit_errors = [], [], []
    panel._set_status = panel.audit_status.append
    panel._speak = panel.audit_speech.append
    monkeypatch.setattr(
        wx, "MessageBox", lambda text, *a, **k: panel.audit_errors.append(text) or wx.ID_OK
    )
    try:
        yield panel, db, first, other
    finally:
        panel.shutdown()


def _begin(panel, first):
    cid = _started(panel, first, "a/one", "Private question")
    panel._render_conversation(add_assistant_heading=True)
    panel._append_history_entry(Message(conversation_id=cid, role="assistant", content=""))
    panel.generating = True
    panel.generation_cancel = threading.Event()
    return cid


def test_missing_account_requires_explicit_selection_even_after_reload(chat, monkeypatch):
    panel, db, first, other = chat
    cid = _started(panel, other, "z/one", "Private old context")
    db.delete_account(other.id)
    panel.reload_accounts()
    panel.open_conversation(cid)
    panel.reload_accounts()
    requests = []
    monkeypatch.setattr(panel, "_start_generation", lambda *a, **k: requests.append(a))
    panel.message_input.SetValue("Continue")
    panel.on_send(None)
    assert panel.selected_account() is None
    assert requests == []
    assert [m.content for m in db.list_messages(cid)] == ["Private old context"]
    panel.account_choice.SetSelection(0)
    panel.load_cached_models()
    panel.on_send(None)
    assert requests[0][0].id == first.id


@pytest.mark.parametrize("interrupted", ["cancel", "error"])
def test_partial_normal_answer_is_saved_with_an_interruption_marker(chat, interrupted):
    panel, db, first, _ = chat
    cid = _begin(panel, first)
    panel._append_assistant_text("A useful partial answer")
    if interrupted == "cancel":
        panel.generation_cancel.set()
    panel._generation_finished(ProviderError("lost connection") if interrupted == "error" else None)
    _wait(lambda: not panel._response_save_running)
    saved = db.list_messages(cid)
    assert saved[-1].role == "assistant"
    assert saved[-1].content.startswith("A useful partial answer")
    assert "interrupted" in saved[-1].content.lower()
    assert not panel.generating


def test_storage_failure_releases_controls_and_retries_before_leaving(chat, monkeypatch):
    panel, db, first, _ = chat
    cid = _begin(panel, first)
    panel._append_assistant_text("Keep this completed answer")
    with monkeypatch.context() as patch:

        def failed_commit(message):
            message.id = 999
            raise sqlite3.OperationalError("disk full")

        patch.setattr(db, "add_message", failed_commit)
        panel._generation_finished(None)
        _wait(lambda: not panel._response_save_running)
        assert not panel.generating
        assert panel.assistant_buffer == "Keep this completed answer"
        panel.on_new_conversation(None)
        _wait(lambda: not panel._response_save_running)
        assert panel.current_conversation_id == cid
    panel.on_new_conversation(None)
    _wait(lambda: not panel._response_save_running)
    panel.on_new_conversation(None)
    _wait(lambda: panel.current_conversation_id is None)
    assert panel.current_conversation_id is None
    assert db.list_messages(cid)[-1].content == "Keep this completed answer"
    assert panel._unsaved_response is None


def test_provider_error_details_stay_in_ui_and_out_of_logs(chat, caplog):
    panel, _db, first, _ = chat
    _begin(panel, first)

    class Service:
        def generate(self, *args):
            raise ProviderError("AUDIT_PRIVATE_PROMPT_MARKER")
            yield

    panel.generation_service = Service()
    panel._start_generation(first, "a/one", panel._generation_settings(first, "a/one"), False)
    _wait(lambda: not panel.generating)
    assert any("AUDIT_PRIVATE_PROMPT_MARKER" in text for text in panel.audit_errors)
    assert "AUDIT_PRIVATE_PROMPT_MARKER" not in caplog.text


def test_stop_returns_promptly_and_late_worker_cannot_finish_a_new_turn(chat):
    panel, _db, first, _ = chat
    _begin(panel, first)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    class Service:
        def generate(self, account, settings, cancel):
            entered.set()
            try:
                release.wait(3)
                yield StreamEvent("text", text="stale answer")
            finally:
                finished.set()

    panel.generation_service = Service()
    panel._start_generation(first, "a/one", panel._generation_settings(first, "a/one"), False)
    try:
        assert entered.wait(1)
        old_cancel = panel.generation_cancel
        panel.on_stop(None)
        assert old_cancel.is_set()
        assert not panel.generating
        newer_cancel = threading.Event()
        panel.generation_cancel = newer_cancel
        panel.generating = True
        panel.assistant_buffer = "new answer"
        release.set()
        assert finished.wait(3)
        wx.GetApp().Yield()
        assert panel.generating and panel.generation_cancel is newer_cancel
        assert panel.assistant_buffer == "new answer"
    finally:
        release.set()
        if entered.is_set():
            assert finished.wait(3)
        wx.GetApp().Yield()


@pytest.mark.parametrize("change", ["none", "new", "close"])
def test_attachment_reads_are_bounded_off_gui_and_late_results_are_discarded(
    chat, monkeypatch, tmp_path, change
):
    from pathlib import Path
    from accessible_ai.ui import chat_panel as module

    panel, _db, _first, _other = chat
    target = tmp_path / "large.txt"
    target.write_bytes(b"before")
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    reads = []
    original_open = Path.open

    class Reader:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def read(self, size=-1):
            reads.append((threading.get_ident(), size))
            entered.set()
            release.wait(3)
            return self.handle.read(size)

    def open_path(path, *args, **kwargs):
        handle = original_open(path, *args, **kwargs)
        return Reader(handle) if path == target and args == ("rb",) else handle

    class Dialog:
        def __init__(self, *args, **kwargs):
            pass

        def ShowModal(self):
            return wx.ID_OK

        def GetPaths(self):
            return [str(target)]

        def Destroy(self):
            pass

    original_read = module._read_attachments

    def read_attachments(*args):
        try:
            return original_read(*args)
        finally:
            finished.set()

    monkeypatch.setattr(wx, "FileDialog", Dialog)
    monkeypatch.setattr(Path, "open", open_path)
    monkeypatch.setattr(module, "_read_attachments", read_attachments)
    try:
        panel.on_add_files(None)
        assert entered.wait(1)
        heartbeat = []
        wx.CallAfter(heartbeat.append, True)
        _wait(lambda: bool(heartbeat))
        assert not panel.send_button.IsEnabled()
        if change == "new":
            panel.on_new_conversation(None)
        elif change == "close":
            panel.shutdown()
        release.set()
        assert finished.wait(3)
        if change == "none":
            _wait(lambda: not panel._attachments_loading)
            assert [a.data for a in panel.pending_attachments] == [b"before"]
        else:
            wx.GetApp().Yield()
            assert panel.pending_attachments == []
        assert reads == [(reads[0][0], module.MAX_TOTAL_ATTACHMENT_BYTES + 1)]
        assert reads[0][0] != threading.get_ident()
    finally:
        release.set()
        if entered.is_set():
            assert finished.wait(3)
        wx.GetApp().Yield()


def test_attachment_growth_between_stat_and_read_cannot_exceed_limit(chat, monkeypatch, tmp_path):
    from pathlib import Path
    from accessible_ai.ui import chat_panel as module

    panel, _db, _first, _other = chat
    target = tmp_path / "growing.txt"
    target.write_bytes(b"small")
    original_stat = Path.stat

    def stat(path, *args, **kwargs):
        result = original_stat(path, *args, **kwargs)
        if path == target:
            target.write_bytes(b"0123456789abcdef")
        return result

    monkeypatch.setattr(module, "MAX_TOTAL_ATTACHMENT_BYTES", 10)
    monkeypatch.setattr(Path, "stat", stat)
    attachments, errors = module._read_attachments([str(target)], set(), 0, threading.Event())
    assert attachments == []
    assert errors and "limit" in errors[0]


def test_cancelled_silent_sse_read_returns_before_new_bytes_arrive():
    from accessible_ai.providers.base import iter_sse_json

    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    cancel = threading.Event()

    class Response:
        def iter_lines(self):
            entered.set()
            release.wait(3)
            yield "data: [DONE]"

    def consume():
        try:
            assert list(iter_sse_json(Response(), cancel)) == []
        finally:
            returned.set()

    worker = threading.Thread(target=consume)
    worker.start()
    try:
        assert entered.wait(1)
        cancel.set()
        assert returned.wait(1)
    finally:
        release.set()
        worker.join(3)


@pytest.mark.parametrize("interrupted", ["cancel", "error"])
def test_interrupted_regeneration_keeps_previous_completed_answer(chat, interrupted):
    panel, db, first, _other = chat
    cid = _begin(panel, first)
    original = Message(conversation_id=cid, role="assistant", content="Previous answer")
    db.add_message(original)
    panel._regeneration_history = db.list_messages(cid)
    panel.regenerating_message_id = original.id
    panel.assistant_buffer = "Replacement fragment"
    if interrupted == "cancel":
        panel.generation_cancel.set()
    panel._generation_finished(ProviderError("failure") if interrupted == "error" else None)
    assert db.list_messages(cid)[-1].content == "Previous answer"


def test_cancel_closes_a_real_silent_http_stream():
    import httpx
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from accessible_ai.providers.base import iter_sse_json

    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    errors = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.flush()
            entered.set()
            release.wait(3)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    cancel = threading.Event()

    def consume():
        try:
            with httpx.Client(trust_env=False, timeout=2) as client:
                with client.stream("GET", f"http://127.0.0.1:{server.server_port}/") as response:
                    assert list(iter_sse_json(response, cancel)) == []
                    assert response.is_closed
        except Exception as exc:
            errors.append(exc)
        finally:
            returned.set()

    worker = threading.Thread(target=consume)
    worker.start()
    try:
        assert entered.wait(1)
        cancel.set()
        assert returned.wait(1)
        assert errors == []
    finally:
        release.set()
        cancel.set()
        worker.join(3)
        server.shutdown()
        server.server_close()
        serving.join(3)
