"""FreeBuff's pseudo-terminal, prewarm, and chat/log/question helpers.

FreeBuff has no headless protocol: it is an interactive TUI, so BlindPilot
drives it through a pseudo-terminal, scrapes the screen, and reads its chat
and log files on disk. All of that machinery lives here, extracted from
agent_backends.py so FreebuffWorker has a focused surface to talk to.
agent_backends re-exports the public symbols for zero caller churn.

Copyright (c) 2026 doubletaponair and BlindPilot contributors.
Based on the original Claude Code Reader application by doubletaponair:
https://github.com/doubletaponair/claude-code-reader
SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import atexit
import json
import os
import platform
import queue
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Optional, Protocol, cast

import process_utils
import questions

# Shared symbols that stay in agent_backends (resolved after that module loads).
from agent_backends import (
    BACKEND_FREEBUFF,
    FREEBUFF_PREFERRED_MODEL,
    _read_freebuff_choice,
    _strip_terminal_noise,
    _tool_use_label,
    find_backend_cli,
    set_freebuff_model,
    subagent_line,
    subprocess_env,
)
from owned_process import _WindowsJob, owned_popen


def _freebuff_chat_folders(cwd: str) -> list[Path]:
    """Every folder a FreeBuff chat for this workspace could be stored under."""
    root = Path.home() / ".config" / "manicode" / "projects"
    candidates = [root / Path(cwd).name / "chats", root / "chats"]
    try:
        candidates.extend(path for path in root.glob("*/chats") if path not in candidates)
    except OSError:
        pass
    return candidates


def _freebuff_chat_dirs(cwd: str) -> dict[str, float]:
    """Return known FreeBuff chat ids and modification times.

    FreeBuff derives its storage bucket from the Git root, which is not always
    the workspace basename supplied with ``--cwd``.  Search every chat bucket
    so a newly-created id can still be captured and resumed.
    """
    found: dict[str, float] = {}
    for folder in _freebuff_chat_folders(cwd):
        try:
            for path in folder.iterdir():
                if path.is_dir():
                    found[path.name] = max(found.get(path.name, 0.0), path.stat().st_mtime)
        except OSError:
            pass
    return found


def _freebuff_chat_path(cwd: str, session_id: str) -> Optional[Path]:
    """Locate one FreeBuff chat regardless of its Git-derived project bucket."""
    if not session_id:
        return None
    for folder in _freebuff_chat_folders(cwd):
        candidate = folder / session_id
        if candidate.is_dir():
            return candidate
    return None


def _freebuff_resume_chat_path(cwd: str, session_id: str) -> Optional[Path]:
    """The CLI only resumes IDs in this project's bucket; otherwise it falls back."""
    chat = _freebuff_chat_path(cwd, session_id)
    project = Path(cwd).resolve().name
    expected = Path.home() / ".config" / "manicode" / "projects" / project / "chats"
    return chat if chat is not None and chat.parent == expected else None


_FREEBUFF_INTERRUPTED = "[response interrupted]"
_FREEBUFF_BOOT_FINGERPRINT = "Enhanced CLI fingerprint generated successfully"

# What FreeBuff's start screen says while it is waiting for a model to be
# chosen. The wording has moved between releases - the card was once labelled
# RECOMMENDED and is now introduced by a heading - so all of it is recognised:
# a chooser nobody answers is a terminal that never reaches its composer, and a
# message that is never sent.
_FREEBUFF_PICKER_RE = re.compile(r"(?i)RECOMMENDED|Start coding for free|See all \d+ models?")

# Either phrase means the full model list is already on screen. 0.0.168 opens
# on the expanded list and says "Show fewer"; earlier releases opened on a
# recommended card with a "See all N models" entry. Navigating straight to a
# model is only correct on the expanded screen — on the collapsed one the
# first Down moves to that entry instead.
_FREEBUFF_PICKER_EXPANDED_RE = re.compile(r"(?i)show fewer|see all \d+ models?")

# The title FreeBuff draws on the box its `ask_user` tool opens. It is the one
# thing on that screen that is always there and never part of an answer, so it
# is what says a turn has stopped to ask something.
_FREEBUFF_QUESTION_MARKER = "Some questions for you"

# 0.0.180 draws each message in the transcript under a divider carrying the
# time it was sent, so a message is two lines rather than one. Measured on the
# release itself: "   [05:23 PM]" then "   Reply with the single word: pong".
# Dropping the message line and leaving the divider behind reads the time out
# as if the answer had said it.
_FREEBUFF_TIMESTAMP_RE = re.compile(r"^\[\d{1,2}:\d{2}\s*[AP]M\]$", re.IGNORECASE)

# A question in that box: collapsed (right-pointing) or open (down-pointing),
# numbered only when there is more than one.
_FREEBUFF_QUESTION_RE = re.compile(r"^([▼▶])\s*(?:\d+\.\s*)?(\S.*?)\s*$")

# One answer in the open question: a radio circle, or a checkbox where the
# question takes more than one answer.
_FREEBUFF_OPTION_RE = re.compile(r"^\s+([○●☐☑])\s+(\S.*?)\s*$")

# FreeBuff adds this entry itself, below the model's own options, and opens a
# text field when it is chosen. BlindPilot's dialog offers the same thing in
# its own words, so the entry is not repeated there.
_FREEBUFF_CUSTOM_LABEL = "Custom"

_FREEBUFF_MULTI_MARKER = "(Select multiple options)"

# The box's bottom-left corner, and the side it draws down both edges.
_FREEBUFF_BOX_BOTTOM = "╰"
_FREEBUFF_BOX_SIDE = "│"
_FREEBUFF_OPEN_MARK = "▼"
_FREEBUFF_CHECKBOXES = ("☐", "☑")

# Keys the box understands. Down moves through the answers, wrapping into the
# next question once it runs off the end of one; Enter chooses; Tab jumps to
# Submit; Escape closes the box, which FreeBuff reports to the model as the
# questions having been skipped.
_KEY_DOWN = "\x1b[B"
_KEY_ENTER = "\r"
_KEY_TAB = "\t"
_KEY_ESCAPE = "\x1b"

# Long enough for OpenTUI to repaint between keystrokes.
_FREEBUFF_KEY_SETTLE = 0.15

# FreeBuff's own tool takes at least one question and puts no ceiling on
# them; this is the point past which a box has stopped being a question
# and started being a loop.
_FREEBUFF_MAX_QUESTIONS = 8


def _freebuff_question_box(visible: str) -> list[str]:
    """The lines of FreeBuff's question box, with its border taken off."""
    lines = visible.split("\n")
    start = next(
        (index for index, line in enumerate(lines) if _FREEBUFF_QUESTION_MARKER in line),
        -1,
    )
    if start < 0:
        return []
    body: list[str] = []
    for line in lines[start + 1 :]:
        stripped = line.strip()
        if stripped.startswith(_FREEBUFF_BOX_BOTTOM):
            break
        if stripped.startswith(_FREEBUFF_BOX_SIDE):
            stripped = stripped[1:]
        if stripped.endswith(_FREEBUFF_BOX_SIDE):
            stripped = stripped[:-1]
        body.append(stripped.rstrip())
    return body


