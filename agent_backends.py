"""Backend adapters for BlindPilot.

BlindPilot began as Claude Code Reader. Claude's adapter remains in
``blindpilot_app.py``; this module contains the
provider-neutral discovery helpers plus the Codex, FreeBuff, and opencode
workers.
Hermes lives in ``hermes_backend.py`` and ``hermes_worker.py``, imported
on demand so a machine without Hermes pays nothing for it.

Codex's app-server used to be started fresh for every turn and killed at the
end of it. It is now owned by ``backend_pool``: one process shared by every
tab, held across turns instead of torn down between them, so a conversation
does not pay the app-server's start-up -- and, where MCP servers are
configured, all of their child processes' start-up too -- on every message.
``backend_pool.py`` decides how long that process lives; what is here says
only how to start, check, and stop it.

Copyright (c) 2026 doubletaponair and BlindPilot contributors.
Based on the original Claude Code Reader application by doubletaponair:
https://github.com/doubletaponair/claude-code-reader
SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import atexit
import datetime
import difflib
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
from typing import IO, Callable, NamedTuple, Optional, Protocol, Sequence

import diagnostics
from owned_process import owned_popen
import questions
import process_utils


BACKEND_CLAUDE = "claude"
BACKEND_CODEX = "codex"
BACKEND_FREEBUFF = "freebuff"
BACKEND_OPENCODE = "opencode"
BACKEND_HERMES = "hermes"
BACKEND_MUSE = "muse"
# muse.ai, Meta's personal agent on its own cloud machine -- not Muse Code.
BACKEND_MUSEAI = "museai"
BACKEND_COMMANDCODE = "commandcode"
BACKEND_GEMINI = "gemini"
BACKEND_ANTIGRAVITY = "antigravity"
BACKEND_COPILOT = "copilot"
# FreeBuff has no model-list or model-selection CLI flags. Its installed
# package and downloaded executable do contain the live picker catalog, so the
# adapter discovers that catalog at runtime and writes the same setting the
# picker uses. GLM 5.3 is the preferred default while FreeBuff still offers it;
# FreeBuff drops and renames models between releases, so this is a preference
# rather than a requirement, and a release without it falls back to FreeBuff's
# own choice rather than failing.
FREEBUFF_PREFERRED_MODEL = "z-ai/glm-5.3-flash"
_FREEBUFF_SETTINGS_LOCK = threading.Lock()
_freebuff_catalog_cache: tuple[tuple[str, float, int], list[str]] | None = None


# ----- Subagents -----
# Every backend can hand work to agents of its own -- Claude Code's Agent tool,
# Codex's spawnAgent, Hermes' delegate_task, Muse's native subagents, opencode's
# task tool, Command Code's agent tool, FreeBuff's spawn_agents -- and each one
# reports them differently. The window lists the ones running and shows what
# each is doing, so every worker translates its provider's reports into one
# call: (agent id, name, status, line). An empty name or status leaves that part
# as it was, and an empty line adds nothing to the agent's log.
SubagentReport = Callable[[str, str, str, str], None]

SUBAGENT_RUNNING = "running"
SUBAGENT_COMPLETED = "completed"
SUBAGENT_FAILED = "failed"
SUBAGENT_STOPPED = "stopped"

_SUBAGENT_STATUS_WORDS = {
    SUBAGENT_RUNNING: (
        "running",
        "inprogress",
        "in_progress",
        "pendinginit",
        "pending",
        "starting",
        "started",
        "accepted",
        "active",
        "queued",
        "recoverypending",
    ),
    SUBAGENT_COMPLETED: (
        "completed",
        "complete",
        "succeeded",
        "success",
        "done",
        "finished",
        "resultready",
        "ok",
    ),
    SUBAGENT_FAILED: (
        "failed",
        "errored",
        "error",
        "notfound",
        "timeout",
        "timed_out",
        "timedout",
        "rejected",
    ),
    SUBAGENT_STOPPED: (
        "stopped",
        "cancelled",
        "canceled",
        "interrupted",
        "shutdown",
        "killed",
        "closed",
        "aborted",
        "closing",
    ),
}


def subagent_status(raw: object) -> str:
    """A provider's word for an agent's state, in the window's four.

    Empty for a word none of them uses, which the window reads as "unchanged":
    a newer provider's new state should not make a running agent look ended.
    """
    word = str(raw or "").strip().casefold().replace("-", "").replace(" ", "")
    for status, words in _SUBAGENT_STATUS_WORDS.items():
        if word in words:
            return status
    return ""


def subagent_line(text: object, limit: int = 300) -> str:
    """One readable line for an agent's log, from whatever the provider sent."""
    words = " ".join(str(text or "").split())
    return words if len(words) <= limit else words[: limit - 1].rstrip() + "…"


def ignore_subagents(_agent_id: str, _name: str, _status: str, _line: str) -> None:
    """The report for a worker created without one, as the tests create them."""


# ----- Mid-run questions -----
#
# Every backend BlindPilot drives can stop mid-turn to ask the person a
# multiple-choice question, and each one describes that question differently:
# Claude Code sends the AskUserQuestion tool's input through a `can_use_tool`
# control request, Codex sends an `item/tool/requestUserInput` JSON-RPC
# request, opencode publishes a `question.asked` event, and FreeBuff draws its
# `ask_user` tool straight onto its terminal. The window should not have to
# know any of that, so each adapter translates its provider's shape into the
# two types below, and translates the answers back on the way out.


@dataclass(frozen=True)
class BackendInfo:
    id: str
    label: str
    executable: str
    install_command: str
    login_args: tuple[str, ...]
    supports_model: bool
    supports_effort: bool
    supports_permissions: bool
    # Whether the provider can summarise a long conversation in place to free
    # up its context window. FreeBuff's CLI has no such command — its only
    # context control is starting a new conversation.
    supports_compaction: bool = False
    # Whether the CLI opens the sign-in page itself. Claude Code does, even
    # when BlindPilot starts it with no console, so that one is left to the CLI
    # and BlindPilot only opens the pages nobody else will. Codex is the
    # counter-example: its own browser launch does not reliably arrive from a
    # hidden process with its output on a pipe, and it has no flag to suppress
    # it, so BlindPilot opens Codex's page itself -- a duplicate tab is the
    # lesser cost of a sign-in page that never opens.
    login_opens_browser: bool = False
    # What the CLI's "paste the code from the browser" prompt looks like. It is
    # written without a newline after it, so it is matched against the output
    # as it arrives rather than line by line. Empty when the CLI never asks.
    login_code_prompt: str = ""
    # Whether signing in has to happen in a terminal the user can type into.
    # Claude and Codex authenticate through a browser and report the result on
    # exit, so BlindPilot can run them hidden and watch. Hermes' equivalent is
    # an interactive picker: run hidden with no stdin it simply fails, so the
    # wizard opens a real console instead of pretending to have signed in.
    login_needs_terminal: bool = False
    # Whether that terminal is kept off screen rather than shown to the user.
    # Hermes' setup asks questions the user has to answer, so its window is
    # theirs to read. Command Code needs a terminal only because its Ink UI
    # refuses to start without one -- the sign-in itself happens in the browser
    # the CLI opens -- so its terminal runs hidden like FreeBuff's and the
    # wizard waits for the command to finish instead of for the user.
    login_terminal_hidden: bool = False
    # A command to run before signing in. Command Code's `login` answers
    # "Already logged in" and exits whenever a credential file exists, so a key
    # the server has revoked can never be replaced from the wizard. Its own
    # troubleshooting says `logout` then `login`; this is that first half.
    login_reset_args: tuple[str, ...] = ()
    # Whether the backend takes an attachment's BYTES rather than its path.
    # The CLI backends run on this machine, so naming the file is enough for
    # them. Hermes may be running somewhere else entirely -- in WSL, or on
    # another machine over the network -- where a path from here means nothing
    # or, worse, means a different file that happens to share the name. Those
    # backends are handed the file itself.
    uploads_attachments: bool = False
    # Whether the sign-in terminal stays open after the sign-in lands. Gemini
    # CLI and Antigravity CLI sign in from inside their own interactive UI,
    # which keeps running afterwards, so the wizard watches for the CLI to
    # report itself signed in rather than for the terminal to close.
    login_watch_until_signed_in: bool = False
    # The npm package BlindPilot installs and updates this backend from. Empty
    # for a backend that ships its own installer, which is what says npm is not
    # the tool to send the user after. One field, so the installer, the update
    # check and the wizard cannot disagree about the package name.
    npm_package: str = ""


