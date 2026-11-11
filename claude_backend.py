"""Claude Code backend for BlindPilot.

Wraps the Claude Code CLI's JSON-streaming output as a BlindPilot turn worker.
The worker lives in ``claude_worker.py``; this module is the adapter entry point
that ``agent_backends.ADAPTERS`` loads, symmetrical with ``hermes_backend.py``.

Copyright (c) 2026 doubletaponair and BlindPilot contributors.
Based on the original Claude Code Reader application by doubletaponair:
https://github.com/doubletaponair/claude-code-reader
SPDX-License-Identifier: MIT
"""

from __future__ import annotations

from claude_worker import ClaudeWorker

__all__ = ["ClaudeWorker"]
