"""Codex worker, PlanAccount, CodexServer, and all Codex-specific helpers.

Extracted from agent_backends.py so the Codex surface has a focused home.
agent_backends re-exports every public symbol for zero caller churn.
"""

from __future__ import annotations

import collections
import json
import logging
import platform
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple, Optional, Sequence, cast

import backend_pool
import certificates
import process_utils
import questions
from accessible_ai import plan_token
from accessible_ai.providers import plan_relay
from accessible_ai.storage import preferences
from markdown_rows import SentenceStreamer
from owned_process import owned_popen

# Shared symbols that stay in agent_backends (resolved after that module fully loads)
from agent_backends import (
    BACKEND_CODEX,
    SUBAGENT_COMPLETED,
    SUBAGENT_RUNNING,
    UNBOUND,
    _TurnWorker,
    _app_version,
    _as_number,
    _codex_home,
    ask_through_the_tool,
    blindpilot_config_dir,
    find_backend_cli,
    subagent_line,
    subagent_status,
    subprocess_env,
)


def _codex_app_server_binary(binary: str) -> str:
    """Prefer Codex's native executable over an npm wrapper on Windows.

    Terminating an npm ``.cmd`` launcher does not terminate its native child,
    which leaves the app-server writer lock attached to the thread. Launching
    the packaged executable directly makes the process BlindPilot holds the
    app-server itself; its own children are the tree kill's business.
    """
    if platform.system() != "Windows" or Path(binary).suffix.casefold() == ".exe":
        return binary
    # The platform package is an optional dependency of @openai/codex, and
    # npm installs those hoisted: a sibling of the codex package inside the
    # shared @openai folder, not nested under the package itself. A nested
    # copy appears only when npm could not hoist, so look there first (that
    # is also the copy Node's own resolution finds first) and fall back to
    # the hoisted one. Finding neither leaves the .cmd wrapper, which
    # CreateProcess cannot run at all: Popen raises WinError 5.
    package_root = Path(binary).parent / "node_modules" / "@openai" / "codex"
    hoisted_root = Path(binary).parent / "node_modules" / "@openai"
    candidates: list[Path] = []
    for root in (package_root / "node_modules" / "@openai", hoisted_root):
        try:
            candidates = sorted(root.glob("codex-win32-*/vendor/**/bin/codex.exe"))
        except OSError:
            candidates = []
        if candidates:
            break
    return str(candidates[0]) if candidates else binary


