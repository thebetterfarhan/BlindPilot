"""Backward-compatibility shim for agent_backends.

Stable registry content lives in ``backend_registry``.
CLI-probe utilities live in ``backend_probes``.
This module re-exports everything from both for callers that import it
by its original name.
"""

from __future__ import annotations

from backend_probes import *  # noqa: F401, F403
from backend_probes import (  # noqa: F401
    _app_version,
    _as_number,
    _codex_home,
    _codex_usage_windows,
    _fallback_cli_paths,
    _freebuff_picker_options,
    _freebuff_usage_payload,
    _hermes_home,
    _ordered_windows,
    _probe_backend,
    _read_freebuff_choice,
    _usage_window_line,
)
from backend_registry import *  # noqa: F401, F403
from backend_registry import (  # noqa: F401
    _CODEX_WORKER_REEXPORTS,
    _FREEBUFF_TERMINAL_REEXPORTS,
    _FREEBUFF_WORKER_REEXPORTS,
    _OPENCODE_WORKER_REEXPORTS,
    _TurnWorker,
    _strip_terminal_noise,
    _tool_use_label,
)
from typing import Any


def __getattr__(name: str) -> Any:
    if name in _CODEX_WORKER_REEXPORTS:
        import codex_worker as _cw

        return getattr(_cw, name)
    if name in _FREEBUFF_TERMINAL_REEXPORTS:
        import freebuff_terminal as _ft

        return getattr(_ft, name)
    if name in _FREEBUFF_WORKER_REEXPORTS:
        import freebuff_worker as _fw

        return getattr(_fw, name)
    if name in _OPENCODE_WORKER_REEXPORTS:
        import opencode_worker as _ow

        return getattr(_ow, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