BACKENDS = {
    BACKEND_CLAUDE: BackendInfo(
        BACKEND_CLAUDE,
        "Claude Code",
        "claude",
        "See https://claude.com/claude-code",
        # "claude /login" is a slash command typed inside the interactive
        # session, not a command line. Run that way it opens a terminal UI that
        # BlindPilot has no console for, so it sat there until it timed out and
        # no browser ever opened. "claude auth login" is the command line.
        ("auth", "login"),
        True,
        True,
        True,
        supports_compaction=True,
        login_opens_browser=True,
        login_code_prompt=r"[Pp]aste code here[^>]*>",
    ),
    BACKEND_CODEX: BackendInfo(
        BACKEND_CODEX,
        "Codex",
        "codex",
        "npm install -g @openai/codex",
        ("login",),
        True,
        True,
        True,
        supports_compaction=True,
        # Codex opens the auth URL itself when it can, but not reliably from
        # the hidden process BlindPilot starts; its page is opened here instead
        # (see the login_opens_browser comment above).
        login_opens_browser=False,
        npm_package="@openai/codex",
    ),
    BACKEND_FREEBUFF: BackendInfo(
        BACKEND_FREEBUFF,
        "FreeBuff",
        "freebuff",
        "npm install -g freebuff",
        ("login",),
        True,
        False,
        False,
        supports_compaction=False,
        npm_package="freebuff",
    ),
    BACKEND_OPENCODE: BackendInfo(
        BACKEND_OPENCODE,
        "opencode",
        "opencode",
        "npm install -g opencode-ai",
        ("providers", "login"),
        True,
        True,
        True,
        supports_compaction=True,
        # Pictures and text files travel as file parts the model reads, so
        # the worker takes the files themselves rather than their paths.
        uploads_attachments=True,
        npm_package="opencode-ai",
    ),
    BACKEND_HERMES: BackendInfo(
        BACKEND_HERMES,
        "Hermes",
        "hermes",
        "See https://hermes-agent.nousresearch.com/docs",
        ("model",),
        True,
        # Hermes takes a reasoning level as a per-session override on
        # session.create. This said False at first, with a comment claiming the
        # protocol had no such control; it has one, and the effect is
        # measurable -- a session created with "low" reads back "low" while one
        # created without it reads the profile's own level. Saying False here
        # hid the control in the picker AND told the setup wizard to announce
        # that Hermes "does not expose a reasoning effort level".
        True,
        True,
        supports_compaction=True,
        login_needs_terminal=True,
        # Hermes' gateway takes an upload of the file, so an attachment works
        # the same whether Hermes is here, in WSL, or on another machine.
        uploads_attachments=True,
    ),
    BACKEND_MUSE: BackendInfo(
        BACKEND_MUSE,
        "Muse Code",
        "muse",
        "See https://developer.meta.com/ai/products/muse-code/ -- one-line install: "
        "curl -fsSL https://dev.meta.ai/install.sh | bash",
        # `muse login` is the device flow: it prints the sign-in address and
        # waits for the browser approval. It needs no code typed back, so the
        # login runner watches for the URL like Claude's and Codex's.
        ("login",),
        True,
        True,
        True,
        supports_compaction=True,
        # Images travel as base64 parts the model sees, and the rest are
        # named in the prompt by paths translated for the host -- on Windows
        # it runs inside WSL, where a path from here names nothing. Either
        # way the worker takes the files themselves, like Hermes' does.
        uploads_attachments=True,
    ),
    BACKEND_COMMANDCODE: BackendInfo(
        BACKEND_COMMANDCODE,
        "Command Code",
        # The npm package installs four launchers (cmd, cmdc, command-code,
        # commandcode). This one, never `cmd`: on Windows `shutil.which("cmd")`
        # finds the command interpreter in System32, not the agent.
        "command-code",
        "npm install -g command-code",
        # `cmd login` is the OAuth flow, which opens the browser itself.
        ("login",),
        True,
        True,
        True,
        supports_compaction=True,
        # "cmd login" is not a plain sign-in: it mounts an Ink terminal UI for
        # its authentication spinner, and Ink refuses to start when stdin is
        # not a TTY ("Raw mode is not supported on the current process.stdin,
        # which Ink uses as input stream by default"). Run hidden behind the
        # wizard's pipes it died on that mount before it reached the browser,
        # and its crash output -- Ink's own documentation URL included -- was
        # read out as the sign-in address, pointing a listener at
        # github.com/vadimdemedes/ink. It needs a real terminal, but there is
        # nothing in that terminal for anyone to read or answer -- the sign-in
        # is the browser page the CLI opens -- so it gets a hidden one rather
        # than a console window to tab past.
        login_needs_terminal=True,
        login_terminal_hidden=True,
        login_reset_args=("logout",),
        npm_package="command-code",
    ),
    BACKEND_GEMINI: BackendInfo(
        BACKEND_GEMINI,
        "Gemini CLI",
        "gemini",
        "npm install -g @google/gemini-cli",
        # Gemini CLI has no sign-in command line: its first run asks how to
        # sign in, and "Sign in with Google" opens the browser. Started in its
        # screen-reader mode, so that question reads as plain text.
        ("--screen-reader",),
        True,
        # No reasoning-level flag: the model choice is the only lever.
        False,
        True,
        # Its /compress is interactive-only; headless turns cannot ask for it.
        supports_compaction=False,
        login_needs_terminal=True,
        login_watch_until_signed_in=True,
        npm_package="@google/gemini-cli",
    ),
    BACKEND_ANTIGRAVITY: BackendInfo(
        BACKEND_ANTIGRAVITY,
        "Antigravity CLI",
        "agy",
        "See https://antigravity.google/docs/cli/install/ -- macOS and Linux: "
        "curl -fsSL https://antigravity.google/cli/install.sh | bash -- Windows "
        "PowerShell: irm https://antigravity.google/cli/install.ps1 | iex",
        # Running `agy` with no arguments is its Google sign-in: on this
        # machine it opens the browser itself (Antigravity CLI docs).
        (),
        True,
        True,
        True,
        supports_compaction=False,
        login_needs_terminal=True,
        login_watch_until_signed_in=True,
    ),
    # Added last on purpose: a backend's send earcon is pitched by its place
    # in this dict, so new backends go at the end and existing pitches stay.
    BACKEND_MUSEAI: BackendInfo(
        BACKEND_MUSEAI,
        "muse.ai",
        "muse-cli",
        "uv tool install muse-cli",
        # muse-cli signs in by copying the muse.ai cookies out of a Chrome
        # that is signed in with remote debugging on; it prints what to do
        # when that is missing, so the user needs to see its terminal.
        ("auth", "export"),
        # The agent picks its own model and runs its own tools on its own
        # machine; none of these are BlindPilot's to set.
        False,
        False,
        False,
        supports_compaction=False,
        login_needs_terminal=True,
    ),
    BACKEND_COPILOT: BackendInfo(
        BACKEND_COPILOT,
        "GitHub Copilot CLI",
        "copilot",
        "npm install -g @github/copilot",
        # GitHub's device flow: Copilot prints a one-time code and the page to
        # type it into, so the person needs to read its terminal.
        ("login",),
        True,
        True,
        True,
        # Its /compact is interactive-only; a headless turn cannot ask for it.
        supports_compaction=False,
        login_needs_terminal=True,
        npm_package="@github/copilot",
    ),
}

# Alphabetical by label, so every menu and list of backends reads in the same
# predictable order. `BACKENDS` itself stays in the order it was written.
BACKEND_IDS = tuple(sorted(BACKENDS, key=lambda id_: BACKENDS[id_].label.casefold()))
BACKEND_LABELS = {id_: BACKENDS[id_].label for id_ in BACKEND_IDS}
# The order backends were added in, which never changes: what the send
# earcon's pitch is keyed on, so a listener's ear for each backend survives
# the menus being re-sorted.
BACKEND_PITCH_ORDER = tuple(BACKENDS)

# What a "compact this conversation" turn looks like per provider: the text to
# send, and any extra keyword arguments its worker needs.
#
# Claude Code takes ``/compact`` as an ordinary message even in headless
# streaming mode, and acts on it. Codex and opencode have no such message —
# for both it is a request of its own — so their workers are told to compact
# instead, and ignore the text. The text is still shown to the user either
# way, so the row in the list says what was asked for.
_COMPACTION_REQUESTS: dict[str, tuple[str, dict]] = {
    BACKEND_CLAUDE: ("/compact", {}),
    BACKEND_CODEX: ("/compact", {"compact": True}),
    BACKEND_OPENCODE: ("/compact", {"compact": True}),
    # Hermes compacts through a request of its own, like Codex. Its own name
    # for the command is /compress; the text shown to the user says what was
    # asked for in BlindPilot's words, and the worker acts on the flag.
    BACKEND_HERMES: ("/compact", {"compact": True}),
    # Muse compacts the same way, over session/compact; the worker turns the
    # flag into that request.
    BACKEND_MUSE: ("/compact", {"compact": True}),
    BACKEND_COMMANDCODE: ("Summarize this conversation for continuation.", {"compact": True}),
}


def compaction_request(backend: object) -> Optional[tuple[str, dict]]:
    """(message text, extra worker arguments) to compact, or None if it can't."""
    return _COMPACTION_REQUESTS.get(normalize_backend(backend))


def normalize_backend(value: object) -> str:
    """Return a supported backend id, defaulting to Claude Code."""
    if not isinstance(value, str):
        return BACKEND_CLAUDE
    compact = re.sub(r"[\s_-]+", "", value).casefold()
    aliases = {
        "claude": BACKEND_CLAUDE,
        "claudecode": BACKEND_CLAUDE,
        "codex": BACKEND_CODEX,
        "freebuff": BACKEND_FREEBUFF,
        "opencode": BACKEND_OPENCODE,
        "opencodeai": BACKEND_OPENCODE,
        "hermes": BACKEND_HERMES,
        "hermesagent": BACKEND_HERMES,
        "nous": BACKEND_HERMES,
        "muse": BACKEND_MUSE,
        "musecode": BACKEND_MUSE,
        "meta": BACKEND_MUSE,
        "museai": BACKEND_MUSEAI,
        "muse.ai": BACKEND_MUSEAI,
        "musecli": BACKEND_MUSEAI,
        # "cmd" is one of Command Code's launcher names, and a stored settings
        # value using it should resolve here rather than fall back to Claude.
        "commandcode": BACKEND_COMMANDCODE,
        "cmd": BACKEND_COMMANDCODE,
        "gemini": BACKEND_GEMINI,
        "geminicli": BACKEND_GEMINI,
        "googlegemini": BACKEND_GEMINI,
        "antigravity": BACKEND_ANTIGRAVITY,
        "antigravitycli": BACKEND_ANTIGRAVITY,
        "googleantigravity": BACKEND_ANTIGRAVITY,
        "agy": BACKEND_ANTIGRAVITY,
        "copilot": BACKEND_COPILOT,
        "copilotcli": BACKEND_COPILOT,
        "githubcopilot": BACKEND_COPILOT,
        "githubcopilotcli": BACKEND_COPILOT,
    }
    return aliases.get(compact, BACKEND_CLAUDE)


def backend_label(backend: str) -> str:
    return BACKEND_LABELS[normalize_backend(backend)]


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


def _claude_usage_reply(stdout: "IO[str]", request_id: str) -> Optional[dict]:
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


def _claude_usage_exchange(proc: "subprocess.Popen[str]") -> Optional[dict]:
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
        with _OPENCODE_CATALOG_LOCK:
            _opencode_catalog_cache.clear()
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


class _Unbound:
    """No Account taken for this conversation yet: read the saved choice.

    ``None`` is a real answer -- Codex's own login -- so "nothing chosen yet"
    cannot be spelled as ``None`` as well. A tab tells a turn which Account its
    conversation is bound to; one that is not bound yet is told this, and the
    turn reads the saved choice once and keeps it for the rest of the turn.
    """

    def __repr__(self) -> str:
        return "UNBOUND"


UNBOUND = _Unbound()


