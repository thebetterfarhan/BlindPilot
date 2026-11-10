# BlindPilot contributor guide

Screen-reader-first desktop app that runs AI coding CLIs in normal windows.
Accessibility is core behavior, not an extra: keyboard-only operation and NVDA,
JAWS, and VoiceOver speech are part of the product.

The global policy in `~/.claude/CLAUDE.md` applies; this file adds only what is
local to BlindPilot.

## Setup

After cloning, install the pre-commit hooks:

```sh
pre-commit install
pre-commit install --hook-type pre-push
```

## Inner loop

Fast check after each edit:

```sh
python -m ruff check .
python -m ruff format --check .
python -m pytest -q -W error
python -m mypy
```

While working, run the tests the change touches. The pre-push hook runs the
whole suite; running it here as well pays for it twice.

## Gate

Before a commit or PR, every command above must exit 0. `-W error` is
intentional: warnings are failures.

## wx and the test environment

wxPython (`wx`) must be installed and a display must be available to collect
tests that import `blindpilot_app` directly. On a headless Linux machine
without wx, those tests fail collection with `ModuleNotFoundError: No module
named 'wx'` — that is expected behavior, not a code bug. `pytest` will report
them as errors during collection and skip them.

To test UI-adjacent code without wx, use the stub in `tests/stub_wx.py`:

```python
import tests.stub_wx  # installs the wx stub before blindpilot_app is imported
import blindpilot_app as app
```

Tests that do need real wx should guard collection with:

```python
wx = pytest.importorskip("wx")
```

placed after the `import blindpilot_app` line so collection itself is not
gated, only execution.

## Module map

| File(s) | What lives there |
|---|---|
| `blindpilot_app.py` | The main window, all tabs, every dialog, the row renderer. 17 k lines — the monolith. |
| `agent_backends.py` | Backend registry, status probes, lazy re-exports (see below). `_TurnWorker` (same file) is the base class all workers extend; subclasses provide `_backend`, `_do_run`, `_setup`, and optionally `_teardown` and `_fail`. |
| `*_backend.py` | Per-backend discovery, auth, model listing, and account helpers. |
| `*_worker.py` | Per-backend `AgentWorker` subclass that owns the subprocess. |
| `backend_pool.py` | Held-process pool for backends that keep a process alive between turns. |
| `markdown_rows.py` | Splits a streamed assistant turn into navigable list rows. |
| `tests/stub_wx.py` | wx stub for headless tests. |
| `tests/blocking_contract.py` | Primitives that arm the 50 ms GUI-thread enforcement. |
| `tests/doubles.py` | Generic test doubles shared across the suite. |

## agent_backends lazy re-exports

`agent_backends.py` re-exports names from `codex_worker`, `freebuff_terminal`,
`freebuff_worker`, and `opencode_worker` through a module-level `__getattr__`
to keep those heavy modules from loading at startup. The sets
`_CODEX_WORKER_REEXPORTS`, `_FREEBUFF_TERMINAL_REEXPORTS`,
`_FREEBUFF_WORKER_REEXPORTS`, and `_OPENCODE_WORKER_REEXPORTS` list what each
one exports. `__getattr__` is typed `-> Any` so mypy treats the re-exported
names correctly.

When adding a new re-exported name: add it to the right frozenset. When
writing a type annotation that uses one (e.g. `PlanAccount`), import it
directly from its source module for the type checker, or rely on the `Any`
type mypy assigns to it via `__getattr__`.

## GUI callbacks

Keep GUI callbacks within the 50 ms budget. Callbacks must not wait or join,
start processes, or perform network or other potentially slow work. Move such
work to a worker and deliver results through the GUI event path only while the
owning UI is live. See [ADR 0001](docs/adr/0001-gui-thread-and-shutdown.md) for
the shutdown and process-ownership decision.

The budget is enforced by `tests/blocking_contract.py`. It arms the entry
points a handler may not reach (process starts, network reads, unbounded
waits), so a handler that violates the budget produces a deterministic failing
test rather than a stall that someone has to feel. When adding a new GUI
callback, add a test through the blocking contract.

## Notes

- Do not edit without an explicit ask: `BlindPilot.spec` (release workflow
  builds from it), `.release-setup/`, `artifacts/`, `docs/visual-audit/sandbox/`,
  and `docs/visual-audit/shots/` (audit evidence, gitignored by design).
- `pytest.ini`, `ruff.toml`, and `mypy.ini` hold the local configuration. Do not
  loosen a check to make it pass.
- An accessibility change is not complete until it is verified with a real
  screen reader, not only a successful build or a UI-tree dump.

Issue and domain conventions: `docs/agents/issue-tracker.md`, `docs/agents/triage-labels.md`, and `docs/agents/domain.md`.
