"""Measured bindings retain wx event dispatch semantics."""

import pytest
import wx

import blindpilot_app as app
from blocking_contract import BlockedTheGuiThread, gui_thread_may_not_block


def test_bound_button_receives_event_and_can_be_unbound(frame):
    button = wx.Button(frame)
    seen = []
    wrapped = app._bind_gui(button, wx.EVT_BUTTON, seen.append)
    event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())
    assert button.GetEventHandler().ProcessEvent(event)
    assert seen == [event]
    assert button.Unbind(wx.EVT_BUTTON, handler=wrapped)
    assert not button.GetEventHandler().ProcessEvent(event)


def test_id_range_skip_and_multiple_handlers_keep_wx_dispatch_order(frame):
    button = wx.Button(frame, id=wx.ID_ANY)
    seen = []

    def first(event):
        seen.append(("first", event))
        event.Skip()

    def second(event):
        seen.append(("second", event))
        event.Skip()

    first_handler = app._bind_gui(
        button, wx.EVT_BUTTON, first, id=button.GetId(), id2=button.GetId()
    )
    app._bind_gui(button, wx.EVT_BUTTON, second, id=button.GetId(), id2=button.GetId())
    event = wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId())

    button.GetEventHandler().ProcessEvent(event)
    assert [name for name, _event in seen] == ["second", "first"]
    assert all(seen_event is event for _name, seen_event in seen)
    assert event.GetSkipped()
    assert button.Unbind(
        wx.EVT_BUTTON, id=button.GetId(), id2=button.GetId(), handler=first_handler
    )


def test_bound_close_handler_can_veto_event(frame):
    seen = []

    def veto(event):
        seen.append(event)
        event.Veto()

    wrapped = app._bind_gui(frame, wx.EVT_CLOSE, veto)
    event = wx.CloseEvent(wx.EVT_CLOSE.typeId, frame.GetId())
    event.SetCanVeto(True)

    wrapped(event)

    assert seen == [event]
    assert event.GetVeto()


def test_binding_forwards_wx_arguments_and_uses_stable_diagnostic_name(monkeypatch):
    calls = []
    event = wx.EVT_BUTTON
    source = object()

    def action(_event):
        return None

    class Receiver:
        def Bind(self, actual_event, handler, *args, **kwargs):
            calls.append((actual_event, handler, args, kwargs))

    wrapped = app._bind_gui(Receiver(), event, action, source, id=42, id2=43)

    assert calls[0][0] is event
    assert calls[0][1] is wrapped
    assert calls[0][2] == (source,)
    assert calls[0][3] == {"id": 42, "id2": 43}
    assert app._bind_gui(Receiver(), event, action).__name__ == "run"


def test_explicit_label_does_not_evaluate_action_repr(frame):
    class Action:
        def __repr__(self):
            raise AssertionError("repr should not be evaluated")

        def __call__(self, _event):
            pass

    button = wx.Button(frame)
    app._bind_gui(button, wx.EVT_BUTTON, Action(), label="Save button")


def test_binding_records_slow_callback_with_useful_event_name(frame, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(app.time, "perf_counter", lambda: now[0])
    app._over_budget_work.clear()
    button = wx.Button(frame)

    def slow(_event):
        now[0] += 0.06

    app._bind_gui(button, wx.EVT_BUTTON, slow)(wx.CommandEvent())

    assert (
        app.over_budget_work()[0][0]
        == "EVT_BUTTON: test_binding_records_slow_callback_with_useful_event_name.<locals>.slow"
    )


def test_migrated_history_button_text_and_keyboard_bindings_pass_blocking_contract(
    frame, monkeypatch, tmp_path
):
    callbacks = []
    bound = {}
    original_gui_work = app._gui_work

    def capture(name, action):
        wrapped = original_gui_work(name, action)
        bound[name] = wrapped
        return wrapped

    monkeypatch.setattr(app, "_gui_work", capture)
    monkeypatch.setattr(app, "_load_config", lambda: {"history_sort": "recent"})
    monkeypatch.setattr(app.HistoryDialog, "_reload", lambda self: None)
    monkeypatch.setattr(app.HistoryDialog, "_refresh", lambda self: callbacks.append("text"))
    monkeypatch.setattr(app.HistoryDialog, "_accept", lambda self: callbacks.append("button"))
    monkeypatch.setattr(app.HistoryDialog, "_on_key", lambda self, _event: callbacks.append("key"))
    dialog = app.HistoryDialog(frame, "codex", str(tmp_path))
    try:
        button = dialog.FindWindow(wx.ID_OK)
        events = [
            (
                "EVT_BUTTON: HistoryDialog -> _accept",
                wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId()),
            ),
            (
                "EVT_TEXT: HistoryDialog.filter_box -> _refresh",
                wx.CommandEvent(wx.EVT_TEXT.typeId, dialog.filter_box.GetId()),
            ),
            (
                next(name for name in bound if name.startswith("EVT_CHAR_HOOK:")),
                wx.KeyEvent(wx.wxEVT_CHAR_HOOK),
            ),
        ]
        with gui_thread_may_not_block():
            for name, event in events:
                bound[name](event)

        assert callbacks == ["button", "text", "key"]
    finally:
        dialog.Destroy()


