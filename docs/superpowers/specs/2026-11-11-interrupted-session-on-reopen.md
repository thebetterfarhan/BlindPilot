# Interrupted session on reopen

Follows [ADR 0005](../../adr/0005-bounded-shutdown.md). Ready to build.

## Goal

When a person reopens a conversation that ended in the middle of a tool call,
BlindPilot says so, in speech and in the conversation list. Quitting, a crash,
or a power cut can all leave this state. It must never look like a finished
turn, and BlindPilot must never run the call again by itself.

## Already built

`session_history.ends_mid_tool_call(records)` is a pure check over Claude
transcript records. It returns true when a `tool_use` has no `tool_result` by
the end. It has tests in `tests/test_interrupted_session.py`. Nothing calls it.

## Behavior

1. `prepare_history_open` reads the transcript on its worker, as it does today
   for tasks, and sets `HistoryOpenData.interrupted`. It does nothing for
   backends whose transcript is not Claude's JSONL shape (Copilot and the
   others report `False`).
2. On open, the tab announces once, after "Resumed …": "The last tool call did
   not finish before BlindPilot closed. It was not run again."
3. The conversation list gets one final row, kind `note`, labelled "Interrupted:
   the last tool call did not finish." A screen reader user reaches it with the
   same arrow keys as any row. It carries no payload that can be sent.
4. Resuming and sending a new message works as it does today. Nothing re-sends
   the unfinished call.

## Seams to test

- `prepare_history_open` over a JSONL fixture: `interrupted` true, false, and
  false for a non-Claude backend. Use `tmp_path`.
- The row builder: an interrupted open ends with exactly one `note` row.
- The open callback announces once, through `tests/blocking_contract.py`.

## Out of scope

- Marking interruptions for backends other than Claude.
- Offering a "retry the call" action.

## Done when

- The gate passes: ruff check, ruff format --check, `pytest -q -W error`, mypy.
- NVDA, JAWS and VoiceOver read the announcement and the note row. An
  accessibility change is not complete without a real screen reader.