class _TurnWorker(threading.Thread):
    """What every turn in this module is built on, whichever backend runs it.

    The three workers below differ in how they talk to a provider, not in how
    a turn starts and stops: each is handed the same message, conversation and
    six callbacks, each has to say why it stopped exactly once, and each has to
    report a crash rather than leave the window silent with Send re-enabled.
    Written out per backend, that scaffold was three copies, and a fix to one
    of them was a fix to one third of the app.

    A subclass supplies `_do_run` (the turn itself), `_setup` for the fields
    its own protocol needs, and, if it borrowed anything, `_teardown`. It does
    not restate the signature: the window builds whichever worker the backend
    chose from one set of arguments, and writing that set out once is what
    keeps the three of them interchangeable.
    """

    # This backend as `BACKENDS` names it: the diagnostics record is filed
    # under it, and a crash report calls the provider by its label.
    _backend = ""

    def __init__(
        self,
        prompt: str,
        session_id: Optional[str],
        cwd: str,
        permission_mode: str,
        *,
        model: str = "",
        effort: str = "",
        compact: bool = False,
        attachments: tuple[str, ...] = (),
        on_session: Callable[[str], None],
        on_started: Callable[[], None],
        on_activity: Callable[[str, str], None],
        on_complete: Callable[[str], None],
        on_failed: Callable[[str], None],
        on_done: Callable[[], None],
        on_question: Optional[questions.AskQuestions] = None,
        on_permission: Optional[Callable[[str, dict, list], dict]] = None,
        on_subagent: Optional[SubagentReport] = None,
        plan_account: object = UNBOUND,
    ) -> None:
        super().__init__(daemon=True)
        self._prompt = prompt
        self._session_id = session_id
        self._cwd = cwd
        self._permission_mode = permission_mode
        self._model = model
        self._effort = effort
        # Compaction is a request of its own rather than a message, so this
        # turn summarises the conversation instead of adding to it. Backends
        # that cannot compact are never asked to (see `supports_compaction`).
        self._compact = compact
        # Files handed to the turn itself rather than written into the prompt.
        # Only the backends that take them read this; the rest never see one.
        self._attachments = list(attachments or [])
        self._on_session = on_session
        self._on_started = on_started
        self._on_activity = on_activity
        self._on_complete = on_complete
        self._on_failed = on_failed
        self._on_done = on_done
        self._on_question = on_question
        self._on_permission = on_permission
        self._on_subagent = on_subagent or ignore_subagents
        # The Account the turn's conversation is bound to, or `UNBOUND` to take
        # the saved choice. Only Codex reads this; the rest never see one. It
        # is `object` because `None` is itself a binding -- Codex's own login.
        self._plan_account = plan_account
        self._cancelled = False
        # Set once the turn's ending has been reported, whichever way it
        # ended. The first account of a failure is the one that can be acted
        # on; a crash while cleaning up after it is not worth speaking over
        # the top of it. An Event rather than a flag because either thread can
        # get there first -- a turn can end on the reader while the window's
        # own thread is failing it.
        self._settled = threading.Event()
        self._accepting_input = threading.Event()
        self._setup()

    def _setup(self) -> None:
        """The state this backend's turn needs on top of the shared fields."""

    def accepting_input(self) -> bool:
        return self._accepting_input.is_set() and not self._cancelled

    def _fail(self, message: str) -> None:
        """Report why the turn ended, once."""
        if self._settled.is_set():
            return
        self._settled.set()
        diagnostics.log_unfinished_turn(
            self._backend,
            session_id=self._session_id or "(new)",
            permission_mode=self._permission_mode,
            model=self._model or "(default)",
            cancelled=self._cancelled,
            detail=message,
        )
        self._on_failed(message)

    def run(self) -> None:
        try:
            self._do_run()
        except Exception as exc:
            # `finally` re-enables Send and stops the progress earcon either
            # way, so anything thrown here used to end the turn exactly as a
            # finished one ends - with no answer and nothing said. The
            # traceback went to a stderr the windowed build does not have.
            self._fail(f"BlindPilot stopped reading {backend_label(self._backend)}: {exc}")
        finally:
            self._accepting_input.clear()
            self._teardown()
            self._on_done()

    def _do_run(self) -> None:
        """The turn itself. Every subclass has one."""
        raise NotImplementedError

    def _teardown(self) -> None:
        """Let go of whatever this turn borrowed, however it ended."""


def _line_change_counts(old: str, new: str) -> tuple[int, int]:
    """Lines added and removed between two versions of a block.

    A real diff rather than a count of the lines involved: replacing a
    twenty-line function to change two of its lines is a two-line edit, and
    "twenty added, twenty removed" would be a worse answer than none.
    """
    added = removed = 0
    matcher = difflib.SequenceMatcher(None, old.splitlines(), new.splitlines(), autojunk=False)
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            removed += old_end - old_start
        if tag in ("replace", "insert"):
            added += new_end - new_start
    return added, removed


def _lines_phrase(added: int, removed: int) -> str:
    """ ", 5 lines added, 3 removed" - or nothing at all.

    Appended to the line the tool call already has rather than spoken as a
    second one: this narration is long enough to fall behind on a fan-out
    without adding an utterance per edit. Halves that are zero are left out,
    because "0 removed" is a word said for no reason every time.
    """
    parts = []
    if added:
        parts.append(f"{added} line{'' if added == 1 else 's'} added")
    if removed:
        # "added" already carried the word "lines" when both are present.
        word = "" if added else f" line{'' if removed == 1 else 's'}"
        parts.append(f"{removed}{word} removed")
    return ", " + ", ".join(parts) if parts else ""


def _tool_use_label(name: str, params: dict) -> str:
    """One spoken line describing the tool Claude just invoked.

    This is the narration that answers "what is it doing right now" — the CLI
    does not forward thinking blocks in print mode, so the tool calls are the
    live signal. Phrased as an action ("Reading foo.py") rather than a raw tool
    name and JSON blob.
    """

    def first(*keys: str) -> str:
        for key in keys:
            value = params.get(key)
            if isinstance(value, str) and value.strip():
                return " ".join(value.split())
        return ""

    target = first("file_path", "path", "notebook_path")
    short = os.path.basename(target) if target else ""

    def text(key: str) -> str:
        value = params.get(key)
        return value if isinstance(value, str) else ""

    if name == "Read":
        return f"Reading {short}" if short else "Reading a file"
    if name in ("Edit", "NotebookEdit", "MultiEdit"):
        # How much it changed is the only sense of scale available to somebody
        # who cannot see the diff, and "changed a line" and "rewrote the file"
        # are the same sentence without it.
        edits = params.get("edits")
        if isinstance(edits, list):
            added = removed = 0
            for edit in edits:
                if not isinstance(edit, dict):
                    continue
                one, two = edit.get("old_string"), edit.get("new_string")
                if isinstance(one, str) and isinstance(two, str):
                    more, fewer = _line_change_counts(one, two)
                    added += more
                    removed += fewer
        else:
            added, removed = _line_change_counts(text("old_string"), text("new_string"))
        where = f"Editing {short}" if short else "Editing a file"
        return where + _lines_phrase(added, removed)
    if name == "Write":
        where = f"Writing {short}" if short else "Writing a file"
        written = len(text("content").splitlines())
        if not written:
            return where
        return f"{where}, {written} line{'' if written == 1 else 's'}"
    if name in ("Bash", "PowerShell"):
        cmd = first("command")
        return f"Running: {cmd}" if cmd else f"Running a {name} command"
    if name in ("Grep", "Glob"):
        pattern = first("pattern")
        return f"Searching for {pattern}" if pattern else "Searching"
    if name in ("WebFetch", "WebSearch"):
        what = first("url", "query")
        return f"Fetching {what}" if what else "Searching the web"
    if name == "Task":
        return f"Delegating: {first('description') or 'a subtask'}"
    if name == "TodoWrite":
        return "Updating the task list"
    detail = first("description", "command", "query", "prompt")
    return f"Using {name}: {detail}" if detail else f"Using {name}"


_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[()][A-Z0-9])")


def _strip_terminal_noise(text: str) -> str:
    text = _ANSI_RE.sub("", text).replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ------------------------------------------------------------------------
# opencode
#
# opencode is driven through its own headless HTTP server rather than through
# one process per turn, because the server is the same surface its terminal
# interface talks to and is the only one that exposes everything BlindPilot
# needs from a provider: a streaming answer, steering a turn that is already
# running, answering a permission request, compaction, the catalog behind
# ``/model``, and the provider catalog behind ``/connect``. One server is
# started on first use and shared by every tab. It listens on the loopback
# interface behind a password generated for this run, so nothing else on the
# machine can drive it.

_OPENCODE_SERVER_LOCK = threading.Lock()
_opencode_server: Optional["OpencodeServer"] = None
_OPENCODE_CATALOG_LOCK = threading.Lock()
# One catalog per working directory: a project can pin its own model list.
_opencode_catalog_cache: dict[str, dict] = {}

# Effort levels are per-model in opencode — a model's "variants". The picker
# wants one list, so the levels every model offers are pooled and shown in the
# order a person would expect rather than the order they were discovered in.
_OPENCODE_EFFORT_ORDER = ("none", "minimal", "low", "medium", "high", "max", "thinking")

# What BlindPilot's provider-neutral permission modes mean to opencode. A rule
# set is applied in order, so the wildcard comes first and the exceptions
# follow, which is the shape opencode's own agents use. "default" is absent on
# purpose: it means "whatever opencode itself is configured to do", so no rule
# set is sent at all.
_OPENCODE_PERMISSIONS: dict[str, list[dict[str, str]]] = {
    "acceptEdits": [
        {"permission": "*", "pattern": "*", "action": "allow"},
        {"permission": "bash", "pattern": "*", "action": "ask"},
        {"permission": "external_directory", "pattern": "*", "action": "ask"},
        {"permission": "doom_loop", "pattern": "*", "action": "ask"},
    ],
    "plan": [
        {"permission": "*", "pattern": "*", "action": "allow"},
        {"permission": "edit", "pattern": "*", "action": "deny"},
        {"permission": "bash", "pattern": "*", "action": "ask"},
        {"permission": "external_directory", "pattern": "*", "action": "deny"},
    ],
    "auto": [
        {"permission": "*", "pattern": "*", "action": "allow"},
        {"permission": "external_directory", "pattern": "*", "action": "ask"},
    ],
    "bypassPermissions": [
        {"permission": "*", "pattern": "*", "action": "allow"},
    ],
}


