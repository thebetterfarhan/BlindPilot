# History Opening Cancellation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Cancel saved-conversation opening immediately and keep turn/task parsing off the GUI thread.

**Architecture:** Extend the existing history-opening worker to prepare turns and task state as one result. Retain request identity, add explicit cooperative cancellation, and apply only current results on the GUI thread.

**Tech Stack:** Python 3.12, wxPython, pytest, Ruff, mypy, existing transcript readers.

**Spec:** `docs/superpowers/specs/2026-11-09-history-opening-cancellation-design.md` (user approved).

## Global Constraints

- GUI callbacks keep the existing 50 ms budget; never join/wait on workers.
- Preserve the existing 64 MiB transcript/task limits and malformed-record rules.
- Preserve legacy reader behavior for callers without the new options.
- No generic TaskSlot framework, live-turn/process cancellation, installs/clones, or row-rendering refactor.
- Cancelled openings never create/fill tabs/viewers, invoke on-open actions, report late errors, or alter newer request state.
- Optional task failure or task-cap skip after successful text load opens with empty tasks and one warning; valid empty/unsupported task state is silent.
- Luna-only implementers, mergers and independent reviewers. No Astra. Existing environment/dependencies are reused.
- Work builds from callback-coverage commit b89ade8. Do not change or merge the fork's main branch.

## Review Focus

- Completion queued immediately before Cancel must still be discarded (Task 2 queued-delivery test).
- An old worker error must not close newer progress (Task 2 replacement race test).
- Missing/growing task transcript after valid text must warn without losing text (Task 1 optional-metadata tests).
- Blank/malformed JSONL lines must not bypass cancellation checkpoints or leak a file handle (Task 2 reader tests).
- muse.ai restoration callers must not fall back to GUI-thread task parsing (Task 1 compatibility tests).

### Task 1: Prepare conversation turns and task state in the opening worker

**Files:** Modify `session_history.py`, `blindpilot_app.py`; extend `tests/test_history_resume_async.py`, `tests/test_task_list.py`, and relevant `tests/test_museai_main_chat.py` cases; add focused prepared-history tests if needed.

**Interfaces:** Produce `HistoryOpenData` with `turns: list[HistoryTurn]`, `tasks: TaskList`, `task_warning: str = ""`, and `prepare_history_open(entry: HistoryEntry) -> HistoryOpenData`. Extend `restore_tasks(entry, *, strict_errors: bool = False)` so the new path can distinguish real read/cap failure while legacy calls remain silent. Extend `SessionPanel.restore_history(entry, turns, *, tasks: TaskList | None = None)` so absent tasks means an empty in-memory task list, never a transcript read.

- [ ] Write a failing prepared-load test using a real supported transcript with task events; assert correct turns and reconstructed task state through the preparation interface.
- [ ] Run that test and confirm failure before introducing the new preparation interface.
- [ ] Implement the data/result interface and strict optional task-error reporting. Unsupported backends and valid empty tasks must be successful empty results; cap/read errors in the new path become one warning, preserving legacy suppression.
- [ ] Add tests for actual task failure after valid turns, a size-cap skip after text load, legacy compatibility and valid empty/unsupported task state. Use deterministic fixture/file transitions; do not let parser mocks replace behavior being asserted.
- [ ] Adapt the history-opening worker and completion handler to pass prepared task state. Remove task file I/O from session restoration; handle the two muse.ai restoration callers explicitly with empty task state.
- [ ] Extend real asynchronous opening tests to prove both parsers run off the GUI thread and prepared task state reaches the opened session. Verify optional warning exactly once and text remains available. Update test patches at the true used binding; preserve the stale-patch contract.
- [ ] Run focused history/task/muse.ai tests, lint/format, configured types and full warning-as-error suite before committing this complete vertical slice.

### Task 2: Immediate cancellation and safe reader checkpoints

**Files:** Modify `session_history.py`, `blindpilot_app.py`; extend `tests/test_history_resume_async.py` and relevant history-reader/task-list tests.

**Interfaces:** Consume Task 1's `HistoryOpenData`. Add keyword-only `cancelled: Callable[[], bool] | None = None` to `prepare_history_open`, `load_turns` and `restore_tasks`, propagating it to relevant reader/iteration helpers. Use a distinct `HistoryOpenCancelled` exception for cooperative cancellation so it cannot become successful partial data or an optional-task warning. Keep default one-argument reader behavior compatible where no cancellation is requested.

- [ ] Write a failing real-reader test that sets cancellation after a controlled number of JSONL records; assert remaining records are not consumed and the cancellation outcome is distinct from ordinary failure.
- [ ] Add checkpoints before and after reader calls, between preparation phases and before delivery; check physical JSONL lines including blank/malformed records, task replay and available row/item loops. Existing blocking steps retain their timeouts; no arbitrary thread/process termination.
- [ ] Test pre-cancelled input, mid-read/replay cancellation, cancellation between phases, file/resource cleanup and no partial result. Use synchronization or counted records rather than wall-clock thresholds.
- [ ] Write a failing GUI test where the worker remains blocked: Cancel/close must clear progress and announce once before release. Release late success/error and assert no tab, viewer, on-open action or late announcement.
- [ ] Implement immediate detachment: set the cancellation event, invalidate request identity and clear progress/widget references before destruction. Explicit Cancel announces once; replacement quietly invalidates old work. Cancellation after completion is harmless.
- [ ] Add main-window close cancellation/invalidation with no waits and no announcement into a closing UI.
- [ ] Cover queued completion before/after Cancel, repeated cancel/close, superseded success/error leaving new progress intact, and unchanged successful opening. Use actual production delivery seams.
- [ ] Run focused checks, Ruff check/format, configured mypy, full `-W error -n 4 --dist loadfile` suite and both source startup smokes under Xvfb. Use the prepared activation script; remove GitHub sign-in variables only from test subprocesses.
- [ ] Commit the clean slice, integrate it, then obtain independent Luna standards/spec reviews of the combined feature. Fix material findings in one fresh Luna implementer and rerun affected/required gates.

## Execution and delivery

Tracker: spec #99, Task 1 #100, Task 2 #101; #101 is blocked by #100.

The user explicitly selected to-spec, to-tickets and implement-spec; no further execution-method selection is needed. Publish one parent spec and two linked child tickets, with Task 2 blocked by Task 1. Fresh Luna implementers use separate worktrees from the integration branch; merger agents integrate clean committed results. Capture final test evidence and unrun Windows/macOS/live screen-reader checks. Do not push new feature code or publish the environment without authorization for this feature.
