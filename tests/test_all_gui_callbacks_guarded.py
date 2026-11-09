from __future__ import annotations

import ast
import socket
import subprocess
import urllib.request
from pathlib import Path

import blindpilot_app as app
from blocking_contract import BlockedTheGuiThread, _GUARDED, gui_thread_may_not_block


def test_every_forbidden_primitive_is_blocked_without_handler_exceptions():
    with gui_thread_may_not_block():
        for owner, name, message, _offends in _GUARDED:
            entry = getattr(owner, name)
            if owner in (subprocess, socket, urllib.request):
                args = ()
            else:
                args = (object(),)
            try:
                entry(*args)
            except BlockedTheGuiThread as exc:
                assert str(exc) == message
            else:
                owner_name = getattr(owner, "__name__", type(owner).__name__)
                raise AssertionError(f"{owner_name}.{name} escaped the GUI guard")


def test_every_application_binding_uses_the_budget_wrapper():
    source = Path(app.__file__).read_text(encoding="utf-8")
    uncovered = _uncovered_bindings(source)

    assert not uncovered, "Unmeasured GUI callbacks:\n" + "\n".join(uncovered)


def _uncovered_bindings(source: str) -> list[str]:
    tree = ast.parse(source)
    bind_aliases = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "Bind"
        for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        if isinstance(target, ast.Name)
    }
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    uncovered: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        is_bind = isinstance(node.func, ast.Attribute) and node.func.attr == "Bind"
        is_bind_alias = isinstance(node.func, ast.Name) and node.func.id in bind_aliases
        if not (is_bind or is_bind_alias):
            continue
        scope = parents.get(node)
        while scope is not None and not isinstance(
            scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
        ):
            scope = parents.get(scope)
        centralized = isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)) and scope.name in {
            "_bind_gui",
            "_menu_item",
            "_bind_shortcut",
        }
        if not centralized:
            uncovered.append(f"line {node.lineno}: {ast.unparse(node)}")
    return uncovered


def test_binding_coverage_finds_unmeasured_event_families_and_aliases():
    source = """
button.Bind(wx.EVT_BUTTON, handler)
window.Bind(wx.EVT_CHAR_HOOK, handler)
obj.Bind(event_alias, handler_alias)
obj.Bind(event=event_alias, handler=handler_alias)
obj.Bind(wx.EVT_TEXT, handler, source)
obj.Bind(event_alias, handler_alias, id=10)
obj.Bind(handler=handler, event=event_alias)
binder = button.Bind
binder(wx.EVT_BUTTON, handler_alias)
"""
    uncovered = _uncovered_bindings(source)
    assert len(uncovered) == 8


def test_binding_coverage_allows_only_named_binding_seams():
    source = """
def _bind_gui(receiver, event, handler):
    receiver.Bind(event, handler)
    receiver.Bind(event=event, handler=handler)
    binding = receiver.Bind
    binding(event, handler)
def _menu_item(receiver, event, handler):
    receiver.Bind(event, handler)
def _bind_shortcut(receiver, event, handler):
    receiver.Bind(event, handler)
"""
    assert _uncovered_bindings(source) == []


def test_callafter_and_calllater_are_measured(monkeypatch):
    recorded: list[tuple[str, object]] = []
    scheduled: list[object] = []

    def measured(name, action):
        recorded.append((name, action))
        return action

    monkeypatch.setattr(app, "_gui_work", measured)
    monkeypatch.setattr(app, "_ORIGINAL_CALL_AFTER", lambda callback: scheduled.append(callback))
    monkeypatch.setattr(
        app,
        "_ORIGINAL_CALL_LATER",
        lambda _milliseconds, callback: scheduled.append(callback) or object(),
    )
    app.wx.CallAfter(lambda: None)
    app.wx.CallLater(10, lambda: None)

    assert [name.split(":", 1)[0] for name, _ in recorded] == ["CallAfter", "CallLater"]
    assert len(scheduled) == 2