def _opencode_data_dir() -> Path:
    """Where opencode keeps its database and credentials, on every platform."""
    override = os.environ.get("OPENCODE_DATA")
    if override:
        return Path(override)
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "opencode"


def _free_port() -> int:
    """A port the operating system says is free right now."""
    import socket

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _opencode_server_binary(binary: str) -> str:
    """Prefer opencode's own executable over an npm wrapper on Windows.

    Same reason Codex gets the same treatment: terminating a ``.cmd`` launcher
    does not terminate the native child it started, and a server nobody owns
    keeps both its port and its database open for the rest of the session.
    """
    if platform.system() != "Windows" or Path(binary).suffix.casefold() == ".exe":
        return binary
    candidate = Path(binary).parent / "node_modules" / "opencode-ai" / "bin" / "opencode.exe"
    return str(candidate) if candidate.is_file() else binary


def opencode_error_text(error: object, fallback: str) -> str:
    """A sentence worth speaking out of whatever the server or urllib raised."""
    import urllib.error

    if isinstance(error, urllib.error.HTTPError):
        try:
            payload = json.loads(error.read().decode("utf-8", "replace"))
        except (OSError, ValueError):
            payload = None
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, dict) and isinstance(data.get("message"), str):
                return data["message"]
            for key in ("message", "error"):
                if isinstance(payload.get(key), str) and payload[key]:
                    return str(payload[key])
        return f"{fallback} (HTTP {error.code})"
    if isinstance(error, dict):
        data = error.get("data")
        if isinstance(data, dict) and isinstance(data.get("message"), str):
            return data["message"]
        for key in ("message", "name"):
            if isinstance(error.get(key), str) and error[key]:
                return str(error[key])
    text = str(error).strip()
    return text or fallback


class OpencodeServer:
    """The one ``opencode serve`` process BlindPilot talks to."""

    def __init__(self, binary: str) -> None:
        import base64
        import secrets
        from collections import deque

        password = secrets.token_urlsafe(24)
        env = subprocess_env(binary)
        env["OPENCODE_SERVER_PASSWORD"] = password
        self._log: "deque[str]" = deque(maxlen=50)
        self._url = ""
        self._auth = "Basic " + base64.b64encode(f"opencode:{password}".encode("utf-8")).decode(
            "ascii"
        )
        # The probe in _free_port closes before this child can bind, so
        # another process may take the port in between; a second attempt on a
        # fresh port turns that race into a slower start instead of a failed
        # turn. The child exits promptly when the port is gone, so a failed
        # attempt does not wait out the 60 seconds.
        # shortcut: three attempts, enough for a busy machine; raise with the
        # wait above if start-up races stay common.
        for _attempt in range(3):
            self._listening = threading.Event()
            # Started from the home directory rather than any one project: one
            # server serves them all, and the ``directory`` query parameter on each
            # request is what says which project a call is about.
            self._proc = owned_popen(
                [binary, "serve", "--port", str(_free_port()), "--hostname", "127.0.0.1"],
                cwd=str(Path.home()),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                encoding="utf-8",
                errors="replace",
                env=env,
                **process_utils.own_group_kwargs(),
                **process_utils.no_window_kwargs(),
            )
            threading.Thread(target=self._pump, daemon=True).start()
            self._listening.wait(60)
            if self._url:
                break
            self.stop()
        if not self._url:
            detail = "\n".join(list(self._log)[-5:]).strip()
            raise OSError(detail or "opencode's server did not start.")

    # The reader doubles as the thing that keeps the pipe from filling up, so
    # it runs for the life of the process rather than only until start-up.
    def _pump(self) -> None:
        stdout = self._proc.stdout
        if stdout is not None:
            for line in stdout:
                text = line.rstrip()
                if text:
                    self._log.append(text)
                if not self._url:
                    found = re.search(r"listening on (http://\S+)", text)
                    if found:
                        self._url = found.group(1).rstrip("/")
                        self._listening.set()
        # The process ended. Release anyone still waiting to be told the URL.
        self._listening.set()

    @property
    def base_url(self) -> str:
        return self._url

    def alive(self) -> bool:
        return self._proc.poll() is None

    def open(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        body: object = None,
        timeout: Optional[float] = 120,
    ):
        """The raw response, for callers that want to read it as it arrives."""
        import urllib.parse
        import urllib.request

        url = self._url + path
        query = {key: value for key, value in (params or {}).items() if value}
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, method=method)
        request.add_header("Authorization", self._auth)
        if data is not None:
            request.add_header("Content-Type", "application/json")
        return urllib.request.urlopen(request, timeout=timeout)

    def request(
        self,
        method: str,
        path: str,
        params: Optional[dict] = None,
        body: object = None,
        timeout: Optional[float] = 120,
    ) -> object:
        with self.open(method, path, params, body, timeout) as response:
            raw = response.read().decode("utf-8", "replace")
        return json.loads(raw) if raw.strip() else None

    def stop(self) -> None:
        proc = self._proc
        try:
            if proc.poll() is None:
                if platform.system() != "Windows":
                    # It owns a SQLite database, so it is asked to close before
                    # it is made to. On Windows terminate() is TerminateProcess,
                    # which asks nothing and would leave its children behind, so
                    # there the tree is ended in one go below.
                    try:
                        proc.terminate()
                        proc.wait(timeout=5)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                process_utils.end_process_group(proc, timeout=5)
        finally:
            # Released whether the process was stopped just now or had already
            # exited on its own: a dead server's stdout pipe is still a handle
            # until something closes it.
            process_utils._close_process_pipes(proc)


def opencode_server() -> OpencodeServer:
    """The shared server, started on first use and reused from then on."""
    global _opencode_server
    with _OPENCODE_SERVER_LOCK:
        running = _opencode_server
        if running is not None and running.alive():
            return running
        binary = find_backend_cli(BACKEND_OPENCODE)
        if not binary:
            raise OSError("opencode is not installed. Run: npm install -g opencode-ai")
        _opencode_server = OpencodeServer(_opencode_server_binary(binary))
        return _opencode_server


def stop_opencode_server() -> None:
    """Shut the shared server down — on exit, and before an update replaces it."""
    global _opencode_server
    with _OPENCODE_SERVER_LOCK:
        running, _opencode_server = _opencode_server, None
    if running is not None:
        running.stop()


atexit.register(stop_opencode_server)


def _opencode_catalog(cwd: Optional[str] = None, refresh: bool = False) -> dict:
    """opencode's providers, their models, and the defaults it would pick.

    Asked per directory, because a project's own ``opencode.json`` can pin a
    model or turn providers off, and cached per directory for the same reason.
    Caching matters: the catalog is hundreds of models across nearly two
    hundred providers, and the picker is opened far more often than a
    project's set of providers changes.
    """
    key = cwd or ""
    with _OPENCODE_CATALOG_LOCK:
        cached = _opencode_catalog_cache.get(key)
    if cached is not None and not refresh:
        return cached
    server = opencode_server()
    params = {"directory": cwd} if cwd else None
    providers = server.request("GET", "/config/providers", params=params, timeout=60)
    config = server.request("GET", "/config", params=params, timeout=60)
    commands = server.request("GET", "/command", params=params, timeout=60)
    providers = providers if isinstance(providers, dict) else {}
    config = config if isinstance(config, dict) else {}
    catalog = {
        "providers": providers.get("providers") or [],
        "default": providers.get("default") or {},
        "model": config.get("model") or "",
        "commands": _opencode_command_list(commands),
    }
    with _OPENCODE_CATALOG_LOCK:
        _opencode_catalog_cache[key] = catalog
    return catalog


def _opencode_command_list(payload: object) -> list[tuple[str, str]]:
    """opencode's slash commands, as (name, description).

    Only the commands proper: opencode lists installed skills here as well, and
    a picker a person arrows through is worth keeping to the length of what
    they typed a slash to find.
    """
    commands: list[tuple[str, str]] = []
    for entry in payload if isinstance(payload, list) else []:
        if not isinstance(entry, dict) or entry.get("source") != "command":
            continue
        name = str(entry.get("name") or "")
        if name:
            commands.append((name, str(entry.get("description") or "")))
    commands.sort()
    return commands


def opencode_commands(cwd: Optional[str] = None) -> list[tuple[str, str]]:
    """The commands read for this directory, or none if it has not been read.

    Deliberately never asks the server: the slash picker opens on a key press,
    and the catalog is already read in the background whenever opencode is the
    chosen backend.
    """
    with _OPENCODE_CATALOG_LOCK:
        catalog = _opencode_catalog_cache.get(cwd or "")
    commands = catalog.get("commands") if catalog else None
    return list(commands) if isinstance(commands, list) else []


