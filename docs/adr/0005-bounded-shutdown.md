# ADR 0005: Bounded shutdown

- **Status:** Accepted
- **Date:** 2026-11-11
- **Amends:** [ADR 0001](0001-gui-thread-and-shutdown.md) (the single three-second deadline)
- **Context:** ADR 0001 gives shutdown one three-second deadline for joining
  cancellation workers, but dropping held processes afterwards has no limit,
  and the shutdown thread is not a daemon. A backend that hangs while stopping
  leaves BlindPilot running with no window, which a screen reader user cannot
  see or be told about.

## Decisions

Shutdown has a cap in two phases. Cancellation workers get three seconds to
join. Dropping held processes then gets five more. At eight seconds in total,
shutdown makes a forced drop of every owned process still standing, bounds that
too, and ends the process.

A forced drop applies only to owned processes, verified as ADR 0001 describes.
Anything that cannot be verified is left alone, as before.

Quitting is silent when shutdown finishes within the cap. When the cap is
reached, shutdown leaves a small notice file in the data directory. At the next
launch BlindPilot announces it once, after the main window shows, then deletes
it. It is never a dialog.

A new copy launched while shutdown runs waits up to the cap for the old copy to
finish, then starts. It does not raise a window that no longer exists, and it
does not hand a session link to a copy that has begun shutdown.

The main window does not save transcripts at quit. The backend CLIs keep their
own sessions. On reopen, a session whose last item is an unfinished tool call is
marked as interrupted. The tool call is never run again automatically.

## Consequences

Shutdown time is bounded at eight seconds, so no windowless copy lingers. A
forced drop can cut a backend mid-write; the interrupted marker is how that
shows on reopen, and the same marker covers crashes and power loss.

Shutdown gets its own contract test beside `tests/blocking_contract.py`: the cap
is honored, unverifiable descendants are left alone, and the `atexit` sweep is
harmless when the worker already ran. Windows Job Object and POSIX
session-leader behavior is checked on each supported OS as a release-checklist
item, not a unit test.
