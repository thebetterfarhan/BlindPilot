# GUI Callback Coverage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Measure explicit GUI event callbacks without mistaking responsive dialog interaction for a stall.

**Architecture:** An explicit binding helper delegates to the existing timing wrapper. Per-callback timing frames pause at supported modal dialog lifecycle hooks. Migrate bindings mechanically, with structural and behavioral coverage.

**Tech Stack:** Python 3.12, wxPython 4.3.2, pytest, Ruff, mypy, Xvfb.

**Spec:** `docs/superpowers/specs/2026-11-09-gui-callback-coverage-design.md` (user approved).

## Global Constraints

- Preserve the 50 ms budget, 200-entry diagnostic buffer and DEBUG-only reporting.
- Scope is `blindpilot_app.py` and focused tests; slow-handler fixes are separate.
- Preserve event identity, Skip/Veto, ordering, exceptions and handler-specific Unbind.
- No global replacement of Bind or ShowModal; no speech/focus/control changes.
- No Astra models. No push or environment publication. Use existing isolated checkout.

## Review Focus

- Nested dialogs must not double-subtract elapsed time (Task 1 fake-clock test).
- Callbacks inside dialogs must still reveal their own slow work (Task 1 test).
- Dialog teardown or callback exceptions must not leak timing state (Task 1 test).
- ID ranges, keyword Bind arguments and repeated handler use must retain routing and unbinding (Task 1 real-control tests).
- Existing measured menu bindings must not produce duplicate diagnostics after migration (Task 2 regression test).

### Task 1: Measured binding helper with responsive dialog accounting

**Files:** Modify `blindpilot_app.py`; extend `tests/test_gui_thread_budget.py`; create `tests/test_gui_binding.py`.

**Interfaces:** Produce `_bind_gui(receiver, event, action, *bind_args, label=None, **bind_kwargs)` returning the wrapped handler; consume `_gui_work(name, action)` with its existing return-None convention. Add `_GuiTimingFrame` accounting internally, and `_GuiModalHook(wx.ModalDialogHook)` lifecycle handling. Keep names private.

- [ ] Write helper tests proving arguments/IDs/source forwarding, exact event identity, exception propagation, Skip/Veto and removal by the returned handler; prove the helper records controlled slow work.
- [ ] Run those tests and confirm failure because the helper does not exist.
- [ ] Add the helper adjacent to `_gui_work`, retaining the existing diagnostic surface.
- [ ] Write deterministic clock tests: 10 ms active + long modal + 10 ms active is below budget; 60 ms active before/after modal is recorded; a 60 ms inner callback is recorded; nested modal time is excluded once; callback exceptions and early modal teardown leave no active timing frames.
- [ ] Run modal tests and confirm current elapsed-wall-time accounting fails them.
- [ ] Implement per-frame pause depths with modal-entry snapshots; resume matching frames on exit. Register one hook after wx.App creation, retain it strongly, and unregister in guaranteed shutdown cleanup. Exercise hook lifetime explicitly in tests that reuse wx.App.
- [ ] Use controlled real wx.Dialog and native message-dialog smoke tests with scheduled dismissal to verify Enter/Exit delivery. If a native path bypasses hooks, add a narrow explicit timing pause at that path, preserving all non-modal work in its caller.
- [ ] Migrate one representative button binding through the helper so the slice has a production caller. Run `xvfb-run -a python -m pytest -q -W error tests/test_gui_thread_budget.py tests/test_gui_binding.py tests/test_all_gui_callbacks_guarded.py`.

### Task 2: Complete app binding coverage and regression checks

**Files:** Modify `blindpilot_app.py`, `tests/test_all_gui_callbacks_guarded.py`, `tests/test_gui_binding.py`.

**Interfaces:** Consume `_bind_gui` from Task 1. Retain `_menu_item` and `_bind_shortcut` as independently verified measured seams.

- [ ] Expand the AST rule to positional and keyword Bind event/handler arguments; allow direct Bind only within `_bind_gui`, `_menu_item` and `_bind_shortcut`. Include synthetic negative examples for buttons, keyboard and aliased handler variables.
- [ ] Run the coverage test and confirm it reports the remaining unmeasured direct bindings.
- [ ] Route every other explicit binding in the app through `_bind_gui`; strip redundant inline `_gui_work` wrappers while retaining diagnostic labels and all Bind arguments.
- [ ] Add representative button, text, keyboard and notebook callback tests through captured real bindings with the existing blocking contract. Confirm intentionally forbidden calls fail; do not infer all handlers are nonblocking from AST coverage alone.
- [ ] Add regression tests that existing menu/timer and scheduled-callback paths record each overrun once.
- [ ] Run focused tests, then Ruff check/format, mypy, the complete warning-as-error suite and both startup smoke checks. Use `/workspace/.local/blindpilot/activate.sh` and Xvfb. Isolate GH_TOKEN/GITHUB_TOKEN/COPILOT_GITHUB_TOKEN from tests only, preserving the persistent environment.
- [ ] Review the final diff independently using a non-Astra model. Record Linux results and outstanding real-screen-reader/Windows/macOS validation. Newly found slow handlers remain follow-up work.

## Delivery

GitHub issues #97 and #98 track these tasks, with #98 natively blocked by #97. The user selected Matt Pocock's implement-spec workflow: fresh implementer worktrees, merger agents and final independent code review on one integration branch. Keep this single approved design. No Astra models, push or environment publication.