def _opencode_models_from_cli() -> list[str]:
    """The catalog as the CLI prints it, for when the server will not start."""
    binary = find_backend_cli(BACKEND_OPENCODE)
    if not binary:
        return []
    try:
        result = subprocess.run(
            [binary, "models"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            env=subprocess_env(binary),
            **process_utils.no_window_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    models: list[str] = []
    for line in (result.stdout or "").splitlines():
        name = line.strip()
        if name and "/" in name and " " not in name and name not in models:
            models.append(name)
    return models


def opencode_model_options(
    cwd: Optional[str] = None,
) -> tuple[list[str], list[str], str, str, str]:
    """Every model opencode can reach, the effort levels they offer, and the
    model it would use if BlindPilot named none."""
    try:
        catalog = _opencode_catalog(cwd)
    except (OSError, ValueError) as exc:
        models = _opencode_models_from_cli()
        if not models:
            return [], [], "", "", opencode_error_text(exc, "opencode's model list is unavailable.")
        return (
            models,
            list(_OPENCODE_EFFORT_ORDER),
            "",
            "",
            "Could not reach opencode's server; showing the list its CLI prints.",
        )

    models = []
    efforts: list[str] = []
    for provider in catalog["providers"]:
        if not isinstance(provider, dict):
            continue
        provider_id = str(provider.get("id") or "")
        entries = provider.get("models")
        if not provider_id or not isinstance(entries, dict):
            continue
        for model_id, model in sorted(entries.items()):
            models.append(f"{provider_id}/{model_id}")
            variants = model.get("variants") if isinstance(model, dict) else None
            if isinstance(variants, dict):
                for variant in variants:
                    if variant not in efforts:
                        efforts.append(variant)

    known = [effort for effort in _OPENCODE_EFFORT_ORDER if effort in efforts]
    efforts = known + sorted(effort for effort in efforts if effort not in known)
    if not models:
        return [], efforts, "", "", "opencode has no connected providers yet. Type /connect."
    return models, efforts, opencode_default_model(catalog=catalog), "", ""


def opencode_default_model(cwd: Optional[str] = None, catalog: Optional[dict] = None) -> str:
    """The ``provider/model`` opencode itself would run, or "" if it has none."""
    try:
        catalog = catalog if catalog is not None else _opencode_catalog(cwd)
    except (OSError, ValueError):
        return ""
    configured = catalog.get("model")
    if isinstance(configured, str) and "/" in configured:
        return configured
    defaults = catalog.get("default") or {}
    for provider in catalog.get("providers") or []:
        if not isinstance(provider, dict):
            continue
        provider_id = str(provider.get("id") or "")
        model_id = defaults.get(provider_id)
        if provider_id and isinstance(model_id, str) and model_id:
            return f"{provider_id}/{model_id}"
    return ""


def opencode_split_model(model: str) -> tuple[str, str]:
    """``provider/model`` as the two halves the server asks for separately."""
    provider, _, name = (model or "").partition("/")
    return (provider.strip(), name.strip()) if name.strip() else ("", "")


def opencode_model_efforts(model: str, cwd: Optional[str] = None) -> list[str]:
    """The effort levels this one model offers, so an unusable one is dropped.

    opencode rejects a variant a model does not define, and the picker offers
    the levels pooled across every model, so the choice has to be checked
    against the model it is about to be sent with.
    """
    provider_id, model_id = opencode_split_model(model)
    if not provider_id:
        return []
    try:
        catalog = _opencode_catalog(cwd)
    except (OSError, ValueError):
        return []
    for provider in catalog["providers"]:
        if isinstance(provider, dict) and provider.get("id") == provider_id:
            entry = (provider.get("models") or {}).get(model_id)
            variants = entry.get("variants") if isinstance(entry, dict) else None
            return list(variants) if isinstance(variants, dict) else []
    return []


# ---- /connect: the providers, and the credentials that reach them ----


def opencode_providers() -> tuple[list[tuple[str, str]], set[str], str]:
    """Every provider opencode knows, and which of them are already connected.

    This is what its ``/connect`` command offers. The ones already reachable
    come first, so the list opens on what is actually in use.
    """
    try:
        server = opencode_server()
        payload = server.request("GET", "/provider", timeout=60)
    except (OSError, ValueError) as exc:
        return [], set(), opencode_error_text(exc, "Could not read opencode's provider list.")
    if not isinstance(payload, dict):
        return [], set(), "opencode returned no provider list."
    connected = {str(name) for name in (payload.get("connected") or [])}
    everything: list[tuple[str, str]] = []
    for provider in payload.get("all") or []:
        if not isinstance(provider, dict):
            continue
        provider_id = str(provider.get("id") or "")
        if provider_id:
            everything.append((provider_id, str(provider.get("name") or provider_id)))
    everything.sort(key=lambda item: (item[0] not in connected, item[1].casefold()))
    return everything, connected, ""


def opencode_auth_methods(provider_id: str) -> list[dict]:
    """How this provider can be signed in to.

    Providers with nothing special to say are absent from opencode's own list;
    for them the only way in is an API key, which is what the fallback says.
    """
    try:
        server = opencode_server()
        payload = server.request("GET", "/provider/auth", timeout=60)
    except (OSError, ValueError):
        payload = None
    methods = payload.get(provider_id) if isinstance(payload, dict) else None
    if isinstance(methods, list) and methods:
        return [method for method in methods if isinstance(method, dict)]
    return [{"type": "api", "label": "Manually enter API key"}]


def _opencode_write(
    method: str, path: str, body: Optional[dict], timeout: int, failure: str
) -> str:
    """Change what opencode has stored. Returns "" on success, else the error.

    Every one of these writes leaves the cached provider list stale, so the
    invalidation belongs with the write rather than with each caller: forget
    it once and the sign-in screen goes on showing an account that has just
    been disconnected.
    """
    try:
        opencode_server().request(method, path, body=body, timeout=timeout)
    except (OSError, ValueError) as exc:
        return opencode_error_text(exc, failure)
    invalidate_backend_cache(BACKEND_OPENCODE)
    return ""


def opencode_connect_api_key(provider_id: str, key: str, metadata: Optional[dict] = None) -> str:
    """Store an API key for a provider. Returns "" on success, else the error."""
    body: dict = {"type": "api", "key": key}
    if metadata:
        body["metadata"] = {str(k): str(v) for k, v in metadata.items() if v}
    return _opencode_write(
        "PUT", f"/auth/{provider_id}", body, 60, f"Could not connect {provider_id}."
    )


def opencode_disconnect(provider_id: str) -> str:
    """Forget a provider's credentials. Returns "" on success, else the error."""
    return _opencode_write(
        "DELETE", f"/auth/{provider_id}", None, 60, f"Could not disconnect {provider_id}."
    )


def opencode_oauth_start(
    provider_id: str, method: int, inputs: Optional[dict] = None
) -> tuple[dict, str]:
    """Begin a browser sign-in. Returns (authorization, error).

    The authorization carries the URL to open, instructions worth reading out,
    and whether the provider finishes on its own ("auto") or hands back a code
    the user has to paste ("code").
    """
    body: dict = {"method": method}
    if inputs:
        body["inputs"] = {str(k): str(v) for k, v in inputs.items() if v}
    try:
        payload = opencode_server().request(
            "POST", f"/provider/{provider_id}/oauth/authorize", body=body, timeout=120
        )
    except (OSError, ValueError) as exc:
        return {}, opencode_error_text(exc, f"Could not start sign-in for {provider_id}.")
    return (payload if isinstance(payload, dict) else {}), ""


def opencode_oauth_finish(provider_id: str, method: int, code: str = "") -> str:
    """Complete a browser sign-in. Returns "" on success, else the error."""
    body: dict = {"method": method}
    if code:
        body["code"] = code
    return _opencode_write(
        "POST",
        f"/provider/{provider_id}/oauth/callback",
        body,
        300,
        f"Could not finish signing in to {provider_id}.",
    )


def _opencode_tool_label(name: str, arguments: object) -> str:
    """One spoken line for a tool that has just started."""
    values = arguments if isinstance(arguments, dict) else {}
    detail = ""
    for key in ("command", "filePath", "path", "pattern", "query", "url", "description"):
        value = values.get(key)
        if isinstance(value, str) and value.strip():
            detail = " ".join(value.split())
            break
    verbs = {
        "bash": "Running",
        "edit": "Editing",
        "write": "Writing",
        "read": "Reading",
        "glob": "Finding files",
        "grep": "Searching",
        "list": "Listing",
        "webfetch": "Fetching",
        "websearch": "Searching the web",
        "task": "Delegating",
    }
    verb = verbs.get(name, f"Using {name}")
    return f"{verb}: {detail}" if detail else verb


# An assistant message the provider refuses on replay poisons the whole
# conversation: opencode rebuilds the request from its stored history every
# step, so once one malformed message is in, every later turn fails with the
# same 400 until the message is gone. DeepSeek-class providers report it as
# "content or tool_calls must be set"; the paired complaint about tool results
# left dangling is the same break in a different coat.
_POISON_HISTORY_RE = re.compile(
    r"Invalid assistant message|tool_calls['?]? must be followed by tool messages"
    r"|missing in assistant tool call message|reasoning_content.*must be passed back",
    re.IGNORECASE,
)


def _opencode_entry_info(entry: dict) -> dict:
    """The `info` block of a history entry, or the entry itself.

    opencode nests message metadata under `info` in some shapes and inlines it
    in others. Written inline as a conditional this called `.get` twice, so the
    guard tested one value and the code then used another - harmless in
    practice, and exactly the shape that stops being harmless after an edit.
    """
    found = entry.get("info")
    return found if isinstance(found, dict) else entry


def _poison_history_error(text: str) -> bool:
    """Whether a backend error says the stored history itself was refused."""
    return bool(_POISON_HISTORY_RE.search(text or ""))


def _opencode_questions(raw: object) -> tuple[questions.Question, ...]:
    """Read a question.asked event into BlindPilot's own question shape.

    opencode's own flags are `multiple` for more than one answer and `custom`
    for a typed one. Only `multiple` is trusted as written: its tool tells the
    model not to offer an "Other" of its own because the client adds one, so a
    typed answer is offered whether or not `custom` was set.
    """
    return questions._questions(raw, multi_key="multiple")


# Magic bytes to media types for attached pictures. The server has no upload
# endpoint, so bytes travel inline with the prompt as data: URLs.
_OPENCODE_IMAGE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)

# Anything past this is named by path rather than sent as bytes: the model
# gets a file it can open instead of a payload that dwarfs the prompt.
_OPENCODE_MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024


def _opencode_file_part(path: str) -> Optional[dict]:
    """One attached file as a prompt file part, or None to name it in text.

    Pictures travel as data: URLs with their media type (measured: a 1x1 PNG
    round-tripped through prompt_async and was stored back with its filename),
    and so does anything that reads as text; anything else -- an unreadable
    file, a picture past the cap, bytes no model would read -- is named by its
    path in the prompt text instead.
    """
    import base64

    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    if not raw or len(raw) > _OPENCODE_MAX_ATTACHMENT_BYTES:
        return None
    mime = ""
    for magic, kind in _OPENCODE_IMAGE_MAGIC:
        if raw.startswith(magic):
            mime = kind
            break
    if not mime and raw.startswith(b"RIFF") and raw[8:12] == b"WEBP":
        mime = "image/webp"
    if not mime:
        try:
            raw.decode("utf-8")
        except ValueError:
            return None
        mime = "text/plain"
    return {
        "type": "file",
        "mime": mime,
        "filename": Path(path).name,
        "url": f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}",
    }


def _opencode_file_name(value: object) -> str:
    """The bare filename of a path the server wrote, whichever slashes it used."""
    text = str(value or "")
    return text.replace("\\", "/").rsplit("/", 1)[-1] if text else ""