def _freebuff_open_question(visible: str) -> tuple[int, int, Optional[questions.Question]]:
    """(how many questions there are, which one is open, and that question).

    FreeBuff shows one question's answers at a time and keeps the rest folded
    away, so this reads whichever one is open. The count is what says how many
    more are still to come.
    """
    body = _freebuff_question_box(visible)
    if not body:
        return 0, -1, None
    total = 0
    open_index = -1
    text = ""
    options: list[questions.QuestionOption] = []
    multi = False
    for line in body:
        header = _FREEBUFF_QUESTION_RE.match(line)
        if header:
            if header.group(1) == _FREEBUFF_OPEN_MARK:
                if open_index >= 0:
                    # Two open questions at once is a half-drawn frame; keep
                    # the first, which is the one already read.
                    break
                open_index = total
                text = header.group(2)
                options = []
                multi = False
            total += 1
            continue
        if open_index < 0 or total != open_index + 1:
            continue
        if _FREEBUFF_MULTI_MARKER in line:
            multi = True
            continue
        option = _FREEBUFF_OPTION_RE.match(line)
        if option:
            if option.group(1) in _FREEBUFF_CHECKBOXES:
                multi = True
            options.append(questions.QuestionOption(option.group(2)))
    if open_index < 0 or not text:
        return total, -1, None
    if options and options[-1].label == _FREEBUFF_CUSTOM_LABEL:
        options = options[:-1]
    return (
        total,
        open_index,
        questions.Question(question=text, options=tuple(options), multi_select=multi),
    )


# How long a frame must hold still before it is read out. The terminal repaints
# in bursts, so this is only long enough to let one burst land whole.
_FREEBUFF_FRAME_SECONDS = 0.1

# Never wait longer than this to read out a finished sentence, however busy the
# repainting is. Text that arrives faster than the frames settle would otherwise
# never reach the listener until the turn ended.
_FREEBUFF_MAX_LAG_SECONDS = 0.4

# The longest a single FreeBuff turn is listened to. Reaching it is not the
# turn finishing, and is reported as what it is - see the end of `_do_run`.
_FREEBUFF_TURN_SECONDS = 60 * 60

# How long a starting FreeBuff may paint nothing at all before the message is
# given up on. This bounds the wait by silence rather than by the clock: any
# repaint at all -- a download progress bar, a splash, the model picker -- is
# progress and starts it again, so a slow first launch that is visibly doing
# something is never cut off. What it does catch is a FreeBuff that starts,
# connects, and then paints nothing ever again, which is what 0.0.163 does
# here: the turn's own deadline is an hour, so waiting that out cost the
# message and an hour of silence with it. Two minutes because a first launch
# downloads a 125MB FreeBuff and then unpacks it, and only the download half
# draws a progress bar.
_FREEBUFF_STARTUP_SILENCE_SECONDS = 120.0

# How long a mid-turn drop is listened to before it is called. A session can
# rejoin on its own — the reconnection lands within a second when it does —
# and a turn abandoned at the first word of trouble would throw away work that
# a moment of patience would have saved.
_FREEBUFF_DROP_PATIENCE_SECONDS = 30.0

# How long the model picker is walked before the chosen model is given up on.
# The navigation above it is a fixed number of arrow keys, so this only ever
# elapses when the model is not on the cards at all — see the swap below.
_FREEBUFF_PICKER_PATIENCE_SECONDS = 5.0


def _unwrap_screen_text(text: str) -> str:
    """Rejoin the lines the terminal broke, keeping the ones the answer meant.

    A captured frame is text laid out to the width of FreeBuff's box, so most of
    its newlines belong to the terminal rather than the answer.  Left in, every
    one of them reads as the end of a sentence, and the reading stops in the
    middle of a clause.  A line that runs the full width was broken because it
    ran out of room and continues below; a short one ended because the text did.
    """
    lines = [line.rstrip() for line in text.splitlines()]
    if len(lines) < 2:
        return text.strip()
    width = max(len(line) for line in lines)
    if width < 24:
        return text.strip()
    pieces: list[str] = []
    for index, line in enumerate(lines[:-1]):
        pieces.append(line)
        following = lines[index + 1]
        wrapped = bool(line) and bool(following) and len(line) >= width - 12
        pieces.append(" " if wrapped else "\n")
    pieces.append(lines[-1])
    return "".join(pieces).strip()


def _keyed(text: str, letters_only: bool = False) -> tuple[str, list[int]]:
    """A comparable form of ``text``, and where each kept character came from.

    Whitespace always goes, because the terminal decides where the answer
    breaks its lines and revises that as the text grows: the same words can be
    one line in one frame and two in the next. Dropping everything except
    letters and digits goes further, and makes the answer as the terminal drew
    it comparable with the Markdown it was drawn from.
    """
    kept: list[str] = []
    positions: list[int] = []
    for index, character in enumerate(text):
        if character.isspace() or (letters_only and not character.isalnum()):
            continue
        folded = character.casefold() if letters_only else character
        kept.append(folded)
        # One position per character of the *key*, not per character of the
        # text. `casefold` is not length-preserving - German "ss" comes from a
        # single letter, Turkish and the ligatures do the same - and a map with
        # one entry per input character then runs short, so the index used to
        # cut the answer pointed at the wrong letter or off the end entirely.
        positions.extend([index] * len(folded))
    # The sentinel, so an answer that was read out in full is still indexable.
    positions.append(len(text))
    return "".join(kept), positions


def _freebuff_echo(text: str) -> tuple[str, str]:
    """What to look for on screen for a message typed into FreeBuff.

    FreeBuff writes what it is given into the transcript as plain text, with
    nothing to mark it as the person's own words, so an echo is recognised by
    its content: the first line, which is what a transcript line is matched
    against, and the whole message reduced to letters and digits, which is what
    says whether the lines below that match are the rest of the echo or the
    reply's first line.
    """
    first = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return first[:60], _keyed(text, letters_only=True)[0]


def _unspoken_tail(narrated: str, answer: str) -> str:
    """The part of the finished answer that was never read out.

    What was read came off the screen, laid out for a terminal; the finished
    answer comes from the saved chat, written as Markdown. Comparing only the
    letters and digits is what makes the two comparable, so that an answer
    which scrolled out of view while it was being written is still finished
    aloud, and one that was read in full is not read twice.
    """
    spoken_key, _spoken_positions = _keyed(narrated, letters_only=True)
    answer_key, positions = _keyed(answer, letters_only=True)
    if not answer_key:
        return ""
    if not spoken_key:
        return answer.strip()
    if answer_key.startswith(spoken_key):
        return answer[positions[len(spoken_key)] :].strip()
    if answer_key in spoken_key:
        return ""
    # The two do not line up at all, which means the reading was of something
    # else. Reading the answer again is better than never reading it.
    return answer.strip()


