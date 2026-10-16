"""Direct prompt delivery to an integration-managed OpenCode serve.

OpenCode v2 serves ``POST /api/session/<session_id>/prompt`` behind HTTP
Basic auth (``opencode:<password>``).  The adapter injects the rendered Agent
Link prompt through that native endpoint instead of appending a JSONL bridge
file for a plugin watcher.  The prompt ``id`` is the Agent Link ``message_id``,
so a same-id replay is idempotent upstream (first admission wins); the adapter
never invents a new id for an existing message.  ``delivery`` is ``"queue"``
and ``resume`` is deliberately not sent, so scheduling follows the framework
default -- busy delivery, idle wake and restart recovery remain **unknown**
(``[推断]`` in the migration report), never claimed as verified.

Descriptor contract (frozen; see ``docs/HOST-PROTOCOL.md`` §6.3):

- session descriptor ``$AGENT_LINK_SESSIONS/opencode/<session_id>.json``:
  identifies one target session on one integration-managed serve and
  references the server descriptor by ``serve_id``.  It never contains the
  password (C2).
- server descriptor ``$AGENT_LINK_SESSIONS/opencode/servers/<serve_id>.json``:
  holds the single copy of the integration-generated password.

Both files must be 0600 and carry ``managed: true``; the attach path is
unsupported and must not write a password descriptor, so descriptors without
that mark are rejected.  Every delivery re-reads both descriptors: password
rotation, a replaced serve or a dead pid is observed immediately and nothing
is cached across runs.  No audit detail, log line or exception text ever
contains the password or the Authorization header (C3).
"""
from __future__ import annotations

import base64
import http.client
import json
import os
import stat
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from ...errors import DeliveryFailure, ValidationError
from ...shared import build_child_prompt, build_parent_prompt
from ...storage import validate_identifier
from ..push import session_dir

#: Descriptor protocol for the integration-managed OpenCode prompt transport.
PROTOCOL = "agent-link-opencode-push/1"
#: Fixed route; only a descriptor-validated session id is interpolated.
PROMPT_ROUTE = "/api/session/{session_id}/prompt"
#: Upstream delivery mode (OpenCode queue semantics; steer is the framework default).
DELIVERY = "queue"
#: Request/response body cap, matching the session push transport.
MAX_BODY_BYTES = 65536
#: Client timeout for one prompt POST.
TIMEOUT_SECONDS = 10
#: Integration-generated passwords are long random strings; reject stubs early.
MIN_PASSWORD_LENGTH = 16
#: Reserved credential-kind values; unknown kinds fail closed (C6).
SESSION_CREDENTIAL_KIND = "server_descriptor"
SERVER_CREDENTIAL_KIND = "inline_password"
SERVERS_DIR_NAME = "servers"


def session_descriptor_path(framework: str, session_id: str) -> Path:
    return session_dir() / framework / f"{session_id}.json"


def server_descriptor_path(framework: str, serve_id: str) -> Path:
    return session_dir() / framework / SERVERS_DIR_NAME / f"{serve_id}.json"


def write_session_descriptor(descriptor: dict, *, sessions=None) -> Path:
    """Create the session descriptor 0600 with ``O_EXCL`` (never replace).

    Raises ``FileExistsError`` when a descriptor already exists so callers can
    fail explicitly instead of mixing two generations of descriptors.
    """
    path = (Path(sessions) if sessions is not None else session_dir()) / descriptor["framework"] / (
        f"{descriptor['session_id']}.json"
    )
    return _write_private_exclusive(path, descriptor)


def write_server_descriptor(descriptor: dict, *, sessions=None) -> Path:
    """Create the serve descriptor 0600 with ``O_EXCL`` (never replace)."""
    path = (
        (Path(sessions) if sessions is not None else session_dir())
        / descriptor["framework"]
        / SERVERS_DIR_NAME
        / f"{descriptor['serve_id']}.json"
    )
    return _write_private_exclusive(path, descriptor)