class OpencodeWorker(_TurnWorker):
    """Run one opencode turn against the shared headless server."""

    _backend = BACKEND_OPENCODE

    # Every route this worker calls names the session, so a turn that has not
    # got one yet carries the empty string rather than None.
    _session_id: str

    def _setup(self) -> None:
        self._session_id = self._session_id or ""
        self._server: Optional[OpencodeServer] = None
        self._stream: object = None
        # Resolved once the turn starts. Working out whether a model offers the
        # chosen effort can mean reading opencode's catalog, and a message can
        # be steered into a running turn from the window's own thread.
        self._variant = ""
        self._roles: dict[str, str] = {}
        self._emitted: set[str] = set()
        self._answer: list[str] = []
        # Child sessions the task tool started for this conversation, with
        # where each stands, and which task part started each. A child
        # shares the event stream and asks its permissions there too.
        self._children: dict[str, str] = {}
        self._task_children: dict[str, str] = {}
        self._tools_running: set[str] = set()
        # Set when a question was answered this turn. The provider poison that
        # a broken question replay leaves behind is only worth the surgery
        # below where a question was actually part of the turn.
        self._question_answered = False
        # One repair per turn: if the retry is refused too, report it rather
        # than looping.
        self._history_repaired = False

    # ----- what the window drives -----

    def steer(self, text: str) -> bool:
        """Add a message to the turn that is already running.

        opencode admits a prompt sent mid-turn and hands it to the model at the
        next step, which is exactly what steering means here.

        Answers from what this worker already knows and sends on a thread of
        its own, because this is called from the window's thread: a request
        waited on there is a window that stops answering the screen reader.
        Whether the turn is still accepting input is the question being asked,
        and that is known here; a message opencode then refuses is rare enough
        to belong in the transcript rather than in a frozen window.
        """
        if not self.accepting_input() or not self._session_id or self._server is None:
            return False
        server, session = self._server, self._session_id
        # A steering message is its own message, not the prompt again: the
        # turn's attachments went out with the prompt and stay there.
        body = self._prompt_body(text, with_attachments=False)

        def deliver() -> None:
            try:
                server.request(
                    "POST",
                    f"/session/{session}/prompt_async",
                    params={"directory": self._cwd},
                    body=body,
                    timeout=60,
                )
            except (OSError, ValueError) as exc:
                if not self._cancelled:
                    detail = opencode_error_text(exc, "the request failed")
                    self._on_activity(
                        "tool", f"opencode did not take the steering message: {detail}"
                    )

        threading.Thread(target=deliver, daemon=True).start()
        return True

    def cancel(self) -> None:
        self._cancelled = True
        self._accepting_input.clear()
        server, session = self._server, self._session_id
        if server is not None:
            # Aborting the conversation does not abort the sessions its task
            # tool opened; each running agent is aborted by its own id.
            for child, status in list(self._children.items()):
                if status != SUBAGENT_RUNNING:
                    continue
                try:
                    server.request(
                        "POST",
                        f"/session/{child}/abort",
                        params={"directory": self._cwd},
                        timeout=10,
                    )
                except (OSError, ValueError):
                    pass
        if server is not None and session:
            try:
                server.request(
                    "POST",
                    f"/session/{session}/abort",
                    params={"directory": self._cwd},
                    timeout=30,
                )
            except (OSError, ValueError):
                pass
        # Closing the event stream is what unblocks the run loop. The server
        # itself is shared, and stays up for the next turn.
        self._close_stream()

    def _teardown(self) -> None:
        self._close_stream()

    def _close_stream(self) -> None:
        stream = self._stream
        if stream is not None:
            try:
                stream.close()  # type: ignore[attr-defined]
            except Exception:
                pass

    # ----- the turn -----

    def _on_session_error(self, properties: dict) -> bool:
        """A session.error ends the turn — unless one repair attempt fits.

        A provider that refuses a stored message refuses every following
        request too, so an ordinary failure here would leave the conversation
        permanently stuck behind a turn that can never be replayed. The one
        break BlindPilot itself can walk into is a question round-trip, so
        when the error reads like refused history and a question was answered
        this turn, the question's step is deleted and the prompt sent again.

        Returns False when the turn is over for good (failure reported, or the
        reader should keep going on the repaired conversation).
        """
        text = opencode_error_text(properties.get("error"), "opencode reported an error")
        if not (_poison_history_error(text) and self._question_answered):
            self._fail(text)
            return False
        if self._history_repaired or self._cancelled:
            self._fail(text)
            return False
        self._history_repaired = True
        if self._repair_history():
            self._on_activity(
                "tool",
                "opencode refused the conversation after the question; "
                "removing the broken step and trying again.",
            )
            self._resend()
            return True
        self._fail(text)
        return False

    def _repair_history(self) -> bool:
        """Delete the poisoned question step and everything after it.

        The message the provider cannot replay is two-fold: the question
        step whose tool call comes back unpaired, and the empty assistant
        step it died on — neither content nor tool calls, which is the very
        text of the 400. Both go, and so does anything written after them,
        since every later step is refused for the same reason.

        Returns True when something was actually removed. The listing's order
        is not counted on: the question step to cut at is the latest one by
        its own timestamp.
        """
        server = self._server
        session = self._session_id
        if server is None or not session:
            return False
        try:
            messages = server.request(
                "GET", f"/session/{session}/message", params={"directory": self._cwd}, timeout=60
            )
        except (OSError, ValueError):
            return False
        entries = [
            entry
            for entry in (messages if isinstance(messages, list) else [])
            if isinstance(entry, dict)
        ]

        def stamp(entry: dict) -> float:
            info = _opencode_entry_info(entry)
            stamped = info.get("time")
            time = stamped if isinstance(stamped, dict) else {}
            try:
                return float(time.get("created") or info.get("time_created") or 0)
            except (TypeError, ValueError):
                return 0.0

        cutoff_id = ""
        cutoff_stamp = -1.0
        for entry in entries:
            info = _opencode_entry_info(entry)
            if str(info.get("role") or "") != "assistant":
                continue
            message_id = str(info.get("id") or "")
            if not message_id:
                continue
            listed = entry.get("parts")
            parts = listed if isinstance(listed, list) else []
            for part in parts:
                if not isinstance(part, dict) or str(part.get("tool") or "") != "question":
                    continue
                reported = part.get("state")
                state = reported if isinstance(reported, dict) else {}
                if str(state.get("status") or "") != "completed":
                    continue
                when = stamp(entry)
                if when >= cutoff_stamp:
                    cutoff_id, cutoff_stamp = message_id, when
                break
        if not cutoff_id:
            return False
        # The step the question died on has no timestamp of its own worth
        # trusting, so the cut is by position in the listing relative to the
        # question step, and by time for anything ordered oddly.
        try:
            index = next(
                position
                for position, entry in enumerate(entries)
                if _opencode_entry_info(entry).get("id") == cutoff_id
            )
        except StopIteration:
            return False
        doomed = [
            entry
            for position, entry in enumerate(entries)
            if position >= index and stamp(entry) >= cutoff_stamp
        ]
        removed = False
        for entry in doomed:
            info = _opencode_entry_info(entry)
            message_id = str(info.get("id") or "")
            if not message_id:
                continue
            try:
                server.request(
                    "DELETE",
                    f"/session/{session}/message/{message_id}",
                    params={"directory": self._cwd},
                    timeout=60,
                )
            except (OSError, ValueError):
                return removed
            removed = True
        return removed

    def _resend(self) -> None:
        """Send this turn's prompt again on the repaired conversation."""
        server = self._server
        if server is None or not self._session_id:
            self._fail("opencode's server went away while repairing the conversation")
            return
        command = self._as_command()
        try:
            if command is not None:
                self._start_command(*command)
            else:
                server.request(
                    "POST",
                    f"/session/{self._session_id}/prompt_async",
                    params={"directory": self._cwd},
                    body=self._prompt_body(self._prompt),
                    timeout=120,
                )
        except (OSError, ValueError) as exc:
            self._fail(opencode_error_text(exc, "opencode would not take the message again."))
            return
        self._answer.clear()
        self._emitted.clear()
        self._accepting_input.set()

    def _prompt_body(self, text: str, with_attachments: bool = True) -> dict:
        parts: list[dict] = []
        named: list[str] = []
        if with_attachments:
            for path in self._attachments:
                part = _opencode_file_part(path)
                if part is not None:
                    parts.append(part)
                elif str(path or "").strip():
                    named.append(str(path).strip())
        if named:
            listing = "\n".join(named)
            text = (
                f"{text}\n\nAttached files (please read them):\n{listing}"
                if text
                else f"Attached files (please read them):\n{listing}"
            )
        # Text first, files after, the way the server stores them; a turn of
        # bare files carries no text part at all.
        if text or not parts:
            parts.insert(0, {"type": "text", "text": text})
        body: dict = {"parts": parts}
        provider_id, model_id = opencode_split_model(self._model)
        if provider_id:
            body["model"] = {"providerID": provider_id, "modelID": model_id}
        if self._permission_mode == "plan":
            # opencode's own "plan" agent is the mode BlindPilot calls plan,
            # so plan mode selects it rather than describing it in rules.
            body["agent"] = "plan"
        if self._variant:
            body["variant"] = self._variant
        return body

    def _do_run(self) -> None:
        try:
            self._server = opencode_server()
        except OSError as exc:
            self._fail(opencode_error_text(exc, "opencode's server could not be started."))
            return
        if self._compact and not self._session_id:
            self._fail("There is no opencode conversation to compact yet")
            return
        # Effort levels are per model in opencode, and it rejects one the model
        # does not define, so the pooled choice from the picker is checked
        # against the model this turn will use.
        if self._effort:
            model = self._model or opencode_default_model(self._cwd)
            if self._effort in opencode_model_efforts(model, self._cwd):
                self._variant = self._effort

        # Subscribing before anything is asked for is what makes the first
        # words of the answer part of this turn rather than of the next one.
        try:
            self._stream = self._server.open(
                "GET", "/event", params={"directory": self._cwd}, timeout=None
            )
        except (OSError, ValueError) as exc:
            self._fail(opencode_error_text(exc, "Could not follow opencode's progress."))
            return

        try:
            self._open_session()
        except (OSError, ValueError) as exc:
            self._fail(opencode_error_text(exc, "Could not start an opencode conversation."))
            return
        self._on_session(self._session_id)

        command = self._as_command()
        try:
            if self._compact:
                self._start_compaction()
            elif command is not None:
                self._start_command(*command)
            else:
                self._server.request(
                    "POST",
                    f"/session/{self._session_id}/prompt_async",
                    params={"directory": self._cwd},
                    body=self._prompt_body(self._prompt),
                    timeout=120,
                )
        except (OSError, ValueError) as exc:
            self._fail(opencode_error_text(exc, "opencode would not accept the message."))
            return

        self._accepting_input.set()
        self._on_started()
        self._read_events()

    def _open_session(self) -> None:
        assert self._server is not None
        rules = _OPENCODE_PERMISSIONS.get(self._permission_mode)
        if self._session_id:
            # A resumed conversation keeps its id, but the permission mode may
            # have been changed since, so its rules are re-stated every turn.
            if rules is not None:
                self._server.request(
                    "PATCH",
                    f"/session/{self._session_id}",
                    params={"directory": self._cwd},
                    body={"permission": rules},
                    timeout=60,
                )
            return
        body: dict = {}
        if rules is not None:
            body["permission"] = rules
        if self._permission_mode == "plan":
            # opencode's own "plan" agent is the mode BlindPilot calls plan,
            # so plan mode selects it rather than describing it in rules.
            body["agent"] = "plan"
        provider_id, model_id = opencode_split_model(self._model)
        if provider_id:
            body["model"] = {"providerID": provider_id, "id": model_id}
        created = self._server.request(
            "POST", "/session", params={"directory": self._cwd}, body=body, timeout=120
        )
        session_id = created.get("id") if isinstance(created, dict) else None
        if not session_id:
            raise OSError("opencode did not return a conversation id")
        self._session_id = str(session_id)

    def _as_command(self) -> Optional[tuple[str, str]]:
        """(command, arguments) if the prompt names one of opencode's commands.

        opencode's commands are prompt templates its server expands — the text
        "/init" sent as a message is just those five characters, and would be
        answered rather than run. Anything it does not recognise stays an
        ordinary message, so a sentence that happens to start with a slash is
        not swallowed.
        """
        text = self._prompt.strip()
        if not text.startswith("/"):
            return None
        # Split on any whitespace, not a space: a command sent with attached
        # files has their paths on the lines after it.
        parts = text[1:].split(None, 1)
        if not parts:
            return None
        name, arguments = parts[0], parts[1] if len(parts) > 1 else ""
        known = {command for command, _description in opencode_commands(self._cwd)}
        return (name, arguments.strip()) if name in known else None

    def _start_command(self, command: str, arguments: str) -> None:
        """Run one of opencode's commands, and narrate it like any other turn.

        Its command request only answers once the whole turn is over, so it is
        made from a thread of its own and the event stream is what the window
        hears from — the same way it hears an ordinary message.
        """
        body: dict = {"command": command, "arguments": arguments}
        provider_id, model_id = opencode_split_model(self._model)
        if provider_id:
            body["model"] = f"{provider_id}/{model_id}"
        if self._permission_mode == "plan":
            # opencode's own "plan" agent is the mode BlindPilot calls plan,
            # so plan mode selects it rather than describing it in rules.
            body["agent"] = "plan"

        def work() -> None:
            try:
                assert self._server is not None
                self._server.request(
                    "POST",
                    f"/session/{self._session_id}/command",
                    params={"directory": self._cwd},
                    body=body,
                    timeout=None,
                )
            except (OSError, ValueError) as exc:
                if self._cancelled:
                    return
                detail = opencode_error_text(exc, "the request failed")
                self._fail(f"opencode could not run /{command}: {detail}")
                # Nothing else will end the turn now, so release the reader.
                self._close_stream()

        threading.Thread(target=work, daemon=True).start()

    def _start_compaction(self) -> None:
        assert self._server is not None
        provider_id, model_id = opencode_split_model(
            self._model or opencode_default_model(self._cwd)
        )
        if not provider_id:
            raise OSError("Choose an opencode model before compacting")
        self._server.request(
            "POST",
            f"/session/{self._session_id}/summarize",
            params={"directory": self._cwd},
            body={"providerID": provider_id, "modelID": model_id},
            timeout=120,
        )

    def _read_events(self) -> None:
        stream = self._stream
        assert stream is not None
        try:
            for raw in stream:  # type: ignore[attr-defined]
                if self._cancelled:
                    return
                line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                try:
                    event = json.loads(line[5:].strip())
                except ValueError:
                    continue
                kind = str(event.get("type") or "")
                properties = event.get("properties") or {}
                # Every session on the server shares one stream — the title
                # writer and any subagent included — so anything that names a
                # different conversation belongs to somebody else's turn.
                session = properties.get("sessionID")
                if session in self._children:
                    # One of this turn's agents. Its idle or error is its own
                    # ending, never this turn's; the task part says how it went.
                    if kind not in ("session.idle", "session.error", "session.status"):
                        self._handle_event(kind, properties)
                    continue
                if session not in (None, self._session_id):
                    continue
                if kind == "session.error":
                    # The repair path re-sends and keeps reading; only an
                    # unrepaired failure returns here for good.
                    if not self._on_session_error(properties):
                        return
                if kind in ("session.idle", "session.compacted"):
                    self._finish()
                    return
                self._handle_event(kind, properties)
        except Exception as exc:
            # Deliberately broad: Stop closes this connection from another
            # thread, mid-read, and what a socket torn out from under the HTTP
            # reader raises is not something to enumerate. A stop is expected
            # and silent; anything else is still reported as the failure it is.
            if not self._cancelled:
                self._fail(opencode_error_text(exc, "opencode's event stream ended"))
            return
        finally:
            self._accepting_input.clear()
        if not self._cancelled:
            self._fail("opencode closed the connection before the turn finished")

    def _finish(self) -> None:
        if self._settled.is_set():
            return
        self._settled.set()
        if self._compact:
            # A compaction produces no answer of its own, so say what happened
            # rather than finishing in silence.
            self._on_complete("Conversation compacted.")
        else:
            # opencode writes an answer as one text part per step, and a step
            # boundary is a paragraph boundary — run together they read as one
            # sentence that was never written.
            self._on_complete("\n\n".join(self._answer).strip())

    def _handle_event(self, kind: str, properties: dict) -> None:
        if kind == "message.updated":
            info = properties.get("info") or {}
            if isinstance(info, dict) and info.get("id"):
                self._roles[str(info["id"])] = str(info.get("role") or "")
        elif kind == "message.part.updated":
            self._part(properties.get("part") or {})
        elif kind in ("permission.asked", "permission.v2.asked"):
            self._answer_permission(properties)
        elif kind in ("question.asked", "question.v2.asked"):
            self._answer_question(properties)
        elif kind == "session.status":
            status = properties.get("status") or {}
            if isinstance(status, dict) and status.get("type") == "retry":
                self._on_activity(
                    "tool", f"opencode is retrying (attempt {status.get('attempt') or 1})"
                )

    def _part(self, part: object) -> None:
        if not isinstance(part, dict):
            return
        owner = part.get("sessionID")
        if owner in self._children:
            self._child_part(str(owner), part)
            return
        if owner not in (None, self._session_id):
            # Somebody else's conversation: its words are not this answer.
            return
        if part.get("type") == "tool" and part.get("tool") == "task":
            self._task_part(part)
        part_id = str(part.get("id") or "")
        kind = str(part.get("type") or "")
        if kind in ("text", "reasoning"):
            # The user's own message arrives as a part too; only the model's
            # side of the conversation belongs in the response rows.
            if self._roles.get(str(part.get("messageID") or "")) != "assistant":
                return
            # opencode opens a part empty, streams it a token at a time, then
            # repeats it in full: that last repeat is what says it is finished
            # and can be read out as one row rather than a hundred.
            text = str(part.get("text") or "").strip()
            if not text or part_id in self._emitted:
                return
            self._emitted.add(part_id)
            if kind == "reasoning":
                self._on_activity("thinking", text)
            else:
                self._answer.append(text)
                self._on_activity("assistant", text)
        elif kind == "tool":
            self._tool(part_id, part)
        elif kind in ("step-start", "step-finish"):
            # Lifecycle bookkeeping. A step-finish also carries the step's
            # tokens and cost, which no report reads, so there is nothing
            # here worth interrupting the transcript for.
            return
        elif kind in ("patch", "subtask", "file"):
            self._artifact_part(part_id, kind, part)
        elif kind:
            self._unknown_part(part_id, kind, part)

    def _task_part(self, part: dict) -> None:
        """Follow the agent a task tool call runs, by its child session.

        The task tool opens a child session (`parentID` is this one) and names
        it in the running part's `state.metadata.sessionId`; the part's own
        completion or error is how the agent ended.
        """
        state = part.get("state")
        if not isinstance(state, dict):
            return
        part_id = str(part.get("id") or "")
        metadata = state.get("metadata")
        child = str(metadata.get("sessionId") or "") if isinstance(metadata, dict) else ""
        child = child or self._task_children.get(part_id, "")
        if not child:
            return
        status = subagent_status(state.get("status"))
        if child not in self._children:
            self._children[child] = SUBAGENT_RUNNING
            self._task_children[part_id] = child
            raw_input = state.get("input")
            params = raw_input if isinstance(raw_input, dict) else {}
            name = subagent_line(params.get("description") or params.get("subagent_type"), 80)
            task = subagent_line(params.get("prompt"))
            self._on_subagent(
                child, name or "Subagent", SUBAGENT_RUNNING, f"Task: {task}" if task else ""
            )
        if status and status != SUBAGENT_RUNNING and self._children.get(child) != status:
            self._children[child] = status
            detail = subagent_line(state.get("error") or state.get("title"))
            self._on_subagent(child, "", status, detail or f"Finished: {status}")

    def _child_part(self, child: str, part: dict) -> None:
        """What one of this turn's agents wrote or ran, once per part."""
        part_id = str(part.get("id") or "")
        kind = str(part.get("type") or "")
        if kind == "text":
            if self._roles.get(str(part.get("messageID") or "")) != "assistant":
                return
            text = subagent_line(part.get("text"))
            if text and part_id not in self._emitted:
                self._emitted.add(part_id)
                self._on_subagent(child, "", "", text)
        elif kind == "tool":
            state = part.get("state")
            if not isinstance(state, dict):
                return
            status = str(state.get("status") or "")
            if status == "running" and part_id not in self._tools_running:
                self._tools_running.add(part_id)
                label = _opencode_tool_label(str(part.get("tool") or "tool"), state.get("input"))
                self._on_subagent(child, "", "", label)
            elif status == "error":
                self._on_subagent(child, "", "", f"Failed: {subagent_line(state.get('error'))}")

    def _artifact_part(self, part_id: str, kind: str, part: dict) -> None:
        """Say a patch, subtask, or file the turn produced, once.

        opencode streams every part whole and then repeats it, so anything
        said here is said once per part id like the text rows. The user's own
        file parts are their message going out, not the turn coming back, and
        are left to the prompt row.
        """
        if self._roles.get(str(part.get("messageID") or "")) != "assistant":
            return
        if not part_id or part_id in self._emitted:
            return
        if kind == "patch":
            files = part.get("files")
            names = [
                _opencode_file_name(entry)
                for entry in (files if isinstance(files, list) else [])
                if _opencode_file_name(entry)
            ]
            line = f"Changed: {', '.join(names)}" if names else "Changed files"
        elif kind == "subtask":
            agent = str(part.get("agent") or "").strip()
            detail = str(part.get("description") or "").strip().splitlines()
            first = detail[0][:200] if detail and detail[0] else ""
            line = (
                f"Subagent {agent}: {first}"
                if agent and first
                else (first or f"Subagent {agent}".rstrip())
            )
        else:
            name = _opencode_file_name(part.get("filename"))
            line = f"Attached: {name}" if name else "Attached a file"
        self._emitted.add(part_id)
        self._on_activity("tool", line)

    def _unknown_part(self, part_id: str, kind: str, part: dict) -> None:
        """Say something for a part kind this file has never heard of.

        A newer opencode that streams a kind we do not know about should
        still be visible in the transcript rather than silently swallowed.
        """
        if self._roles.get(str(part.get("messageID") or "")) != "assistant":
            return
        if not part_id or part_id in self._emitted:
            return
        detail = (
            str(part.get("text") or "")
            or str(part.get("description") or "")
            or str(part.get("delta") or "")
        ).strip()
        first = detail.splitlines()[0][:120] if detail else ""
        self._emitted.add(part_id)
        self._on_activity("tool", f"{kind}: {first}" if first else kind)

    def _tool(self, part_id: str, part: dict) -> None:
        state = part.get("state")
        if not isinstance(state, dict):
            return
        status = str(state.get("status") or "")
        name = str(part.get("tool") or "tool")
        if status == "running" and part_id not in self._tools_running:
            self._tools_running.add(part_id)
            self._on_activity("tool", _opencode_tool_label(name, state.get("input")))
        elif status == "completed":
            output = str(state.get("output") or "").strip()
            if output:
                self._on_activity("result", output)
        elif status == "error":
            message = str(state.get("error") or "").strip()
            self._on_activity("result", message or f"{name} failed")

    def _post(self, routes: list[tuple[str, Optional[dict]]], what: str) -> bool:
        """POST to the first of these routes opencode accepts.

        A permission request and a question both hold the turn open until they
        are answered, and opencode is in the middle of moving both onto new
        endpoints. Trying the old one and then the new one costs one failed
        request in the worst case; getting it wrong costs a turn that never
        ends, so the routes are tried rather than guessed at.
        """
        if self._server is None:
            return False
        problem = ""
        for path, body in routes:
            try:
                self._server.request(
                    "POST", path, params={"directory": self._cwd}, body=body, timeout=30
                )
                return True
            except (OSError, ValueError) as exc:
                problem = opencode_error_text(exc, f"opencode would not accept {what}")
        # Saying so matters: unanswered, the turn waits for an answer that is
        # never coming, and silence would look like the model thinking.
        self._on_activity("notice", f"Could not answer {what}: {problem}")
        return False

    def _answer_permission(self, properties: dict) -> None:
        """Answer a permission request the way the chosen mode says to.

        BlindPilot decides from the permission mode rather than interrupting
        with a dialog, the same way its Codex adapter does, so a run never
        stops waiting on an answer nobody was asked for.
        """
        request_id = str(properties.get("id") or "")
        if not request_id or self._server is None:
            return
        permission = str(properties.get("permission") or "")
        mode = self._permission_mode
        if mode == "bypassPermissions":
            reply = "always"
        elif mode == "auto":
            reply = "once"
        elif mode == "acceptEdits" and permission in ("edit", "write", "patch"):
            reply = "once"
        else:
            reply = "reject"
        answered = self._post(
            [
                (
                    f"/session/{self._session_id}/permissions/{request_id}",
                    {"response": reply},
                ),
                (
                    f"/api/session/{self._session_id}/permission/{request_id}/reply",
                    {"reply": reply},
                ),
            ],
            "a permission request",
        )
        if answered and reply == "reject":
            self._on_activity(
                "tool",
                f"Declined {permission or 'a request'} — the permission mode does not allow it",
            )

    def _answer_question(self, properties: dict) -> None:
        """Put a mid-run question to the person and send opencode their answers.

        opencode takes one list of chosen labels per question, in the order it
        asked them, and a question left unanswered has to be rejected rather
        than replied to — a turn waiting on an answer that is never coming
        never ends. It is in the middle of moving both onto new endpoints, so
        each is tried in turn the same way a permission reply is.
        """
        request_id = str(properties.get("id") or "")
        if not request_id or self._server is None:
            return
        asked = _opencode_questions(properties.get("questions"))
        answers = self._on_question(asked) if (asked and self._on_question) else None
        self._on_activity("tool", questions.question_summary(asked, answers))
        if answers is None:
            self._post(
                [
                    (f"/question/{request_id}/reject", None),
                    (f"/api/session/{self._session_id}/question/{request_id}/reject", None),
                ],
                "a question",
            )
            return
        self._question_answered = True
        body = {"answers": [list(answer) for answer in answers]}
        self._post(
            [
                (f"/question/{request_id}/reply", body),
                (f"/api/session/{self._session_id}/question/{request_id}/reply", body),
            ],
            "a question",
        )


