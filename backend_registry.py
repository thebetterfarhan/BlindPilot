"""Stable registry for BlindPilot's backend metadata.

Constants, data classes, the turn-worker base, and the backend table live here.
Probe utilities (CLI discovery, auth checks, usage queries) live in
``backend_probes``.  ``agent_backends`` re-exports everything from both modules
for backwards compatibility.

Copyright (c) 2026 doubletaponair and BlindPilot contributors.
Based on the original Claude Code Reader application by doubletaponair:
https://github.com/doubletaponair/claude-code-reader
SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import difflib
import os
import re
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol

import diagnostics
import questions


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


# ----- Subagents -----
# Every backend can hand work to agents of its own -- Claude Code's Agent tool,
# Codex's spawnAgent, Hermes' delegate_task, Muse's native subagents, opencode's
# task tool, Command Code's agent tool, FreeBuff's spawn_agents -- and each one
# reports them differently. The window lists the ones running and shows what
# each is doing, so every worker translates its provider's reports into one
# call: (agent id, name, status, line). An empty name or status leaves that part
# as it was, and an empty line adds nothing to the agent's log.
SubagentReport = Callable[[str, str, str, str], None]


@dataclass(frozen=True)
class TurnCallbacks:
    """Named bundle of the nine callbacks every turn worker accepts.

    Before this, `_TurnWorker.__init__` listed all nine as keyword parameters,
    so every construction site in the window named them all. Adding a new
    callback required a grep-and-replace across every call site. With the
    bundle, a new field is optional by default and costs nothing at existing
    call sites.
    """

    on_session: Callable[[str], None]
    on_started: Callable[[], None]
    on_activity: Callable[[str, str], None]
    on_complete: Callable[[str], None]
    on_failed: Callable[[str], None]
    on_done: Callable[[], None]
    on_question: Optional[questions.AskQuestions] = None
    on_permission: Optional[Callable[[str, dict, list], dict]] = None
    on_subagent: Optional[SubagentReport] = None


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
        callbacks: TurnCallbacks,
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
        self._on_session = callbacks.on_session
        self._on_started = callbacks.on_started
        self._on_activity = callbacks.on_activity
        self._on_complete = callbacks.on_complete
        self._on_failed = callbacks.on_failed
        self._on_done = callbacks.on_done
        self._on_question = callbacks.on_question
        self._on_permission = callbacks.on_permission
        self._on_subagent = callbacks.on_subagent or ignore_subagents
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


def _claude_worker(_claude: AgentWorkerFactory) -> AgentWorkerFactory:
    from claude_backend import ClaudeWorker

    return ClaudeWorker


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


_WORKER_DISPATCH: dict[str, Callable[[AgentWorkerFactory], AgentWorkerFactory]] = {
    BACKEND_CLAUDE: _claude_worker,
    BACKEND_CODEX: lambda _: __import__("codex_worker").CodexWorker,
    BACKEND_FREEBUFF: lambda _: __import__("freebuff_worker").FreebuffWorker,
    BACKEND_OPENCODE: lambda _: __import__("opencode_worker").OpencodeWorker,
    BACKEND_HERMES: _hermes_worker,
    BACKEND_MUSE: _muse_worker,
    BACKEND_MUSEAI: _museai_worker,
    BACKEND_COMMANDCODE: _commandcode_worker,
    BACKEND_GEMINI: _gemini_worker,
    BACKEND_ANTIGRAVITY: _antigravity_worker,
    BACKEND_COPILOT: _copilot_worker,
}


def worker_class(backend: str, claude_worker: AgentWorkerFactory) -> AgentWorkerFactory:
    """The worker that runs this backend's turns."""
    backend_id = normalize_backend(backend)
    worker_fn = _WORKER_DISPATCH.get(backend_id)
    if worker_fn is None:
        raise KeyError(f"no worker for {backend_id}")
    return worker_fn(claude_worker)


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


# Re-exports from opencode_worker, same lazy mechanism. The opencode discovery
# helpers that stay here (``_opencode_providers``, ``_opencode_account_lines``)
# are not re-exports; they are defined in this module beside the other
# backends' status code.
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
