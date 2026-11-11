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
    _TurnWorker,
    _strip_terminal_noise,
    _tool_use_label,
)
from typing import Any


# ---------------------------------------------------------------------------
# Lazy re-exports from the worker modules — public symbols stay importable from
# here so no caller has to change. Implemented via __getattr__ rather than a
# module-level import to avoid a circular-import problem: those modules import
# shared symbols from backend_registry, so loading them at this shim's import
# time would pull a half-initialised registry in behind them. __getattr__ is
# only called when a name is not already in the module's namespace, so the lazy
# load happens the first time a caller reaches for one of these names.
# ---------------------------------------------------------------------------
_CODEX_WORKER_REEXPORTS = frozenset(
    {
        "PLAN_PROVIDER_ID",
        "PLAN_TOKEN_REFRESH_MS",
        "PLAN_TOKEN_TIMEOUT_MS",
        "AccountDrop",
        "AccountSwitch",
        "CodexServer",
        "CodexWorker",
        "PendingSwitch",
        "PlanAccount",
        "PlanTokenError",
        "codex_model_options",
        "codex_plan_account",
        "codex_plan_args",
        "codex_pool_key",
        "codex_question_args",
        "drop_codex_plan_account",
        "drop_pending_codex_processes",
        "forget_codex_plan_account",
        "pending_codex_plan_account",
        "plan_token_command",
        "save_codex_plan_account",
        "switch_codex_plan_account",
        "codex_adapter",
        "auto_review_line",
    }
)
_FREEBUFF_TERMINAL_REEXPORTS = frozenset(
    {
        "FreebuffTerminal",
        "prewarm_freebuff",
        "discard_freebuff_prewarm",
        "spawn_hidden_terminal",
        "end_hidden_terminal",
    }
)
_FREEBUFF_WORKER_REEXPORTS = frozenset({"FreebuffWorker"})
_OPENCODE_WORKER_REEXPORTS = frozenset(
    {
        "OpencodeServer",
        "OpencodeWorker",
        "opencode_server",
        "stop_opencode_server",
        "opencode_error_text",
        "opencode_commands",
        "opencode_model_options",
        "opencode_default_model",
        "opencode_split_model",
        "opencode_model_efforts",
        "opencode_providers",
        "opencode_auth_methods",
        "opencode_connect_api_key",
        "opencode_disconnect",
        "opencode_oauth_start",
        "opencode_oauth_finish",
    }
)


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