def _append_delta(spoken: str, current: str) -> str:
    """The part of ``current`` that has not been read out yet.

    The screen is a window onto the answer, not the whole of it: once the answer
    is taller than the terminal, the top scrolls away and a frame becomes a
    continuation rather than an extension.  Matching the longest overlap between
    the end of what was read and the start of what is on screen keeps the
    reading in one piece across that scroll, instead of repeating a paragraph or
    silently dropping one.
    """
    if not current:
        return ""
    if not spoken:
        return current
    spoken_key, _spoken_positions = _keyed(spoken)
    current_key, positions = _keyed(current)
    if not current_key:
        return ""
    if current_key.startswith(spoken_key):
        return current[positions[len(spoken_key)] :]
    # The tail of what was read is normally still on screen, so find it there
    # first; that is one string search rather than a scan over every overlap.
    anchor = spoken_key[-160:]
    if anchor:
        position = current_key.rfind(anchor)
        if position >= 0:
            return current[positions[position + len(anchor)] :]
    for size in range(min(len(spoken_key), len(current_key), 400), 0, -1):
        if spoken_key.endswith(current_key[:size]):
            return current[positions[size] :]
    return current


def _freebuff_answer_message(payload: object) -> Optional[dict]:
    """Return the newest AI message that actually carries a reply.

    FreeBuff writes a ``mode-divider`` message ahead of every turn, and it also
    has ``variant: "ai"``.  Skipping messages with no reply blocks keeps the
    identity of the turn's real answer stable from the moment it appears.
    """
    if not isinstance(payload, list):
        return None
    for item in reversed(payload):
        if not isinstance(item, dict) or item.get("variant") != "ai":
            continue
        blocks = item.get("blocks")
        if isinstance(blocks, list) and any(
            isinstance(block, dict) and block.get("type") in ("text", "agent") for block in blocks
        ):
            return item
    return None


