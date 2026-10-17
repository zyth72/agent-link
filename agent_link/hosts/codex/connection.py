"""Per-session Codex desktop transport connection descriptors.

The desktop app gives long-lived parent processes an inherited
``CODEX_APP_TOOLS_PIPE_PATH``.  After the app restarts, a host service that was
started by the old app instance still has the old value in its environment.
This module records the *current* desktop transport for one parent session in a
small private file so notification dispatch can read the freshest value on
every call instead of mutating process-global ``os.environ``.

Only two transport variables are ever persisted:
``CODEX_APP_TOOLS_PIPE_PATH`` and ``CODEX_APP_TOOLS_CALLER_HOST_ID``.  Full
environments, credentials, model keys and message contents are never stored in
the descriptor.  A descriptor proves that a trusted current Codex environment
registered a session; it does **not** prove the pipe is alive, that a host
service is running, or that a model processed a notification.

Directory: ``~/.agent-link/codex-connections`` by default, overridden with
``AGENT_LINK_CODEX_CONNECTIONS`` (tests always set it).  Directories are 0700
and files 0600.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import tempfile

from ...errors import ValidationError
from ...storage import utc_now_iso, validate_identifier

PROTOCOL = "agent-link-codex-connection/1"
FRAMEWORK = "codex-desktop"
CONNECTIONS_ENV = "AGENT_LINK_CODEX_CONNECTIONS"
SESSION_ENV_VARS = ("CODEX_THREAD_ID", "CODEX_SESSION_ID")
TRANSPORT_ENV_ALLOWLIST = (
    "CODEX_APP_TOOLS_PIPE_PATH",
    "CODEX_APP_TOOLS_CALLER_HOST_ID",
)
PIPE_ENV = "CODEX_APP_TOOLS_PIPE_PATH"


class ConnectionDescriptorError(ValidationError):
    """A descriptor exists but is corrupt, unsafe or for another session."""

    code = "connection_descriptor_invalid"


def connections_dir() -> Path:
    override = os.environ.get(CONNECTIONS_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".agent-link" / "codex-connections"


def _descriptor_path(session_id: str, directory: Path | None = None) -> Path:
    session_id = validate_identifier(session_id, "session_id")
    directory = Path(directory) if directory is not None else connections_dir()
    return directory / f"{session_id}.json"


def _secure_directory(directory: Path) -> Path:
    directory = Path(directory).expanduser()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not directory.is_dir():
        raise ValidationError(f"connection directory is not a directory: {directory}")
    try:
        os.chmod(directory, 0o700)
    except OSError as error:
        raise ValidationError(f"cannot secure connection directory {directory}: {error}")
    return directory


def env_session_id(env=None) -> str | None:
    """Return the current trusted Codex session id from the environment."""
    env = os.environ if env is None else env
    for name in SESSION_ENV_VARS:
        value = env.get(name)
        if value:
            return validate_identifier(value, "session_id")
    return None


def resolve_session_id(explicit=None, env=None) -> str:
    """Prefer the current Codex environment and reject an explicit mismatch.

    The environment variables are injected by the trusted Codex desktop
    process.  When present they win over an explicit value so a stale chat id
    cannot register another conversation by accident.  An explicit id is
    allowed only when the environment does not identify a session (for example
    tool metadata supplied it, or a hook passed it through stdin).
    """
    env = os.environ if env is None else env
    from_env = env_session_id(env)
    if explicit is not None:
        explicit = validate_identifier(explicit, "session_id")
        if from_env is not None and explicit != from_env:
            raise ValidationError(
                "explicit session_id does not match the current Codex session environment;"
                " refusing to register another chat"
            )
    if from_env is not None:
        return from_env
    if explicit is not None:
        return explicit
    raise ValidationError(
        "a session id is required: pass --session-id or run inside the Codex chat where "
        + "/".join(SESSION_ENV_VARS)
        + " is set"
    )


def transport_env_from_environment(env=None) -> dict:
    """Extract only the allowlisted transport variables from *env*."""
    env = os.environ if env is None else env
    transport = {}
    for key in TRANSPORT_ENV_ALLOWLIST:
        value = env.get(key)
        if value is None or value == "":
            continue
        if not isinstance(value, str) or "\x00" in value:
            raise ValidationError(f"{key} must be a non-empty string")
        transport[key] = value
    return transport


def _validate_descriptor(data, session_id: str) -> dict:
    if not isinstance(data, dict):
        raise ConnectionDescriptorError("connection descriptor must be a JSON object")
    expected = {
        "protocol",
        "framework",
        "session_id",
        "connection_generation",
        "updated_at",
        "transport_env",
    }
    if set(data) != expected:
        raise ConnectionDescriptorError(
            "connection descriptor fields must be exactly " + ", ".join(sorted(expected))
        )
    if data.get("protocol") != PROTOCOL:
        raise ConnectionDescriptorError(f"unsupported connection protocol: {data.get('protocol')!r}")
    if data.get("framework") != FRAMEWORK:
        raise ConnectionDescriptorError(f"connection framework is not {FRAMEWORK!r}")
    if data.get("session_id") != session_id:
        raise ConnectionDescriptorError(
            "connection descriptor session does not match its file name/current session"
        )
    generation = data.get("connection_generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise ConnectionDescriptorError("connection_generation must be a positive integer")
    if not isinstance(data.get("updated_at"), str) or not data["updated_at"]:
        raise ConnectionDescriptorError("connection updated_at must be a non-empty string")
    transport = data.get("transport_env")
    if not isinstance(transport, dict):
        raise ConnectionDescriptorError("connection transport_env must be an object")
    if set(transport) - set(TRANSPORT_ENV_ALLOWLIST):
        raise ConnectionDescriptorError(
            "connection descriptor contains non-allowlisted environment variables"
        )
    if not isinstance(transport.get(PIPE_ENV), str) or not transport[PIPE_ENV]:
        raise ConnectionDescriptorError(f"connection descriptor is missing {PIPE_ENV}")
    for key, value in transport.items():
        if not isinstance(value, str) or not value or "\x00" in value:
            raise ConnectionDescriptorError(f"connection transport value for {key} is invalid")
    return dict(data)


def _read_file(path: Path, session_id: str) -> dict | None:
    try:
        stat = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ConnectionDescriptorError(f"cannot inspect connection descriptor {path}: {error}")
    if not path.is_file() or path.is_symlink():
        raise ConnectionDescriptorError(f"connection descriptor is not a regular file: {path}")
    if stat.st_mode & 0o077:
        raise ConnectionDescriptorError(
            f"connection descriptor has group/other permissions: {path}"
        )
    try:
        raw = path.read_text("utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ConnectionDescriptorError(f"cannot read connection descriptor {path}: {error}")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ConnectionDescriptorError(f"connection descriptor is not valid JSON: {error}")
    return _validate_descriptor(data, session_id)


def read_descriptor(session_id: str, directory: Path | None = None) -> dict | None:
    """Read and validate the freshest descriptor for *session_id*.

    A missing file returns ``None`` (the caller may use the legacy inherited
    environment).  Any existing but corrupt, unsafe or mismatched file raises
    instead of silently falling back to a stale pipe.
    """
    session_id = validate_identifier(session_id, "session_id")
    directory = Path(directory) if directory is not None else connections_dir()
    return _read_file(directory / f"{session_id}.json", session_id)


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextmanager
def _registration_lock(directory: Path):
    """Serialize same-user registrations so generation/idempotency stay exact."""
    lock_path = directory / ".registration.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _public(descriptor: dict, *, idempotent: bool, registered: bool) -> dict:
    transport = descriptor["transport_env"]
    return {
        "registered": registered,
        "idempotent": idempotent,
        "protocol": descriptor["protocol"],
        "session_id": descriptor["session_id"],
        "connection_generation": descriptor["connection_generation"],
        "updated_at": descriptor["updated_at"],
        "transport_env_keys": sorted(transport),
        "pipe_present": PIPE_ENV in transport,
        "transport_liveness": "unknown",
    }


def register_connection(
    explicit_session_id=None,
    *,
    env=None,
    directory: Path | None = None,
) -> dict:
    """Register the current transport for one parent session, atomically.

    A missing pipe raises ``ValidationError`` for the explicit CLI command;
    automatic startup paths use :func:`maybe_register_connection` so they can
    skip without touching an existing descriptor.
    """
    env = os.environ if env is None else env
    session_id = resolve_session_id(explicit_session_id, env)
    transport = transport_env_from_environment(env)
    if PIPE_ENV not in transport:
        raise ValidationError(
            f"{PIPE_ENV} is absent in the current Codex environment; nothing was registered"
        )
    directory = _secure_directory(directory or connections_dir())
    target = _descriptor_path(session_id, directory)
    with _registration_lock(directory):
        existing = None
        try:
            existing = _read_file(target, session_id)
        except ConnectionDescriptorError:
            # A trusted current environment may heal a corrupt local file.
            existing = None
        if existing is not None and existing["transport_env"] == transport:
            return _public(existing, idempotent=True, registered=False)
        generation = 1 if existing is None else existing["connection_generation"] + 1
        descriptor = {
            "protocol": PROTOCOL,
            "framework": FRAMEWORK,
            "session_id": session_id,
            "connection_generation": generation,
            "updated_at": utc_now_iso(),
            "transport_env": transport,
        }
        _atomic_write(target, descriptor)
        return _public(descriptor, idempotent=False, registered=True)


def maybe_register_connection(
    explicit_session_id=None,
    *,
    env=None,
    directory: Path | None = None,
) -> dict:
    """Best-effort auto-registration used by host/helper startup and hooks.

    No pipe means no write: an older descriptor is never erased just because a
    process lacks the current desktop environment.  A session mismatch still
    raises so callers cannot accidentally register another chat.
    """
    env = os.environ if env is None else env
    session_id = resolve_session_id(explicit_session_id, env)
    if not env.get(PIPE_ENV):
        return {
            "registered": False,
            "idempotent": False,
            "session_id": session_id,
            "reason": "missing_pipe_env",
            "transport_liveness": "unknown",
        }
    return register_connection(session_id, env=env, directory=directory)


def connection_status(session_id, *, directory: Path | None = None) -> dict:
    """Return safe local descriptor metadata; never a liveness claim."""
    session_id = validate_identifier(session_id, "session_id")
    directory = Path(directory) if directory is not None else connections_dir()
    descriptor = read_descriptor(session_id, directory)
    if descriptor is None:
        return {
            "present": False,
            "session_id": session_id,
            "descriptor_path": str(directory / f"{session_id}.json"),
            "transport_liveness": "unknown",
        }
    result = _public(descriptor, idempotent=False, registered=False)
    result.update(
        {
            "present": True,
            "descriptor_path": str(directory / f"{session_id}.json"),
            "transport_liveness": "unknown",
            "probe": "none",
        }
    )
    return result