class AgentWorker(Protocol):
    """The part of a backend's worker that the window actually drives.

    All four workers are threads, but a thread is not what the window wants
    from them: it wants to start a turn, ask whether it is still running, stop
    it, and wait for it to let go. Saying that here is what lets the window
    hold whichever worker the backend chose without knowing which one it is —
    and lets `cancel` be a method the code is allowed to call, rather than one
    a reader has to take on trust.
    """

    def start(self) -> None: ...

    def is_alive(self) -> bool: ...

    def steer(self, text: str) -> bool: ...

    def join(self, timeout: Optional[float] = None) -> None: ...

    def cancel(self) -> None: ...


# Callable rather than type[...]: each backend's worker takes the keywords its
# own turn needs — Codex alone understands `compact` — and the caller passes
# the extras for the backend it picked.
AgentWorkerFactory = Callable[..., AgentWorker]


# ----- What every backend must answer -----
#
# The app asks the same four questions of every backend: is it signed in, what
# can it say about the account, how much allowance is left, and which worker
# runs its turns. These were four hand-written if/elif chains, so a new backend
# that missed one answered "not signed in" or ran Claude's worker without
# saying so. One adapter per backend makes a missing answer a KeyError in a
# test instead of a quiet wrong answer in the window.


@dataclass(frozen=True)
class BackendAdapter:
    """How one backend answers the questions asked of every backend."""

    auth_ok: Callable[[int], bool]
    account_lines: Callable[[str, int], list[str]]
    usage_lines: Callable[[str, int], list[str]]
    worker: Callable[[AgentWorkerFactory], AgentWorkerFactory]


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


