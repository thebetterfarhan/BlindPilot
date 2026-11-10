# Cancellable saved-conversation opening

Status: approved by the user on 2026-11-09 following grill-with-docs.
Tracker: spec #99, implementation tickets #100 and #101 in the user's fork.
Code inspected at `thebetterfarhan/BlindPilot@b89ade8`.

## Intent

Keep saved-conversation opening responsive when a read is slow, the user
cancels, or a newer opening replaces it. This slice targets read-only history
loading and task restoration. It does not change installs, clones, running
agent turns, the shared TaskSlot abstraction, or large-conversation rendering.

## Agreed behavior

- Explicit Cancel and closing the opening progress dialog dismiss progress
  immediately and announce cancellation once. The GUI never joins or waits
  for the worker.
- A cancelled opening cannot create/fill a tab, open a read-only viewer,
  invoke its on-open callback, announce a later error/completion, or overwrite
  the state of a newer opening.
- A replacement opening invalidates its predecessor and requests that its
  unnecessary work stop. Proposed default: replacement is quiet about the
  old cancellation and announces loading the new conversation normally.
- Workers stop at safe cancellation checkpoints. Cooperative cancellation is
  not a promise to interrupt a blocking OS, database, WSL or CLI read instantly.
- Successfully loaded conversation text still opens if optional task-list
  restoration fails: use an empty task list and announce one concise warning.
  An unsupported backend or a valid empty task list is not a restoration error.
- Approved edge-case choice: when text has already loaded but task replay is
  skipped because the transcript exceeds the existing 64 MiB cap, open the text
  with the same optional-task warning. Preserve the cap and legacy callers'
  silent-empty behavior; do not silently imply that task restoration succeeded.
- Existing fatal text-load failures and empty/unreadable conversation behavior
  remain distinct from optional task-list failure and cancellation.

## Current evidence

`MainFrame._resume_history` starts a worker calling `load_turns(entry)` without
passing its cancellation event. `_cancel_history_open` sets that event but
keeps progress until `_history_open_loaded` receives the finished read and
discards it. Tests cover discarding the result, not early worker termination.

`SessionPanel.restore_history` calls `restore_tasks(entry)` on the GUI thread.
For Claude and Copilot, that replays a JSONL transcript up to the existing
64 MiB cap. Moving turn loading off-thread therefore leaves another transcript
scan inside the GUI completion path.

## Approaches

1. **Selected: extend the existing opening worker.** Prepare turns and task
   state off-thread and deliver a single result; retain per-opening identity
   and add cooperative read checkpoints. This directly fixes the chosen flow.
2. **Not selected: introduce a generic cancellable TaskSlot framework first.**
   This broadens migration to unrelated dialogs before the saved-opening fix.

## Implementation direction

Prepare one result containing conversation turns, task state and an optional
task-restoration warning. It contains no wx objects. Do not add a GUI-thread
fallback that rereads tasks when prepared task state is absent. Update actual
restoration callers deliberately; existing muse.ai callers have no supported
task replay and should continue receiving an empty task list without file I/O.

Use explicit optional cancellation arguments/checks in the history-reading
path. Preserve default behavior for other callers that do not request
cancellation. Do not make cancellation look like a successful partial
conversation or a task-restoration failure.

- Check before starting work, between turn loading and task replay, before
  delivery, and between safely interruptible parsing/iteration units.
- In local JSONL paths, check while consuming lines/records, including malformed
  or blank records, and while replaying task events. Keep existing transcript
  size caps and malformed-record handling.
- For database, whole-document, WSL and CLI readers, check before/after existing
  blocking steps and in available iteration loops. Preserve existing read
  deadlines. Do not kill arbitrary threads or provider-owned live processes.
- Preserve existing task-reader compatibility where errors are deliberately
  suppressed for legacy callers, while allowing the new prepared-load path to
  distinguish an actual optional restoration failure from a legitimate empty
  task list. Settle the narrow interface in the implementation plan.

Cancellation must atomically detach the current opening from progress state on
the GUI thread before destroying the dialog. Invalidate request identity and
clear widget references so queued close/cancel/completion callbacks cannot
double-announce or touch destroyed controls. Check identity again on delivery.
Cancelling a request that has already completed is harmless.

Closing the main window also requests cancellation and invalidates pending
history delivery without waiting or announcing into a closing UI. Keep the
existing owned-process shutdown policy in ADR 0001.

## Validation seams

- History reader/preparation interface: cancellation already set, mid-record
  cancellation, cancellation before task replay, and no partial delivery.
  Use controlled records/events rather than wall-clock guesses to prove that
  unused records are not consumed. Verify files/resources close on cancellation.
- Prepared task state: successful restoration, valid empty/unsupported backend,
  actual task-read failure or size-cap skip after valid text, and cancellation
  during replay. Preserve legacy task-reader behavior in separate tests.
- Opening worker and GUI delivery: controlled blocked read proves progress
  closes and cancellation is announced before the read is released; eventual
  success and failure are silent and create no tab/viewer/on-open action.
- Replacement/owner lifecycle: older success or error cannot clear newer
  progress; closing the main window makes queued delivery harmless. Cover both
  a queued completion before Cancel and one queued after it.
- Restoration interface: task replay runs on a worker; GUI application of
  prepared turns/tasks cannot reach the task parser. Exercise the existing
  import-binding/stale-patch guard when adapting tests to the new load seam.
- Run focused history/task/UI tests, Ruff, configured mypy, full warning-as-error
  suite, and source startup smoke checks under the prepared Linux environment.

The user explicitly deferred real screen-reader validation for this work.
Record Windows/macOS and live screen-reader checks as unrun; do not claim them.

## Limits and delivery

This does not fix every possible delay or chunk large row rendering. It does
not make two transcript reads an atomic filesystem snapshot. Read-only work
may finish a non-interruptible step after cancellation, but its result remains
irrelevant and it must not start further phases.

No application code changes before design approval. The previous callback
coverage feature is already committed and pushed separately. Publish/push
decisions for this new feature will be made when its implementation is ready.
