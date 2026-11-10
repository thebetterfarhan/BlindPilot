"""A closed connection yields nothing, even if a frame was already waiting."""

from __future__ import annotations

import sys
import time

import hermes_backend as hb

HELLO = '{"jsonrpc": "2.0", "method": "hello"}'


def _child(script: str) -> list[str]:
    return [sys.executable, "-c", script]


def _wait_for_frame(transport: hb.StdioTransport) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with transport._frames_ready:
            if transport._frames:
                return
        time.sleep(0.01)
    raise AssertionError("the child's frame never arrived")


def test_receive_after_close_does_not_return_a_frame_that_was_already_queued():
    transport = hb.StdioTransport(
        ".",
        argv=_child(f"import time; print({HELLO!r}, flush=True); time.sleep(30)"),
        peer="Test",
    )
    transport.start()
    try:
        _wait_for_frame(transport)
        transport.close()
        assert transport.receive(0.001) is None
    finally:
        transport.close()


def test_a_frame_sent_before_the_child_exits_is_still_delivered():
    transport = hb.StdioTransport(
        ".",
        argv=_child(f"print({HELLO!r}, flush=True)"),
        peer="Test",
    )
    transport.start()
    try:
        assert transport.receive(5) == {"jsonrpc": "2.0", "method": "hello"}
    finally:
        transport.close()