def _hermes_worker(_claude: AgentWorkerFactory) -> AgentWorkerFactory:
    from hermes_worker import HermesWorker

    return HermesWorker


def _muse_worker(_claude: AgentWorkerFactory) -> AgentWorkerFactory:
    from muse_worker import MuseWorker

    return MuseWorker


def _museai_worker(_claude: AgentWorkerFactory) -> AgentWorkerFactory:
    from museai_worker import MuseAiWorker

    return MuseAiWorker


def _commandcode_worker(_claude: AgentWorkerFactory) -> AgentWorkerFactory:
    from commandcode_worker import CommandcodeWorker

    return CommandcodeWorker


def _gemini_worker(_claude: AgentWorkerFactory) -> AgentWorkerFactory:
    from google_worker import GeminiWorker

    return GeminiWorker


def _antigravity_worker(_claude: AgentWorkerFactory) -> AgentWorkerFactory:
    from google_worker import AntigravityWorker

    return AntigravityWorker


def _copilot_worker(_claude: AgentWorkerFactory) -> AgentWorkerFactory:
    from copilot_backend import CopilotWorker

    return CopilotWorker


ADAPTERS: dict[str, BackendAdapter] = {
    BACKEND_CLAUDE: BackendAdapter(
        auth_ok=_cli_signed_in(BACKEND_CLAUDE, ("auth", "status")),
        account_lines=lambda binary, timeout: _claude_account_lines(
            *_probe_backend(binary, ["auth", "status"], timeout)
        ),
        usage_lines=lambda binary, timeout: _usage_lines(
            _claude_usage_windows(_claude_usage_payload(binary, timeout))
        ),
        worker=lambda claude_worker: claude_worker,
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
        worker=lambda _claude: OpencodeWorker,
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


def worker_class(backend: str, claude_worker: AgentWorkerFactory) -> AgentWorkerFactory:
    """The worker that runs this backend's turns."""
    return _adapter(backend).worker(claude_worker)


# ---------------------------------------------------------------------------
# Re-exports from codex_worker — public symbols stay importable from here so
# no caller has to change. Implemented via __getattr__ rather than a module-
# level import to avoid a circular-import problem: codex_worker imports shared
# symbols from this module, so this module must not import from codex_worker at
# load time. __getattr__ is only called when a name is not already in the
# module's namespace, so the lazy load happens the first time a caller reaches
# for one of these names.
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


# Re-exports from freebuff_terminal and freebuff_worker, same lazy mechanism.
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


def __getattr__(name: str) -> object:
    if name in _CODEX_WORKER_REEXPORTS:
        import codex_worker as _cw

        return getattr(_cw, name)
    if name in _FREEBUFF_TERMINAL_REEXPORTS:
        import freebuff_terminal as _ft

        return getattr(_ft, name)
    if name in _FREEBUFF_WORKER_REEXPORTS:
        import freebuff_worker as _fw

        return getattr(_fw, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
