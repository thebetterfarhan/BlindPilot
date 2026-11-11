"""Claude and Codex supply a real force stop for the shutdown cap (ADR 0005)."""

from __future__ import annotations

import io
import os
import platform
import subprocess
import sys
import time

import pytest

import claude_session as cs
import codex_worker
import process_utils

posix_only = pytest.mark.skipif(platform.system() == "Windows", reason="process groups are POSIX")

# A root that starts one child of its own, reports the child's pid, then hangs.
_ROOT = (
    "import subprocess, sys, time\n"
    "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
    "print(c.pid, flush=True)\n"
    "time.sleep(60)\n"
)


def _start_root() -> tuple[subprocess.Popen, int]:
    proc = subprocess.Popen(
        [sys.executable, "-c", _ROOT],
        stdout=subprocess.PIPE,
        text=True,
        **process_utils.own_group_kwargs(),
    )
    assert proc.stdout is not None
    return proc, int(proc.stdout.readline())


def _gone(pid: int) -> bool:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.02)
    return False


def _claude(proc):
    return cs.ClaudeSession(proc, cs.Wants(cwd=".", permission_mode="default"))


_BUILDERS = {
    "claude": (_claude, cs.claude_adapter),
    "codex": (codex_worker.CodexServer, codex_worker.codex_adapter),
}


@pytest.fixture(params=sorted(_BUILDERS))
def backend(request):
    build, adapter = _BUILDERS[request.param]
    return build, adapter()


def test_the_adapter_supplies_a_force_stop(backend):
    assert backend[1].force_stop is not None


@posix_only
def test_force_stop_ends_the_root_and_its_child_and_twice_is_harmless(backend):
    build, adapter = backend
    proc, child = _start_root()
    handle = build(proc)
    try:
        started = time.monotonic()
        adapter.force_stop(handle)
        assert time.monotonic() - started < 2.0
        assert proc.poll() is not None
        assert _gone(child)
        adapter.force_stop(handle)
    finally:
        proc.kill()
        proc.wait()
        if proc.stdout:
            proc.stdout.close()


@posix_only
def test_force_stop_after_stop_is_harmless(backend):
    build, adapter = backend
    proc, child = _start_root()
    handle = build(proc)
    try:
        adapter.stop(handle)
        adapter.force_stop(handle)
        assert _gone(child)
    finally:
        proc.kill()
        proc.wait()
        if proc.stdout:
            proc.stdout.close()


class _Exited:
    pid = 4242
    stdin = None
    stdout = io.StringIO("")
    stderr = io.StringIO("")

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        raise AssertionError("an exited root must not be killed")


def test_an_exited_root_is_not_signalled_by_pid_or_group(backend, monkeypatch):
    build, adapter = backend
    monkeypatch.setattr(os, "killpg", lambda *a: pytest.fail("signalled a group"), raising=False)
    monkeypatch.setattr(os, "kill", lambda *a: pytest.fail("signalled a pid"))
    adapter.force_stop(build(_Exited()))


class _InOurGroup:
    """A live root that never became a group leader."""

    pid = 4242
    stdin = None
    stderr = io.StringIO("")

    def __init__(self):
        self.killed = False
        self.stdout = io.StringIO("")

    def poll(self):
        return None

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


@posix_only
def test_a_root_in_our_own_group_never_makes_us_signal_ourselves(backend, monkeypatch):
    build, adapter = backend
    root = _InOurGroup()
    monkeypatch.setattr(os, "getpgid", lambda pid: os.getpgrp())
    monkeypatch.setattr(os, "killpg", lambda *a: pytest.fail("signalled a group"))
    adapter.force_stop(build(root))
    assert root.killed


def test_a_refusing_root_cannot_stretch_the_force_budget(backend):
    build, adapter = backend

    class _Stubborn(_InOurGroup):
        def wait(self, timeout=None):
            assert timeout is not None and 0 < timeout <= 1.5
            raise subprocess.TimeoutExpired("x", timeout)

    adapter.force_stop(build(_Stubborn()))
