# Extend GUI callback measurement

Status: approved by the user on 2026-11-09; tracked by GitHub issues #97 and #98.

## Intent and scope

Improve evidence about GUI responsiveness before restructuring BlindPilot.
The user selected reliability and screen-reader responsiveness, then chose a
small explicit binding helper. Expand checks in `blindpilot_app.py`; newly
discovered slow handlers are separate fixes. Preserve ADR 0001's 50 ms budget.

## Current behavior

`_gui_work` records named callbacks exceeding 50 ms in a bounded 200-entry deque
and DEBUG logs. Menus, accelerators, timers, CallAfter and CallLater use it.
The structural binding test checks only EVT_MENU and EVT_TIMER. Direct control,
keyboard, selection and tab bindings remain outside that structural coverage.
Elapsed wall time currently includes time a handler spends in a modal dialog.

## Approaches

1. **Selected: explicit binding helper.** Local, reviewable binding changes and
   structural coverage tests. No global interception of wx binding behavior.
2. **Rejected: intercept all wx bindings.** Broader automatic coverage, but
   changes third-party behavior and makes callback identity harder to inspect.

## Proposed behavior

- Add one internal helper that accepts the receiver, event, handler and wx Bind
  arguments, wraps the handler with `_gui_work`, and forwards binding arguments
  unchanged. Derive useful diagnostic names from event and handler identity;
  allow an explicit label for ambiguous lambdas. Return the wrapped callable
  so any caller that needs handler-specific Unbind can retain that exact object.
- Migrate direct bindings in `blindpilot_app.py` through the helper, including
  keyboard, buttons, text, focus, selection and notebook events. Keep existing
  measured menu/shortcut seams and avoid wrapping a callback twice.
- Broaden the AST coverage check to explicit Bind calls in that module,
  including positional and keyword event/handler arguments. Route every
  non-centralized binding through the helper, including existing inline
  `_gui_work` bindings after removing their redundant wrapper. Permit direct
  Bind only inside the helper, `_menu_item` and `_bind_shortcut`; verify those
  seams behaviorally. Do not add a baseline
  that silently accepts unmeasured event families.
- Preserve event object identity, positional and keyword arguments, Skip/Veto,
  handler ordering and exception propagation. Keep `_gui_work`'s existing
  return-None convention; wx event dispatch does not consume handler results.
  Do not change focus, narration, controls, backend logic or worker ownership.
- Keep production behavior diagnostic: exceeding 50 ms records/logs an overrun;
  it does not raise, announce, or interrupt the user's action.

## Responsive modal dialogs

Human interaction with a modal dialog is not a GUI stall. wxPython 4.3.2 exposes
`wx.ModalDialogHook` with Enter/Exit/Register. Use that supported lifecycle seam
to pause timing of callbacks already active when a modal dialog opens, then
resume them when it closes. Callbacks dispatched inside the dialog must retain
their own measurement, and nested dialogs must not double-subtract time.
Keep a stack of active callback frames with pause depth per frame. Each modal
entry snapshots and pauses only frames already active; its matching exit
resumes that snapshot. Frames created inside the modal remain timed until a
nested modal pauses them. Restore accounting in finally blocks, balance hook
registration with application lifetime, and preserve original exceptions even
when cleanup encounters a destroyed dialog.

Verify both wx.Dialog.ShowModal and native message dialogs. If a relevant native
dialog bypasses the hook, report the gap and use a narrowly scoped explicit
pause around that call; never exempt the whole handler. Keep work before and
after modal interaction measurable. Do not globally replace ShowModal or Bind.

## Validation

- Deterministic fake-clock tests prove quick/slow callbacks, modal exclusion,
  slow work before/after a dialog, nested dialogs and callbacks within a dialog.
  Include exceptional callbacks and early dialog teardown so timing state
  cannot leak into subsequent events.
- Exercise the binding helper with real wx controls: event arguments, Skip/Veto
  where applicable, multiple handlers and handler-specific unbinding.
- Run structural coverage against the real module and negative synthetic cases.
- Run representative button, text, keyboard and tab callbacks with the existing
  GUI blocking contract. Include controlled forbidden-operation negative cases;
  a structural test alone cannot prove the absence of stalls.
- Run focused GUI tests, Ruff, mypy, the full warning-as-error suite and GUI
  startup smoke checks using the prepared Python environment and Xvfb.
- Automated Linux checks do not establish Windows/macOS or real screen-reader
  behavior. Record those limits; do not claim accessibility verification.

## Non-goals and risks

No backend extraction, shared task framework, performance dashboard, new speech,
change to the budget, or speculative handler optimization. Native dialog hook
coverage and callback identity are the key implementation risks. Preserve
existing user changes. No push or environment publication is part of this work.