def _write_private_exclusive(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return path


def remove_descriptor_if_matches(path: Path, *, serve_id: str, pid: int) -> bool:
    """Remove a descriptor only when it still names this serve (E1 cleanup)."""
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(current, dict):
        return False
    if current.get("serve_id") != serve_id or current.get("pid") != pid:
        return False
    try:
        path.unlink()
    except OSError:
        return False
    return True


def read_session_descriptor(framework: str, session_id: str, *, sessions=None) -> dict:
    """Raw session descriptor for runner/tooling; raises when missing or invalid."""
    validate_identifier(framework, "framework")
    validate_identifier(session_id, "session_id")
    base = Path(sessions) if sessions is not None else session_dir()
    path = base / framework / f"{session_id}.json"
    _require_private_file(path, "session descriptor")
    return _load_json_object(path, "session descriptor")


def read_server_descriptor(framework: str, serve_id: str, *, sessions=None) -> dict:
    """Raw serve descriptor for runner/tooling; raises when missing or invalid."""
    validate_identifier(framework, "framework")
    validate_identifier(serve_id, "serve_id")
    base = Path(sessions) if sessions is not None else session_dir()
    path = base / framework / SERVERS_DIR_NAME / f"{serve_id}.json"
    _require_private_file(path, "serve descriptor")
    return _load_json_object(path, "serve descriptor")


def _require_private_file(path: Path, label: str) -> None:
    """The credential contract requires 0600; fail closed on group/other bits."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        raise DeliveryFailure(f"opencode {label} missing: {path}; no file fallback") from None
    except OSError as error:
        raise DeliveryFailure(f"opencode {label} unreadable: {error}") from None
    if mode & 0o077:
        raise DeliveryFailure(
            f"opencode {label} is not private (mode {mode:04o}, require 0600): {path}"
        )


def _load_json_object(path: Path, label: str) -> dict:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise DeliveryFailure(f"opencode {label} missing: {path}; no file fallback") from None
    except OSError as error:
        raise DeliveryFailure(f"opencode {label} unreadable: {error}") from None
    try:
        data = json.loads(raw)
    except ValueError:
        raise DeliveryFailure(f"opencode {label} is not valid JSON: {path}") from None
    if not isinstance(data, dict):
        raise DeliveryFailure(f"opencode {label} must be a JSON object: {path}")
    return data


def _loopback_origin(value, label: str) -> str:
    """Only a plain loopback HTTP origin is accepted (style of hosts/push.py)."""
    if not isinstance(value, str):
        raise DeliveryFailure(f"opencode {label} endpoint must be a string")
    url = urlsplit(value)
    if (
        url.scheme != "http"
        or url.hostname != "127.0.0.1"
        or not url.port
        or url.username
        or url.password
        or url.path not in ("", "/")
        or url.query
        or url.fragment
    ):
        raise DeliveryFailure(
            f"opencode {label} endpoint must be a loopback HTTP origin (http://127.0.0.1:PORT)"
        )
    return value.rstrip("/")


def _valid_pid(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness check; PermissionError still means 'exists'."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OverflowError:
        return False
    except PermissionError:
        return True
    except OSError:
        # Unknown failure (unsupported platform); leave the decision to HTTP.
        return True
    return True


def _load_password(framework: str, descriptor: dict, path: Path, endpoint: str) -> str:
    """Resolve the single password copy through the referenced server descriptor."""
    if "password" in descriptor:
        raise DeliveryFailure(
            f"opencode session descriptor must not carry a password (C2: single copy in the "
            f"server descriptor): {path}"
        )
    credential_kind = descriptor.get("credential_kind")
    if credential_kind != SESSION_CREDENTIAL_KIND:
        raise DeliveryFailure(
            f"opencode session descriptor credential_kind must be {SESSION_CREDENTIAL_KIND!r}, "
            f"got {credential_kind!r}: {path}"
        )
    serve_id = descriptor.get("serve_id")
    try:
        validate_identifier(serve_id, "serve_id")
    except ValidationError:
        raise DeliveryFailure(
            f"opencode session descriptor has an invalid serve_id: {path}"
        ) from None

    server_path = server_descriptor_path(framework, serve_id)
    _require_private_file(server_path, "serve descriptor")
    server = _load_json_object(server_path, "serve descriptor")
    if (
        server.get("protocol") != PROTOCOL
        or server.get("framework") != framework
        or server.get("serve_id") != serve_id
    ):
        raise DeliveryFailure(f"opencode serve descriptor identity mismatch: {server_path}")
    if server.get("managed") is not True:
        raise DeliveryFailure(
            f"opencode serve descriptor is unmanaged (managed must be true): {server_path}"
        )
    if server.get("credential_kind") != SERVER_CREDENTIAL_KIND:
        raise DeliveryFailure(
            f"opencode serve descriptor credential_kind must be {SERVER_CREDENTIAL_KIND!r}, "
            f"got {server.get('credential_kind')!r}: {server_path}"
        )
    if _loopback_origin(server.get("endpoint"), "serve descriptor") != endpoint:
        raise DeliveryFailure(
            f"opencode serve descriptor endpoint does not match the session descriptor: {server_path}"
        )
    if server.get("pid") != descriptor.get("pid"):
        raise DeliveryFailure(
            f"opencode serve descriptor pid does not match the session descriptor: {server_path}"
        )
    password = server.get("password")
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
        raise DeliveryFailure(
            f"opencode serve descriptor password is invalid "
            f"(require >= {MIN_PASSWORD_LENGTH} characters): {server_path}"
        )
    return password


def _load_credentials(framework: str, session_id: str) -> tuple[str, str]:
    """Load and validate the session/serve descriptors; return endpoint+password."""
    path = session_descriptor_path(framework, session_id)
    _require_private_file(path, "session descriptor")
    descriptor = _load_json_object(path, "session descriptor")
    if (
        descriptor.get("protocol") != PROTOCOL
        or descriptor.get("framework") != framework
        or descriptor.get("session_id") != session_id
    ):
        raise DeliveryFailure(f"opencode session descriptor identity mismatch: {path}")
    if descriptor.get("managed") is not True:
        raise DeliveryFailure(
            f"opencode session descriptor is unmanaged (managed must be true; the attach path "
            f"is unsupported and must not carry a password): {path}"
        )
    endpoint = _loopback_origin(descriptor.get("endpoint"), "session descriptor")
    pid = descriptor.get("pid")
    if not _valid_pid(pid) or not _pid_alive(pid):
        raise DeliveryFailure(
            f"opencode serve process is not running (session descriptor pid): {path}"
        )
    password = _load_password(framework, descriptor, path, endpoint)
    return endpoint, password


def post_prompt(
    framework: str,
    session_id: str,
    prompt: str,
    *,
    message_id: str,
    timeout: float | None = None,
    resume: bool | None = None,
) -> str:
    """POST raw prompt text through the frozen descriptors.

    Shared by the host adapters (which render a prompt first) and the runner
    ``send`` command.  ``resume`` is normally left ``None`` so the framework
    default applies (the adapters never send it); passing it explicitly is
    for orchestration and no-model real-interface tests.  Returns the audit
    detail string; raises :class:`DeliveryFailure` with the same mapping as
    :func:`deliver`.
    """
    validate_identifier(framework, "framework")
    validate_identifier(session_id, "session_id")
    validate_identifier(message_id, "message_id")
    if not isinstance(prompt, str) or not prompt.strip():
        raise DeliveryFailure("opencode prompt must be a non-empty string")
    endpoint, password = _load_credentials(framework, session_id)
    return _post_prompt(
        endpoint,
        session_id,
        password,
        prompt,
        message_id,
        TIMEOUT_SECONDS if timeout is None else timeout,
        resume=resume,
    )


def _post_prompt(
    endpoint: str,
    session_id: str,
    password: str,
    prompt: str,
    message_id: str,
    timeout: float,
    *,
    resume: bool | None = None,
) -> str:
    prompt_id = f"msg_{message_id}"
    body_payload = {"id": prompt_id, "text": prompt, "delivery": DELIVERY}
    if resume is not None:
        body_payload["resume"] = resume
    body = json.dumps(body_payload, ensure_ascii=False).encode("utf-8")
    if len(body) > MAX_BODY_BYTES:
        raise DeliveryFailure(f"opencode prompt exceeds {MAX_BODY_BYTES} bytes")

    authorization = "Basic " + base64.b64encode(
        b"opencode:" + password.encode("utf-8")
    ).decode("ascii")
    request = urllib.request.Request(
        endpoint + PROMPT_ROUTE.format(session_id=session_id),
        data=body,
        headers={"Authorization": authorization, "Content-Type": "application/json"},
        method="POST",
    )

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    # No proxy environment, no redirect following: only the descriptor origin.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            status = response.status
            data = response.read(MAX_BODY_BYTES + 1)
    except urllib.error.HTTPError as error:
        code = error.code
        error.close()
        if code == 401:
            raise DeliveryFailure(
                "opencode push rejected credentials (HTTP 401): configuration error; "
                "no retry; check the serve descriptor password"
            ) from None
        if code == 404:
            raise DeliveryFailure(
                "opencode push target session not found (HTTP 404): failed_or_unknown; "
                "no automatic resend"
            ) from None
        raise DeliveryFailure(
            f"opencode push failed (HTTP {code}): failed_or_unknown; no automatic resend"
        ) from None
    except (OSError, ValueError, http.client.HTTPException) as error:
        # Deliberately do not echo str(error): request headers are never logged.
        raise DeliveryFailure(
            f"opencode push timed out or is unavailable ({type(error).__name__}); "
            "outcome unknown, no file fallback"
        ) from error

    if not 200 <= status < 300:
        raise DeliveryFailure(f"opencode push returned unexpected status {status}")
    if len(data) > MAX_BODY_BYTES:
        raise DeliveryFailure("opencode push response too large")
    try:
        result = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise DeliveryFailure(
            "opencode push response is not valid JSON; failed_or_unknown"
        ) from None
    # OpenCode v2 wraps responses in {"data": ...}; accept the unwrapped form
    # too so older fixtures stay readable.
    if isinstance(result, dict) and isinstance(result.get("data"), dict):
        result = result["data"]
    if not isinstance(result, dict) or result.get("sessionID") != session_id:
        raise DeliveryFailure(
            "opencode push was not acknowledged for the bound session; "
            "not recorded as host_accepted"
        )
    if "id" in result and result.get("id") != prompt_id:
        raise DeliveryFailure("opencode push acknowledged a different prompt id")
    return f"push:opencode/{session_id}#{message_id}"


def deliver(root, binding, message, *, role: str) -> str:
    """POST one rendered prompt to the bound OpenCode session.

    Returns the audit detail string (never contains the password); raises
    :class:`DeliveryFailure` on any missing/mismatched descriptor, transport
    failure, timeout, non-2xx response or unacknowledged session, which the
    core records as ``failed_or_unknown`` with no automatic resend.
    """
    framework = binding[f"{role}_framework"]
    session_id = binding[f"{role}_session_id"]
    renderer = build_parent_prompt if role == "parent" else build_child_prompt
    prompt = renderer(root, binding, message)
    return post_prompt(
        framework,
        session_id,
        prompt,
        message_id=message["message_id"],
    )