def _freebuff_answer_id(chat: Optional[Path]) -> str:
    """Identify the newest answer already in a chat, before a new turn starts."""
    if chat is None:
        return ""
    try:
        payload = json.loads((chat / "chat-messages.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    message = _freebuff_answer_message(payload)
    return str(message.get("id") or "") if message else ""


def _freebuff_chat_snapshot(
    chat: Optional[Path],
) -> tuple[str, str, str, list[tuple[str, str, str, tuple[str, ...]]]]:
    """Read the newest answer's id, reasoning, text, and agent states.

    Each agent comes with its log so far: the task it was given, each tool
    it called, and what it wrote.

    The id is what tells one turn's answer from the one before it: FreeBuff
    rewrites the whole file on every save, so text alone cannot distinguish new
    output from a resumed conversation's history.
    """
    if chat is None:
        return "", "", "", []
    try:
        payload = json.loads((chat / "chat-messages.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "", "", "", []
    message = _freebuff_answer_message(payload)
    if message is None:
        return "", "", "", []
    message_id = str(message.get("id") or "")

    thinking: list[str] = []
    answer: list[str] = []
    agents: list[tuple[str, str, str, tuple[str, ...]]] = []
    blocks = message.get("blocks")
    if not isinstance(blocks, list):
        blocks = []
    for index, block in enumerate(blocks):
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind == "text":
            content = str(block.get("content") or "").strip()
            # Closing the hidden terminal stamps this marker onto whatever the
            # turn had produced, so it has to come off the end as well as being
            # rejected on its own.
            if content.endswith(_FREEBUFF_INTERRUPTED):
                content = content[: -len(_FREEBUFF_INTERRUPTED)].strip()
            if not content:
                continue
            if block.get("textType") == "reasoning":
                thinking.append(content)
            else:
                answer.append(content)
        elif kind == "agent":
            name = str(block.get("agentName") or block.get("agentType") or "agent").strip()
            status = str(block.get("status") or "running").strip().casefold()
            agent_id = str(block.get("agentId") or f"{index}:{name}")
            agents.append((agent_id, name, status, _freebuff_agent_log(block)))
    return message_id, "\n\n".join(thinking), "\n\n".join(answer), agents


def _freebuff_agent_log(block: dict) -> tuple[str, ...]:
    """One spawned agent's log, from its block in the saved chat."""
    lines: list[str] = []
    task = subagent_line(block.get("initialPrompt"))
    if task:
        lines.append(f"Task: {task}")
    inner = block.get("blocks")
    for step in inner if isinstance(inner, list) else []:
        if not isinstance(step, dict):
            continue
        kind = step.get("type")
        if kind == "tool":
            params = step.get("input")
            lines.append(
                _tool_use_label(
                    str(step.get("toolName") or "tool"), params if isinstance(params, dict) else {}
                )
            )
        elif kind == "text":
            text = subagent_line(step.get("content"))
            if text:
                lines.append(text)
        elif kind == "agent":
            lines.append(f"Started {step.get('agentName') or step.get('agentType') or 'an agent'}")
    return tuple(lines)


def _freebuff_chat_stamp(chat: Optional[Path]) -> tuple:
    """A cheap fingerprint of a chat's files, to skip re-reading them.

    The loop looks at the chat many times a second so that the end of a turn is
    noticed the moment it happens; asking the filesystem what changed is orders
    of magnitude cheaper than parsing the whole conversation to find out.
    """
    if chat is None:
        return ()
    marks: list[tuple[int, int]] = []
    for name in ("chat-messages.json", "log.jsonl"):
        try:
            stat = (chat / name).stat()
            marks.append((stat.st_mtime_ns, stat.st_size))
        except OSError:
            marks.append((0, 0))
    return tuple(marks)


def _freebuff_run_status(chat: Optional[Path], offset: int = 0) -> str:
    """Return complete/cancelled/disconnected from FreeBuff's per-chat log.

    "session over" alone is not a drop: 0.0.168 opens every new chat with
    "Freebuff session over; holding queued messages until rejoin" and then
    reconnects within a second. A drop is a "session over" that no later
    "Reconnection detected" or "Start agent" line answers, so the reader
    walks the lines in order and only reports what is still pending at the
    end. Complete and cancelled stay whole-log readings, since a turn that
    finished is finished however the session ended afterwards.
    """
    if chat is None:
        return ""
    tail = _freebuff_log_tail(chat, offset)
    if not tail:
        return ""
    messages = _freebuff_log_messages(tail)
    if any(message.startswith("Agent run cancelled by user") for message in messages):
        return "cancelled"
    if "Main prompt finished" in messages:
        return "complete"
    if _freebuff_log_pending_drop(tail):
        return "disconnected"
    return ""


def _freebuff_log_messages(tail: str) -> list[str]:
    """Read event names, never prompt or answer text inside the log data."""
    messages = []
    for line in tail.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            # The final line can still be in the middle of being written.
            continue
        if isinstance(entry, dict) and isinstance(entry.get("msg"), str):
            messages.append(entry["msg"])
    return messages


def _freebuff_log_pending_drop(tail: str) -> bool:
    """Whether a log's last word on the session is a drop nobody answered.

    The lines are walked in order: "session over" opens a pending drop and a
    later "Reconnection detected" or "Start agent" closes it. What is left
    pending at the end is the session's state right now.
    """
    pending = False
    for line in _freebuff_log_messages(tail):
        if line.startswith("[chat-runtime] Freebuff session over;"):
            pending = True
        elif (
            line.startswith(("Reconnection detected", "Start agent "))
            or line == _FREEBUFF_BOOT_FINGERPRINT
        ):
            pending = False
    return pending


def _freebuff_log_tail(chat: Path, offset: int = 0) -> str:
    """The per-chat log from ``offset`` on, decoded lossily."""
    try:
        with (chat / "log.jsonl").open("rb") as handle:
            handle.seek(max(0, offset))
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


# Said once, in the same words, wherever a genuine drop is noticed: a turn
# accepted and then never answered, on a session that logged "session over"
# and never reconnected.
_FREEBUFF_REJOIN_MESSAGE = (
    "FreeBuff dropped its session and is holding the message until it rejoins, "
    "but nothing is answering. Quit and reopen FreeBuff, then send the message again."
)


def _freebuff_log_boot_finished(tail: str) -> bool:
    """Whether a launch's log shows a FreeBuff that can take a message.

    Every measured boot (0.0.180, 2026-09-01; 0.2.26, 2026-10-12) writes the
    same sequence: "session over", then the enhanced-fingerprint line once
    sign-in is settled, then -- in the older release -- "Reconnection
    detected". 0.2.26 never writes the reconnection line, so the fingerprint
    is the last boot milestone both releases share. A boot is finished when
    that line is in the log, or when a session that dropped has visibly
    reconnected. Nothing earlier counts: the MCP-servers line is first, and
    a tail with no drop yet is a boot that has not reached the drop either.
    """
    messages = _freebuff_log_messages(tail)
    if _freebuff_log_pending_drop(tail):
        return False
    if _FREEBUFF_BOOT_FINGERPRINT in messages:
        return True
    return any(
        message.startswith(("Reconnection detected", "Start agent ")) for message in messages
    )


def _freebuff_boot_ready(
    cwd: str,
    before: dict[str, float],
    chat_path: Optional[Path],
    log_offset: int,
    pid: int = 0,
) -> bool:
    """Whether FreeBuff's chat runtime has reconnected and can take a message.

    0.0.168 paints its composer before it has finished starting, and a message
    given to that composer is lost silently: the chat log records no send at
    all. That is still true in 0.2.26, and worse: measured live, keystrokes
    sent 1.4 seconds after launch vanish without reaching the composer,
    while the same send at 2.6 seconds is answered. The recovery used to be
    visible in the chat's own log -- "session over" answered by
    "Reconnection detected" within a second -- but 0.2.26 writes no
    reconnection line, so readiness is the boot's last shared milestone (see
    :func:`_freebuff_log_boot_finished`). For a conversation this launch
    created, the whole log is read and the milestone must be seen; for a
    resumed one, only the lines written since the launch started are read,
    and the milestone must appear in those new lines too. A launch that has
    created no chat yet cannot be ready either: its
    chat is created when the terminal starts, so a missing chat is a boot
    still in progress, and the startup-silence failure is the honest end of
    a launch that never gets there. Taking it as ready was the hole the
    1.4-second send fell through.
    """
    if chat_path is not None:
        tail = _freebuff_log_tail(chat_path, log_offset)
        if not tail.strip():
            return False
        return _freebuff_log_boot_finished(tail)
    new_tail = _freebuff_new_chat_log(before, cwd, pid)
    if new_tail is None:
        return False
    if not new_tail.strip():
        # A chat folder with nothing logged yet is a boot still in progress;
        # the recovery line arrives moments later.
        return False
    return _freebuff_log_boot_finished(new_tail)


def _freebuff_new_chat_path(before: dict[str, float], cwd: str, pid: int = 0) -> Optional[Path]:
    """Find this terminal's chat using the process id FreeBuff logs at boot."""
    after = _freebuff_chat_dirs(cwd)
    new_ids = set(after) - set(before)
    if not new_ids:
        return None
    family = process_utils._descendant_pids({pid}) if pid else set()
    for chat_id in sorted(new_ids, key=lambda chat_id: after[chat_id], reverse=True):
        target = _freebuff_chat_path(cwd, chat_id)
        if target is None:
            continue
        if not pid:
            return target
        try:
            with (target / "log.jsonl").open("rb") as log:
                first_lines = log.read(8192).decode("utf-8", "replace")
        except OSError:
            continue
        for line in first_lines.splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and entry.get("pid") in family:
                return target
    return None


def _freebuff_new_chat_log(before: dict[str, float], cwd: str, pid: int = 0) -> Optional[str]:
    """The whole log of the chat this launch created, or None if it made none."""
    target = _freebuff_new_chat_path(before, cwd, pid)
    if target is None:
        return None
    return _freebuff_log_tail(target, 0)


def _freebuff_dropped_new_chat(before: dict[str, float], cwd: str, pid: int = 0) -> bool:
    """Whether a chat created during this launch is waiting on a dead session.

    "session over" is also the first line of a healthy boot, so the question
    is not whether it was logged but whether anything ever answered it. Only
    chats created during this launch are consulted: a drop logged by an
    earlier session must not fail this one.
    """
    tail = _freebuff_new_chat_log(before, cwd, pid)
    return bool(tail and tail.strip()) and _freebuff_log_pending_drop(cast(str, tail))


def _last_visible_line(visible: str) -> str:
    """The last non-blank line on the screen, short enough to be spoken."""
    lines = [line.strip() for line in _strip_terminal_noise(visible).splitlines()]
    reason = next((line for line in reversed(lines) if line), "")
    if len(reason) > 200:
        reason = reason[:199] + "…"
    return reason


def _freebuff_launch_failure(visible: str) -> str:
    """Say why FreeBuff's terminal closed, in its own words where it gave any.

    A terminal that dies during start-up has usually printed one line saying
    why. ``env: node: No such file or directory`` is the common one on macOS,
    where an application started from the Dock inherits a PATH holding no Node
    at all, and it is far more use than a guess at reinstalling.
    """
    reason = _last_visible_line(visible)
    if not reason:
        return (
            "FreeBuff's terminal closed before it was ready for a prompt, "
            "without saying why. Check that FreeBuff runs in a terminal, then "
            "try again."
        )
    if "node" in reason.casefold() and "not found" in reason.casefold().replace(
        "no such file or directory", "not found"
    ):
        return (
            f"FreeBuff could not start: {reason}. FreeBuff runs on Node.js, and "
            "BlindPilot could not find it. Install Node.js, or use File \u2192 "
            "Manage Backends to let BlindPilot install one for you."
        )
    return f"FreeBuff's terminal closed before it was ready for a prompt: {reason}"


def _freebuff_startup_silence(visible: str, seconds: float) -> str:
    """Say that FreeBuff started, went quiet, and never asked for a prompt.

    A terminal that dies prints why on its way out, and
    :func:`_freebuff_launch_failure` reports that sentence. This is the other
    shape of the same lost message: FreeBuff is still running, still connected,
    and simply never paints a composer to type into.
    """
    waited = int(seconds)
    reason = _last_visible_line(visible)
    if reason:
        return (
            f"FreeBuff stopped short of a prompt and printed nothing for {waited} "
            f"seconds. The last thing it showed was: {reason}"
        )
    return (
        f"FreeBuff started but printed nothing at all for {waited} seconds, so "
        "there was never a prompt to send the message to. Run freebuff in a "
        "terminal to see what it does there: if it hangs there too, the "
        "installed FreeBuff release is at fault rather than BlindPilot."
    )


class FreebuffTerminal(Protocol):
    """The pseudo-terminal handle, whichever implementation produced it.

    _ConPtyTerminal on Windows and pexpect everywhere else hand back objects
    with nothing in common but the three calls made on them here. What those
    calls give back differs too, and none of it is used, so none of it is named.
    """

    def write(self, text: str, /) -> object: ...

    def terminate(self, force: bool = False) -> object: ...

    def close(self, force: bool = False) -> object: ...


def _drain_to_queue(
    read_one: Callable[[], Optional[str]], stream_ended: threading.Event
) -> Callable[[float], str]:
    """Pump a terminal into a queue on a thread of its own, and hand back a reader.

    Draining starts at launch rather than when somebody first asks for
    output: a prewarmed terminal has no worker reading it yet, and a full PTY
    buffer blocks the TUI before it can process input even though its
    connection log says ready.

    ``read_one`` hands back whatever has arrived, "" for nothing yet, and
    None once nothing more ever will. Reading to that rather than while the
    terminal is alive is what keeps the last line of a terminal that died at
    startup -- the one that says why -- still buffered after isalive() goes
    false.
    """
    chunks: queue.Queue[str] = queue.Queue()

    def pump() -> None:
        try:
            while not stream_ended.is_set():
                data = read_one()
                if data is None:
                    break
                if data:
                    chunks.put(data)
        finally:
            # Nothing more will ever arrive. Saying so turns a terminal that
            # died at startup into a reported failure instead of an hour of
            # silence.
            stream_ended.set()

    threading.Thread(target=pump, daemon=True).start()

    def read(timeout: float) -> str:
        try:
            return chunks.get(timeout=timeout)
        except queue.Empty:
            return ""

    return read


class _ConPtyTerminal:
    """A process in a pseudoconsole this process created itself.

    pywinpty cannot be used for this: its native module calls AllocConsole
    in the host process before CreatePseudoConsole (the import is in the
    shipped ``_winpty`` binary), so a windowless BlindPilot gains a real
    console window -- hidden again by its own watcher only after it has
    appeared and taken screen-reader focus, which NVDA announces as
    "terminal". Calling CreatePseudoConsole directly allocates nothing in
    this process: the pseudoconsole's hosting window is not created, and
    the only console host spawned is OpenConsole in headless mode.
    """

    def __init__(
        self, args: list[str], cwd: str, env: dict[str, str], rows: int, cols: int
    ) -> None:
        import codecs
        import ctypes
        import ctypes.wintypes as wintypes

        self._ctypes = ctypes

        class _Coord(ctypes.Structure):
            _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

        self._coord = _Coord
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.pid = 0
        self._closed = False
        kernel32 = ctypes.windll.kernel32
        handle = ctypes.c_void_p
        kernel32.CreatePipe.argtypes = [
            ctypes.POINTER(handle),
            ctypes.POINTER(handle),
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.CreatePipe.restype = wintypes.BOOL
        kernel32.CreatePseudoConsole.argtypes = [
            self._coord,
            handle,
            handle,
            wintypes.DWORD,
            ctypes.POINTER(handle),
        ]
        kernel32.CreatePseudoConsole.restype = ctypes.c_long
        kernel32.CreateProcessW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.LPCWSTR,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        kernel32.CreateProcessW.restype = wintypes.BOOL
        kernel32.ReadFile.argtypes = [
            handle,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        kernel32.ReadFile.restype = wintypes.BOOL
        kernel32.WriteFile.argtypes = kernel32.ReadFile.argtypes
        kernel32.WriteFile.restype = wintypes.BOOL
        kernel32.InitializeProcThreadAttributeList.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
        kernel32.UpdateProcThreadAttribute.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
        kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.argtypes = [handle]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.GetExitCodeProcess.argtypes = [handle, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.TerminateProcess.argtypes = [handle, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.WaitForSingleObject.argtypes = [handle, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.ClosePseudoConsole.argtypes = [handle]
        kernel32.PeekNamedPipe.argtypes = [
            handle,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.PeekNamedPipe.restype = wintypes.BOOL

        input_read, input_write = handle(), handle()
        output_read, output_write = handle(), handle()
        if not kernel32.CreatePipe(ctypes.byref(input_read), ctypes.byref(input_write), None, 0):
            raise OSError("CreatePipe failed for the terminal's input")
        if not kernel32.CreatePipe(ctypes.byref(output_read), ctypes.byref(output_write), None, 0):
            kernel32.CloseHandle(input_read)
            kernel32.CloseHandle(input_write)
            raise OSError("CreatePipe failed for the terminal's output")
        self._input_write = input_write
        self._output_read = output_read
        self._pseudo = handle()
        self._process = None
        self._job: Optional[_WindowsJob] = None
        try:
            pseudo = handle()
            result = kernel32.CreatePseudoConsole(
                self._coord(cols, rows), input_read, output_write, 0, ctypes.byref(pseudo)
            )
            if result != 0:
                raise OSError(f"CreatePseudoConsole failed: HRESULT {result & 0xFFFFFFFF:#x}")
            self._pseudo = pseudo
            self._process, pid = self._create_process(args, cwd, env, pseudo)
        except Exception:
            self.close()
            raise
        finally:
            # The pseudoconsole keeps its own references to these two ends.
            kernel32.CloseHandle(input_read)
            kernel32.CloseHandle(output_write)
        self.pid = int(pid)

    def _create_process(self, args: list[str], cwd: str, env: dict[str, str], pseudo: object):
        import ctypes
        import ctypes.wintypes as wintypes

        kernel32 = ctypes.windll.kernel32

        class _StartupInfo(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("lpReserved", wintypes.LPWSTR),
                ("lpDesktop", wintypes.LPWSTR),
                ("lpTitle", wintypes.LPWSTR),
                ("dwX", wintypes.DWORD),
                ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD),
                ("dwYSize", wintypes.DWORD),
                ("dwXCountChars", wintypes.DWORD),
                ("dwYCountChars", wintypes.DWORD),
                ("dwFillAttribute", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD),
                ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
                ("hStdInput", ctypes.c_void_p),
                ("hStdOutput", ctypes.c_void_p),
                ("hStdError", ctypes.c_void_p),
            ]

        class _StartupInfoEx(ctypes.Structure):
            _fields_ = [("StartupInfo", _StartupInfo), ("lpAttributeList", ctypes.c_void_p)]

        class _ProcessInformation(ctypes.Structure):
            _fields_ = [
                ("hProcess", ctypes.c_void_p),
                ("hThread", ctypes.c_void_p),
                ("dwProcessId", wintypes.DWORD),
                ("dwThreadId", wintypes.DWORD),
            ]

        startup = _StartupInfoEx()
        startup.StartupInfo.cb = ctypes.sizeof(_StartupInfoEx)
        # STARTF_USESTDHANDLES with no handles named: the kernel then gives
        # the child the pseudoconsole as its standard handles. Left at the
        # default, the child inherits this process's standard handles
        # instead (measured: a python child's prints reached BlindPilot's
        # own stdout, bypassing the terminal entirely), while through
        # pywinpty the same child reported proper console handles.
        startup.StartupInfo.dwFlags = 0x00000100
        startup.StartupInfo.hStdInput = None
        startup.StartupInfo.hStdOutput = None
        startup.StartupInfo.hStdError = None
        size = ctypes.c_size_t(0)
        kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        startup.lpAttributeList = ctypes.cast(buffer, ctypes.c_void_p)
        if not kernel32.InitializeProcThreadAttributeList(
            startup.lpAttributeList, 1, 0, ctypes.byref(size)
        ):
            raise OSError("InitializeProcThreadAttributeList failed")
        try:
            # PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE
            if not kernel32.UpdateProcThreadAttribute(
                startup.lpAttributeList,
                0,
                ctypes.c_size_t(0x00020016),
                pseudo,
                ctypes.sizeof(ctypes.c_void_p),
                None,
                None,
            ):
                raise OSError("UpdateProcThreadAttribute failed for the pseudoconsole")
            argv = list(args)
            command = subprocess.list2cmdline(argv)
            if argv and argv[0].lower().endswith((".cmd", ".bat")):
                # CreateProcess cannot run a batch file; cmd hosts it, as it
                # did under pywinpty (its /c invocation is what process
                # listings of the old path show).
                comspec = os.environ.get("COMSPEC") or r"C:\Windows\System32\cmd.exe"
                # Expand each argument once inside quotes: literal % and shell
                # operators in a folder name must never become cmd commands.
                if any('"' in arg for arg in argv):
                    raise OSError("Batch launcher arguments cannot contain double quotes")
                env = dict(env)
                for index, arg in enumerate(argv):
                    env[f"BLINDPILOT_PTY_ARG_{index}"] = arg
                arguments = " ".join(
                    f'"%BLINDPILOT_PTY_ARG_{index}%"' for index in range(len(argv))
                )
                command = f'{subprocess.list2cmdline([comspec])} /d /v:off /s /c "{arguments}"'
            command_line = ctypes.create_unicode_buffer(command)
            env_block = ctypes.create_unicode_buffer(
                "\0".join(f"{key}={value}" for key, value in env.items()) + "\0"
            )
            info = _ProcessInformation()
            # EXTENDED_STARTUPINFO_PRESENT | CREATE_UNICODE_ENVIRONMENT |
            # CREATE_SUSPENDED: no child can run before Job assignment.
            if not kernel32.CreateProcessW(
                None,
                command_line,
                None,
                None,
                False,
                0x00080000 | 0x00000400 | 0x00000004,
                env_block,
                cwd,
                ctypes.byref(startup),
                ctypes.byref(info),
            ):
                raise ctypes.WinError(kernel32.GetLastError())
        finally:
            kernel32.DeleteProcThreadAttributeList(startup.lpAttributeList)
        try:
            job = _WindowsJob()
            job.assign_handle(info.hProcess)
            if kernel32.ResumeThread(info.hThread) == 0xFFFFFFFF:
                raise OSError("ResumeThread failed for the FreeBuff backend")
            self._job = job
        except BaseException:
            kernel32.TerminateProcess(info.hProcess, 1)
            kernel32.CloseHandle(info.hProcess)
            if "job" in locals():
                job.close()
            raise
        finally:
            kernel32.CloseHandle(info.hThread)
        return info.hProcess, int(info.dwProcessId)

    def read_chunk(self) -> Optional[str]:
        """One read; None when nothing more will ever arrive.

        The output pipe stays open while the pseudoconsole does, even after
        the process has exited, so a plain blocking ReadFile would never
        report the end: bytes are taken only when the pipe says some are
        waiting, and the end is the exited process with an empty pipe. The
        host can still be forwarding the last output just after the exit,
        so an empty pipe has to stay empty briefly before it is believed.
        """
        ctypes = self._ctypes
        kernel32 = ctypes.windll.kernel32
        empty_since: Optional[float] = None
        idle_since: Optional[float] = None
        while True:
            if self._closed:
                return None
            available = ctypes.wintypes.DWORD(0)
            if not kernel32.PeekNamedPipe(
                self._output_read, None, 0, None, ctypes.byref(available), None
            ):
                return None
            if available.value:
                buffer = ctypes.create_string_buffer(min(4096, available.value))
                read = ctypes.wintypes.DWORD(0)
                if (
                    not kernel32.ReadFile(
                        self._output_read, buffer, len(buffer), ctypes.byref(read), None
                    )
                    or not read.value
                ):
                    return None
                return self._decoder.decode(buffer.raw[: read.value])
            delay = 0.01
            if self.isalive():
                empty_since = None
                # A live but silent terminal (an agent thinking, a prompt
                # waiting) was polled 100 times a second for as long as
                # it sat there. Back off once it has been quiet a second;
                # arriving data is still picked up within 50 ms.
                if idle_since is None:
                    idle_since = time.monotonic()
                elif time.monotonic() - idle_since > 1.0:
                    delay = 0.05
            elif empty_since is None:
                empty_since = time.monotonic()
            elif time.monotonic() - empty_since > 0.5:
                return None
            time.sleep(delay)

    def write(self, text: str) -> None:
        if self._closed:
            raise OSError("the terminal's input pipe is closed")
        ctypes = self._ctypes
        kernel32 = ctypes.windll.kernel32
        data = text.encode("utf-8")
        view = (ctypes.c_char * len(data)).from_buffer_copy(data)
        written_total = 0
        while written_total < len(data):
            written = ctypes.wintypes.DWORD(0)
            if not kernel32.WriteFile(
                self._input_write,
                ctypes.byref(view, written_total),
                len(data) - written_total,
                ctypes.byref(written),
                None,
            ):
                raise OSError("the terminal's input pipe is closed")
            written_total += written.value

    def isalive(self) -> bool:
        if self._closed:
            return False
        kernel32 = self._ctypes.windll.kernel32
        code = self._ctypes.wintypes.DWORD(0)
        if not kernel32.GetExitCodeProcess(self._process, self._ctypes.byref(code)):
            return False
        return code.value == 259  # STILL_ACTIVE

    def terminate(self, force: bool = False) -> None:
        kernel32 = self._ctypes.windll.kernel32
        if self._job is not None:
            self._job.close()
        if self.isalive():
            kernel32.TerminateProcess(self._process, 1)

    def close(self, force: bool = False) -> None:
        if self._closed:
            return
        self._closed = True
        kernel32 = self._ctypes.windll.kernel32
        # Closing the pseudoconsole ends every process attached to it --
        # FreeBuff and the tools it started -- which terminate alone, aimed
        # only at the first process, would leave running.
        for handle in (self._input_write, self._output_read):
            try:
                kernel32.CloseHandle(handle)
            except Exception:
                pass
        # End owned descendants before closing the pseudoconsole, which can
        # otherwise wait forever for a child still writing to it.
        if self._job is not None:
            self._job.close()
        if self._pseudo:
            kernel32.ClosePseudoConsole(self._pseudo)
        if self._process is None:
            return
        if kernel32.WaitForSingleObject(self._process, 2000) == 258:  # WAIT_TIMEOUT
            kernel32.TerminateProcess(self._process, 1)
        kernel32.CloseHandle(self._process)


def _spawn_freebuff_pty(
    args: list[str], cwd: str, stream_ended: threading.Event
) -> tuple[FreebuffTerminal, Callable[[float], str]]:
    """Start FreeBuff under a pseudo-terminal, off screen, and read it.

    Returns the terminal and a call that yields whatever it has produced.
    Output is pumped into a queue by a thread of its own, so a terminal that
    started before anyone asked for its output keeps every byte of it.
    """
    if platform.system() == "Windows":
        # No console is claimed for this process, and none is needed: the
        # pseudoconsole is created directly (see _ConPtyTerminal), because
        # the library that used to create it allocated a console in this
        # process on the way. The watcher below stays as the safety net for
        # consoles FreeBuff's own tools can still raise.
        roots = {os.getpid()}

        def hide_terminal() -> None:
            # Poll hard while the terminal is starting, then slowly: the
            # tools FreeBuff itself starts can each raise a console of
            # their own, and any of them would take the reader's focus.
            # Tearing the terminal down raises one last console, after the
            # output has stopped, so this outlives the stream it guards.
            started = time.monotonic()
            grace_until: Optional[float] = None
            while True:
                now = time.monotonic()
                if stream_ended.is_set():
                    if grace_until is None:
                        grace_until = now + 5
                    elif now > grace_until:
                        return
                try:
                    process_utils.hide_console_windows(roots)
                except Exception:
                    # Never let this thread die: it is the only thing
                    # keeping the terminal off the screen for the whole run.
                    pass
                quick = now - started < 8 or grace_until is not None
                # 25 ms still catches a console within a frame or two of
                # it appearing; the old 5 ms cadence held a core busy in
                # EnumWindows sweeps for the whole fast phase.
                time.sleep(0.025 if quick else 0.25)

        threading.Thread(target=hide_terminal, daemon=True).start()

        try:
            pty = _ConPtyTerminal(args, cwd, subprocess_env(args[0]), rows=60, cols=180)
        except Exception:
            # No pump will ever set this, and the watcher above polls
            # EnumWindows until it is set.
            stream_ended.set()
            raise
        if pty.pid:
            roots.add(int(pty.pid))

        def read_one() -> Optional[str]:
            # ConPTY has no timeout of its own: the read blocks until either
            # bytes or the end of the terminal, and anything it raises is
            # the end of it.
            try:
                return pty.read_chunk()
            except Exception:
                return None

        return pty, _drain_to_queue(read_one, stream_ended)

    import pexpect

    child = pexpect.spawn(
        args[0],
        args[1:],
        cwd=cwd,
        encoding="utf-8",
        # Without this the terminal inherits whatever PATH the window was
        # started with. Launched from the macOS Dock that is launchd's, which
        # holds no Node, and FreeBuff's `#!/usr/bin/env node` shim dies before
        # it can paint anything.
        env=subprocess_env(args[0]),
        dimensions=(60, 180),
        timeout=0.25,
    )

    def read_one_posix() -> Optional[str]:
        # pexpect reads with a timeout of its own, so a quiet terminal is a
        # TIMEOUT rather than the end of one.
        try:
            return cast(str, child.read_nonblocking(4096, timeout=0.25))
        except pexpect.TIMEOUT:
            return ""
        except (pexpect.EOF, OSError):
            return None

    return child, _drain_to_queue(read_one_posix, stream_ended)


class _HiddenConsoleProcess:
    """The handle for a command given a console Windows created hidden.

    The wizard asks only whether the command is still running and tells it to
    stop, which are the same two questions it asks of a pseudo-terminal, so the
    two are interchangeable from there.
    """

    def __init__(self, proc: "subprocess.Popen[bytes]") -> None:
        self._proc = proc

    def isalive(self) -> bool:
        return self._proc.poll() is None

    def terminate(self, force: bool = False) -> None:
        process_utils.end_process_group(self._proc)

    def close(self, force: bool = False) -> None:
        try:
            self._proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _start_owned(*args, **kwargs):
    """Start a hidden console as an owned process.

    A seam of its own so the ownership rule can be tested without patching a
    name this module imported by value, which proves nothing about the call.
    """
    return owned_popen(*args, **kwargs)


def _spawn_hidden_console(args: list[str], cwd: str) -> object:
    """Start a command with a console of its own that is never shown.

    ``CREATE_NEW_CONSOLE`` gives it the real terminal that commands like these
    need, and the startup show flag has Windows create that console hidden
    rather than hiding it afterwards. The alternative, ``AllocConsole``, hands
    back a console that has already appeared and hiding it is the next thing
    that happens: a frame of a console window, titled with our own executable,
    in front of the user. A sign-in creates no window rather than hiding one.
    """
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    # Owned, so terminating it can end the tree rather than the wrapper: a
    # .cmd launcher's node child otherwise outlives it and keeps the sign-in
    # state nobody can see or release.
    proc = _start_owned(
        args,
        cwd=cwd,
        env=subprocess_env(args[0]),
        creationflags=subprocess.CREATE_NEW_CONSOLE,
        startupinfo=startupinfo,
        close_fds=True,
    )
    # Belt and braces. Nothing was shown a moment ago, and nothing should be
    # shown while this runs either: a console window is the one thing this must
    # never put in front of the user. The same watcher FreeBuff runs hides
    # anything Windows raises anyway.
    roots = {os.getpid(), proc.pid}

    def watch() -> None:
        started = time.monotonic()
        while proc.poll() is None:
            try:
                process_utils.hide_console_windows(roots)
            except Exception:
                # Never let this thread die: it is the only thing keeping the
                # console off screen for the whole run.
                pass
            time.sleep(0.025 if time.monotonic() - started < 8 else 0.25)

    threading.Thread(target=watch, daemon=True).start()
    return _HiddenConsoleProcess(proc)


def spawn_hidden_terminal(args: list[str], cwd: str) -> object:
    """Run a command in a terminal nobody can see, and hand back its handle.

    A backend whose sign-in mounts a terminal UI needs a real terminal even
    when the user never looks at it: Command Code's login renders an Ink
    spinner that refuses to start without one. The alternative to a hidden
    terminal is a console window that appears, can take focus and screen-reader
    focus with it, and exists only to satisfy the UI.

    Windows gets a console created hidden (see :func:`_spawn_hidden_console`).
    Everywhere else there is no window to avoid, so it is the off-screen
    pseudo-terminal FreeBuff runs in, whose output is drained and thrown away:
    a terminal buffer nobody reads fills up and stops the command writing into
    it. `end_hidden_terminal` stops either.
    """
    if platform.system() == "Windows":
        return _spawn_hidden_console(args, cwd)
    ended = threading.Event()
    pty, read = _spawn_freebuff_pty(args, cwd, ended)

    def drain() -> None:
        while not ended.is_set():
            read(0.25)

    threading.Thread(target=drain, daemon=True).start()
    return pty


# A FreeBuff terminal takes seconds to reach its composer, and that wait is the
# largest part of how long a message takes to start answering. One is therefore
# started ahead of time, for the conversation the next message will most likely
# continue, and handed to the run that claims it.
_FREEBUFF_PREWARM_LOCK = threading.Lock()
_freebuff_prewarm: Optional[dict] = None
_freebuff_prewarm_generation = 0
# How long an unclaimed terminal is kept before a timer closes it. Long enough
# to cover thinking time between messages, short enough that a terminal started
# for a message that never comes does not sit there all day.
_FREEBUFF_PREWARM_TTL = 15 * 60


def end_hidden_terminal(pty: object) -> None:
    """End the terminal and give the pseudo-terminal itself back.

    Both calls are wanted, not the first one that works: `terminate` stops
    FreeBuff, `close` releases the handle the terminal was reached through.
    Returning after a successful terminate left that handle open every time.
    """
    for method, arguments in (("terminate", (True,)), ("close", (True,))):
        call = getattr(pty, method, None)
        if call is None:
            continue
        try:
            call(*arguments)
        except Exception:
            continue


def _end_prewarm(holding: dict) -> None:
    """Close a waiting terminal that will not be claimed. Not under the lock."""
    holding["ended"].set()
    timer = holding.get("timer")
    if timer is not None:
        timer.cancel()
    end_hidden_terminal(holding["pty"])


def discard_freebuff_prewarm() -> None:
    """Throw away any terminal held for the next message."""
    global _freebuff_prewarm, _freebuff_prewarm_generation
    with _FREEBUFF_PREWARM_LOCK:
        _freebuff_prewarm_generation += 1
        holding, _freebuff_prewarm = _freebuff_prewarm, None
    if holding is not None:
        _end_prewarm(holding)


def prewarm_freebuff(cwd: str, session_id: Optional[str], model: str, delay: float = 0.0) -> None:
    """Start the terminal the next FreeBuff message will use.

    Doing nothing here is always safe: a message that finds no terminal waiting,
    or one started for a different conversation, simply starts its own.
    """
    global _freebuff_prewarm_generation
    # Resolved the same way the turn resolves it, or a fresh install with no
    # recorded choice prewarms under "" and the turn, looking for the
    # preferred model, kills the waiting terminal every time.
    key = (
        os.path.abspath(cwd),
        session_id or "",
        model or _read_freebuff_choice() or FREEBUFF_PREFERRED_MODEL,
    )
    with _FREEBUFF_PREWARM_LOCK:
        holding = _freebuff_prewarm
        if holding is not None and holding["key"] == key and not holding["ended"].is_set():
            # One is already waiting for exactly this. Starting another would
            # throw away a terminal that has finished starting for one that has
            # not, which is the opposite of the point.
            return
        _freebuff_prewarm_generation += 1
        generation = _freebuff_prewarm_generation

    def work() -> None:
        if delay:
            time.sleep(delay)
        binary = find_backend_cli(BACKEND_FREEBUFF)
        if not binary:
            return
        # Claiming a terminal must wait for an in-progress spawn to publish
        # it. Otherwise Send launches a second CLI and chat discovery can
        # follow the idle background chat forever. Delayed starts must also
        # be invalidated when Send or a newer warmup has already taken over.
        with _FREEBUFF_PREWARM_LOCK:
            if generation != _freebuff_prewarm_generation:
                return
            launch(binary)

    def launch(binary: str) -> None:
        chat = _freebuff_resume_chat_path(cwd, session_id) if session_id else None
        if session_id and chat is None:
            return
        # FreeBuff reads the selected model once, at launch, and rewrites the
        # setting to its own recommendation after a turn. Applying the choice
        # before the terminal starts is what makes a waiting one usable.
        try:
            set_freebuff_model(key[2])
        except OSError:
            return
        args = [binary, "--cwd", cwd, "--trust-agents"]
        if session_id:
            args.extend(["--continue", session_id])
        # A new conversation's chat is created when the terminal starts, not
        # when it is given a message, so the record of which chats existed
        # beforehand has to be taken here. Taken later it would already include
        # this one, and the message would finish without ever learning the id of
        # the conversation it had just had.
        before = _freebuff_chat_dirs(cwd)
        log_offset = 0
        if chat is not None:
            try:
                log_offset = (chat / "log.jsonl").stat().st_size
            except OSError:
                pass
        ended = threading.Event()
        try:
            pty, read = _spawn_freebuff_pty(args, cwd, ended)
        except Exception:
            return
        holding: dict = {
            "key": key,
            "pty": pty,
            "read": read,
            "ended": ended,
            "before": before,
            "log_offset": log_offset,
            "expires": time.monotonic() + _FREEBUFF_PREWARM_TTL,
        }

        def expire() -> None:
            global _freebuff_prewarm
            with _FREEBUFF_PREWARM_LOCK:
                if _freebuff_prewarm is not holding:
                    # Claimed or replaced meanwhile; it is somebody else's now.
                    return
                _freebuff_prewarm = None
            _end_prewarm(holding)

        timer = threading.Timer(_FREEBUFF_PREWARM_TTL, expire)
        timer.daemon = True
        holding["timer"] = timer
        global _freebuff_prewarm
        stale, _freebuff_prewarm = _freebuff_prewarm, holding
        timer.start()
        if stale is not None:
            _end_prewarm(stale)

    threading.Thread(target=work, daemon=True).start()


def _take_freebuff_prewarm(cwd: str, session_id: Optional[str], model: str) -> Optional[dict]:
    """Claim the waiting terminal, if it is the one this message needs."""
    key = (os.path.abspath(cwd), session_id or "", model)
    global _freebuff_prewarm, _freebuff_prewarm_generation
    with _FREEBUFF_PREWARM_LOCK:
        _freebuff_prewarm_generation += 1
        holding = _freebuff_prewarm
        if holding is None:
            return None
        expired = time.monotonic() > holding["expires"]
        _freebuff_prewarm = None
        if holding["key"] == key and not expired and not holding["ended"].is_set():
            timer = holding.get("timer")
            if timer is not None:
                timer.cancel()
            return holding
    # Whatever is waiting cannot serve this message. Drop it rather than leave
    # a terminal running for a conversation nobody resumed.
    _end_prewarm(holding)
    return None


atexit.register(discard_freebuff_prewarm)