def _codex_model_payload(binary: str, cwd: Optional[str], extra: Sequence[str]) -> object:
    """Run one form of `codex debug models` and decode its JSON, or None.

    ``extra`` is what tells the two apart: empty is the refreshing form, and
    ``--bundled`` is the copy compiled into the installed binary. A failure of
    either - offline, a timeout, output that is no longer JSON - is reported as
    None rather than raising, so the caller can try the other.
    """
    try:
        result = subprocess.run(
            [_codex_app_server_binary(binary), "debug", "models", *extra],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            env=subprocess_env(binary),
            **process_utils.no_window_kwargs(),
        )
        return json.loads(result.stdout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def _codex_catalog(payload: object) -> tuple[list[str], list[str]]:
    """(models, efforts) out of a `debug models` payload.

    Only the models Codex's own picker shows are kept. The catalog also carries
    entries marked ``visibility: "hide"`` - models being retired, and internal
    ones like codex-auto-review - and offering one means choosing it, waiting on
    a model the service refuses, and losing the message.
    """
    models: list[str] = []
    efforts: list[str] = []
    entries = payload if isinstance(payload, list) else None
    if entries is None and isinstance(payload, dict):
        entries = payload.get("models", [])
    if not isinstance(entries, list):
        return models, efforts
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("visibility", "list") != "list":
            continue
        model = entry.get("slug") or entry.get("id") or entry.get("model")
        if isinstance(model, str) and model and model not in models:
            models.append(model)
        supported = entry.get("supported_reasoning_levels") or []
        if isinstance(supported, list):
            for level in supported:
                effort = level.get("effort") if isinstance(level, dict) else level
                if isinstance(effort, str) and effort and effort not in efforts:
                    efforts.append(effort)
    return models, efforts


def codex_model_options(
    cwd: Optional[str] = None,
) -> tuple[list[str], list[str], str, str, str]:
    """Read Codex's live model catalog and configured defaults.

    `debug models` refreshes the catalog from the service before dumping it, so
    a model the service has retired drops out and a new one appears without
    waiting for the next Codex release. `--bundled` skips that refresh and dumps
    only the copy compiled into the installed binary, which is how a removed
    model used to stay on offer until the CLI itself was updated; it is kept as
    the fallback for a machine that cannot reach the service.
    """
    binary = find_backend_cli(BACKEND_CODEX)
    efforts: list[str] = []
    if not binary:
        return [], efforts, "", "", "Codex was not found."

    stale = False
    models, efforts = _codex_catalog(_codex_model_payload(binary, cwd, ()))
    if not models:
        models, fallback_efforts = _codex_catalog(_codex_model_payload(binary, cwd, ("--bundled",)))
        if models:
            stale = True
            efforts = fallback_efforts or efforts
    if not efforts:
        efforts = ["low", "medium", "high", "xhigh"]

    current_model = ""
    current_effort = ""
    try:
        import tomllib

        with open(_codex_home() / "config.toml", "rb") as handle:
            config = tomllib.load(handle)
        if isinstance(config.get("model"), str):
            current_model = config["model"]
        if isinstance(config.get("model_reasoning_effort"), str):
            current_effort = config["model_reasoning_effort"]
    except (OSError, ValueError):
        pass
    if not models:
        error = "Codex returned an empty model catalog."
    elif stale:
        # Read from the installed release because the service could not be
        # reached, so a model removed upstream may still be listed here.
        error = "Could not refresh Codex's model list; showing the installed release's copy."
    else:
        error = ""
    return models, efforts, current_model, current_effort, error


# Codex ships `request_user_input` — the tool it stops a turn to ask a
# multiple-choice question with — switched off, and available only in plan mode
# even when switched on. Both are settings rather than protocol, so they are
# passed for this app server alone and nothing is written to the user's
# ~/.codex/config.toml.
_CODEX_QUESTION_FEATURE = "default_mode_request_user_input"
_CODEX_QUESTION_ARGS = (
    "-c",
    "tools.experimental_request_user_input={enabled=true}",
    "--enable",
    _CODEX_QUESTION_FEATURE,
)


# Why a question has to be asked with the tool rather than written out, for
# whichever backend's tool is named. Switching the tool on is not the same as
# being asked through it: a model that writes its question into its answer
# makes no tool call, so nothing announces it and no dialog opens, the turn
# ends, and the person is left waiting on an answer nobody told them was
# wanted. A skill that runs an interview -- "grill me" is the one this was
# found through -- asks in prose as a matter of course, which is why the
# reason is spelled out rather than left as a preference: an instruction that
# only says "prefer this tool" loses to a skill that says to interview in
# prose.


def _codex_developer_instructions() -> str:
    """BlindPilot's instruction, behind whatever the person wrote themselves.

    ``-c`` replaces a key rather than adding to it, so setting this key blind
    would cost somebody their own ``developer_instructions`` for as long as
    BlindPilot was running. Theirs is read first and kept in front, which is
    both the order they wrote it in and the order that leaves BlindPilot's
    addition reading as the footnote it is.
    """
    theirs = ""
    try:
        import tomllib

        with open(_codex_home() / "config.toml", "rb") as handle:
            theirs = str(tomllib.load(handle).get("developer_instructions") or "")
    except (OSError, ValueError):
        # No config file, or one Codex itself would refuse to load and report
        # on. Either way there is nothing here of theirs to lose.
        theirs = ""
    return f"{theirs.strip()}\n\n{ask_through_the_tool('request_user_input')}".strip()


def codex_question_args() -> tuple[str, ...]:
    """The app-server arguments that make a question reach the dialog.

    Built per launch rather than held as a constant, because the instruction
    half has to read the user's config first. Quoted as a TOML string with
    ``json.dumps``: ``-c`` parses its value as TOML and only falls back to a
    literal when that fails, and a sentence with a quote or a newline in it
    should not be relying on the fallback. ``ensure_ascii`` is off because
    JSON writes a character outside the BMP -- an emoji in somebody's own
    instructions -- as an escaped surrogate pair, which TOML refuses,
    and the refusal would send the whole quoted value through as a literal.
    """
    value = json.dumps(_codex_developer_instructions(), ensure_ascii=False)
    return (*_CODEX_QUESTION_ARGS, "-c", f"developer_instructions={value}")


# ----- Codex on a ChatGPT-plan account -----
#
# Codex is pointed at the plan's Responses endpoint through configuration
# overrides on its own command line, and fetches its own bearer token by
# running this app's token-printing mode on an interval. The token is
# therefore in neither the child's command line nor its environment. The
# endpoint itself is reached through a loopback relay (accessible_ai.providers.
# plan_relay) because the plan token is a subscription-sharing token, and that
# endpoint refuses several item types Codex emits; the relay drops them.

PLAN_PROVIDER_ID = "blindpilot-chatgpt"

# How often Codex runs the token command again. Each run is its own process with
# no token in memory, so each one performs an exchange and rotates the refresh
# token; ten minutes is how often that is worth paying, and a run that fails --
# another process holding the store, a machine that was asleep -- is retried long
# before the token under it expires.
PLAN_TOKEN_REFRESH_MS = 600_000
# Longer than the command's own bounded wait for another process's refresh (two
# 30-second exchanges plus 30), so a refresh that waited is not reported as a
# failing token command.
PLAN_TOKEN_TIMEOUT_MS = 120_000


@dataclass(frozen=True)
class PlanAccount:
    """The ChatGPT-plan account a Codex turn runs on.

    The id is what the token command takes; the name is how a failure is
    reported, because an id on its own does not say which account it was.
    """

    account_id: int
    name: str = ""

    @property
    def label(self) -> str:
        return self.name or f"account {self.account_id}"


class PlanTokenError(OSError):
    """The bound Account's token command failed, so no turn can run on it.

    An OSError like every other reason a launch does not happen, so a caller
    that only knows "Codex could not start" still catches it. The distinct type
    is what says the failure names the Account rather than the CLI, which is
    what lets the window stop retrying an Account that has gone.
    """


# ----- Which Account the Codex backend uses -----
#
# Chosen once for the backend, saved beside the window's own preferences, and
# inherited by new conversations: nothing here is per conversation. The value is
# either a specific ChatGPT-plan Account or nothing at all, and nothing at all
# means Codex's own login -- the behaviour every existing setup has, so an
# install that has chosen nothing runs exactly as it did.
#
# A conversation that has begun keeps the Account it began on, so this is read
# once per conversation rather than once per turn: a switch affects the next
# conversation, not the thread somebody is in the middle of. A tab hands its
# bound Account to each of its turns (see `CodexWorker`), and only a turn told
# `UNBOUND` reads this at all.
#
# Read on the thread about to launch a process, never on the window's: it is a
# small file, but it is a file. Kept in this module rather than with the other
# preferences because the Codex launch is what needs it, and the window already
# imports this module rather than the other way round.

# The key the choice is stored under in the settings file. Named for the
# backend, because it is that backend's choice and not a global one.
CODEX_PLAN_ACCOUNT = "codex_plan_account"


def _settings_file() -> Path:
    """The settings file the window keeps its preferences in."""
    return blindpilot_config_dir() / "config.json"


def codex_plan_account() -> Optional[PlanAccount]:
    """The Account the Codex backend is set to use, or None for its own login."""
    saved = preferences.load_config((_settings_file(),)).get(CODEX_PLAN_ACCOUNT)
    if not isinstance(saved, dict):
        return None
    try:
        account_id = int(saved.get("account_id", 0))
    except (TypeError, ValueError):
        # Written by hand, or by a release that meant something else by it.
        # Codex's own login is the answer that always works.
        return None
    if account_id <= 0:
        return None
    return PlanAccount(account_id=account_id, name=str(saved.get("name") or ""))


def save_codex_plan_account(account: Optional[PlanAccount]) -> bool:
    """Remember which Account the Codex backend uses. False if it was not saved.

    Only this key changes: the same file holds the window's preferences, and
    the write is `preferences.update_config`, which reads and writes the file
    under one lock -- the same save path the window uses -- so a choice saved
    here cannot put back a preference saved a moment earlier, and a
    preference saved by the window cannot cost somebody their choice.
    """
    value = (
        None if account is None else {"account_id": int(account.account_id), "name": account.name}
    )
    return preferences.update_config(_settings_file(), {CODEX_PLAN_ACCOUNT: value})


def codex_pool_key(account: Optional[PlanAccount] = None) -> tuple:
    """The pool key for the Codex backend, on this Account if one is chosen.

    One place decides what an Account means to the pool, so the turn that takes
    a process, the drop that gives it up and the usage report that reads it can
    never disagree about which Account's process they are talking about.
    """
    return backend_pool.pool_key(
        BACKEND_CODEX, account=None if account is None else account.account_id
    )


class AccountDrop(NamedTuple):
    """What letting go of an Account's Held process did, in terms the caller says.

    ``waited``: a turn was speaking through the process, so the drop waits for
    that turn to finish and somebody has to be told so. ``choice_cleared``: the
    Codex backend was set to this Account, and is now back on Codex's own login.
    """

    waited: bool = False
    choice_cleared: bool = False


def drop_codex_plan_account(account_id: int) -> bool:
    """Drop what Codex holds for this Account, now or once its turn finishes.

    Signing out of an Account, deleting it, or choosing another one takes its
    Held process away: nothing may be sent on a credential that has just been
    ended, and nothing on a process whose Account is no longer chosen. A turn
    already running through it is never interrupted -- it finishes on the token
    it started with, which is what switching promises -- so the drop waits for
    it and returns True, which is the caller's cue to say the wait out loud.

    The start lock is the turn's own, so a turn between being handed this
    process and being counted in it cannot be dropped underneath. It is never
    waited for here: a turn holds it across a launch, which runs the token
    command and starts a process, and this is called from the window's thread.
    Not getting it means a turn is launching right now, which is reason enough
    to owe the drop and say it is waiting.
    """
    key = codex_pool_key(PlanAccount(account_id=account_id))
    pool = backend_pool.pool()
    if not _CODEX_START_LOCK.acquire(blocking=False):
        pool.wait_to_drop(key)
        return True
    try:
        return pool.drop_when_idle(key)
    finally:
        _CODEX_START_LOCK.release()


def drop_pending_codex_processes() -> None:
    """Carry out the drops owed by finished turns, now that it is safe to.

    Called as a turn hands its process back, which is where a wait owed to that
    turn is paid. The start lock is taken without waiting, because a turn holds
    it from taking a process to being counted in it, and a drop landing in that
    gap would stop the very process the next turn has just been handed. Not
    getting it means a turn is being handed one right now: the keys stay owed,
    and the next turn to finish asks again.

    A choice of Account that was waiting for a turn is applied here too, for
    the same reason and at the same moment: the turn it was waiting for has
    just finished.
    """
    if _CODEX_START_LOCK.acquire(blocking=False):
        try:
            backend_pool.pool().drop_pending()
        finally:
            _CODEX_START_LOCK.release()
    _apply_pending_plan_choice()


# ----- Choosing another Account while a turn is running -----
#
# A turn already running finishes on the Account it started with, so the switch
# cannot take effect while it runs: the choice is made, said, and kept here
# until the turn that blocked it hands its process back (see
# `drop_pending_codex_processes`). Until then the saved choice -- and so the
# Account every new conversation is bound to -- is still the one being left.

_PENDING_CHOICE_LOCK = threading.Lock()
_pending_choice: Optional[PlanAccount] = None
_pending_choice_owed = False


class AccountSwitch(NamedTuple):
    """What choosing another Account for Codex did, in terms the caller says.

    ``saved``: the choice in the settings file is now the one asked for.
    ``waited``: a turn was running on the Account being left, so it is not --
    the switch happens when that turn finishes, and somebody has to be told so.
    """

    saved: bool = False
    waited: bool = False


class PendingSwitch(NamedTuple):
    """A switch waiting for a turn, if there is one, and the Account it names."""

    owed: bool = False
    account: Optional[PlanAccount] = None


def switch_codex_plan_account(account: Optional[PlanAccount]) -> AccountSwitch:
    """Choose the Account the Codex backend uses, now or once its turn finishes.

    Nothing is running: the choice is saved and the Account being left has its
    process dropped, so the next conversation starts on the new Account.

    A turn is running on the Account being left: it finishes on that Account,
    which is what switching promises, so the choice is kept rather than saved
    and takes effect when the turn hands its process back. The caller is told
    it is waiting so it can say so; a switch that silently waits is one nobody
    knows to wait for.

    The start lock is the turn's own, so this answers without queueing behind a
    turn that is launching a process (see `drop_codex_plan_account`).
    """
    global _pending_choice, _pending_choice_owed

    previous = codex_plan_account()
    same = (
        account is not None and previous is not None and account.account_id == previous.account_id
    )
    if previous is None or same:
        # Nothing is being left -- Codex's own login either way, or the same
        # Account under a new name -- so there is no process to wait for.
        return AccountSwitch(save_codex_plan_account(account), waited=False)
    if not drop_codex_plan_account(previous.account_id):
        return AccountSwitch(save_codex_plan_account(account), waited=False)
    with _PENDING_CHOICE_LOCK:
        _pending_choice = account
        _pending_choice_owed = True
    return AccountSwitch(saved=False, waited=True)


def pending_codex_plan_account() -> PendingSwitch:
    """The switch waiting for a turn to finish, if one is waiting."""
    with _PENDING_CHOICE_LOCK:
        return PendingSwitch(_pending_choice_owed, _pending_choice)


def _forget_pending_plan_choice(account_id: int) -> None:
    """Drop a switch that names an Account which is no longer usable."""
    global _pending_choice, _pending_choice_owed

    with _PENDING_CHOICE_LOCK:
        account = _pending_choice
        if account is not None and account.account_id == int(account_id):
            _pending_choice = None
            _pending_choice_owed = False


def _apply_pending_plan_choice() -> None:
    """Save the choice that was waiting for a turn, if that turn has finished.

    Asked where a turn hands its process back. A process another tab is still
    speaking through is left where it is, and the choice stays owed: the wait
    is for all of the turns running on the Account being left, not for
    whichever of them finishes first. A write the file refuses also leaves it
    owed, so the next turn to finish asks again rather than the choice being
    lost in silence.
    """
    global _pending_choice, _pending_choice_owed

    with _PENDING_CHOICE_LOCK:
        if not _pending_choice_owed:
            return
        choice = _pending_choice
    previous = codex_plan_account()
    if previous is not None and backend_pool.pool().busy(codex_pool_key(previous)):
        return
    if not save_codex_plan_account(choice):
        return
    with _PENDING_CHOICE_LOCK:
        _pending_choice = None
        _pending_choice_owed = False


def forget_codex_plan_account(account_id: int) -> AccountDrop:
    """Stop using an Account for Codex, and drop the process it was running on.

    For the three ways an Account stops being usable -- signed out, deleted, or
    switched away from. If it is the Account the backend is set to use, the
    choice goes back to Codex's own login: a signed-out Account cannot pay for
    a turn, and a saved id that nothing can run on is a choice that lies about
    what Codex will use. Everything stays stored but the choice itself, so the
    control that offers it and the file it is saved in agree.

    The drop is the one `drop_codex_plan_account` makes, with the same wait. A
    switch still waiting for a turn to finish is dropped if it names this
    Account, because an Account nobody can run on must not be saved later.
    """
    chosen = codex_plan_account()
    cleared = chosen is not None and chosen.account_id == int(account_id)
    if cleared:
        save_codex_plan_account(None)
    _forget_pending_plan_choice(int(account_id))
    return AccountDrop(drop_codex_plan_account(int(account_id)), cleared)


def plan_token_command(account_id: int) -> tuple[str, ...]:
    """The command that prints one access token for an account and exits.

    Codex runs it itself, from whatever folder it was started in. A frozen
    build's ``sys.executable`` is BlindPilot, whose entry point answers the flag
    before importing the window; from source it is a Python, so it is given the
    entry point by absolute path rather than relying on a working directory.
    """
    tail = (plan_token.FLAG, str(int(account_id)))
    if getattr(sys, "frozen", False):
        return (sys.executable, *tail)
    entry = Path(__file__).resolve().with_name("blind_pilot.py")
    return (sys.executable, str(entry), *tail)


def codex_plan_args(account_id: int) -> tuple[str, ...]:
    """Arguments that put Codex on the plan's Responses endpoint through the relay.

    The token is not among them: Codex runs the token command beside them to
    obtain its own bearer token, and runs it again on an interval. Quoted as
    TOML strings because ``-c`` parses its value as TOML; ``ensure_ascii`` is
    off for the paths, since JSON would write a character outside the BMP as an
    escaped surrogate pair, which TOML refuses.
    """
    provider = f"model_providers.{PLAN_PROVIDER_ID}"
    command = plan_token_command(account_id)
    overrides = (
        ("model_provider", json.dumps(PLAN_PROVIDER_ID)),
        (f"{provider}.name", json.dumps("OpenAI")),
        (
            f"{provider}.base_url",
            json.dumps(plan_relay.plan_base_url(ssl_context=certificates.certificate_context())),
        ),
        (f"{provider}.wire_api", json.dumps("responses")),
        (f"{provider}.supports_websockets", "false"),
        # requires_openai_auth is deliberately absent: Codex's reference says
        # not to combine it with auth, and its default is already false.
        (f"{provider}.auth.command", json.dumps(command[0], ensure_ascii=False)),
        (f"{provider}.auth.args", json.dumps(list(command[1:]), ensure_ascii=False)),
        (f"{provider}.auth.refresh_interval_ms", str(PLAN_TOKEN_REFRESH_MS)),
        (f"{provider}.auth.timeout_ms", str(PLAN_TOKEN_TIMEOUT_MS)),
    )
    args: list[str] = []
    for key, value in overrides:
        args += ["-c", f"{key}={value}"]
    return tuple(args)


def _codex_questions(raw: object) -> tuple[questions.Question, ...]:
    """Read request_user_input's params into BlindPilot's own question shape.

    `isOther` is Codex asking for the "Other" answer its tool description
    tells the model not to write itself.
    """
    return questions._questions(raw, custom_key="isOther", secret_key="isSecret", id_key="id")


# How long a failing Codex turn waits for the last line of stderr, which is
# the one that says why. Long enough for a line already written to arrive,
# short enough that a pipe which never closes cannot hold the turn open.
_CODEX_LAST_WORDS_SECONDS = 1.0

# How long an interrupt is given to be confirmed before the turn's thread is
# treated as wedged. Half of the window's whole teardown budget
# (_CANCEL_JOIN_SECONDS, 3.0), leaving the rest for the join that follows.
_CODEX_INTERRUPT_VERIFY_SECONDS = 1.5

# How long a cancel waits to be told the id of a turn it has already asked for.
# Stop can land in the gap between `turn/start` and the reply that names the
# turn, and a turn with no name cannot be interrupted -- so the last thing a
# stopped turn does is look for that reply. Small: the reply is either already
# on its way or it is not coming, and this is spent before the verify wait, so
# the two together still fit inside the window's teardown budget.
_CODEX_TURN_ID_GRACE_SECONDS = 0.25

# How long a turn waits on its inbox before looking at its own cancelled flag.
# Short enough that Stop is answered promptly, long enough that a quiet turn
# is not a spin.
_CODEX_POLL_SECONDS = 0.1

# How much of the app-server's stderr is kept. The process now outlives
# thousands of turns, and only the tail of it is ever read; an uncapped list
# is a leak on the branch whose whole point is not leaving things behind.
_CODEX_STDERR_LINES = 200

# How many conversations may hold a note about a turn given up on them. A
# thread nobody ever resumes never collects its note, so the notes are capped
# rather than trusted to be read.
_CODEX_ABANDONED_THREADS = 64


class _Closed:
    """What the reader puts in every inbox when the app-server's stdout ends.

    A turn blocked on its queue has to be woken by something; `None` is a
    value the protocol could plausibly carry, so this is a type of its own.
    """


_CODEX_CLOSED = _Closed()


class _Asked:
    """The mark a turn puts in its own inbox when it asks for a turn.

    Everything already queued behind it was said before this turn asked for
    anything, so none of it can be about this turn. Everything after it may
    be. The inbox is first in, first out and both threads write to it in
    order, so the mark separates the two exactly -- which a flag set at send
    time does not, because by then the earlier messages are already queued and
    are read after it.
    """


_CODEX_ASKED = _Asked()


class _TurnWatch:
    """One turn's completion, and how many callers are waiting to hear it."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.watchers = 0


# Only one Codex app-server is ever wanted, so only one turn at a time may go
# looking for one. Without this, two tabs sending their first Codex message
# together both find the pool empty, both launch a server, and the second one
# handed back displaces -- and stops -- the first, killing a live turn.
_CODEX_START_LOCK = threading.Lock()


def _unhandled_request(request_id: object) -> dict:
    return {
        "id": request_id,
        "error": {"code": -32601, "message": "BlindPilot cannot handle this request yet"},
    }


def _declined_request(message: dict) -> dict:
    """The reply that says no to a server request nobody is going to answer.

    Codex holds an unanswered request id open, so silence is not the same as
    refusal. Declining is: the turn it belongs to was interrupted, and the
    person is not going to be shown a question from it.
    """
    method = str(message.get("method") or "")
    request_id = message.get("id")
    if method == "item/tool/requestUserInput":
        questions = _codex_questions((message.get("params") or {}).get("questions"))
        answers: dict[str, dict] = {
            question.id or question.question: {"answers": []} for question in questions
        }
        return {"id": request_id, "result": {"answers": answers}}
    if method.endswith("requestApproval"):
        return {"id": request_id, "result": {"decision": "decline"}}
    return _unhandled_request(request_id)


class CodexServer:
    """One ``codex app-server`` process, shared by every tab using Codex.

    The app-server multiplexes: several threads live in one process, keyed by
    threadId, which is what lets one server serve every tab instead of one per
    tab. Replies are routed back to whichever turn asked, by request id.
    """

    def __init__(self, proc: object) -> None:
        self.proc = proc
        # Two locks, never held together. `_write_lock` guards only the
        # stdin write+flush pair, so a wedged app-server blocking on flush
        # cannot also block every other tab's `next_id()`. `_state_lock`
        # guards only in-process bookkeeping, the id counter and the routing
        # tables. A turn registers its reply queue before sending, and taking
        # the lock `send` uses for I/O there would self-deadlock.
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._next_id = 10
        # request id -> the queue the waiting turn is reading
        self._waiting: dict[int, "queue.Queue[object]"] = {}
        # Of those, the requests whose reply names a thread to subscribe.
        self._thread_replies: set[int] = set()
        # threadId -> the queue that conversation's turn is reading. Only ever
        # holds conversations somebody is reading right now: a message for a
        # thread with no entry belongs to a turn that has ended, and creating
        # an entry for it would both leak and replay it into the next turn.
        self._threads: dict[str, "queue.Queue[object]"] = {}
        # turnId -> the completion the reader reports, and its watchers.
        self._turns: dict[str, _TurnWatch] = {}
        # threadId -> the turns given up on it that may still be generating.
        # Written when a cancel could not be confirmed, read by the next turn
        # to resume that conversation, and cleared as it is read.
        self._abandoned: dict[str, set[str]] = {}
        # Bounded, because this list now belongs to a process that outlives
        # thousands of turns rather than dying with one of them.
        self._stderr: "collections.deque[str]" = collections.deque(maxlen=_CODEX_STDERR_LINES)
        # How many lines have ever been written, which is what lets a turn
        # read only its own: the deque forgets, so a position in it is not a
        # position in the stream.
        self._stderr_written = 0
        self._readers: list[threading.Thread] = []
        self._stderr_reader: Optional[threading.Thread] = None
        self._closed = False
        self._read_error = ""
        # How many turns are speaking through this process right now.
        self._borrowers = 0

    # ----- lifetime -----

    def start_readers(self) -> None:
        """Read this process's stdout and stderr for as long as it lives.

        One thread per pipe per *process*, not per turn: the app-server
        multiplexes, so a turn that read stdout itself would swallow every
        other tab's notifications.
        """
        if getattr(self.proc, "stdout", None) is not None:
            reader = threading.Thread(target=self._read_stdout, name="codex-stdout", daemon=True)
            self._readers.append(reader)
            reader.start()
        else:
            self._close("")
        if getattr(self.proc, "stderr", None) is not None:
            stderr_reader = threading.Thread(
                target=self._read_stderr, name="codex-stderr", daemon=True
            )
            self._stderr_reader = stderr_reader
            self._readers.append(stderr_reader)
            stderr_reader.start()

    def alive(self) -> bool:
        poll = getattr(self.proc, "poll", None)
        return poll is not None and poll() is None

    def next_id(self) -> int:
        with self._state_lock:
            self._next_id += 1
            return self._next_id

    def send(self, message: dict) -> bool:
        stdin = getattr(self.proc, "stdin", None)
        if stdin is None:
            return False
        try:
            data = json.dumps(message, ensure_ascii=False) + "\n"
            with self._write_lock:
                stdin.write(data)
                stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    # ----- routing -----

    def expect(
        self, request_id: int, listener: "queue.Queue[object]", *, binds_thread: bool = False
    ) -> None:
        """Send this request's reply to that queue. Register before sending.

        `binds_thread` also subscribes the conversation the reply names.
        `thread/start` and `thread/resume` both answer with the conversation's
        id (both `required: ["thread", ...]` in the app server's own schema),
        and the reader binds that conversation to this queue *before* it hands
        the reply on. Everything arrives on one stdout read by one thread, so
        any message routed after that reply finds the binding already in place,
        and nothing has to be buffered for a thread nobody is reading yet.

        Messages routed *before* the reply are a different matter: a threadId
        nothing is bound to yet is dropped. In practice the only notification
        Codex sends between creating a thread and answering for it is
        `mcpServer/startupStatus/updated`, which this worker ignores.
        """
        with self._state_lock:
            closed = self._closed
            if not closed:
                self._waiting[request_id] = listener
                if binds_thread:
                    self._thread_replies.add(request_id)
        if closed:
            listener.put_nowait(_CODEX_CLOSED)

    def unexpect(self, request_ids: Sequence[int]) -> None:
        with self._state_lock:
            for request_id in request_ids:
                self._waiting.pop(request_id, None)
                self._thread_replies.discard(request_id)

    def attach(self, thread_id: str, listener: "queue.Queue[object]") -> None:
        """Read this conversation's notifications from that queue.

        Ordinarily `expect(..., binds_thread=True)` has already done this from
        the reply. This is the belt and braces for a turn that had to fall
        back to the session id it was given, and it is idempotent.
        """
        if not thread_id:
            return
        with self._state_lock:
            closed = self._closed
            if not closed:
                self._threads[thread_id] = listener
        if closed:
            listener.put_nowait(_CODEX_CLOSED)

    def detach_listener(self, listener: "queue.Queue[object]") -> None:
        """Stop reading every conversation bound to this queue.

        By identity rather than by id, because the id is exactly what a turn
        may not have. The reader binds the conversation the moment the reply
        names it; a turn cancelled between that routing and its own reading of
        the reply never learns the id, and detaching by an id it does not have
        would leave the binding there for the life of the process.
        """
        with self._state_lock:
            for thread_id in [k for k, v in self._threads.items() if v is listener]:
                del self._threads[thread_id]

    def borrow(self) -> None:
        """One more turn is speaking through this process."""
        with self._state_lock:
            self._borrowers += 1

    def give_back(self) -> None:
        with self._state_lock:
            self._borrowers = max(0, self._borrowers - 1)

    def borrower_count(self) -> int:
        """How many turns hold this process right now.

        Not the same as how many conversations are bound: a turn that has sent
        `thread/start` and is waiting for the reply holds the process without
        being bound to anything, and is exactly the turn that must not have it
        stopped underneath it.
        """
        with self._state_lock:
            return self._borrowers

    def watch_turn(self, turn_id: str) -> threading.Event:
        """The event the reader sets when this turn completes.

        Counted, because two watchers are ordinary: the turn's own thread
        registers when the turn starts, and a `cancel` on the window's thread
        registers again when it interrupts. The event goes when the last of
        them lets go, so a process that outlives thousands of turns does not
        accumulate one of these for each.
        """
        with self._state_lock:
            watch = self._turns.get(turn_id)
            if watch is None:
                watch = _TurnWatch()
                self._turns[turn_id] = watch
            watch.watchers += 1
            if self._closed:
                watch.done.set()
            return watch.done

    def forget_turn(self, turn_id: str) -> None:
        with self._state_lock:
            watch = self._turns.get(turn_id)
            if watch is None:
                return
            watch.watchers -= 1
            if watch.watchers <= 0:
                del self._turns[turn_id]

    def read_error(self) -> str:
        """Why the reader stopped, if it stopped on an error rather than EOF."""
        with self._state_lock:
            return self._read_error

    def stderr_mark(self) -> int:
        """Where a turn's own stderr begins, to be read back with `stderr_since`."""
        with self._state_lock:
            return self._stderr_written

    def stderr_since(self, mark: int) -> list[str]:
        """Only the lines written after that mark.

        A held process explains itself for every turn it ever runs. Reporting
        a death at turn fifty with a warning from turn three is a wrong reason
        spoken aloud to somebody who cannot scroll back and check.
        """
        with self._state_lock:
            fresh = self._stderr_written - mark
            if fresh <= 0:
                return []
            lines = list(self._stderr)
        return lines[-fresh:] if fresh < len(lines) else lines

    def abandon_turn(self, thread_id: str, turn_id: str) -> None:
        """Remember that this turn was given up while it may still be running.

        Kept on the process rather than the worker, because the worker is one
        turn and the thing that has to act on it is the next one. An
        interrupt Codex never confirmed leaves a turn generating, and its
        words arriving mid-way through the next prompt's turn used to be
        appended to that prompt's answer.
        """
        if not thread_id or not turn_id:
            return
        with self._state_lock:
            self._abandoned.setdefault(thread_id, set()).add(turn_id)
            while len(self._abandoned) > _CODEX_ABANDONED_THREADS:
                # A conversation nobody ever resumes would otherwise keep its
                # note for the life of the process. Oldest first: dicts keep
                # insertion order, and the newest note is the one still live.
                self._abandoned.pop(next(iter(self._abandoned)))

    def take_abandoned_turns(self, thread_id: str) -> set[str]:
        """The turns given up on this conversation, cleared as they are read."""
        with self._state_lock:
            return self._abandoned.pop(thread_id, set())

    def await_last_words(self, timeout: float) -> None:
        """Let the final line of stderr land before a turn reports the death."""
        reader = self._stderr_reader
        if reader is not None:
            reader.join(timeout=timeout)

    def _read_stdout(self) -> None:
        stdout = getattr(self.proc, "stdout", None)
        if stdout is None:
            self._close("")
            return
        try:
            for raw in stdout:
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                try:
                    self._route(message)
                except Exception as exc:
                    # One message nobody anticipated must not be read as the
                    # stream breaking: this reader serves every tab, and
                    # closing here would end all of their turns at once. It is
                    # written down, though: the turn that message was for now
                    # waits on a reply that will not come, and without a record
                    # the only sign of it is a turn that ends at the reaper a
                    # quarter of an hour later. Method and id only -- what the
                    # message was about is not this log's business.
                    try:
                        logging.getLogger("blindpilot.codex").warning(
                            "dropped an app-server message that could not be routed: "
                            "method=%r id=%r (%s)",
                            message.get("method"),
                            message.get("id"),
                            type(exc).__name__,
                        )
                    except Exception:
                        pass
                    continue
        except Exception as exc:
            # Whatever broke the pipe is the reason every turn on this server
            # is about to end, so it is carried rather than lost to a thread
            # nobody joins.
            self._close(f"BlindPilot stopped reading Codex: {exc}")
            return
        self._close("")

    def _read_stderr(self) -> None:
        # Its own reader, not `process_utils.read_stderr_lines`: the cursor a
        # turn uses to read only its own stderr counts every line, so capping
        # the list would make that cursor skip the lines the cap dropped.
        stderr = getattr(self.proc, "stderr", None)
        if stderr is None:
            return
        try:
            for line in stderr:
                if line.strip():
                    with self._state_lock:
                        self._stderr.append(line.strip())
                        self._stderr_written += 1
        except Exception:
            # stderr is only ever read for the reason a turn failed; failing
            # to read it must not become a second failure of its own.
            pass

    def _route(self, message: dict) -> None:
        method = message.get("method")
        if method is None:
            # A reply. Only the turn that asked wants it.
            request_id = message.get("id")
            waiting = None
            if isinstance(request_id, int):
                with self._state_lock:
                    waiting = self._waiting.pop(request_id, None)
                    binds = request_id in self._thread_replies
                    self._thread_replies.discard(request_id)
                    if waiting is not None and binds and not self._closed:
                        result = message.get("result")
                        thread = result.get("thread") if isinstance(result, dict) else None
                        # Shape-checked at every step. This runs on the one
                        # reader thread, and anything raised here would be
                        # caught as a broken stream and end every tab's turn,
                        # where the same malformed reply parsed in a worker
                        # only ever ended that worker's own.
                        thread_id = str(thread.get("id") or "") if isinstance(thread, dict) else ""
                        if thread_id:
                            self._threads[thread_id] = waiting
            if waiting is not None:
                waiting.put_nowait(message)
            return
        params = message.get("params")
        if not isinstance(params, dict):
            params = {}
        if method == "turn/completed":
            turn = params.get("turn")
            turn_id = str((turn or {}).get("id") or "") if isinstance(turn, dict) else ""
            with self._state_lock:
                watch = self._turns.get(turn_id)
            if watch is not None:
                watch.done.set()
        thread_id = str(params.get("threadId") or "")
        listener: Optional["queue.Queue[object]"] = None
        with self._state_lock:
            if self._closed:
                return
            if thread_id:
                # Nobody reading means the turn it belongs to has ended -- a
                # trailing delta or a `turn/completed` for a turn that was
                # interrupted. Keeping it would hand the *next* turn on this
                # conversation somebody else's ending to act on.
                listener = self._threads.get(thread_id)
            elif len(self._threads) == 1:
                # `threadId` is required on every notification and every server
                # request this worker acts on (checked against the app server's
                # own `generate-json-schema` for 0.149.1). What is left without
                # one is process-wide -- `configWarning`, `thread/started` --
                # and with a single conversation open there is no ambiguity
                # about who should hear it. With more than one there is, so an
                # unnamed message is dropped rather than told to the wrong tab.
                listener = next(iter(self._threads.values()))
        if listener is None:
            if method and "id" in message:
                # A request, though, must still be answered. Codex holds the
                # id open until it is, and the turn it belongs to never ends.
                # Outside the state lock: send takes the write lock, and the
                # two are never held together.
                self.send(_declined_request(message))
            return
        listener.put_nowait(message)

    def _close(self, error: str) -> None:
        """Wake everyone waiting on this server and say why, once."""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            self._read_error = error
            listeners = list(self._waiting.values()) + list(self._threads.values())
            # Replies can no longer arrive, and no further notification can be
            # routed, so nothing is left to deliver to either.
            self._waiting.clear()
            self._thread_replies.clear()
            self._threads.clear()
            # stdout has ended, so this process is finished with every turn it
            # was running. Saying so lets a cancel racing the death answer at
            # once instead of spending its whole verify budget on a pipe that
            # will never speak again.
            turns = list(self._turns.values())
        for listener in listeners:
            listener.put_nowait(_CODEX_CLOSED)
        for watch in turns:
            watch.done.set()

    def confirm_interrupt(self, thread_id: str, turn_id: str, timeout: float) -> bool:
        """Wait for Codex to say the turn stopped. Overridden in tests.

        This runs on the thread that called `cancel`, not the turn's own, so
        it must not read the message loop: it waits on the event the shared
        reader sets when it sees `turn/completed` for this turn id. Returning
        False means nothing confirmed, which is what makes "interrupt, verify,
        abandon the thread if unsure" a decision rather than a guess.
        """
        done = self.watch_turn(turn_id)
        try:
            return done.wait(timeout)
        finally:
            self.forget_turn(turn_id)

    def interrupt(self, thread_id: str, turn_id: str, timeout: float) -> bool:
        """Ask Codex to stop this turn and say whether it confirmed."""
        if not thread_id or not turn_id:
            return False
        # Held across the send, so a completion arriving the instant the
        # interrupt lands is not missed, and given back either way: the caller
        # may be `cancel`, which has no turn of its own to clean up after.
        self.watch_turn(turn_id)
        try:
            if not self.send(
                {
                    "method": "turn/interrupt",
                    "id": self.next_id(),
                    "params": {"threadId": thread_id, "turnId": turn_id},
                }
            ):
                return False
            return self.confirm_interrupt(thread_id, turn_id, timeout)
        finally:
            self.forget_turn(turn_id)

    def force_stop(self) -> None:
        """End the exact root now, for a shutdown whose stop ran out of time.

        Never waits on a lock ``stop`` could be holding: ``stop`` calls
        ``end_process_group`` with none held. Waiting turns are woken and the
        pipes released afterwards, as ``stop`` does. Harmless after ``stop``
        and when called twice.
        """
        try:
            process_utils.end_process_group(self.proc, timeout=process_utils.FORCE_STOP_WAIT)
        finally:
            self._close("")
            process_utils._close_process_pipes(self.proc)

    def stop(self) -> None:
        try:
            process_utils.end_process_group(self.proc, timeout=2)
        finally:
            # Both happen even when the process had already exited: waiting
            # turns must still be woken, and the pipe handles must still be
            # released.
            self._close("")
            process_utils._close_process_pipes(self.proc)


def _run_token_command(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """Run the plan-token command once and hand back what it printed and said."""
    return subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=PLAN_TOKEN_TIMEOUT_MS / 1000.0,
        **process_utils.no_window_kwargs(),
    )


def _plan_token_failure(account: PlanAccount, reason: str) -> str:
    return (
        f"The ChatGPT-plan account {account.label} could not be used for Codex: "
        f"its token command failed. {reason}"
    )


def _plan_token_or_report(account: PlanAccount) -> None:
    """Fetch a token now, so a broken credential is reported instead of a turn.

    Codex runs this same command itself before each request, and a failure there
    reaches the person as a turn that came back with nothing said. Running it
    here makes the failure an error naming the account, before any process is
    started. It costs one process and its exchange; the chat path refreshes
    again when it first asks, since nothing but the stored record is shared.
    """
    command = plan_token_command(account.account_id)
    try:
        done = _run_token_command(command)
    except (OSError, subprocess.SubprocessError) as exc:
        raise PlanTokenError(_plan_token_failure(account, str(exc).strip())) from exc
    printed = (done.stdout or "").strip()
    if done.returncode == 0 and printed:
        return
    said = [line.strip() for line in (done.stderr or "").splitlines() if line.strip()]
    reason = said[-1].removeprefix("error:").strip() if said else ""
    raise PlanTokenError(
        _plan_token_failure(
            account, reason or f"it exited with {done.returncode} and printed no token"
        )
    )


def _start_codex_server(
    binary: str = "", plan_account: Optional[PlanAccount] = None
) -> CodexServer:
    """Launch the shared app-server. Raises OSError if it cannot be started.

    Given a ``plan_account``, the server runs on that ChatGPT-plan account: it is
    pointed at OpenAI's Responses endpoint and runs ``plan_token_command`` to
    obtain its own bearer token. Without one, Codex uses whatever login the user
    already has, which is the behaviour every existing setup relies on.
    """
    binary = binary or find_backend_cli(BACKEND_CODEX) or ""
    if not binary:
        raise OSError("Codex is not installed. Run: npm install -g @openai/codex")
    server_binary = _codex_app_server_binary(binary)
    plan: tuple[str, ...] = ()
    if plan_account is not None:
        _plan_token_or_report(plan_account)
        plan = codex_plan_args(plan_account.account_id)
    proc = owned_popen(
        [server_binary, *plan, *codex_question_args(), "app-server", "--stdio"],
        cwd=str(Path.home()),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        encoding="utf-8",
        errors="replace",
        env=subprocess_env(server_binary),
        # npm's `codex` is a Node launcher that runs the real Codex as a child
        # of its own; stopping only the launcher leaves that child holding the
        # app-server session.
        **process_utils.own_group_kwargs(),
        **process_utils.no_window_kwargs(),
    )
    server = CodexServer(proc)
    server.start_readers()
    return server


def codex_adapter() -> backend_pool.Adapter:
    """How the pool starts, checks, interrupts and stops Codex."""

    def alive(server: object) -> bool:
        return bool(cast(CodexServer, server).alive())

    def stop(server: object) -> None:
        cast(CodexServer, server).stop()

    def busy(server: object) -> bool:
        # Borrowing, not reading: a turn that has sent `thread/start` and is
        # waiting for the reply holds the process while bound to no
        # conversation at all, and is exactly the turn the reaper must not
        # stop the process underneath.
        return cast(CodexServer, server).borrower_count() > 0

    return backend_pool.Adapter(
        alive=alive,
        # The pool's generic interrupt has no turn to name. Codex's cancel path
        # goes through CodexServer.interrupt with the ids it holds; reaching
        # here means nothing could be confirmed.
        interrupt=lambda _server, _timeout: False,
        stop=stop,
        busy=busy,
        force_stop=lambda server: cast(CodexServer, server).force_stop(),
    )


def _codex_usage_handshake(server: CodexServer) -> bool:
    """Introduce BlindPilot to a server started for this question alone."""
    sent = server.send(
        {
            "method": "initialize",
            "id": server.next_id(),
            "params": {
                "clientInfo": {
                    "name": "blindpilot",
                    "title": "BlindPilot",
                    "version": _app_version(),
                }
            },
        }
    )
    return sent and server.send({"method": "initialized", "params": {}})


def _codex_rate_limits(server: CodexServer, timeout: int) -> object:
    """Ask one app-server for the account's windows and wait for the reply."""
    inbox: queue.Queue[object] = queue.Queue()
    request_id = server.next_id()
    server.expect(request_id, inbox)
    try:
        if not server.send({"method": "account/rateLimits/read", "id": request_id, "params": {}}):
            return None
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                message = inbox.get(timeout=remaining)
            except queue.Empty:
                return None
            if not isinstance(message, dict):
                # The server closed under the question.
                return None
            if message.get("id") != request_id:
                continue
            return message.get("result")
    finally:
        server.unexpect([request_id])


def _codex_usage_result(binary: str, timeout: int) -> object:
    """Ask Codex for the account's windows, sharing the app-server if there is one.

    The pooled process is used where one is running: it has been through the
    handshake already, several tabs may be mid-turn on it, and this question
    is a read that does not touch any of their conversations. It is borrowed
    for the length of the question so the reaper cannot stop it underneath.
    Where no process is held, one is started for the question and stopped
    again, the way every other part of this report is asked.

    Asked of the Account the backend is set to use, because that is whose
    windows the report is about: a plan's remaining usage is not the same
    question as the one Codex's own login answers.
    """
    account = codex_plan_account()
    key = codex_pool_key(account)
    shared = backend_pool.pool()
    server: Optional[CodexServer] = None
    with _CODEX_START_LOCK:
        held = shared.take(key)
        if held is not None:
            shared.keep(key, held)
            server = cast(CodexServer, held.handle)
            server.borrow()
    if server is not None:
        try:
            return _codex_rate_limits(server, timeout)
        finally:
            server.give_back()
    try:
        started = _start_codex_server(binary, plan_account=account)
    except OSError:
        return None
    try:
        if not _codex_usage_handshake(started):
            return None
        return _codex_rate_limits(started, timeout)
    finally:
        started.stop()


def auto_review_line(params: dict) -> str:
    """What Codex's automatic approval review decided, in one sentence.

    ``item/autoApprovalReview/completed`` (codex-cli 0.164.0, marked unstable):
    ``review`` holds the status, risk level and rationale; ``action`` what was
    asked for. Still in progress, or a shape this does not know, says nothing.
    """
    review = params.get("review")
    action = params.get("action")
    if not isinstance(review, dict):
        return ""
    verdict = {
        "approved": "approved",
        "denied": "denied",
        "timedOut": "timed out on",
        "aborted": "stopped reviewing",
    }.get(str(review.get("status") or ""))
    if not verdict:
        return ""
    what = "a request"
    if isinstance(action, dict):
        kind = action.get("type")
        if kind == "command" and action.get("command"):
            what = f"running {subagent_line(action['command'], 120)}"
        elif kind == "execve" and action.get("program"):
            what = f"running {action['program']}"
        elif kind == "applyPatch":
            what = "a file change"
        elif kind == "networkAccess" and action.get("host"):
            what = f"network access to {action['host']}"
        elif kind == "mcpToolCall" and action.get("toolName"):
            what = f"the {action['toolName']} tool"
    line = f"Automatic review {verdict} {what}"
    risk = review.get("riskLevel")
    if isinstance(risk, str) and risk:
        line += f", {risk} risk"
    rationale = review.get("rationale")
    if isinstance(rationale, str) and rationale.strip():
        line += f": {subagent_line(rationale, 200)}"
    return line + ("" if line.endswith(".") else ".")


class CodexWorker(_TurnWorker):
    """Run one Codex turn through the official app-server JSONL protocol."""

    _backend = BACKEND_CODEX

    def _setup(self) -> None:
        # Borrowed from the pool, which starts and stops it; other tabs read it too.
        self._server: Optional[CodexServer] = None
        self._held: Optional[backend_pool.HeldProcess] = None
        # The pool key this turn borrowed its process under, which carries the
        # Account it runs on. Remembered rather than recomputed, so a choice
        # changed while the turn is running cannot make the turn give up
        # somebody else's process.
        self._pool_key: Optional[tuple] = None
        # `_plan_account` -- the Account this turn runs on, or None for Codex's
        # own login -- is the base class's, given by the tab that owns the
        # conversation. What decides whether a refusal is a plan's, and so
        # whether it is reported in the chat half's words.
        # Where this turn's stderr begins in a process older than the turn.
        self._stderr_mark = 0
        # This turn's queue, read by it alone. Unbounded, because a turn's own
        # answer text must never be dropped to make room for more of it. Empty
        # until `expect` or `attach` says what should arrive in it; one of
        # those has to be called before it is read, because they are also what
        # wakes a reader of a server already closed.
        self._inbox: Optional["queue.Queue[object]"] = None
        self._expected: list[int] = []
        self._borrowed = False
        self._thread_id = self._session_id or ""
        self._turn_id = ""
        # Agents this turn's thread spawned (spawnAgent), by their own thread
        # id, with where each stands, and the turn each is running. Their
        # threads are read on this turn's inbox so what they do reaches the
        # window and what they ask is answered, not declined unread.
        self._children: dict[str, str] = {}
        self._child_turns: dict[str, str] = {}
        # The conversation given up after an unconfirmed interrupt. The next
        # turn resumes it from disk; the server stays, other tabs are on it.
        self.abandoned_thread = ""
        # Set when Codex answered `thread/resume` with an error: the tab's
        # session id names a conversation that no longer exists, and the
        # window clears it so the next message starts a new one.
        self.lost_session = False
        # Set when the Account this conversation runs on could not produce a
        # token, so nothing was started on it. The window lets go of that
        # Account while no conversation has begun, where retrying its dead id
        # would fail every turn.
        self.plan_token_failed = False
        # True from the moment `turn/start` goes out, before any id names it.
        self._turn_requested = False
        # Decides whether the turn was asked for or the cancel landed first.
        self._start_gate = threading.Lock()
        # Set once `_turn_id` is final, so a cancel can wait instead of polling.
        self._turn_id_known = threading.Event()
        # Set when the turn ended by itself, so a cancel has nothing to interrupt.
        self._finished = threading.Event()
        # The turn id registered with the server, given back exactly once.
        self._watched = ""
        # Until this turn asks for a turn, anything naming one names an older one.
        self._turn_asked = False
        # Turns known to be somebody else's, however late the rest of them arrive.
        self._stale_turns: set[str] = set()
        self._assistant_parts: list[str] = []
        self._assistant_delta_seen: set[str] = set()
        self._assistant_streams: dict[str, list[str]] = {}
        # Live sentence release per message item, so the listener hears a
        # Codex answer as it is written rather than when its item completes.
        self._assistant_live: dict[str, SentenceStreamer] = {}
        self._reasoning_streams: dict[str, list[str]] = {}
        self._tool_outputs: dict[str, list[str]] = {}

    @property
    def _stderr_lines(self) -> list[str]:
        """What the shared server has said on stderr, newest last.

        Only what has been said since this turn borrowed the process. The pipe
        belongs to the process now rather than the turn, because one process
        outlives many turns and is read by one thread for all of them -- so
        without the mark, a death at turn fifty would be explained with a
        warning from turn three, out loud, to somebody who cannot scroll back
        through the transcript and check.

        It cannot be called `_stderr`: `threading.Thread` keeps a copy of the
        real `sys.stderr` under that name, to report a thread that dies.
        """
        server = self._server
        return server.stderr_since(self._stderr_mark) if server is not None else []

    def _send(self, message: dict) -> bool:
        server = self._server
        if server is None:
            return False
        return server.send(message)

    def _next_id(self) -> int:
        # Ids are the server's, not the turn's: several tabs write to one
        # stdin, and two turns numbering from ten would each be handed the
        # other's replies. Every caller runs after `_borrow_server` set it.
        return cast(CodexServer, self._server).next_id()

    def steer(self, text: str) -> bool:
        if not self.accepting_input() or not self._thread_id or not self._turn_id:
            return False
        return self._send(
            {
                "method": "turn/steer",
                "id": self._next_id(),
                "params": {
                    "threadId": self._thread_id,
                    "input": [{"type": "text", "text": text}],
                    "expectedTurnId": self._turn_id,
                },
            }
        )

    def cancel(self) -> None:
        """Stop this turn without stopping the server the other tabs share.

        Codex's app-server multiplexes: one process holds every tab's thread.
        Killing it here would end four other conversations to stop one, so the
        usual "kill if unsure" is replaced by a middle rung. The interrupt is
        sent and waited on, and if Codex does not confirm the turn stopped, the
        THREAD is abandoned rather than the process: the next turn resumes it
        from its rollout, and the wedged conversation is the only thing that
        pays.

        This runs off the window's thread, and everything it waits on is
        bounded, because the window is waiting on it: at worst the grace for
        naming the turn and then the verify, which together fit inside the
        teardown budget with room for the join that follows.
        """
        # Read before the flag is set, so that this only ever means "the turn
        # had already ended when Stop was pressed" and never "the turn ended
        # because of this cancel".
        finished = self._finished.is_set()
        with self._start_gate:
            # Under the gate, so that a turn about to be asked for either sees
            # this and is never asked for, or was asked for first and is read
            # as running below. Between the two there is no third state in
            # which Codex is starting a turn nobody here knows about.
            self._cancelled = True
            server = self._server
            thread_id = self._thread_id
            asked_for_a_turn = self._turn_requested
        self._accepting_input.clear()
        if finished:
            # The answer landed while Stop was on its way. There is nothing to
            # interrupt, and waiting for a confirmation of it would spend the
            # whole verify budget and then give up a perfectly good thread:
            # `_release` has already handed back the watch whose completion
            # the reader set, so a fresh one would never be set at all.
            return
        if server is None or not thread_id or not asked_for_a_turn:
            # No turn of this conversation's has been asked for, and the gate
            # means none ever will be: the turn reads the flag set above before
            # it asks. So there is nothing running to stop, and nothing to give
            # up a perfectly good conversation over.
            return
        turn_id = self._turn_id
        if not turn_id and self._turn_requested:
            # Stop landed between asking for a turn and being told its id. The
            # turn is running -- writing files, spending tokens -- and the only
            # thing missing is its name, which is usually already on its way.
            self._turn_id_known.wait(_CODEX_TURN_ID_GRACE_SECONDS)
            turn_id = self._turn_id
        if not turn_id:
            if self._turn_requested:
                # A turn was asked for and never named, so there is nothing to
                # interrupt by name. Saying the tab stopped while trusting the
                # conversation anyway would be the lie: whatever that turn goes
                # on to do is not in this thread's history as the next prompt
                # would read it, so the thread is given up instead.
                self._abandon(thread_id)
            return
        self._stop_children(server)
        if not server.interrupt(thread_id, turn_id, _CODEX_INTERRUPT_VERIFY_SECONDS):
            self._abandon(thread_id, turn_id)

    def _ask_for_a_turn(self) -> bool:
        """Commit to starting a turn, unless Stop got here first.

        The reply that names the conversation and the Stop that arrives while
        this turn is waiting for it are on different threads, and pressing
        Escape within a second of Send puts them in that order. Reading the
        reply and asking for a turn anyway leaves the turn running under a tab
        that has said it stopped -- and, because nothing here has a name to
        interrupt or give up, leaves its whole answer to arrive in the middle
        of the next prompt's.

        The cheapest place to close that is before the request goes out. A turn
        that is never asked for needs no interrupt, and costs the conversation
        nothing: the next prompt resumes it in the ordinary way rather than
        paying for a resume from disk it did not need.

        Returning True says the request is going out, so `cancel` from here on
        finds something running and stops it by the usual route.
        """
        with self._start_gate:
            if self._cancelled:
                return False
            # Set before the send, not after the reply: from here on a turn may
            # be running that a cancel has no name for, and treating that as
            # "nothing was started" is exactly how one gets left running under
            # a tab that says it stopped.
            self._turn_requested = True
        return True

    def _abandon(self, thread_id: str, turn_id: str = "") -> None:
        """Give up this conversation, and warn whoever resumes it next.

        Giving up the thread is only half of it. The turn is STILL GENERATING
        on the server -- that is what an unconfirmed interrupt means -- and the
        next prompt in this tab resumes the same conversation, so that turn's
        words arrive in the middle of somebody else's. `_stale_turns` is how a
        turn recognises another turn's words, and it can only learn a name from
        messages that arrive before this turn asks for anything. A turn that is
        quiet across that window -- mid tool call -- and speaks after it used to
        have its text appended to the next answer.

        So the name is left on the process, which outlives this worker and is
        the one thing the next worker is certain to be holding.
        """
        self.abandoned_thread = thread_id
        server = self._server
        if server is not None:
            server.abandon_turn(thread_id, turn_id)

    def _teardown(self) -> None:
        # This turn is over, however it ended. Read by `cancel` before it
        # interrupts anything, because the watch it would wait on is about
        # to be given back below.
        self._finished.set()
        # Nothing more will be read, so no further name can be learned. A
        # cancel racing the end of the turn stops waiting for one rather
        # than spending its whole grace on a thread that has gone.
        self._turn_id_known.set()
        # The process belongs to the pool now, not to this turn. It is
        # stopped when the conversation goes away, when it is found dead,
        # or when the reaper decides nobody is using it. All the turn
        # gives back is its place in the server's routing tables.
        self._release()

    def _release(self) -> None:
        """Stop the shared reader delivering to a turn that has ended."""
        server = self._server
        if server is None:
            return
        if self._watched:
            server.forget_turn(self._watched)
            self._watched = ""
        server.unexpect(self._expected)
        self._expected = []
        inbox = self._inbox
        if inbox is not None:
            # By identity: a turn cancelled before it read the reply that named
            # its conversation never learned the id to detach by.
            server.detach_listener(inbox)
        held = self._held
        if held is not None:
            # Touched before the borrow is given back, so a reaper sweep
            # between the two sees a busy server or a freshly touched one,
            # never one idle since the start of a turn that just finished.
            held.touch()
        if self._borrowed:
            self._borrowed = False
            server.give_back()
            # A turn that has just finished may be the last one on a process
            # whose Account was signed out, deleted or switched away from while
            # it ran. That drop was waiting for exactly this.
            drop_pending_codex_processes()

    def _borrow_server(self) -> Optional[backend_pool.HeldProcess]:
        """The app-server this turn will speak through, or None having failed.

        Taking and handing back happen under one lock so that two tabs sending
        their first Codex message together cannot each launch a server, with
        the second one kept displacing and stopping the first mid-turn.

        Which process this is depends on the Account the conversation is bound
        to: the tabs on one Account share its process, and a tab on another
        Account is given -- or starts -- that Account's own. A conversation
        resumed from history comes through here too, so it lands on the Account
        its conversation was held under rather than on whichever tab resumed
        it.

        The Account is the tab's answer, taken when the conversation began, and
        only a turn told `UNBOUND` reads the saved choice -- once, and then it
        is this turn's too. So choosing another Account cannot move a
        conversation that has already started, and the rest of a running turn
        cannot be sent somewhere else under it.
        """
        account = self._plan_account
        if account is UNBOUND:
            account = codex_plan_account()
        account = cast(Optional[PlanAccount], account)
        self._plan_account = account
        key = codex_pool_key(account)
        shared = backend_pool.pool()
        with _CODEX_START_LOCK:
            held = shared.take(key)
            # A process started for this turn has said nothing that is not
            # this turn's, so the mark stays where it began.
            reused = held is not None
            if held is None:
                try:
                    started = _start_codex_server(plan_account=account)
                except OSError as exc:
                    self.plan_token_failed = isinstance(exc, PlanTokenError)
                    self._fail(f"Failed to launch Codex: {exc}")
                    return None
                held = backend_pool.HeldProcess(started, codex_adapter())
                self._server = started
                # The handshake belongs to the process, not the turn: a reused
                # server has already been initialized and would refuse a second.
                if not self._handshake():
                    detail = self._why_it_died("Codex did not answer the initialize handshake")
                    self._server = None
                    held.stop()
                    self._fail(detail)
                    return None
            shared.keep(key, held)
            self._pool_key = key
            # Registered under the same lock the drop takes, so a turn that
            # has been handed this server is never counted as absent by a turn
            # deciding whether anyone still wants it.
            self._held = held
            server = cast(CodexServer, held.handle)
            self._server = server
            if reused:
                # Everything after this point on stderr is this turn's to be
                # explained by; anything before it belonged to another turn.
                self._stderr_mark = server.stderr_mark()
            self._inbox = queue.Queue()
            server.borrow()
            self._borrowed = True
        return held

    def _discard_server(self) -> None:
        """Take a server that could not start this conversation out of the pool.

        A turn that cannot start or resume its thread has usually been handed a
        process that will not serve any turn -- an app-server whose handshake
        went unanswered, say -- and leaving it there would fail every prompt
        for the next quarter of an hour, where a per-turn process used to get a
        fresh start each time. It is only dropped while no other tab is holding
        it -- borrowing, not reading: a turn that has sent `thread/start` and
        is still waiting on the reply is bound to no conversation yet, and is
        exactly the turn that must not have its process stopped underneath it.
        This must never be the way one conversation's bad session id ends
        another's live turn.

        Dropped under the key this turn borrowed, which remembers the Account
        the process was started on: a choice changed mid-turn must not take a
        different Account's process down with it.
        """
        server = self._server
        if server is None:
            return
        key = self._pool_key or codex_pool_key()
        with _CODEX_START_LOCK:
            if server.borrower_count() > 1:
                return
            backend_pool.pool().drop(key)

    @staticmethod
    def _policy(mode: str) -> tuple[str, dict]:
        if mode == "bypassPermissions":
            return "never", {"type": "dangerFullAccess"}
        if mode == "plan":
            return "never", {"type": "readOnly", "networkAccess": False}
        if mode == "dontAsk":
            return "never", {"type": "workspaceWrite", "networkAccess": True}
        # Auto asks too, but Codex's automatic approval review answers in the
        # person's place (see `_reviewer`), so a step outside the sandbox is
        # judged rather than refused or put to somebody.
        return "on-request", {"type": "workspaceWrite", "networkAccess": True}

    @staticmethod
    def _reviewer(mode: str) -> dict:
        """Who answers Codex's approval requests: its own reviewer in Auto.

        ``auto_review`` is Codex's risk-judging subagent (app-server
        ``approvalsReviewer``, codex-cli 0.164.0). Every other mode leaves the
        requests to the person, Codex's default.
        """
        return {"approvalsReviewer": "auto_review"} if mode == "auto" else {}

    def _do_run(self) -> None:
        if self._compact and not self._session_id:
            self._fail("There is no Codex conversation to compact yet")
            return
        held = self._borrow_server()
        if held is None:
            return
        server = cast(CodexServer, held.handle)
        inbox = cast("queue.Queue[object]", self._inbox)

        thread_request = self._next_id()
        if self._session_id:
            request = {
                "method": "thread/resume",
                "id": thread_request,
                "params": {"threadId": self._session_id},
            }
        else:
            approval, sandbox = self._policy(self._permission_mode)
            sandbox_type = str(sandbox.get("type", ""))
            params: dict = {
                "cwd": self._cwd,
                "approvalPolicy": approval,
                "sandbox": {
                    "readOnly": "read-only",
                    "workspaceWrite": "workspace-write",
                    "dangerFullAccess": "danger-full-access",
                }.get(sandbox_type, "workspace-write"),
                "serviceName": "blindpilot",
                **self._reviewer(self._permission_mode),
            }
            if self._model:
                params["model"] = self._model
            request = {"method": "thread/start", "id": thread_request, "params": params}
        # `binds_thread`: this is the reply that names the conversation, and
        # the reader subscribes it before handing the reply on, leaving no
        # window in which a notification for it has nowhere to go.
        self._expect(thread_request, binds_thread=True)
        self._send(request)

        turn_request = 0
        compact_request = 0
        started_notified = False
        while True:
            if self._cancelled:
                self._name_the_stopped_turn(inbox, turn_request)
                return
            try:
                queued = inbox.get(timeout=_CODEX_POLL_SECONDS)
            except queue.Empty:
                continue
            if queued is _CODEX_CLOSED:
                break
            if queued is _CODEX_ASKED:
                # Everything read before here was said before this turn asked
                # for anything, so none of it was about this turn.
                self._turn_asked = True
                continue
            if not isinstance(queued, dict):
                continue
            message: dict = queued

            if message.get("id") == thread_request:
                error = message.get("error")
                if error and self._session_id:
                    # The conversation is gone, its rollout rotated or
                    # deleted, and only this tab's session id is wrong. The
                    # server is serving every other tab perfectly well.
                    self.lost_session = True
                    detail = self._error_text(error, "Codex could not resume this conversation")
                    self._fail(f"{detail}. The next message starts a new conversation.")
                    return
                if error:
                    # A server that cannot start a conversation will not start
                    # the next one either; it goes rather than failing every
                    # prompt until the reaper notices it.
                    self._discard_server()
                    self._fail(self._error_text(error, "Could not start a Codex session"))
                    return
                thread = (message.get("result") or {}).get("thread") or {}
                self._thread_id = str(thread.get("id") or self._session_id or "")
                if not self._thread_id:
                    self._discard_server()
                    self._fail("Codex did not return a session id")
                    return
                # Anything given up on this conversation is somebody else's
                # turn, however late it speaks. Read once and cleared: the
                # only turn that has to know is the one now resuming.
                self._stale_turns.update(server.take_abandoned_turns(self._thread_id))
                # The reader has already subscribed this conversation from the
                # reply. This is the belt and braces for the fall back to the
                # session id above, and it is idempotent.
                server.attach(self._thread_id, inbox)
                self._on_session(self._thread_id)
                if self._compact:
                    # Compaction is not a message: it answers immediately with
                    # an empty result and then runs a turn of its own, whose
                    # notifications the loop below already understands.
                    compact_request = self._next_id()
                    # Compaction runs a turn of its own, so from here a cancel
                    # has something to stop -- but the reply to this request is
                    # empty, so nothing but the turn's own notifications will
                    # ever name it.
                    if not self._ask_for_a_turn():
                        return
                    inbox.put(_CODEX_ASKED)
                    self._expect(compact_request)
                    self._send(
                        {
                            "method": "thread/compact/start",
                            "id": compact_request,
                            "params": {"threadId": self._thread_id},
                        }
                    )
                    continue
                approval, sandbox = self._policy(self._permission_mode)
                params = {
                    "threadId": self._thread_id,
                    "input": [{"type": "text", "text": self._prompt}],
                    "cwd": self._cwd,
                    "approvalPolicy": approval,
                    "sandboxPolicy": sandbox,
                    **self._reviewer(self._permission_mode),
                }
                if self._model:
                    params["model"] = self._model
                if self._effort:
                    params["effort"] = self._effort
                turn_request = self._next_id()
                if not self._ask_for_a_turn():
                    # Stopped while waiting to be told this conversation's
                    # name. Nothing is running yet, and this is the last
                    # moment at which that is still true.
                    return
                inbox.put(_CODEX_ASKED)
                self._expect(turn_request)
                self._send({"method": "turn/start", "id": turn_request, "params": params})
                continue

            if compact_request and message.get("id") == compact_request:
                error = message.get("error")
                if error:
                    self._fail(self._error_text(error, "Codex could not compact"))
                    return
                # The result is empty; the compaction turn announces itself.
                continue

            if turn_request and message.get("id") == turn_request:
                error = message.get("error")
                if error:
                    self._fail(self._error_text(error, "Codex could not start the turn"))
                    return
                turn = (message.get("result") or {}).get("turn") or {}
                self._turn_id = str(turn.get("id") or "")
                self._watch_turn()
                self._accepting_input.set()
                if not started_notified:
                    self._on_started()
                    started_notified = True
                continue

            method = str(message.get("method") or "")
            params = message.get("params") or {}
            child = str(params.get("threadId") or "") if isinstance(params, dict) else ""
            if child and child in self._children:
                self._child_message(child, method, params, message)
                continue
            if not self._is_this_turn(method, params):
                # The conversation is right but the turn is not: Codex is still
                # winding up a turn that was interrupted, and its trailing
                # deltas and its `turn/completed` are not this turn's to act on.
                if not self._turn_asked:
                    # Learned by name, so the rest of that turn is recognised
                    # however late it comes -- including after the point where
                    # position alone would have let it through.
                    named = self._turn_named(params)
                    if named:
                        self._stale_turns.add(named)
                if method and "id" in message:
                    # It asked something, though, and an answer it never gets
                    # is a request id Codex holds for ever.
                    self._send(_declined_request(message))
                continue
            if self._compact and not self._turn_id:
                # Only compaction: `thread/compact/start` answers empty, so
                # nothing but the turn's own messages will ever name its turn.
                # Everywhere else the `turn/start` reply names it, and adopting
                # from a notification instead would give a straggler from the
                # turn before this one a way in.
                named = self._turn_named(params)
                if named:
                    self._turn_id = named
                    self._watch_turn()

            if method and "id" in message:
                self._handle_server_request(message)
                continue

            if method == "turn/started":
                turn = params.get("turn") or {}
                self._turn_id = str(turn.get("id") or self._turn_id)
                self._watch_turn()
                self._accepting_input.set()
                if not started_notified:
                    self._on_started()
                    started_notified = True
            elif method == "item/started":
                self._item_started(params.get("item") or {})
            elif method == "item/completed":
                self._item_completed(params.get("item") or {})
            elif method == "item/agentMessage/delta":
                self._agent_delta(params)
            elif method in (
                "item/reasoning/summaryTextDelta",
                "item/reasoning/textDelta",
                "item/plan/delta",
            ):
                delta = str(params.get("delta") or "")
                if delta:
                    item_id = str(params.get("itemId") or "")
                    self._reasoning_streams.setdefault(item_id, []).append(delta)
            elif method == "item/autoApprovalReview/completed":
                said = auto_review_line(params)
                if said:
                    self._on_activity("notice", said)
            elif method == "turn/plan/updated":
                report_tasks = getattr(self, "on_tasks", None)
                if report_tasks:
                    report_tasks("codex", params.get("plan"))
            elif method == "thread/tokenUsage/updated":
                usage = params.get("tokenUsage") or {}
                last = usage.get("last") or {}
                used = _as_number(last.get("totalTokens"))
                if used:
                    window = _as_number(usage.get("modelContextWindow")) or 0
                    self.context_tokens = (int(used), int(window))
            elif method == "item/commandExecution/outputDelta":
                item_id = str(params.get("itemId") or "")
                delta = str(params.get("delta") or "")
                if delta:
                    self._tool_outputs.setdefault(item_id, []).append(delta)
            elif method in ("warning", "configWarning"):
                warning = str(params.get("message") or params.get("summary") or "")
                if warning and _CODEX_QUESTION_FEATURE not in warning:
                    # The one warning that is skipped is Codex's notice that
                    # BlindPilot switched a still-developing feature on. It is
                    # true, it is deliberate, and repeating it at the top of
                    # every single turn is noise nobody can act on.
                    self._on_activity("tool", f"Codex warning: {warning}")
            elif method == "turn/completed":
                self._accepting_input.clear()
                turn = params.get("turn") or {}
                status = turn.get("status")
                if status == "failed":
                    self._fail(self._error_text(turn.get("error"), "Codex turn failed"))
                elif status == "interrupted":
                    if not self._cancelled:
                        self._fail("Codex turn was interrupted")
                elif self._compact:
                    # A compaction turn produces no answer text of its own, so
                    # say what happened rather than finishing in silence.
                    self._on_complete("Conversation compacted.")
                else:
                    self._on_complete("".join(self._assistant_parts).strip())
                return

        if not self._cancelled:
            self._fail(self._why_it_died("Codex app server closed before the turn completed"))

    def _handshake(self) -> bool:
        """Introduce BlindPilot to a freshly started app-server, once.

        This is the process's handshake, not the turn's: a server taken back
        out of the pool has already been through it, and a second `initialize`
        is not something the protocol offers.
        """
        sent = self._send(
            {
                "method": "initialize",
                "id": self._next_id(),
                "params": {
                    "clientInfo": {
                        "name": "blindpilot",
                        "title": "BlindPilot",
                        "version": _app_version(),
                    },
                    # request_user_input is still marked experimental in the
                    # app-server protocol, and Codex only sends experimental
                    # requests to a client that asked for them.
                    "capabilities": {"experimentalApi": True},
                },
            }
        )
        return sent and self._send({"method": "initialized", "params": {}})

    def _expect(self, request_id: int, binds_thread: bool = False) -> None:
        """Have the shared reader deliver this request's reply to this turn."""
        server, inbox = self._server, self._inbox
        if server is None or inbox is None:
            return
        self._expected.append(request_id)
        server.expect(request_id, inbox, binds_thread=binds_thread)

    def _watch_turn(self) -> None:
        """Let a cancel on another thread find out when this turn stops.

        Registered once per turn id, and given back in `_release`: the server
        counts its watchers, and one that is never given back is one event kept
        for the life of a process that now outlives thousands of turns.
        """
        if not self._turn_id:
            return
        # A cancel on another thread may be waiting to learn this, so that it
        # has something to name in its interrupt.
        self._turn_id_known.set()
        server = self._server
        if server is None or self._turn_id == self._watched:
            return
        if self._watched:
            server.forget_turn(self._watched)
        self._watched = self._turn_id
        server.watch_turn(self._turn_id)

    def _name_the_stopped_turn(self, inbox: "queue.Queue[object]", turn_request: int) -> None:
        """Before leaving, learn the id of a turn that was asked for unnamed.

        Stop can land in the gap between `turn/start` and the reply that names
        the turn. The turn is running on the server -- under `workspaceWrite`
        or `dangerFullAccess`, writing files and spending tokens -- and the
        only thing missing is the name to interrupt it by, which the reply is
        about to carry. So the last thing this turn does is look for that one
        reply, and hand the id to the `cancel` waiting on `_turn_id_known`.

        Only the reply to this turn's own request id is taken. Everything else
        read here is dropped unlooked at: the answer is already abandoned, and
        a message acted on after Stop is how a stopped turn's words get spliced
        into the next one's.
        """
        try:
            if self._turn_id or not turn_request:
                return
            deadline = time.monotonic() + _CODEX_TURN_ID_GRACE_SECONDS
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    return
                try:
                    queued = inbox.get(timeout=min(left, _CODEX_POLL_SECONDS))
                except queue.Empty:
                    continue
                if queued is _CODEX_CLOSED:
                    # The server has gone, so the turn has gone with it.
                    return
                if not isinstance(queued, dict) or queued.get("id") != turn_request:
                    continue
                if queued.get("method") is not None:
                    # A request from Codex, not the reply to ours. Its ids come
                    # from the other direction's namespace and both counters
                    # start small, so one can carry the number this turn is
                    # waiting on; read as the reply it would name no turn and
                    # end the search early.
                    continue
                if queued.get("error"):
                    # Codex refused the turn, so nothing is running to name and
                    # the conversation is as it was.
                    self._turn_requested = False
                    return
                # Shape-checked at every step: a malformed reply here would
                # come back as "BlindPilot stopped reading Codex" on a turn the
                # person has already stopped.
                result = queued.get("result")
                turn = result.get("turn") if isinstance(result, dict) else None
                self._turn_id = str(turn.get("id") or "") if isinstance(turn, dict) else ""
                return
        finally:
            # Whether or not it was found, nobody else is going to find it.
            self._turn_id_known.set()
            if self.abandoned_thread and self._turn_id:
                # The cancel gave up waiting for this name before the reply
                # carrying it arrived. Late is still in time to interrupt the
                # turn, which is better than leaving it to leak output into a
                # later conversation.  The normal cancel path has already
                # returned, so this worker owns the final interrupt attempt.
                abandoned_thread = self.abandoned_thread
                server = self._server
                if server is not None and server.interrupt(
                    abandoned_thread, self._turn_id, _CODEX_INTERRUPT_VERIFY_SECONDS
                ):
                    self.abandoned_thread = ""
                else:
                    self._abandon(abandoned_thread, self._turn_id)

    @staticmethod
    def _turn_named(params: dict) -> str:
        turn = params.get("turn")
        if isinstance(turn, dict) and turn.get("id"):
            return str(turn["id"])
        return str(params.get("turnId") or "")

    def _is_this_turn(self, method: str, params: dict) -> bool:
        """Whether a message about a turn is about the turn this worker is running.

        A stopped turn goes on producing for a moment after the interrupt, on
        the same thread. What separates its messages from this turn's is where
        they sit relative to `_CODEX_ASKED`, the mark this turn put in its inbox
        when it asked for a turn, plus the names of turns already rejected, for
        stragglers that arrive after the mark.
        """
        named = self._turn_named(params)
        if not named:
            # Nothing to attribute it to: process-wide, or about the thread
            # rather than a turn.
            return True
        if named in self._stale_turns:
            # Already known to belong to the turn before this one. Position
            # cannot vouch for a straggler that arrives after the mark; a name
            # already seen on the wrong side of it can.
            return False
        if self._turn_id:
            return named == self._turn_id
        if not self._turn_asked:
            # Nothing has been asked for, so this is the tail of the turn that
            # ran before this one -- the one somebody stopped.
            return False
        # Asked, and not yet told which turn we were given. An ending is the
        # exception: a turn cannot finish before the beginning we never saw.
        return method != "turn/completed"

    def _why_it_died(self, fallback: str) -> str:
        """The best account available of why the app-server stopped talking.

        stderr is a different pipe read by a different thread, and the line
        worth having - the panic, the unauthorized, the out of memory - is the
        last one written, which is exactly the one still in flight. Everything
        earlier is already in the list, so the race did not lose noise, it lost
        the reason, and it lost it differently each time.

        Codex's words are passed on as Codex wrote them: a ChatGPT-plan failure
        this app can name is one its own token command failed on, and that is
        reported where the command is run.
        """
        self._await_last_words()
        detail = "\n".join(self._stderr_lines[-10:]).strip()
        if detail:
            return detail
        server = self._server
        return (server.read_error() if server is not None else "") or fallback

    def _await_last_words(self) -> None:
        """Let the final line of stderr land before reporting why Codex died.

        Bounded, because a pipe that never closes must not hold the turn open.
        """
        server = self._server
        if server is not None:
            server.await_last_words(_CODEX_LAST_WORDS_SECONDS)

    def _error_text(self, error: object, fallback: str) -> str:
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message:
                return message
        return fallback

    def _handle_server_request(self, message: dict) -> None:
        method = str(message.get("method") or "")
        request_id = message.get("id")
        mode = self._permission_mode
        if method == "item/commandExecution/requestApproval":
            if mode == "default" and self._on_permission is not None:
                decision = self._ask_codex_permission(message, "command")
            else:
                decision = "accept" if mode in ("auto", "bypassPermissions") else "decline"
            self._send({"id": request_id, "result": {"decision": decision}})
        elif method == "item/fileChange/requestApproval":
            if mode == "default" and self._on_permission is not None:
                decision = self._ask_codex_permission(message, "file change")
            else:
                decision = (
                    "accept" if mode in ("acceptEdits", "auto", "bypassPermissions") else "decline"
                )
            self._send({"id": request_id, "result": {"decision": decision}})
        elif method == "item/tool/requestUserInput":
            self._answer_user_input(request_id, message.get("params") or {})
        else:
            self._send(_unhandled_request(request_id))

    def _ask_codex_permission(self, message: dict, kind: str) -> str:
        """Ask the person about a Codex approval request; worker thread blocks.

        Translates the backend-neutral dialog answer to Codex's accept/decline.
        A stop or closed tab denies, the same as the dialog's default.
        """
        params = message.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        tool = str(params.get("command") or params.get("tool") or kind)
        payload = {
            key: params[key] for key in ("command", "cwd", "reason", "explanation") if key in params
        }
        ask = self._on_permission
        if ask is None:
            return "decline"
        try:
            answer = ask(tool, payload, [])
        except Exception:
            return "decline"
        if isinstance(answer, dict) and answer.get("behavior") == "allow":
            return "accept"
        return "decline"

    def _answer_user_input(self, request_id: object, params: dict) -> None:
        """Put request_user_input in front of the person and answer it.

        Codex keys the answers by each question's own id, and takes a list per
        question even where only one answer was asked for. A question nobody
        answered is sent back with an empty list, which is how Codex reads
        "the person had nothing to say to this" — the turn then carries on
        rather than waiting for an answer that is not coming.
        """
        asked = _codex_questions(params.get("questions"))
        answers = self._on_question(asked) if (asked and self._on_question) else None
        self._on_activity("tool", questions.question_summary(asked, answers))
        payload = {
            question.id or question.question: {
                "answers": answers[index] if answers is not None and index < len(answers) else []
            }
            for index, question in enumerate(asked)
        }
        self._send({"id": request_id, "result": {"answers": payload}})

    def _item_started(self, item: dict) -> None:
        self._collab(item)
        label = _codex_item_label(item)
        if label:
            self._on_activity("tool", label)

    def _collab(self, item: dict) -> None:
        """Follow the agents this thread spawns, from its collab tool calls.

        A spawnAgent call names the new agent's thread in
        `receiverThreadIds`, and every collab call carries the last known
        state of the agents it touched in `agentsStates`, keyed by thread.
        """
        if item.get("type") != "collabAgentToolCall":
            return
        receivers = item.get("receiverThreadIds")
        threads = [str(t) for t in receivers if t] if isinstance(receivers, list) else []
        states = item.get("agentsStates")
        states = states if isinstance(states, dict) else {}
        if item.get("tool") == "spawnAgent":
            prompt = subagent_line(item.get("prompt"))
            server, inbox = self._server, self._inbox
            for thread in threads:
                if thread in self._children:
                    continue
                self._children[thread] = SUBAGENT_RUNNING
                if server is not None and inbox is not None:
                    server.attach(thread, inbox)
                self._on_subagent(
                    thread,
                    subagent_line(prompt, 60) or "Subagent",
                    SUBAGENT_RUNNING,
                    f"Task: {prompt}" if prompt else "",
                )
        for thread, state in states.items():
            if thread not in self._children or not isinstance(state, dict):
                continue
            status = subagent_status(state.get("status"))
            if status and status != self._children[thread]:
                self._children[thread] = status
                message = subagent_line(state.get("message"))
                self._on_subagent(thread, "", status, message or f"Finished: {status}")

    def _child_message(self, child: str, method: str, params: dict, message: dict) -> None:
        """Something one of this thread's agents said or asked."""
        if method and "id" in message:
            # Answered as this conversation's own would be: the mode the
            # person chose covers the agents it runs.
            self._handle_server_request(message)
            return
        if method == "turn/started":
            turn = params.get("turn") or {}
            self._child_turns[child] = str(turn.get("id") or "")
            if self._children.get(child) != SUBAGENT_RUNNING:
                self._children[child] = SUBAGENT_RUNNING
                self._on_subagent(child, "", SUBAGENT_RUNNING, "Working")
        elif method == "item/started":
            item = params.get("item") or {}
            label = _codex_item_label(item) if isinstance(item, dict) else ""
            if label:
                self._on_subagent(child, "", "", label)
        elif method == "item/completed":
            item = params.get("item") or {}
            if isinstance(item, dict) and item.get("type") == "agentMessage":
                text = subagent_line(item.get("text"))
                if text:
                    self._on_subagent(child, "", "", text)
        elif method == "turn/completed":
            turn = params.get("turn") or {}
            status = subagent_status(turn.get("status")) or SUBAGENT_COMPLETED
            self._children[child] = status
            self._on_subagent(child, "", status, f"Finished: {status}")

    def _stop_children(self, server: "CodexServer") -> None:
        """Interrupt each agent still running; the parent's interrupt does not.

        Sent and not waited on: the verify that follows is for the
        conversation this tab owns, and an agent that ignores the stop is
        let go with the turn anyway.
        """
        for child, status in list(self._children.items()):
            turn = self._child_turns.get(child, "")
            if status != SUBAGENT_RUNNING or not turn:
                continue
            server.send(
                {
                    "method": "turn/interrupt",
                    "id": server.next_id(),
                    "params": {"threadId": child, "turnId": turn},
                }
            )

    def _agent_delta(self, params: dict) -> None:
        """One streamed fragment of a Codex answer, said as it arrives.

        The terminal shows these deltas as they land; holding them until
        the item completed was the whole wait a listener sat through.
        Only finished sentences go out, so nothing torn is spoken.
        """
        item_id = str(params.get("itemId") or "")
        delta = str(params.get("delta") or "")
        if not delta:
            return
        self._assistant_delta_seen.add(item_id)
        self._assistant_streams.setdefault(item_id, []).append(delta)
        self._assistant_parts.append(delta)
        live = self._assistant_live.get(item_id)
        if live is None:
            live = self._assistant_live[item_id] = SentenceStreamer(
                lambda text: self._on_activity("assistant", text)
            )
        live.feed(delta)

    def _item_completed(self, item: dict) -> None:
        self._collab(item)
        kind = item.get("type")
        item_id = str(item.get("id") or "")
        if kind == "agentMessage":
            text = str(item.get("text") or "")
            if item_id in self._assistant_delta_seen:
                streamed = "".join(self._assistant_streams.pop(item_id, []))
                live = self._assistant_live.pop(item_id, None)
                if live is not None:
                    # Whatever the last sentence left unfinished is all that
                    # has not been heard yet; the rest went out live.
                    live.flush()
                elif streamed:
                    self._on_activity("assistant", streamed)
            elif text:
                self._assistant_parts.append(text)
                self._on_activity("assistant", text)
        elif kind in ("reasoning", "plan"):
            reasoning = "".join(self._reasoning_streams.pop(item_id, []))
            if reasoning:
                self._on_activity("thinking", reasoning)
        elif kind == "commandExecution":
            chunks = self._tool_outputs.pop(item_id, [])
            output = "".join(chunks) or str(item.get("aggregatedOutput") or "")
            if output.strip():
                self._on_activity("result", output.strip())
        elif kind == "fileChange":
            changes = item.get("changes") or []
            summary = []
            for change in changes:
                if isinstance(change, dict) and change.get("path"):
                    summary.append(f"{change.get('kind') or 'changed'}: {change['path']}")
            if summary:
                self._on_activity("result", "\n".join(summary))


def _codex_item_label(item: dict) -> str:
    """The line that says what a Codex item is doing as it starts, or nothing."""
    kind = item.get("type")
    if kind == "commandExecution":
        command = item.get("command")
        if isinstance(command, list):
            command = " ".join(map(str, command))
        return f"Running: {command or 'command'}"
    if kind == "fileChange":
        changes = item.get("changes") or []
        paths = [str(c.get("path")) for c in changes if isinstance(c, dict) and c.get("path")]
        return "Editing " + (", ".join(paths) or "files")
    if kind == "mcpToolCall":
        return f"Using {item.get('server') or 'MCP'}: {item.get('tool') or 'tool'}"
    if kind == "webSearch":
        return f"Searching the web: {item.get('query') or ''}".strip()
    if kind == "imageView":
        return f"Viewing {item.get('path') or 'image'}"
    return ""
