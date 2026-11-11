"""OpencodeWorker, OpencodeServer, and all opencode-specific helpers.

opencode is driven through its own headless HTTP server rather than one
process per turn. Extracted from agent_backends.py so the opencode surface
has a focused home; agent_backends re-exports every public symbol for zero
caller churn, and keeps the provider-neutral discovery helpers that name
opencode (``_opencode_providers``, ``_opencode_account_lines``) beside the
other backends' status code.

Copyright (c) 2026 doubletaponair and BlindPilot contributors.
Based on the original Claude Code Reader application by doubletaponair:
https://github.com/doubletaponair/claude-code-reader
SPDX-License-Identifier: MIT
"""

from __future__ import annotations

import atexit
import json
import platform
import re
import subprocess
import threading
from pathlib import Path
from typing import Optional

import process_utils
import questions
from owned_process import owned_popen

# Shared symbols that stay in agent_backends (resolved after that module loads).
from agent_backends import (
    BACKEND_OPENCODE,
    SUBAGENT_RUNNING,
    _TurnWorker,
    find_backend_cli,
    invalidate_backend_cache,
    subagent_line,
    subagent_status,
    subprocess_env,
)


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


def invalidate_opencode_catalog() -> None:
    """Drop every directory's cached catalog. Called by the central
    ``agent_backends.invalidate_backend_cache`` dispatcher, which owns this
    module's one job of knowing when a runtime refresh has happened."""
    with _OPENCODE_CATALOG_LOCK:
        _opencode_catalog_cache.clear()


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
