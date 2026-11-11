"""CLI-probe utilities for BlindPilot's backends.

Volatile code that discovers CLIs, checks auth, and queries usage lives here.
Stable registry content lives in ``backend_registry``.  ``agent_backends``
re-exports everything from both modules for backwards compatibility.

Copyright (c) 2026 doubletaponair and BlindPilot contributors.
Based on the original Claude Code Reader application by doubletaponair:
https://github.com/doubletaponair/claude-code-reader
SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import datetime
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Callable, NamedTuple, Optional, Sequence

import process_utils

from backend_registry import (
    BACKEND_ANTIGRAVITY,
    BACKEND_CLAUDE,
    BACKEND_CODEX,
    BACKEND_COMMANDCODE,
    BACKEND_COPILOT,
    BACKEND_FREEBUFF,
    BACKEND_GEMINI,
    BACKEND_HERMES,
    BACKEND_MUSE,
    BACKEND_MUSEAI,
    BACKEND_OPENCODE,
    BACKENDS,
    FREEBUFF_PREFERRED_MODEL,
    BackendAdapter,
    normalize_backend,
    _antigravity_worker,
    _claude_worker,
    _commandcode_worker,
    _copilot_worker,
    _gemini_worker,
    _hermes_worker,
    _muse_worker,
    _museai_worker,
    _strip_terminal_noise,
)

_FREEBUFF_SETTINGS_LOCK = threading.Lock()
_freebuff_catalog_cache: tuple[tuple[str, float, int], list[str]] | None = None


def _fallback_cli_paths(name: str) -> tuple[Path, ...]:
    home = Path.home()
    managed = blindpilot_data_dir() / "npm"
    managed_bin = managed if platform.system() == "Windows" else managed / "bin"
    if platform.system() == "Windows":
        appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        candidates: list[Path] = []
        # No .ps1: Popen cannot run one (WinError 193), so offering it would
        # fail every turn where finding nothing lets the wizard offer to install.
        for suffix in (".exe", ".cmd", ""):
            filename = name + suffix
            candidates.extend(
                [
                    managed_bin / filename,
                    appdata / "npm" / filename,
                    home / ".local" / "bin" / filename,
                    home / ".volta" / "bin" / filename,
                    local / "Microsoft" / "WinGet" / "Links" / filename,
                    local / "Programs" / name / filename,
                    # Antigravity's installer puts agy.exe here.
                    local / name / "bin" / filename,
                ]
            )
        return tuple(candidates)
    return (
        managed_bin / name,
        home / ".local" / "bin" / name,
        Path("/opt/homebrew/bin") / name,
        Path("/usr/local/bin") / name,
        home / ".npm-global" / "bin" / name,
        home / ".volta" / "bin" / name,
    )


def find_backend_cli(backend: str) -> Optional[str]:
    """Find a provider CLI even in the restricted PATH inherited by a GUI."""
    info = BACKENDS[normalize_backend(backend)]
    if info.id == BACKEND_HERMES:
        # Hermes installs itself under its own home directory rather than into
        # a shared bin, so it has a search of its own that also knows about the
        # virtual environment the gateway is launched from. It is not an npm
        # package, so it returns before the managed-prefix search below.
        from hermes_backend import find_hermes_cli

        return find_hermes_cli()
    if info.id == BACKEND_MUSE:
        # Muse's launcher is a bash script that only runs on macOS and Linux,
        # and on this machine that means inside WSL. The ordinary fallback
        # search would happily find the same-named launcher a user copied into
        # ~/.local/bin on the Windows side -- a file Popen cannot run -- and
        # then every turn would die on WinError 193, so Muse answers from its
        # own adapter, which knows about WSL, before any path-based search.
        from muse_backend import muse_cli_path

        return muse_cli_path()
    # A backend BlindPilot installed itself is complete, writable by this user,
    # and updated through the same prefix. Prefer it over an older system/npm
    # copy that happens to occur earlier on PATH.
    managed = blindpilot_data_dir() / "npm"
    managed_bin = managed if platform.system() == "Windows" else managed / "bin"
    suffixes = (".exe", ".cmd", "") if platform.system() == "Windows" else ("",)
    for suffix in suffixes:
        candidate = managed_bin / f"{info.executable}{suffix}"
        if candidate.is_file():
            return str(candidate)
    found = process_utils.which_without_cwd(info.executable)
    if found:
        return found
    for candidate in _fallback_cli_paths(info.executable):
        if candidate.is_file():
            return str(candidate)
    if platform.system() != "Windows":
        shell = os.environ.get("SHELL")
        if shell and os.path.isfile(shell):
            try:
                proc = subprocess.run(
                    [shell, "-l", "-c", f"command -v {info.executable}"],
                    capture_output=True,
                    text=True,
                    timeout=8,
                )
                result = proc.stdout.strip().splitlines()
                if proc.returncode == 0 and result and os.path.isfile(result[0]):
                    return result[0]
            except (OSError, subprocess.TimeoutExpired):
                pass
    return None


def _muse_version_probe() -> str:
    """Muse's version through its own bridge, patchable like the rest.

    Same indirection as `_muse_signed_in_checked`: the tests that loop over
    every backend patch this rather than booting the real WSL distribution
    this machine happens to have.
    """
    from muse_backend import muse_version

    return muse_version()


def _muse_signed_in_checked() -> bool:
    """The Muse signed-in answer, behind an indirection the suite can patch.

    Hermes gets the same treatment: a name in this module's own namespace is
    what monkeypatch can reach, and the tests that loop over every backend
    must never reach into the real WSL distribution on a machine that has one.
    """
    from muse_backend import muse_signed_in

    return muse_signed_in()


def backend_auth_ok(backend: str, timeout: int = 12) -> bool:
    """Best-effort non-interactive authentication check.

    Each backend answers it in the way it can -- a CLI subcommand, a stored
    credential, a gateway handshake -- and its adapter holds that answer in one
    place rather than in a chain of comparisons here.
    """
    try:
        return _adapter(backend).auth_ok(timeout)
    except (OSError, subprocess.TimeoutExpired):
        return False


def _freebuff_account() -> Optional[dict]:
    """The account FreeBuff stored at sign-in, or None if there is none to read."""
    credential = Path.home() / ".config" / "manicode" / "credentials.json"
    try:
        payload = json.loads(credential.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    account = payload.get("default") if isinstance(payload, dict) else None
    if (
        isinstance(account, dict)
        and not account.get("authToken")
        and account.get("tokenStore") == "keychain"
    ):
        account["authToken"] = _freebuff_keychain_token(credential)
    return account if isinstance(account, dict) else None


def _freebuff_keychain_token(credential: Path) -> str:
    """Read Bun.secrets' FreeBuff entry, without migrating it back to JSON."""
    from accessible_ai.storage.credentials import CredentialStore

    try:
        if platform.system() == "Windows":
            return CredentialStore._windows_read(f"freebuff-cli/{credential}", encoding="utf-8")
        if platform.system() == "Darwin":
            return CredentialStore._fallback_read(str(credential), "freebuff-cli")
        import secretstorage

        connection = secretstorage.dbus_init()
        try:
            for item in secretstorage.search_items(
                connection, {"service": "freebuff-cli", "account": str(credential)}
            ):
                if not item.is_locked():
                    return item.get_secret().decode("utf-8")
        finally:
            connection.close()
    except Exception:
        # A locked or unavailable secure store cannot confirm a sign-in.
        return ""
    return ""


def _freebuff_signed_in(account: Optional[dict]) -> bool:
    fields = (
        ("authToken", "email")
        if account and account.get("email")
        else ("authToken", "fingerprintId", "fingerprintHash")
    )
    return account is not None and all(
        isinstance(account.get(field), str) and account[field].strip() for field in fields
    )


def _opencode_data_dir() -> Path:
    """Where opencode keeps its database and credentials, on every platform."""
    override = os.environ.get("OPENCODE_DATA")
    if override:
        return Path(override)
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "opencode"