def test_production_notebook_binding_passes_blocking_contract(wx_app, monkeypatch, tmp_path):
    import chat_integration

    callbacks = []
    bound = {}
    original_gui_work = app._gui_work

    def capture(name, action):
        wrapped = original_gui_work(name, action)
        bound[name] = wrapped
        return wrapped

    monkeypatch.setattr(app, "_gui_work", capture)
    monkeypatch.setattr(app, "_load_config", lambda: {"setup_complete": True, "app_mode": "agent"})
    monkeypatch.setattr(app, "_save_config", lambda _value: None)
    monkeypatch.setattr(app, "_start_legacy_chat_import", lambda: None)
    monkeypatch.setattr(chat_integration, "database_path", lambda: tmp_path / "chat.sqlite3")
    monkeypatch.setattr(
        chat_integration, "import_existing_accessible_ai_data", lambda _target: None
    )

    def on_tab_changed(self, _event):
        callbacks.append("tab")

    monkeypatch.setattr(app.MainFrame, "_on_tab_changed", on_tab_changed)
    frame = app.MainFrame(str(tmp_path))
    try:
        handler_name = next(name for name in bound if name.endswith("on_tab_changed"))
        event = wx.BookCtrlEvent(wx.EVT_BOOKCTRL_PAGE_CHANGED.typeId, frame.notebook.GetId())
        callbacks.clear()
        with gui_thread_may_not_block():
            bound[handler_name](event)
        assert callbacks == ["tab"]
    finally:
        frame.Destroy()


def test_gui_guard_rejects_forbidden_work_inside_a_bound_callback(frame):
    import subprocess

    button = wx.Button(frame)
    handler = app._bind_gui(
        button,
        wx.EVT_BUTTON,
        lambda _event: subprocess.Popen(["definitely-not-run"]),
        label="guard negative",
    )
    with gui_thread_may_not_block(), pytest.raises(BlockedTheGuiThread, match="process"):
        handler(wx.CommandEvent(wx.EVT_BUTTON.typeId, button.GetId()))


def test_modal_wait_is_excluded_but_work_before_and_after_is_measured(wx_app, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(app.time, "perf_counter", lambda: now[0])
    app._over_budget_work.clear()
    hook = app._GuiModalHook()

    def action():
        now[0] += 0.03
        hook.Enter(None)
        now[0] += 10
        hook.Exit(None)
        now[0] += 0.03

    app._gui_work("outer", action)()
    assert len(app.over_budget_work()) == 1
    assert app.over_budget_work()[0][0] == "outer"
    assert app.over_budget_work()[0][1] == pytest.approx(60)


def test_nested_modal_and_inner_callback_are_accounted_independently(wx_app, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(app.time, "perf_counter", lambda: now[0])
    app._over_budget_work.clear()
    hook = app._GuiModalHook()

    def inner():
        now[0] += 0.06
        hook.Enter(None)
        now[0] += 20
        hook.Exit(None)

    def outer():
        now[0] += 0.01
        hook.Enter(None)
        now[0] += 10
        app._gui_work("inner", inner)()
        hook.Exit(None)
        now[0] += 0.01

    app._gui_work("outer", outer)()
    assert [name for name, _ in app.over_budget_work()] == ["inner"]


def test_callback_exception_does_not_leak_timing_state(wx_app, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(app.time, "perf_counter", lambda: now[0])
    app._over_budget_work.clear()
    hook = app._GuiModalHook()

    def action():
        hook.Enter(None)
        raise ValueError("original")

    with pytest.raises(ValueError, match="original"):
        app._gui_work("broken", action)()
    assert app._gui_timing_frames == []
    hook.Exit(None)
    now[0] += 10
    app._gui_work("next", lambda: None)()
    assert app.over_budget_work() == []


def test_real_dialog_delivers_modal_lifecycle(wx_app, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(app.time, "perf_counter", lambda: now[0])

    class CountingHook(app._GuiModalHook):
        def __init__(self):
            super().__init__()
            self.entered = 0
            self.exited = 0

        def Enter(self, dialog):
            self.entered += 1
            result = super().Enter(dialog)
            now[0] += 10
            return result

        def Exit(self, dialog):
            self.exited += 1
            return super().Exit(dialog)

    hook = CountingHook()
    hook.Register()
    dialog = wx.Dialog(None, title="Modal lifecycle")
    try:
        wx.CallLater(30, dialog.EndModal, wx.ID_OK)
        app._over_budget_work.clear()
        app._gui_work("dialog", lambda: wx.Dialog.ShowModal(dialog))()
        assert app.over_budget_work() == []
        assert hook.entered == 1
        assert hook.exited == 1
        assert hook.snapshots == []
    finally:
        dialog.Destroy()
        hook.Unregister()


def test_modal_hook_scope_can_be_reused_after_exception(wx_app, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(app.time, "perf_counter", lambda: now[0])
    with pytest.raises(ValueError, match="original"):
        with app._gui_modal_tracking():
            raise ValueError("original")
    with app._gui_modal_tracking() as hook:
        dialog = wx.Dialog(None)
        try:

            def finish():
                now[0] += 0.06
                dialog.EndModal(wx.ID_OK)

            wx.CallLater(80, finish)
            app._over_budget_work.clear()
            app._gui_work("dialog", lambda: wx.Dialog.ShowModal(dialog))()
            # Native dialog transitions vary by platform; test hook accounting.
            assert app.over_budget_work() == [
                (f"CallLater: {finish.__qualname__}", pytest.approx(60))
            ]
            assert hook.snapshots == []
            assert app._gui_timing_frames == []
        finally:
            dialog.Destroy()
