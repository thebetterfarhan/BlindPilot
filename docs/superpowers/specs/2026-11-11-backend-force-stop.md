# Backend force stop

Follows [ADR 0005](../../adr/0005-bounded-shutdown.md). The code is ready to
build; the last step needs a person on Windows and on POSIX.

## Goal

Make the shutdown cap real. `shutdown.finish` calls
`backend_pool.force_drop_all_owned()` at eight seconds, but no backend supplies
`Adapter.force_stop`, so today the forced drop ends nothing.

## Already built

- `Adapter.force_stop` (optional), `HeldProcess.force_stop()` and
  `BackendPool.force_drop_owned()`, with tests in
  `tests/test_pool_forced_drop.py`. A backend with no `force_stop` is left
  alone.

## Behavior

1. `claude_adapter()` and `codex_adapter()` each set `force_stop`.
2. Each `force_stop` ends the exact root through
   `process_utils.end_process_group(proc, timeout=...)`, which already checks
   ownership: it signals the group only while the root is live and is the
   group leader, and it closes the Windows Job Object. It must not use a PID or
   group ID taken before the root was verified.
3. Calling `force_stop` after a normal `stop`, or twice, is harmless.
4. The forced drop returns within the 2 second force budget even if the root
   refuses to die.

## Open question to settle first

`end_process_group` documents `timeout: float = 0.0` yet its text says "five
seconds when it is not given". Find out which is true, and decide what timeout a
forced drop passes. A forced drop must not wait the default five seconds.

## Seams to test

- Each adapter, over a real short-lived child process: `force_stop` ends it and
  its own child, and a second call does nothing.
- A root that already exited: nothing is signalled by PID or group ID.
- A root still in BlindPilot's own group: BlindPilot does not signal itself.

## Out of scope

- Other backends (Hermes, Command Code, FreeBuff, Google). Each gets its own
  ticket once these two work.

## Done when

- The gate passes.
- Release checklist, run by a person: on Windows, a hung backend root and its
  descendants are gone after quit within the cap (Job Object). On Linux and
  macOS, the same through session-leader cleanup. Record the result in the
  release notes.