def _opencode_providers() -> list[str]:
    """The providers opencode could run a model on, read without starting it.

    Answered from the credentials opencode stored when a provider was
    connected, which is where /connect puts them, plus its own key in the
    environment. This has to answer without starting a server, because it is
    asked while the setup wizard is on screen.

    Deliberately not "is any ``*_API_KEY`` set": opencode does read a provider's
    key straight out of the environment, but so do plenty of programs that have
    nothing to do with it, and a confident yes that turns into a wall at the
    first message is worse than an "unconfirmed" the wizard lets you walk past.
    """
    providers: list[str] = []
    try:
        payload = json.loads((_opencode_data_dir() / "auth.json").read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            providers = sorted(str(name) for name in payload)
    except (OSError, ValueError):
        providers = []
    if os.environ.get("OPENCODE_API_KEY", "").strip():
        providers.append("OPENCODE_API_KEY in the environment")
    return providers


def _probe_backend(binary: str, args: list[str], timeout: int) -> tuple[Optional[int], str]:
    """Run a short provider command. Returns (exit code, what it printed).

    The exit code is ``None`` when the command could not be run at all, which
    is a different answer from "it ran and said no" and is reported as such.
    """
    try:
        proc = subprocess.run(
            [binary, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env={**subprocess_env(binary), "PYTHONIOENCODING": "utf-8"},
            **process_utils.no_window_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, ""
    # These CLIs draw their own output, so what comes back is wrapped in the
    # escape sequences that would otherwise be spelled out letter by letter.
    stdout = _strip_terminal_noise(proc.stdout)
    stderr = _strip_terminal_noise(proc.stderr)
    # A CLI that painted a banner on stdout and failed on stderr used to hand
    # back only the banner; when both streams have something to say, both are
    # kept, because the error is the half the caller needs.
    return proc.returncode, f"{stdout}\n{stderr}" if stdout and stderr else stdout or stderr


def _claude_account_lines(code: Optional[int], text: str) -> list[str]:
    """Read `claude auth status`, which answers in JSON."""
    if code is None:
        return ["Signed in: could not ask Claude Code"]
    payload: object = None
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            payload = json.loads(text[start : end + 1])
        except ValueError:
            payload = None
    if not isinstance(payload, dict):
        # A future release that stops answering in JSON still has something to
        # say, and its own words are better than a shrug.
        return [f"Signed in: {'yes' if code == 0 else 'no'}"] + ([text] if text else [])
    lines = [f"Signed in: {'yes' if payload.get('loggedIn') else 'no'}"]
    for field, caption in (
        ("email", "Account"),
        ("subscriptionType", "Subscription"),
        ("authMethod", "Signed in with"),
        ("orgName", "Organisation"),
    ):
        value = str(payload.get(field) or "").strip()
        if value:
            lines.append(f"{caption}: {value}")
    return lines


def _codex_account_lines(code: Optional[int], text: str) -> list[str]:
    """Read `codex login status`, which answers in one sentence."""
    if code is None:
        return ["Signed in: could not ask Codex"]
    lines = [f"Signed in: {'yes' if code == 0 else 'no'}"]
    if text:
        lines.append(f"Account: {' '.join(text.split())}")
    return lines


def _freebuff_account_lines() -> list[str]:
    """FreeBuff has no status command; its stored credentials are the answer."""
    account = _freebuff_account()
    if account is None:
        return ["Signed in: no"]
    lines = [f"Signed in: {'yes' if _freebuff_signed_in(account) else 'no'}"]
    for field, caption in (("name", "Account"), ("email", "Email")):
        value = str(account.get(field) or "").strip()
        if value:
            lines.append(f"{caption}: {value}")
    return lines


def _opencode_account_lines() -> list[str]:
    """opencode has no status command either; its stored credentials answer.

    Read off disk rather than asked of its server, because /status should not
    be the thing that starts one — a report on what is already set up has no
    business spending ten seconds bringing a server to life to say so.
    """
    providers = _opencode_providers()
    if not providers:
        return ["Signed in: no", "Connected providers: none"]
    return ["Signed in: yes", f"Connected providers: {', '.join(providers)}"]


# ----- Plan usage limits -----
#
# Every backend that meters an account of its own is asked how much of it is
# left, and each is asked in the way it can answer:
#
#   Claude Code  fixed windows -- five-hour, weekly, sometimes weekly for one
#                model -- reported as a percentage and a reset time. There is
#                no command line for it: it answers a `get_usage` control
#                request on the same stream-json channel a turn is driven over.
#   Codex        the same shape, from the `account/rateLimits/read` method on
#                its app-server, reusing the pooled one where a tab has it.
#   FreeBuff     a credit balance rather than a window, spent down over a cycle
#                that refills on a date. Its CLI has no command for this
#                either; the balance comes from the same account endpoint its
#                own usage banner reads, with the credentials it signed in
#                with.
#   Hermes       a pool of provider credentials rather than one account, so
#                what it meters is which of them are spent. It writes each
#                credential's last outcome to the pool file, and a spent one
#                carries the time it may be used again where the provider
#                said so.
#   opencode     nothing of its own. It spends whichever provider account is
#                connected, its server offers no route that reports one, and
#                the rate-limit headers it does read are used to decide a
#                retry rather than kept anywhere that could be asked. So it
#                reports nothing, rather than a number that would be a guess.
#   Command Code nothing of its own either: usage is behind the interactive
#                /usage command and the billing website, and the headless
#                surface has no command that reports it.
#
# What is metered is the account's business, not BlindPilot's. A plan may have
# a five-hour window and no weekly one, a weekly window for one model and not
# another, or none at all while the session runs on an API key, on Bedrock or
# on Vertex. A limit the backend does not report is left out rather than
# written as unknown, and an account that reports none at all leaves the status
# report with no usage section, which is what says there is nothing to show.


@dataclass(frozen=True)
class UsageWindow:
    """One metered window of a subscription, as the backend reports it.

    `minutes` is how long the window is, which is only ever used to order the
    lines: a listener wants the window that empties soonest first, and the
    backends hand them over in the order that suits their own protocol.

    `percent` is how full the window is where the backend measures it that way,
    and None where it does not. Not every backend does: FreeBuff counts credits
    off a balance and Hermes reports a credential as spent or not, so those say
    what they know in `detail` instead of inventing a fraction to say it in.
    """

    label: str
    percent: Optional[float] = None
    resets_at: Optional[float] = None
    minutes: Optional[float] = None
    scoped: bool = False
    detail: str = ""


def _as_number(value: object) -> Optional[float]:
    """A number the backend really sent, or None. Booleans are not numbers."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _epoch_from_iso(value: object) -> Optional[float]:
    """Claude Code times a window's end as an ISO 8601 string; Codex uses epoch."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        when = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    if when.tzinfo is None:
        # A time with no zone on it is the server's, and the server speaks UTC.
        when = when.replace(tzinfo=datetime.timezone.utc)
    try:
        return when.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def _until_words(seconds: float) -> str:
    """How long until a window empties, in words rather than a bare timestamp.

    A time of day alone makes the listener do the arithmetic, and "resets at
    08:50" is no help at all to somebody who does not already know what time it
    is now.
    """
    if seconds <= 0:
        return "now"
    minutes = max(1, int(seconds // 60))
    if minutes < 60:
        return f"in {minutes} minute{'' if minutes == 1 else 's'}"
    hours, rest = divmod(minutes, 60)
    if hours < 24:
        if rest:
            return (
                f"in {hours} hour{'' if hours == 1 else 's'} "
                f"{rest} minute{'' if rest == 1 else 's'}"
            )
        return f"in {hours} hour{'' if hours == 1 else 's'}"
    days, hours = divmod(hours, 24)
    if hours:
        return f"in {days} day{'' if days == 1 else 's'} {hours} hour{'' if hours == 1 else 's'}"
    return f"in {days} day{'' if days == 1 else 's'}"


def _reset_words(epoch: Optional[float]) -> str:
    """When the window empties, said in local time and as a wait."""
    if epoch is None:
        return ""
    try:
        when = datetime.datetime.fromtimestamp(epoch).astimezone()
    except (OverflowError, OSError, ValueError):
        return ""
    return f"resets {when.strftime('%a %d %b %H:%M')} ({_until_words(epoch - time.time())})"


def _percent_words(percent: float) -> str:
    """A fraction of a window, rounded the way it is worth saying out loud."""
    if percent <= 0:
        return "0% used"
    if percent < 1:
        return "under 1% used"
    return f"{percent:.0f}% used"


def _usage_window_line(window: UsageWindow) -> str:
    parts = [window.detail] if window.detail else []
    if window.percent is not None:
        parts.append(_percent_words(window.percent))
    reset = _reset_words(window.resets_at)
    if reset:
        parts.append(reset)
    if not parts:
        # Nothing but a name is not a report, and a line ending in a colon
        # reads as a value that went missing.
        return ""
    return f"{window.label}: {', '.join(parts)}"


def _ordered_windows(windows: Sequence[UsageWindow]) -> list[UsageWindow]:
    """Shortest window first, and the account's own windows before one model's.

    Neither backend hands them over in an order worth reading out: Codex sends
    a map keyed by limit id, and a plan that meters one model separately would
    otherwise put that model's weekly window above the account's own.
    """
    return sorted(windows, key=lambda window: (window.minutes or 0.0, window.scoped))


# Claude Code fetches the utilization figures in the background, so the first
# answer after a process starts often carries `rate_limits: null` -- not "this
# account has no windows", just "not fetched yet". The question is asked again
# a couple of times before that is believed. Measured: the fetch has landed
# within a second or two of start-up.
_CLAUDE_USAGE_ATTEMPTS = 3
_CLAUDE_USAGE_RETRY_SECONDS = 2.0

_CLAUDE_USAGE_WINDOWS = (
    ("five_hour", "Five-hour limit", 300.0),
    ("seven_day", "Weekly limit", 10080.0),
    ("seven_day_opus", "Weekly Opus limit", 10080.0),
    ("seven_day_sonnet", "Weekly Sonnet limit", 10080.0),
)


# What has been said already, so a warning that every turn repeats is heard
# once per window: a new window, or the limit being reached, says it again.
_CLAUDE_LIMITS_SAID: set = set()


def claude_limit_warning(info: object) -> str:
    """The line to say for a stream-json `rate_limit_event`, or "".

    Claude Code sends one with every turn. Only `allowed_warning` (past the
    plan's warning threshold) and `rejected` (the limit is reached) are worth
    interrupting for, and each only once per window.
    """
    if not isinstance(info, dict):
        return ""
    status = info.get("status")
    if status not in ("allowed_warning", "rejected"):
        return ""
    kind = info.get("rateLimitType")
    labels = {field: label for field, label, _minutes in _CLAUDE_USAGE_WINDOWS}
    label = labels.get(kind) if isinstance(kind, str) else None
    resets = _as_number(info.get("resetsAt"))
    key = (kind, resets, status)
    if key in _CLAUDE_LIMITS_SAID:
        return ""
    _CLAUDE_LIMITS_SAID.add(key)
    used = _as_number(info.get("utilization"))
    window = UsageWindow(
        label or "Usage limit",
        100.0 if status == "rejected" else (used * 100 if used is not None else None),
        resets,
    )
    line = _usage_window_line(window) or window.label
    if status == "rejected":
        return f"Claude Code {line}. The limit is reached."
    return f"Claude Code {line}."


def _claude_usage_windows(payload: Optional[dict]) -> list[UsageWindow]:
    """Read the `get_usage` answer. Anything not reported is left out."""
    if not isinstance(payload, dict):
        return []
    if payload.get("rate_limits_available") is False:
        # An API key, Bedrock or Vertex session: the plan windows do not apply
        # to it at all, and `rate_limits` is null for that reason rather than
        # because the figures have not arrived.
        return []
    limits = payload.get("rate_limits")
    if not isinstance(limits, dict):
        return []
    windows: list[UsageWindow] = []
    for field, label, minutes in _CLAUDE_USAGE_WINDOWS:
        entry = limits.get(field)
        if not isinstance(entry, dict):
            continue
        percent = _as_number(entry.get("utilization"))
        if percent is None:
            continue
        windows.append(
            UsageWindow(label, percent, _epoch_from_iso(entry.get("resets_at")), minutes)
        )
    # Weekly windows the server names itself, for the models a plan meters
    # separately. Additive, and only ever present when the server sends them.
    scoped = limits.get("model_scoped")
    if isinstance(scoped, list):
        for entry in scoped:
            if not isinstance(entry, dict):
                continue
            percent = _as_number(entry.get("utilization"))
            name = str(entry.get("display_name") or "").strip()
            if percent is None or not name:
                continue
            windows.append(
                UsageWindow(
                    f"Weekly {name} limit",
                    percent,
                    _epoch_from_iso(entry.get("resets_at")),
                    10080.0,
                    scoped=True,
                )
            )
    return windows


def _claude_usage_reply(stdout: IO[str], request_id: str) -> Optional[dict]:
    """Read the stream until this control request is answered, or it ends."""
    for raw in stdout:
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "control_response":
            continue
        response = event.get("response")
        if not isinstance(response, dict) or response.get("request_id") != request_id:
            continue
        if response.get("subtype") != "success":
            # A release that does not know this request says so rather than
            # answering it, and there is nothing to report.
            return None
        return response.get("response")
    return None


def _claude_usage_exchange(proc: subprocess.Popen[str]) -> Optional[dict]:
    """Ask a running process for its usage, retrying while the fetch lands."""
    stdin, stdout = proc.stdin, proc.stdout
    if stdin is None or stdout is None:
        return None
    payload: Optional[dict] = None
    for attempt in range(_CLAUDE_USAGE_ATTEMPTS):
        request_id = uuid.uuid4().hex
        try:
            stdin.write(
                json.dumps(
                    {
                        "type": "control_request",
                        "request_id": request_id,
                        "request": {"subtype": "get_usage", "skip_behaviors": True},
                    }
                )
                + "\n"
            )
            stdin.flush()
        except (OSError, ValueError):
            return payload
        answer = _claude_usage_reply(stdout, request_id)
        if answer is None:
            return payload
        payload = answer
        if _claude_usage_windows(payload) or answer.get("rate_limits_available") is False:
            return payload
        if attempt + 1 < _CLAUDE_USAGE_ATTEMPTS:
            time.sleep(_CLAUDE_USAGE_RETRY_SECONDS)
    return payload


def _claude_usage_payload(binary: str, timeout: int) -> Optional[dict]:
    """Ask a short-lived Claude Code process what the plan windows look like.

    A process of its own rather than the tab's: the pooled one belongs to a
    conversation that may be mid-turn, and the report is asked for from the
    window's own thread. `--no-session-persistence` keeps the question out of
    the conversation list, where a session holding nothing but a control
    request would be offered as something to resume.

    `skip_behaviors` skips a scan of every transcript on this machine. That
    scan is what the CLI's own usage dialog draws its attribution from, and
    none of it is wanted here.
    """
    command = [
        binary,
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--verbose",
        "--no-session-persistence",
    ]
    try:
        proc = subprocess.Popen(
            command,
            cwd=str(Path.home()),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
            env=subprocess_env(binary),
            **process_utils.own_group_kwargs(),
            **process_utils.no_window_kwargs(),
        )
    except (OSError, ValueError):
        return None
    # Reading a pipe has no deadline of its own, so the deadline is the
    # process: ending it is what turns a silent CLI into an end of stream
    # rather than a status dialog that never opens.
    watchdog = threading.Timer(
        max(1.0, float(timeout)), lambda: process_utils.end_process_group(proc)
    )
    watchdog.daemon = True
    watchdog.start()
    try:
        return _claude_usage_exchange(proc)
    finally:
        watchdog.cancel()
        process_utils.end_process_group(proc)
        # Ending the process does not hand its pipes back. /status can be asked
        # as often as somebody presses it, and two file handles a press is a
        # leak in an application that stays open all day.
        for pipe in (proc.stdin, proc.stdout):
            if pipe is not None:
                try:
                    pipe.close()
                except (OSError, ValueError):
                    pass
        try:
            proc.wait(timeout=5)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass


_CODEX_WINDOW_FIELDS = ("primary", "secondary")


def _codex_window_name(minutes: Optional[float], field: str = "") -> str:
    """What to call a window of this length.

    Codex names its windows only by how long they are, and which of primary
    and secondary is the five-hour one differs by account: on a plan whose
    default limit is weekly, primary *is* the weekly window. So the length is
    what the line is named after, and the position is fallen back on only
    where the length is missing -- two windows of one limit both reported
    without a duration have nothing else left to tell them apart.
    """
    if minutes is None:
        return field.capitalize() if field else "Usage"
    length = int(minutes)
    if length == 300:
        return "Five-hour"
    if length == 10080:
        return "Weekly"
    if length % 1440 == 0:
        days = length // 1440
        return f"{days}-day"
    if length % 60 == 0:
        hours = length // 60
        return f"{hours}-hour"
    return f"{length}-minute"


def _codex_limit_identity(group: dict) -> tuple:
    """What makes one snapshot the same metered limit as another.

    The figures themselves, rather than what the lines would be called: two
    genuinely different windows can end up under the same name -- a limit with
    no `limitName` and no `windowDurationMins` on either window is named twice
    over by its position alone -- and telling those apart by name would lose
    one of them.
    """
    parts = [str(group.get("limitName") or "").strip()]
    for field in _CODEX_WINDOW_FIELDS:
        entry = group.get(field)
        if not isinstance(entry, dict):
            parts.append("")
            continue
        parts.append(
            "/".join(
                str(_as_number(entry.get(key)))
                for key in ("usedPercent", "windowDurationMins", "resetsAt")
            )
        )
    return tuple(parts)


def _codex_usage_windows(result: object) -> list[UsageWindow]:
    """Read `account/rateLimits/read`. Anything not reported is left out."""
    if not isinstance(result, dict):
        return []
    groups: list[dict] = []
    # The per-limit map first: it is keyed by the metered limit's own id, and
    # it names the windows a plan meters for one model.
    identities: set[tuple] = set()
    by_limit = result.get("rateLimitsByLimitId")
    if isinstance(by_limit, dict):
        for entry in by_limit.values():
            if isinstance(entry, dict):
                groups.append(entry)
                identities.add(_codex_limit_identity(entry))
    # `rateLimits` is the account's default snapshot, which is one of the
    # limits above again under a different key rather than a window of its own.
    # It is added only where the map did not already carry the same figures.
    default = result.get("rateLimits")
    if isinstance(default, dict) and _codex_limit_identity(default) not in identities:
        groups.append(default)
    windows: list[UsageWindow] = []
    for group in groups:
        name = str(group.get("limitName") or "").strip()
        for field in _CODEX_WINDOW_FIELDS:
            entry = group.get(field)
            if not isinstance(entry, dict):
                continue
            percent = _as_number(entry.get("usedPercent"))
            if percent is None:
                continue
            minutes = _as_number(entry.get("windowDurationMins"))
            base = _codex_window_name(minutes, field)
            label = f"{base} {name} limit" if name else f"{base} limit"
            windows.append(
                UsageWindow(
                    label,
                    percent,
                    _as_number(entry.get("resetsAt")),
                    minutes,
                    scoped=bool(name),
                )
            )
    return windows


def _codex_usage_result(binary: str, timeout: int) -> object:
    """Ask Codex for the account's windows; delegates to codex_worker."""
    import codex_worker as _cw

    return _cw._codex_usage_result(binary, timeout)


# FreeBuff's own banner reads this, and reaching it is one request against an
# account that is already signed in, so the report does not wait on a CLI to
# start. The app URL is the one its CLI compiles in, overridable by the same
# environment variable the CLI reads, for anyone pointed at another deployment.
_FREEBUFF_APP_URL = "https://www.codebuff.com"
_FREEBUFF_USAGE_PATH = "/api/v1/usage"
# A cycle runs a month, which is only ever used to order the lines.
_FREEBUFF_CYCLE_MINUTES = 43200.0


def _freebuff_app_url() -> str:
    return (os.environ.get("NEXT_PUBLIC_CODEBUFF_APP_URL", "").strip() or _FREEBUFF_APP_URL).rstrip(
        "/"
    )


def _freebuff_usage_payload(timeout: int) -> Optional[dict]:
    """Ask FreeBuff's account endpoint what this balance looks like.

    Signed out there is nothing to ask with, and asking anyway would be a
    request that can only come back rejected.
    """
    import urllib.error
    import urllib.request

    account = _freebuff_account()
    if account is None or not _freebuff_signed_in(account):
        return None
    body = json.dumps(
        {
            "fingerprintId": str(account.get("fingerprintId") or ""),
            "authToken": str(account.get("authToken") or ""),
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        _freebuff_app_url() + _FREEBUFF_USAGE_PATH,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": f"BlindPilot/{_app_version()}"},
        method="POST",
    )
    try:
        # The packaged trust store, not OpenSSL's guess: a frozen
        # build's empty store would fail this call like any other.
        from certificates import open_url

        with open_url(request, timeout=max(1, min(timeout, 10))) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (OSError, ValueError, urllib.error.HTTPError):
        return None
    return payload if isinstance(payload, dict) else None


def _credit_words(amount: float) -> str:
    count = int(round(amount))
    return f"{count:,} credit{'' if count == 1 else 's'}"


def _freebuff_usage_windows(payload: Optional[dict]) -> list[UsageWindow]:
    """Read the balance. What is left is the figure; the fraction is derived.

    The endpoint reports what has been spent this cycle and what is left, and
    what is left is the number worth hearing first -- a percentage of a balance
    that top-ups and referrals move around is the less honest half of it, so it
    is only added where the two figures together say what the cycle held.
    """
    if not isinstance(payload, dict):
        return []
    used = _as_number(payload.get("usage"))
    remaining = _as_number(payload.get("remainingBalance"))
    if used is None and remaining is None:
        return []
    said = []
    if remaining is not None:
        said.append(f"{_credit_words(remaining)} left")
    if used is not None:
        said.append(f"{_credit_words(used)} used this cycle")
    granted = (used or 0.0) + (remaining or 0.0)
    percent = used / granted * 100.0 if used is not None and granted > 0 else None
    return [
        UsageWindow(
            "Credits",
            percent,
            _epoch_from_iso(payload.get("next_quota_reset")),
            _FREEBUFF_CYCLE_MINUTES,
            detail=", ".join(said),
        )
    ]


# What Hermes writes against a credential once it has stopped taking work. Any
# other value is a credential that is still spending, and a pool that is all
# still spending has nothing to report.
_HERMES_SPENT_STATUSES = ("exhausted", "rate_limited", "cooldown", "disabled")


def _hermes_limit_words(credential: dict) -> str:
    """Why this credential stopped, in words rather than the provider's code.

    A rate limit and an empty wallet both stop a credential, and they are not
    the same news: one comes back on its own and the other does not.
    """
    reason = str(credential.get("last_error_reason") or "").strip()
    lowered = reason.lower()
    code = _as_number(credential.get("last_error_code"))
    if code == 429 or "rate" in lowered or "usage_limit" in lowered or "quota" in lowered:
        return "rate limit reached"
    if "credit" in lowered or "balance" in lowered or "billing" in lowered or "payment" in lowered:
        return "out of credits"
    return f"unavailable ({reason})" if reason else "unavailable"


def _hermes_usage_windows(payload: Optional[dict]) -> list[UsageWindow]:
    """Read Hermes' credential pool for the ones it has stopped spending.

    Hermes runs on a pool rather than on one account, and a pool has no single
    figure to be a percentage of. What it does know is which credentials are
    spent and, where the provider said so, when each may be used again -- so
    that is what is reported, and a pool with nothing spent reports nothing.
    """
    if not isinstance(payload, dict):
        return []
    pool = payload.get("credential_pool")
    if not isinstance(pool, dict):
        return []
    windows: list[UsageWindow] = []
    for provider, credentials in sorted(pool.items()):
        if not isinstance(credentials, list):
            continue
        for credential in credentials:
            if not isinstance(credential, dict):
                continue
            status = str(credential.get("last_status") or "").strip().lower()
            if status not in _HERMES_SPENT_STATUSES:
                continue
            label = str(credential.get("label") or "").strip()
            named = f"{provider} ({label})" if label and label != provider else str(provider)
            # "Provider" in front of it, because these sit under the account
            # lines in the report and a bare credential name there reads as a
            # heading rather than as a limit that has been reached.
            name = f"Provider {named}"
            windows.append(
                UsageWindow(
                    name,
                    None,
                    _as_number(credential.get("last_error_reset_at")),
                    detail=_hermes_limit_words(credential),
                )
            )
    return windows


def _hermes_pool_payload() -> Optional[dict]:
    """The pool file Hermes keeps its credential outcomes in.

    Read off disk rather than asked of the CLI: `hermes auth list` prints this
    same state, but starting Hermes to print it costs seconds the status report
    should not spend. A Hermes reached over the network keeps its own pool on
    its own machine, and this file is the local one -- the same caveat the
    settings entry for Hermes' config carries.
    """
    try:
        payload = json.loads((_hermes_home() / "auth.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def backend_usage_lines(backend: str, binary: str, timeout: int = 20) -> list[str]:
    """How much of this account's metered allowance is left, as report lines.

    Empty for opencode, which meters nothing of its own, and empty for an
    account whose backend reports no limits: there is nothing truthful to say
    about a limit the backend has not named, and a line saying so would be one
    more thing to arrow past on every other account.
    """
    return _adapter(backend).usage_lines(binary, timeout)


def _hermes_account_lines() -> list[str]:
    """Hermes signs in to a pool of providers, and the pool file is the answer.

    Its `hermes status` does print this, among four screens of everything else
    it knows, and starting Hermes to read it costs seconds. What signing in
    means here is also not one account: Hermes holds a credential per provider
    and moves between them, so what the report names is the provider it is set
    to use and the ones it could fall back to.
    """
    payload = _hermes_pool_payload()
    pool = payload.get("credential_pool") if isinstance(payload, dict) else None
    providers = sorted(
        str(name)
        for name, credentials in (pool or {}).items()
        if isinstance(credentials, list) and credentials
    )
    if not providers:
        return ["Signed in: no", "Connected providers: none"]
    lines = ["Signed in: yes", f"Connected providers: {', '.join(providers)}"]
    active = str((payload or {}).get("active_provider") or "").strip()
    if active:
        lines.insert(1, f"Provider in use: {active}")
    return lines


def _muse_usage_payload() -> object:
    """The usage one of this app's Muse turns last observed, behind the same
    indirection the other Muse answers use so the suite can patch it."""
    from muse_backend import muse_usage_payload

    return muse_usage_payload()


def _muse_usage_windows(payload: object) -> list[UsageWindow]:
    """Read ``usage/read``: a current window and a rolling weekly block.

    Both carry the provider's verbatim used percent with the reset stamp in
    epoch milliseconds; the weekly block has no duration of its own.
    """
    if not isinstance(payload, dict):
        return []
    windows: list[UsageWindow] = []
    for field, fallback in (("window", "Session"), ("weekly", "Weekly")):
        entry = payload.get(field)
        if not isinstance(entry, dict):
            continue
        percent = _as_number(entry.get("usedPercent"))
        if percent is None:
            continue
        resets_at = _as_number(entry.get("resetsAtMs"))
        minutes = _as_number(entry.get("windowDurationMins"))
        base = _codex_window_name(minutes, field) if minutes is not None else fallback
        windows.append(
            UsageWindow(
                f"{base} limit",
                percent,
                resets_at / 1000 if resets_at is not None else None,
                minutes,
            )
        )
    return windows


def _muse_account_lines() -> list[str]:
    """Muse signs in with one Meta account, and what it stored is the answer.

    Muse has no auth-status subcommand, so the credential file is read the
    way FreeBuff's is. The reading itself lives in muse_backend, which knows
    whether the file is on this side of a WSL boundary or not.
    """
    from muse_backend import muse_account_lines

    return muse_account_lines()


def backend_status(backend: str, timeout: int = 20) -> str:
    """What the chosen backend can say about itself, as lines of plain text.

    This is what ``/status`` reports, and every backend answers it. None of
    them answers it themselves in the headless mode BlindPilot drives them in:
    Claude Code's own ``/status`` is interactive-only and replies "/status
    isn't available in this environment" when it is sent as a message, and
    Codex, FreeBuff and opencode have no status command at all, and Hermes'
    prints four screens of everything it knows. So each one is asked in the way
    it can actually answer — a CLI subcommand where there is one, the
    credentials it stored where there is not — and the answers are written the
    same way, so the report reads the same whichever backend is selected.
    """
    backend = normalize_backend(backend)
    from backend_registry import backend_label

    lines = [f"Backend: {backend_label(backend)}"]
    binary = find_backend_cli(backend)
    if not binary:
        lines.append("Command line: not installed")
        return "\n".join(lines)
    lines.append(f"Command line: {binary}")
    if backend == BACKEND_MUSE:
        # The "binary" is a bash launcher inside WSL on Windows; Popen cannot
        # run it from here, so the version is asked through Muse's own bridge.
        version = _muse_version_probe()
        if version:
            lines.append(f"Version: {version.splitlines()[0].strip()}")
    else:
        _code, version = _probe_backend(binary, ["--version"], min(timeout, 20))
        if version:
            lines.append(f"Version: {version.splitlines()[0].strip()}")
    lines.extend(_adapter(backend).account_lines(binary, timeout))
    lines.extend(backend_usage_lines(backend, binary, timeout))
    return "\n".join(lines)


# A macOS application launched from Finder or the Dock inherits launchd's PATH
# — /usr/bin:/bin:/usr/sbin:/sbin — and nothing else. Every provider CLI
# installed by npm or Homebrew is a `#!/usr/bin/env node` shim, so a child
# started with that PATH dies on "env: node: No such file or directory" before
# it prints anything a person could act on. Asking the login shell what PATH it
# would have given is the only way to recover the directories the user actually
# installed into. It costs a shell start-up, so it is asked once.
_login_shell_path: Optional[list[str]] = None
_LOGIN_SHELL_PATH_LOCK = threading.Lock()


def login_shell_path_dirs() -> list[str]:
    """The PATH a terminal would have, for handing to children (POSIX only)."""
    global _login_shell_path
    with _LOGIN_SHELL_PATH_LOCK:
        if _login_shell_path is not None:
            return list(_login_shell_path)
        dirs: list[str] = []
        shell = os.environ.get("SHELL")
        if platform.system() != "Windows" and shell and os.path.isfile(shell):
            try:
                proc = subprocess.run(
                    [shell, "-l", "-c", "printf '%s\\n' \"$PATH\""],
                    capture_output=True,
                    text=True,
                    timeout=8,
                )
                if proc.returncode == 0:
                    dirs = [
                        entry for entry in proc.stdout.strip().split(os.pathsep) if entry.strip()
                    ]
            # A login shell runs the user's own startup files and can fail in
            # ways nothing here can predict. This is a best-effort addition to
            # a PATH that already works for most installs, so a shell that
            # misbehaves costs the addition and nothing else.
            except Exception:
                dirs = []
        _login_shell_path = dirs
        return list(dirs)


class SettingsFile(NamedTuple):
    """One file that configures one backend, and how to describe it.

    `scope` and `note` are both spoken. They are not decoration: the two
    project-level Claude Code files differ only in whether the repository
    carries them, and opening the wrong one silently is how somebody's personal
    settings end up committed to a repository that is not theirs.
    """

    backend: str
    scope: str
    path: Path
    note: str

    @property
    def exists(self) -> bool:
        try:
            return self.path.is_file()
        except OSError:
            return False


def _codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def _hermes_home() -> Path:
    """Where Hermes keeps its configuration, honouring its own override.

    `HERMES_HOME` is how one machine runs several Hermes profiles side by side,
    so reading it is the difference between naming the file somebody is actually
    using and naming a default they abandoned.
    """
    # One rule for the whole app. hermes_backend strips and expands the
    # override; resolving it a second way here meant auth.json and config.yaml
    # were read from a different directory than the CLI and the history reader
    # used whenever HERMES_HOME held a tilde.
    from hermes_backend import hermes_home

    return hermes_home()


def settings_files(cwd: Optional[str] = None) -> list[SettingsFile]:
    """Every settings file BlindPilot knows how to point somebody at.

    Listing them creates nothing. These belong to the CLIs, which write their
    own on first run, and a file invented at a path BlindPilot guessed would
    be worse than no file: it would do nothing while looking like it did.

    Locations are the ones each CLI documents. opencode also reads
    `.opencode/opencode.json` inside a project, and Claude Code reads an
    enterprise policy file above all of these that is not an individual's to
    edit, so neither is offered here.
    """
    home = Path.home()
    project = Path(cwd).expanduser() if cwd else None
    entries = [
        SettingsFile(
            BACKEND_CLAUDE,
            "global",
            home / ".claude" / "settings.json",
            "Applies to every project on this machine.",
        ),
        SettingsFile(
            BACKEND_CODEX,
            "global",
            _codex_home() / "config.toml",
            "Applies to every project. TOML rather than JSON.",
        ),
        SettingsFile(
            BACKEND_FREEBUFF,
            "global",
            home / ".config" / "manicode" / "settings.json",
            "Applies to every project. BlindPilot keeps your model choice here.",
        ),
        SettingsFile(
            BACKEND_OPENCODE,
            "global",
            home / ".config" / "opencode" / "opencode.json",
            "Applies to every project.",
        ),
        SettingsFile(
            BACKEND_HERMES,
            "global",
            _hermes_home() / "config.yaml",
            "Applies to every project. YAML rather than JSON, and it belongs to "
            "the Hermes this backend talks to: a Hermes reached over the network "
            "reads the file on that machine, not this one.",
        ),
        SettingsFile(
            BACKEND_MUSE,
            "global",
            home / ".config" / "muse" / "settings.json",
            "Muse's global settings: MCP servers and workspace trust. On Windows "
            "this file lives inside the WSL distribution the CLI runs in, not on "
            "this side.",
        ),
        SettingsFile(
            BACKEND_MUSEAI,
            "global",
            home / ".config" / "muse-cli" / "config.json",
            "muse-cli's own settings: which muse.ai machine it talks to. The "
            "sign-in cookies sit beside it in cookies.txt. muse.ai's own "
            "settings live on its machine and are changed in the muse.ai app.",
        ),
        SettingsFile(
            BACKEND_COMMANDCODE,
            "global",
            home / ".commandcode" / "config.json",
            "Applies to every project. Holds the chosen model, provider, and reasoning effort.",
        ),
        SettingsFile(
            BACKEND_GEMINI,
            "global",
            home / ".gemini" / "settings.json",
            "Applies to every project. Holds Gemini CLI's model, sign-in method, and tool rules.",
        ),
        SettingsFile(
            BACKEND_ANTIGRAVITY,
            "global",
            home / ".gemini" / "antigravity-cli" / "settings.json",
            "Applies to every project. permissions.allow lists the commands agy may run "
            "unasked in a turn that is not bypassing permissions.",
        ),
        SettingsFile(
            BACKEND_COPILOT,
            "global",
            home / ".copilot" / "settings.json",
            "Applies to every project. Holds Copilot CLI's model and its allowed web addresses.",
        ),
        SettingsFile(
            BACKEND_COPILOT,
            "global",
            home / ".copilot" / "mcp-config.json",
            "Applies to every project. The MCP servers Copilot CLI starts.",
        ),
    ]
    if project is not None:
        entries += [
            SettingsFile(
                BACKEND_CLAUDE,
                "this folder",
                project / ".claude" / "settings.json",
                "Shared: committed to this repository, so anyone who has it gets these.",
            ),
            SettingsFile(
                BACKEND_CLAUDE,
                "this folder, personal",
                project / ".claude" / "settings.local.json",
                "Yours alone: normally ignored by git, so it stays on this machine.",
            ),
            SettingsFile(
                BACKEND_OPENCODE,
                "this folder",
                project / "opencode.json",
                "Applies in this folder, and can pin a model or turn providers off.",
            ),
            SettingsFile(
                BACKEND_MUSE,
                "this folder",
                project / "AGENTS.md",
                "Shared: the project rules Muse reads in this folder, written by "
                "`muse init` and committed to this repository.",
            ),
            SettingsFile(
                BACKEND_COMMANDCODE,
                "this folder",
                project / ".commandcode" / "settings.json",
                "Shared: committed to this repository, so anyone who has it gets "
                "these permission rules and default mode.",
            ),
            SettingsFile(
                BACKEND_COMMANDCODE,
                "this folder, personal",
                project / ".commandcode" / "settings.local.json",
                "Yours alone: normally ignored by git, so it stays on this machine.",
            ),
            SettingsFile(
                BACKEND_GEMINI,
                "this folder",
                project / ".gemini" / "settings.json",
                "Shared: committed to this repository, and overrides your own settings here.",
            ),
        ]
    return entries


def _app_version() -> str:
    """BlindPilot's version, for the User-Agent and Codex clientInfo.

    Read off the window's module when it is loaded rather than imported from
    it, because that module imports this one.
    """
    app = sys.modules.get("blindpilot_app")
    return str(getattr(app, "APP_VERSION", "") or "unknown")


def subprocess_env(binary: str) -> dict[str, str]:
    """The environment every provider CLI must be started with.

    The CLI's own directory goes first — an npm or Homebrew shim finds its
    sibling `node` that way — and the login shell's PATH is appended behind
    whatever this process already has, so a Node installed somewhere else
    entirely (nvm, Volta, asdf) is still reachable without displacing a
    runtime BlindPilot manages itself.
    """
    env = os.environ.copy()
    if Path(binary).stem.casefold() == "muse-cli":
        root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        patch = str(root / "museai_cli_patch")
        env["PYTHONPATH"] = os.pathsep.join(filter(None, (patch, env.get("PYTHONPATH", ""))))
    # Even --version/--list-models can start Command Code's detached updater.
    # Keep updates in BlindPilot's logged, hidden npm installer, including
    # when a backend launches Command Code as a child of its own.
    env["COMMANDCODE_SKIP_UPDATES"] = "1"
    entries = [entry for entry in env.get("PATH", "").split(os.pathsep) if entry.strip()]
    known = set(entries)
    directory = os.path.dirname(binary)
    if directory and directory not in known:
        entries.insert(0, directory)
        known.add(directory)
    for entry in login_shell_path_dirs():
        if entry not in known:
            entries.append(entry)
            known.add(entry)
    env["PATH"] = os.pathsep.join(entries)
    if platform.system() == "Windows":
        # Every CLI is started with `cwd` set to the user's project folder, and
        # CLIs shell out constantly - git, node, npm, sh. Windows has
        # historically searched the current directory for those, and the
        # project folder is usually a repository somebody cloned in order to
        # ask an agent about it, not code they wrote. A `git.exe` committed to
        # it should not be what runs when the agent asks for git.
        #
        # This is the documented way off that search path, read by
        # NeedCurrentDirectoryForExePathW, and it is inherited - so it covers
        # the CLI and everything the CLI goes on to start.
        env["NoDefaultCurrentDirectoryInExePath"] = "1"
    return env


def blindpilot_config_dir() -> Path:
    """Where BlindPilot keeps its own settings, separate from any backend's."""
    if platform.system() == "Windows":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "BlindPilot"
    if platform.system() == "Darwin":
        # The one folder a Mac user looks in. The chat database already lives
        # here (see accessible_ai.storage.paths), so settings belong beside it
        # rather than in a Linux-style dot folder.
        return Path.home() / "Library" / "Application Support" / "BlindPilot"
    return Path.home() / ".config" / "blindpilot"


def blindpilot_data_dir() -> Path:
    """Per-user, non-roaming storage for managed runtimes and CLI packages."""
    if platform.system() == "Windows":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "BlindPilot"
    if platform.system() == "Darwin":
        # One subtree of Application Support, so the whole app owns one folder
        # on a Mac -- nothing hidden in ~/.local and ~/.config.
        return Path.home() / "Library" / "Application Support" / "BlindPilot" / "data"
    data = os.environ.get("XDG_DATA_HOME")
    return (Path(data) if data else Path.home() / ".local" / "share") / "blindpilot"


def migrate_macos_legacy_dirs() -> None:
    """Move an older macOS install's folders into the Application Support home.

    Before BlindPilot knew the platform's conventions it kept config in
    ``~/.config/blindpilot`` and managed runtimes in ``~/.local/share/
    blindpilot``, the Linux layout. A Mac update must not strand a user's
    settings or their installed CLIs out there, so the first launch after the
    move relocates both -- never overwriting anything already in the new
    home, and never failing the launch when a move does not go through (the
    old code keeps reading the old folder then).

    Called at import time, before any settings are read, so the config file
    is found in its new home from this launch on. Idempotent and a no-op on
    every other platform.
    """
    if platform.system() != "Darwin":
        return
    home = Path.home()
    legacy_config = home / ".config" / "blindpilot"
    legacy_data = home / ".local" / "share" / "blindpilot"
    for source, destination in (
        (legacy_config, blindpilot_config_dir()),
        (legacy_data, blindpilot_data_dir()),
    ):
        _move_dir_contents(source, destination)


def _move_dir_contents(source: Path, destination: Path) -> None:
    """Move the entries of *source* into *destination*, never overwriting.

    Each entry is moved on its own so one failure leaves the rest behind;
    entries that would collide with something already in the new home are
    skipped rather than replacing it -- the new copy wins, and the old one
    stays where it is, readable by any rollback.
    """
    try:
        if not source.is_dir():
            return
        destination.mkdir(parents=True, exist_ok=True)
        for entry in list(source.iterdir()):
            target = destination / entry.name
            if target.exists() or target.is_symlink():
                continue
            try:
                shutil.move(str(entry), str(target))
            except OSError:
                continue
        # Only a source left with nothing in it is a source that finished.
        if not any(source.iterdir()):
            try:
                source.rmdir()
            except OSError:
                pass
    except OSError:
        pass


def _freebuff_choice_path() -> Path:
    return blindpilot_config_dir() / "freebuff-model.json"


def _read_freebuff_choice() -> str:
    """Return the FreeBuff model BlindPilot last selected, if any."""
    try:
        payload = json.loads(_freebuff_choice_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    model = payload.get("model") if isinstance(payload, dict) else None
    return model.strip() if isinstance(model, str) else ""


def _write_freebuff_choice(model: str) -> None:
    """Record the selection somewhere only BlindPilot writes.

    FreeBuff resets ``freebuffModel`` in its own settings to whichever model it
    recommends once a turn has run, so that file cannot be read back as the
    user's choice.  This record is what survives.
    """
    path = _freebuff_choice_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({"model": model}, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _freebuff_catalog_cache_path() -> Path:
    return blindpilot_config_dir() / "freebuff-catalog.json"


def _read_freebuff_catalog_cache(stamp: tuple[str, float, int]) -> list[str]:
    """The catalog last read out of this exact FreeBuff release, if any.

    Discovering the catalog means scanning a hundred megabytes of compiled
    FreeBuff, which takes seconds.  Doing that once per installed release rather
    than once per run of BlindPilot is the difference between a message that
    sends immediately and one that waits on a file scan first.
    """
    try:
        payload = json.loads(_freebuff_catalog_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(payload, dict):
        return []
    if payload.get("path") != stamp[0] or payload.get("size") != stamp[2]:
        return []
    mtime = _as_number(payload.get("mtime"))
    if mtime is None or abs(mtime - stamp[1]) > 0.001:
        return []
    models = payload.get("models")
    if not isinstance(models, list):
        return []
    return [model for model in models if isinstance(model, str) and model]


def _write_freebuff_catalog_cache(stamp: tuple[str, float, int], models: list[str]) -> None:
    path = _freebuff_catalog_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Written beside the file and moved into place, the same as the model
        # choice above: a read that lands mid-write must never see a half-copied
        # catalog, which parses as nothing and throws the cache away.
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                {"path": stamp[0], "mtime": stamp[1], "size": stamp[2], "models": models},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError:
        pass


def _freebuff_package_readme(binary: Optional[str]) -> Optional[Path]:
    if not binary:
        return None
    wrapper = Path(binary)
    candidates = [
        wrapper.parent / "node_modules" / "freebuff" / "README.md",
        wrapper.parent.parent / "lib" / "node_modules" / "freebuff" / "README.md",
    ]
    try:
        resolved = wrapper.resolve()
        candidates.extend([resolved.parent / "README.md", resolved.parent.parent / "README.md"])
    except OSError:
        pass
    return next((path for path in candidates if path.is_file()), None)


def _resolve_minified_string(
    source: str,
    expression: str,
    direct: Optional[dict[str, str]] = None,
    seen: Optional[set[str]] = None,
) -> str:
    expression = expression.strip()
    if seen is None:
        seen = set()
    if expression in seen:
        return ""
    seen.add(expression)
    if len(expression) >= 2 and expression[0] == expression[-1] == '"':
        return expression[1:-1]
    if direct is None:
        direct = dict(re.findall(r'(?<![\w$])([A-Za-z_$][\w$]*)="([^"]+)"', source))
    if expression in direct:
        return direct[expression]
    if "." in expression:
        owner, member = expression.split(".", 1)
        marker = f"{owner}={{"
        owner_start = source.find(marker)
        if owner_start >= 0:
            body_start = owner_start + len(marker)
            body = source[body_start : body_start + 3000].split("}", 1)[0]
            value = re.search(rf'(?:^|,){re.escape(member)}:"([^"]+)"', body)
            if value:
                return value.group(1)
    alias = re.search(
        rf"(?<![\w$]){re.escape(expression)}=([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)?)",
        source,
    )
    if alias and alias.group(1) != expression:
        return _resolve_minified_string(source, alias.group(1), direct, seen)
    return ""


# FreeBuff stamps a release date onto some display names, and revises it in
# place: "DeepSeek V4 Pro" became "DeepSeek V4 Pro 08/13" without the packaged
# README following. Reading the two as different models drops the dated one.
_MODEL_DATE_SUFFIX_RE = re.compile(r"\s+\d{1,2}/\d{1,2}(?:/\d{2,4})?$")


def _readme_offset(readme_text: str, display_name: str) -> int:
    """Where the README documents this model, or -1 if it does not at all."""
    offset = readme_text.find(display_name)
    if offset >= 0:
        return offset
    undated = _MODEL_DATE_SUFFIX_RE.sub("", display_name)
    return readme_text.find(undated) if undated != display_name else -1


def _freebuff_models_from_install(binary: Optional[str]) -> list[str]:
    """Read the current picker catalog from the installed FreeBuff release."""
    executable = (
        Path.home()
        / ".config"
        / "manicode"
        / ("freebuff.exe" if platform.system() == "Windows" else "freebuff")
    )
    readme = _freebuff_package_readme(binary)
    try:
        stat = executable.stat()
        stamp = (str(executable), stat.st_mtime, stat.st_size)
    except OSError:
        return []

    global _freebuff_catalog_cache
    if _freebuff_catalog_cache and _freebuff_catalog_cache[0] == stamp:
        return list(_freebuff_catalog_cache[1])
    remembered = _read_freebuff_catalog_cache(stamp)
    if remembered:
        _freebuff_catalog_cache = (stamp, list(remembered))
        return list(remembered)

    try:
        source = executable.read_bytes().decode("latin-1", errors="ignore")
        readme_text = readme.read_text(encoding="utf-8") if readme else ""
    except OSError:
        return []

    pattern = re.compile(
        r'([A-Za-z_$][\w$]*)=\{id:([^,}]+),displayName:"([^"]+)"'
        r'[^{}]{0,1000}?availability:"(?:always|off_peak_only)"'
    )
    matches = list(pattern.finditer(source))
    if not matches:
        return []
    start = max(0, min(match.start() for match in matches) - 50_000)
    end = min(len(source), max(match.end() for match in matches) + 10_000)
    catalog_source = source[start:end]
    direct = dict(re.findall(r'(?<![\w$])([A-Za-z_$][\w$]*)="([^"]+)"', catalog_source))
    discovered: list[tuple[int, str]] = []
    for match in matches:
        display_name = match.group(3)
        # The packaged README documents the user-facing regular/earned models.
        # If it is absent, the catalog-shaped object is still better than a
        # stale list compiled into BlindPilot.
        order = _readme_offset(readme_text, display_name) if readme_text else -1
        if readme_text and order < 0:
            continue
        model_id = _resolve_minified_string(source, match.group(2), direct)
        if not model_id or "/" not in model_id:
            continue
        discovered.append((order if order >= 0 else match.start(), model_id))

    models: list[str] = []
    for _order, model_id in sorted(discovered):
        if model_id not in models:
            models.append(model_id)
    if FREEBUFF_PREFERRED_MODEL in models:
        models.remove(FREEBUFF_PREFERRED_MODEL)
        models.insert(0, FREEBUFF_PREFERRED_MODEL)
    _freebuff_catalog_cache = (stamp, list(models))
    _write_freebuff_catalog_cache(stamp, list(models))
    return models


def freebuff_model_options() -> tuple[list[str], str, str]:
    """Return the model choices exposed by the installed FreeBuff release."""
    binary = find_backend_cli(BACKEND_FREEBUFF)
    models = _freebuff_models_from_install(binary)
    chosen = _read_freebuff_choice()
    saved = ""
    try:
        settings = Path.home() / ".config" / "manicode" / "settings.json"
        payload = json.loads(settings.read_text(encoding="utf-8"))
        selected = payload.get("freebuffModel") if isinstance(payload, dict) else None
        if isinstance(selected, str) and selected.strip():
            saved = selected.strip()
    except (OSError, ValueError):
        pass
    if not models:
        models = [FREEBUFF_PREFERRED_MODEL]
        error = "Could not refresh FreeBuff's model catalog; showing the preferred default."
    else:
        error = ""
    # A remembered choice is only worth offering while the catalog is unknown.
    # When it was read successfully, a model missing from it is one this
    # release has dropped, and listing it means picking it, waiting on a picker
    # that will never show it, and losing the message.
    if error:
        for candidate in (chosen, saved):
            if candidate and candidate not in models:
                models.append(candidate)
    # BlindPilot's own record wins over FreeBuff's settings file. FreeBuff
    # rewrites that file to its recommended model after a turn, so honouring it
    # would quietly downgrade every following turn to the recommendation.
    current = (
        chosen
        if chosen in models
        else FREEBUFF_PREFERRED_MODEL
        if FREEBUFF_PREFERRED_MODEL in models
        else saved
        if saved in models
        else models[0]
    )
    return models, current, error


def _freebuff_display_key(text: str) -> str:
    """Letters and digits only, with a version letter before a number dropped.

    The picker paints a display name, never the model id, and the two disagree
    in ways too small to be worth a table and too varied to keep up with:
    ``mimo/mimo-v2.5`` is drawn "MiMo 2.5" and ``deepseek/deepseek-v4-flash``
    is drawn "DeepSeek V4 Flash", so the ``v`` is dropped by one side or the
    other depending on the model. Reducing both sides the same way makes the
    id a substring of the row it belongs to whichever of them carries it.
    """
    return re.sub(r"v(?=\d)", "", re.sub(r"[^a-z0-9]+", "", text.casefold()))


def _freebuff_picker_options(visible: str, models: list[str]) -> tuple[list[str], int]:
    """Return the model IDs painted by FreeBuff's picker and its focused index.

    The index matters as much as the list: the caller moves to a model by
    pressing Down the difference between two positions in it, so a row that is
    on screen and not in this list does not cost that model, it costs every
    model below it. Matching therefore has to recognise every row the picker
    can move to, or recognise none of them.

    FreeBuff 0.0.168 stopped drawing the focus marker altogether: the welcome
    screen opens with the highlight on the recommended card and no ``›``
    anywhere, so a reader that insists on the marker finds no focused row and
    the message is lost. When no row is marked, the content row of the first
    painted card is taken as the focused one — the highlight starts on the
    first card on both the welcome screen and the expanded list. A model card
    opens with a ``┌─┐`` top and its name on the row below, which the
    advertising boxes below the cards do not share; the position is only
    claimed when that first row was itself matched, so a catalog that has
    lost the very first model still reports no focus rather than a wrong one.
    Releases that draw the marker never reach this.
    """
    # Longest first, so a model whose id is a prefix of another's cannot claim
    # the longer one's row.
    keyed = sorted(
        ((model, _freebuff_display_key(model.rsplit("/", 1)[-1])) for model in models),
        key=lambda pair: len(pair[1]),
        reverse=True,
    )
    options: list[str] = []
    focused = -1
    for raw in visible.splitlines():
        if "│" not in raw:
            continue
        row = _freebuff_display_key(raw)
        matched = next((model for model, key in keyed if key and key in row), "")
        if not matched or matched in options:
            continue
        options.append(matched)
        if "›" in raw or re.search(r"(?:^|│)\s*>\s*", raw):
            focused = len(options) - 1
    if focused < 0 and options:
        lines = [line.strip() for line in visible.splitlines()]
        first_top = next(
            (index for index, line in enumerate(lines) if re.fullmatch(r"┌─+┐", line)),
            -1,
        )
        if first_top >= 0 and first_top + 1 < len(lines):
            row = _freebuff_display_key(lines[first_top + 1])
            matched = next((model for model, key in keyed if key and key in row), "")
            if matched and matched in options:
                focused = 0
    return options, focused


def invalidate_backend_cache(backend: str) -> None:
    """Drop version-derived provider data before an explicit runtime refresh."""
    global _freebuff_catalog_cache, _login_shell_path
    # The wizard may have just put a backend on PATH. The answer cached before
    # that happened is precisely the one that made putting it there necessary.
    _login_shell_path = None
    if normalize_backend(backend) == BACKEND_OPENCODE:
        __import__("opencode_worker").invalidate_opencode_catalog()
    if normalize_backend(backend) == BACKEND_FREEBUFF:
        _freebuff_catalog_cache = None
        try:
            _freebuff_catalog_cache_path().unlink()
        except OSError:
            pass


def set_freebuff_model(model: str) -> None:
    """Select the model in FreeBuff, and record the choice as BlindPilot's own."""
    selected = model.strip()
    if not selected:
        models, current, _error = freebuff_model_options()
        selected = current or (models[0] if models else FREEBUFF_PREFERRED_MODEL)
    settings = Path.home() / ".config" / "manicode" / "settings.json"
    with _FREEBUFF_SETTINGS_LOCK:
        try:
            payload = json.loads(settings.read_text(encoding="utf-8"))
        except FileNotFoundError:
            payload = {}
        except ValueError as exc:
            raise OSError(f"Could not read FreeBuff settings: {exc}") from exc
        if not isinstance(payload, dict):
            raise OSError("FreeBuff settings must contain a JSON object")
        _write_freebuff_choice(selected)
        if payload.get("freebuffModel") == selected:
            return
        payload["freebuffModel"] = selected
        settings.parent.mkdir(parents=True, exist_ok=True)
        temporary = settings.with_suffix(".json.blindpilot.tmp")
        temporary.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, settings)


def ask_through_the_tool(tool: str) -> str:
    """BlindPilot's instruction to ask with `tool` instead of asking in prose."""
    return (
        "You are running inside BlindPilot, which a blind person is driving with a "
        "screen reader. When you want an answer from them, ask with the "
        f"{tool} tool rather than writing the question into your reply. "
        "BlindPilot speaks a tool question and opens it in a dialog; a question "
        "written into a reply is neither spoken as a question nor shown as one, so "
        "the turn ends and nothing tells them an answer is wanted. This holds for "
        "every question, including an interview that asks them one at a time."
    )


def _cli_signed_in(backend: str, args: tuple[str, ...]) -> Callable[[int], bool]:
    """Signed in when this CLI subcommand exits successfully."""

    def check(timeout: int) -> bool:
        binary = find_backend_cli(backend)
        if not binary:
            return False
        proc = subprocess.run(
            [binary, *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            env=subprocess_env(binary),
            **process_utils.no_window_kwargs(),
        )
        return proc.returncode == 0

    return check


def _stored_signed_in(backend: str, read: Callable[[], bool]) -> Callable[[int], bool]:
    """Signed in when the CLI is installed and its stored credential says so."""

    def check(_timeout: int) -> bool:
        return bool(find_backend_cli(backend)) and read()

    return check


def _usage_lines(windows: Sequence[UsageWindow]) -> list[str]:
    return [line for line in map(_usage_window_line, _ordered_windows(windows)) if line]


def _hermes_auth_ok(timeout: int) -> bool:
    from hermes_backend import hermes_auth_ok

    return hermes_auth_ok(timeout=max(timeout, 25))


def _museai_auth_ok(timeout: int) -> bool:
    from museai_backend import museai_auth_ok

    return museai_auth_ok(timeout=max(timeout, 20))


def _commandcode_auth_ok(timeout: int) -> bool:
    # `command-code status` is a Node start-up plus a request; the default
    # twelve seconds has been enough, but give it room like Hermes.
    from commandcode_backend import commandcode_auth_ok

    return commandcode_auth_ok(timeout=max(timeout, 25))


def _gemini_auth_ok(_timeout: int) -> bool:
    from google_backend import gemini_auth_ok

    return gemini_auth_ok()


def _antigravity_auth_ok(timeout: int) -> bool:
    from google_backend import antigravity_auth_ok

    return antigravity_auth_ok(timeout=max(timeout, 30))


def _copilot_auth_ok(timeout: int) -> bool:
    from copilot_backend import copilot_auth_ok

    return copilot_auth_ok(timeout=max(timeout, 20))


def _museai_account_lines() -> list[str]:
    from museai_backend import museai_account_lines

    return museai_account_lines()


def _commandcode_account_lines() -> list[str]:
    from commandcode_backend import commandcode_account_lines

    return commandcode_account_lines()


def _gemini_account_lines() -> list[str]:
    from google_backend import gemini_account_lines

    return gemini_account_lines()


def _antigravity_account_lines() -> list[str]:
    from google_backend import antigravity_account_lines

    return antigravity_account_lines()


def _copilot_account_lines() -> list[str]:
    from copilot_backend import copilot_account_lines

    return copilot_account_lines()


ADAPTERS: dict[str, BackendAdapter] = {
    BACKEND_CLAUDE: BackendAdapter(
        auth_ok=_cli_signed_in(BACKEND_CLAUDE, ("auth", "status")),
        account_lines=lambda binary, timeout: _claude_account_lines(
            *_probe_backend(binary, ["auth", "status"], timeout)
        ),
        usage_lines=lambda binary, timeout: _usage_lines(
            _claude_usage_windows(_claude_usage_payload(binary, timeout))
        ),
        worker=_claude_worker,
    ),
    BACKEND_CODEX: BackendAdapter(
        auth_ok=_cli_signed_in(BACKEND_CODEX, ("login", "status")),
        account_lines=lambda binary, timeout: _codex_account_lines(
            *_probe_backend(binary, ["login", "status"], timeout)
        ),
        usage_lines=lambda binary, timeout: _usage_lines(
            _codex_usage_windows(_codex_usage_result(binary, timeout))
        ),
        worker=lambda _claude: __import__("codex_worker").CodexWorker,
    ),
    BACKEND_FREEBUFF: BackendAdapter(
        auth_ok=_stored_signed_in(
            BACKEND_FREEBUFF, lambda: _freebuff_signed_in(_freebuff_account())
        ),
        account_lines=lambda _binary, _timeout: _freebuff_account_lines(),
        usage_lines=lambda _binary, timeout: _usage_lines(
            _freebuff_usage_windows(_freebuff_usage_payload(timeout))
        ),
        worker=lambda _claude: __import__("freebuff_worker").FreebuffWorker,
    ),
    BACKEND_OPENCODE: BackendAdapter(
        auth_ok=_stored_signed_in(BACKEND_OPENCODE, lambda: bool(_opencode_providers())),
        account_lines=lambda _binary, _timeout: _opencode_account_lines(),
        usage_lines=lambda _binary, _timeout: [],
        worker=lambda _claude: __import__("opencode_worker").OpencodeWorker,
    ),
    BACKEND_HERMES: BackendAdapter(
        auth_ok=_hermes_auth_ok,
        account_lines=lambda _binary, _timeout: _hermes_account_lines(),
        usage_lines=lambda _binary, _timeout: _usage_lines(
            _hermes_usage_windows(_hermes_pool_payload())
        ),
        worker=_hermes_worker,
    ),
    BACKEND_MUSE: BackendAdapter(
        auth_ok=lambda _timeout: _muse_signed_in_checked(),
        account_lines=lambda _binary, _timeout: _muse_account_lines(),
        usage_lines=lambda _binary, _timeout: _usage_lines(
            _muse_usage_windows(_muse_usage_payload())
        ),
        worker=_muse_worker,
    ),
    BACKEND_MUSEAI: BackendAdapter(
        auth_ok=_museai_auth_ok,
        account_lines=lambda _binary, _timeout: _museai_account_lines(),
        usage_lines=lambda _binary, _timeout: [],
        worker=_museai_worker,
    ),
    BACKEND_COMMANDCODE: BackendAdapter(
        auth_ok=_commandcode_auth_ok,
        account_lines=lambda _binary, _timeout: _commandcode_account_lines(),
        usage_lines=lambda _binary, _timeout: [],
        worker=_commandcode_worker,
    ),
    BACKEND_GEMINI: BackendAdapter(
        auth_ok=_gemini_auth_ok,
        account_lines=lambda _binary, _timeout: _gemini_account_lines(),
        usage_lines=lambda _binary, _timeout: [],
        worker=_gemini_worker,
    ),
    BACKEND_ANTIGRAVITY: BackendAdapter(
        auth_ok=_antigravity_auth_ok,
        account_lines=lambda _binary, _timeout: _antigravity_account_lines(),
        usage_lines=lambda _binary, _timeout: [],
        worker=_antigravity_worker,
    ),
    BACKEND_COPILOT: BackendAdapter(
        auth_ok=_copilot_auth_ok,
        account_lines=lambda _binary, _timeout: _copilot_account_lines(),
        usage_lines=lambda _binary, _timeout: [],
        worker=_copilot_worker,
    ),
}


def _adapter(backend: str) -> BackendAdapter:
    """This backend's answers, or a KeyError naming the one that is missing."""
    backend = normalize_backend(backend)
    adapter = ADAPTERS.get(backend)
    if adapter is None:
        raise KeyError(f"no backend adapter for {backend}")
    return adapter
