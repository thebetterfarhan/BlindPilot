"""Every backend's worker really does provide what the window calls on it.

`AgentWorker` is the Protocol the window holds a worker through, so that it
can drive whichever backend was chosen without knowing which one it is. It
declared start, is_alive, join and cancel — but not `steer`, which
`_on_steer` calls directly. All four workers happened to implement it, so
nothing ever broke; a fifth that did not would have type-checked clean and
failed the first time somebody pressed Steer.

The window's own call site used to be `getattr(worker, "steer")(text)`, which
is what hid it: written that way, neither a reader nor a checker could see
that the Protocol was short.
"""

from __future__ import annotations

import inspect

import agent_backends
from commandcode_worker import CommandcodeWorker
from claude_worker import ClaudeWorker
from copilot_backend import CopilotWorker
from google_worker import AntigravityWorker, GeminiWorker
from hermes_worker import HermesWorker
from muse_worker import MuseWorker
from museai_worker import MuseAiWorker

WORKERS = [
    ClaudeWorker,
    agent_backends.CodexWorker,
    agent_backends.FreebuffWorker,
    agent_backends.OpencodeWorker,
    HermesWorker,
    MuseWorker,
    MuseAiWorker,
    CommandcodeWorker,
    GeminiWorker,
    AntigravityWorker,
    CopilotWorker,
]


def test_there_is_a_worker_for_every_backend():
    assert len(WORKERS) == len(agent_backends.BACKEND_IDS)


def test_every_worker_provides_what_the_window_drives_it_through():
    """The Protocol is the promise; this is the four things keeping it."""
    promised = [
        name
        for name in dir(agent_backends.AgentWorker)
        if not name.startswith("_") and callable(getattr(agent_backends.AgentWorker, name, None))
    ]
    assert "steer" in promised, "the Protocol has lost the method Steer depends on"

    for worker in WORKERS:
        missing = [name for name in promised if not hasattr(worker, name)]
        assert not missing, f"{worker.__name__} does not provide {missing}"


def test_the_window_only_calls_what_the_protocol_promises():
    """Anything the window reaches for must be in the contract, or the next
    backend can be written without it and nobody will find out until it is
    asked to do that thing."""
    for name in ("start", "is_alive", "join", "cancel", "steer"):
        assert hasattr(agent_backends.AgentWorker, name), f"AgentWorker is missing {name}"


def test_every_worker_accepts_callbacks():
    """Construction signatures are identical: every worker takes `callbacks`.

    Adding a new callback costs one field on TurnCallbacks — no grep-and-replace
    across call sites. A worker that accepts the nine callbacks as separate kwargs
    instead breaks that contract silently; this test catches it at import time.

    Workers that forward via *args/**kwargs (e.g. _GoogleTurnWorker subclasses)
    satisfy the contract through their base class; the test walks the MRO to
    find the defining `__init__` and checks there.
    """
    for worker in WORKERS:
        # Find the class in the MRO that owns the defining __init__.
        defining_class = next(
            (c for c in worker.__mro__ if "__init__" in c.__dict__),
            worker,
        )
        sig = inspect.signature(defining_class.__init__)
        # A *args/**kwargs forwarder is accepted: `callbacks` will be passed
        # through to the base class that actually names it.
        forwards = any(
            p.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
            for p in sig.parameters.values()
        )
        if not forwards:
            assert "callbacks" in sig.parameters, (
                f"{worker.__name__} does not accept `callbacks`; "
                "add `callbacks: TurnCallbacks` and remove any individual callback kwargs"
            )
            param = sig.parameters["callbacks"]
            assert param.annotation is not inspect.Parameter.empty, (
                f"{worker.__name__}.callbacks has no type annotation"
            )
