"""OpenCode self-managed serve runner (integration side, slice 3a).

Starts one dedicated ``opencode serve`` for a binding and keeps it alive with
the CLI shape of the Pi RPC controller (``serve``/``status``/``send``/
``stop``):

- integration-generated ``OPENCODE_SERVER_PASSWORD`` (one per serve, never the
  user's ``service.json`` credential);
- an isolated session database under the runtime root, stable per binding so a
  restart can adopt the same session;
- a generated config directory holding only the Agent Link plugin wrapper
  (``OPENCODE_CONFIG_DIR``); the user's own config is passed read-only through
  ``OPENCODE_CONFIG`` and is never copied or modified;
- a real readiness handshake (``GET /api/plugin`` must list the Agent Link
  plugin as ``active``); a serve without the plugin fails closed (C6);
- the bound session is pre-created with ``POST /api/session`` to remove the
  discovery race;
- the frozen session/serve descriptors are written 0600 with ``O_EXCL`` and
  removed only when they still name this serve (E1).

The service also exists as a Python class so tests can drive it in-process;
the CLI is ``python3 -m agent_link.hosts.opencode.serve``.
"""
from __future__ import annotations

import argparse
import base64
import http.client
import json
import os
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from ...errors import (
    AgentLinkError,
    ConflictError,
    DeliveryFailure,
    NotFoundError,
    ValidationError,
)
from ...storage import DB_FILENAME, validate_identifier
from . import push

#: Default executable; override with ``--opencode-bin``.
DEFAULT_OPENCODE_BIN = os.environ.get("OPENCODE_BIN") or "opencode"
#: The Agent Link plugin id expected in the /api/plugin readiness handshake.
PLUGIN_ID = "agent-link"
#: Repository extension loaded through the generated plugin wrapper.
DEFAULT_EXTENSION_PATH = (
    Path(__file__).resolve().parents[3] / "extensions" / "opencode" / "agent-link.ts"
)
DEFAULT_READY_TIMEOUT = 60.0
DEFAULT_STOP_GRACE = 10.0
API_TIMEOUT = 10.0
#: Cap on responses read from the local serve (never larger than push caps).
MAX_RESPONSE_BYTES = 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D102 - refuse redirects
        return None


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _basic_authorization(password: str) -> str:
    return "Basic " + base64.b64encode(
        b"opencode:" + password.encode("utf-8")
    ).decode("ascii")


def _redact(text: str, password: str | None) -> str:
    if not text:
        return ""
    if password:
        text = text.replace(password, "<redacted>")
    return text


def _read_binding(root, binding_id: str) -> dict:
    """Read one binding through a read-only connection (never create a DB)."""
    path = Path(root).expanduser().resolve() / DB_FILENAME
    if not path.is_file():
        raise NotFoundError(f"binding database does not exist: {path}")
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0)
    try:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM bindings WHERE binding_id = ?", (binding_id,)
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        raise NotFoundError(f"database {path} has no binding {binding_id!r}")
    return dict(row)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _parse_model(value):
    """Turn ``PROVIDER/MODEL`` into an OpenCode model ref or ``None``."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("--model must be PROVIDER/MODEL")
    provider, separator, model = value.partition("/")
    if not separator or not provider.strip() or not model.strip() or any(
        char.isspace() for char in value
    ):
        raise ValidationError("--model must be PROVIDER/MODEL without whitespace")
    return {"providerID": provider.strip(), "id": model.strip()}


def _default_config_path() -> Path | None:
    configured = os.environ.get("OPENCODE_CONFIG")
    if configured:
        path = Path(configured).expanduser()
        return path if path.is_file() else None
    default = Path.home() / ".config" / "opencode" / "opencode.json"
    return default if default.is_file() else None


def _api(
    endpoint: str,
    password: str,
    method: str,
    path: str,
    payload=None,
    timeout: float = API_TIMEOUT,
):
    """One loopback OpenCode API call; returns ``(status|None, parsed|text)``."""
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Authorization": _basic_authorization(password)}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(endpoint + path, data=data, headers=headers, method=method)
    try:
        with _opener().open(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES)
            status = response.status
    except urllib.error.HTTPError as error:
        status = error.code
        try:
            body = error.read(MAX_RESPONSE_BYTES)
        except OSError:
            body = b""
        finally:
            error.close()
    except (OSError, http.client.HTTPException, ValueError) as error:
        return None, type(error).__name__
    try:
        return status, json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return status, body.decode("utf-8", "replace")[:500]


def _unwrap(payload):
    """OpenCode v2 wraps every response in ``data``; unwrap dicts and lists."""
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def _plugin_active(
    endpoint: str, password: str, directory: Path | None = None, timeout: float = API_TIMEOUT
) -> bool:
    path = "/api/plugin"
    if directory is not None:
        path += "?" + urllib.parse.urlencode(
            {"directory": str(directory), "workspaceID": ""}
        )
    status, payload = _api(endpoint, password, "GET", path, timeout=timeout)
    if status != 200 or not isinstance(payload, dict):
        return False
    entries = payload.get("data")
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if isinstance(entry, dict) and entry.get("id") == PLUGIN_ID:
            state = entry.get("state")
            return isinstance(state, dict) and state.get("status") == "active"
    return False


def _session_present(endpoint: str, password: str, session_id: str, timeout: float = API_TIMEOUT) -> bool:
    status, _payload = _api(endpoint, password, "GET", f"/api/session/{session_id}", timeout=timeout)
    return status == 200


def _prepare_config_dir(config_dir: Path, extension: Path) -> Path:
    """Write only the plugin wrapper; the user config stays wherever it is."""
    config_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(config_dir, 0o700)
    plugin_dir = config_dir / "plugin"
    plugin_dir.mkdir(exist_ok=True)
    os.chmod(plugin_dir, 0o700)
    wrapper = plugin_dir / "agent-link.ts"
    wrapper.write_text(
        f'export {{ default }} from {json.dumps(str(extension))};\n', encoding="utf-8"
    )
    os.chmod(wrapper, 0o600)
    return config_dir


class OpenCodeServe:
    """Own one self-managed ``opencode serve``, its descriptors and cleanup."""

    def __init__(
        self,
        root,
        binding_id: str,
        *,
        role: str = "child",
        opencode_bin: str = DEFAULT_OPENCODE_BIN,
        cwd,
        credentials=None,
        extension=None,
        config=None,
        model=None,
        port: int = 0,
        ready_timeout: float = DEFAULT_READY_TIMEOUT,
        stop_grace: float = DEFAULT_STOP_GRACE,
        db=None,
        env=None,
    ):
        self.root = Path(root).expanduser().resolve()
        self.binding_id = validate_identifier(binding_id, "binding_id")
        if role not in ("child", "parent"):
            raise ValidationError(f"role must be child or parent, got {role!r}")
        self.role = role
        self.opencode_bin = opencode_bin
        self.cwd = Path(cwd).expanduser().resolve()
        self.credentials = Path(credentials).expanduser().resolve() if credentials else None
        self.extension = (
            Path(extension).expanduser().resolve() if extension else DEFAULT_EXTENSION_PATH
        )
        self.config = Path(config).expanduser().resolve() if config else None
        self.model_ref = _parse_model(model)
        self.model = model
        self.requested_port = port
        self.ready_timeout = ready_timeout
        self.stop_grace = stop_grace
        self.base_dir = self.root / "opencode-serve" / self.binding_id
        self.db = Path(db).expanduser().resolve() if db else self.base_dir / "opencode.db"
        self.log_path = self.base_dir / "serve.log"
        self.config_dir = self.base_dir / "config"
        self.env = dict(env if env is not None else os.environ)

        self.serve_id = f"ocserve-{uuid.uuid4().hex}"
        self.password = secrets.token_urlsafe(32)
        self.endpoint: str | None = None
        self.serve_pid: int | None = None
        self.session_id: str | None = None
        self.process: subprocess.Popen | None = None
        self.stop_event = threading.Event()
        self._stop_requested = False
        self._stopped = False
        self._child_exit_code: int | None = None
        self._log_stream = None
        self._session_path: Path | None = None
        self._server_path: Path | None = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> dict:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.base_dir, 0o700)
        binding = _read_binding(self.root, self.binding_id)
        framework = binding[f"{self.role}_framework"]
        if framework != "opencode":
            raise ValidationError(
                f"binding {self.binding_id} {self.role} framework is {framework!r}, not opencode"
            )
        self.session_id = validate_identifier(
            binding[f"{self.role}_session_id"], f"{self.role}_session_id"
        )
        if not self.cwd.is_dir():
            raise ValidationError(f"cwd is not a directory: {self.cwd}")
        if not self.extension.is_file():
            raise ValidationError(f"opencode extension does not exist: {self.extension}")
        if self.credentials is not None and not self.credentials.is_file():
            raise ValidationError(
                f"child credentials file does not exist yet: {self.credentials} "
                "(start the parent host service first)"
            )
        if self.config is not None and not self.config.is_file():
            raise ValidationError(f"base config does not exist: {self.config}")
        if not (1 <= self.requested_port <= 65535) and self.requested_port != 0:
            raise ValidationError(f"port must be 0 or 1..65535, got {self.requested_port}")

        session_path = push.session_descriptor_path("opencode", self.session_id)
        if session_path.exists():
            raise ConflictError(
                f"session descriptor already exists: {session_path} "
                "(another serve may be running; stop it or remove the stale descriptor explicitly)"
            )
        session_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(session_path.parent, 0o700)

        port = self.requested_port or _free_port()
        self.endpoint = f"http://127.0.0.1:{port}"
        _prepare_config_dir(self.config_dir, self.extension)

        self.db.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_stream = open(self.log_path, "ab", buffering=0)  # noqa: SIM115 - closed in stop
        try:
            os.chmod(self.log_path, 0o600)
        except OSError:
            pass

        child_env = self._child_env()
        try:
            self.process = subprocess.Popen(
                [
                    self.opencode_bin,
                    "serve",
                    "--hostname",
                    "127.0.0.1",
                    "--port",
                    str(port),
                ],
                cwd=str(self.cwd),
                env=child_env,
                stdout=self._log_stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as error:
            raise DeliveryFailure(f"cannot start {self.opencode_bin!r}: {error}") from None
        self.serve_pid = self.process.pid

        try:
            self._wait_ready()
            self._create_session()
            self._write_descriptors()
        except BaseException:
            self.stop()
            raise

        return {
            "ready": True,
            "binding_id": self.binding_id,
            "role": self.role,
            "session_id": self.session_id,
            "serve_id": self.serve_id,
            "endpoint": self.endpoint,
            "serve_pid": self.serve_pid,
            "model": self.model,
            "plugin": "active",
            "database": str(self.db),
            "descriptor": str(self._server_path),
            "session_descriptor": str(self._session_path),
        }

    def _child_env(self) -> dict:
        env = dict(self.env)
        for name in (
            "OPENCODE_PASSWORD",
            "OPENCODE_SERVER_PASSWORD",
            "OPENCODE_CONFIG",
            "OPENCODE_CONFIG_CONTENT",
            "OPENCODE_CONFIG_DIR",
            "OPENCODE_DB",
            "OPENCODE_DISABLE_PROJECT_CONFIG",
        ):
            env.pop(name, None)
        env["OPENCODE_SERVER_PASSWORD"] = self.password
        env["OPENCODE_CONFIG_DIR"] = str(self.config_dir)
        env["OPENCODE_DB"] = str(self.db)
        env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
        config = self.config if self.config is not None else _default_config_path()
        if config is not None:
            env["OPENCODE_CONFIG"] = str(config)
        if self.credentials is not None:
            env["AGENT_LINK_CREDENTIALS"] = str(self.credentials)
        return env

    def _log_tail(self, limit: int = 2000) -> str:
        try:
            data = self.log_path.read_bytes()
        except OSError:
            return ""
        return _redact(data[-limit:].decode("utf-8", "replace"), self.password)

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + self.ready_timeout
        while True:
            if self.process is not None and self.process.poll() is not None:
                raise DeliveryFailure(
                    f"opencode serve exited before readiness (code {self.process.returncode}); "
                    f"log tail: {self._log_tail()}"
                )
            if _plugin_active(self.endpoint, self.password, self.cwd, timeout=2.0):
                return
            if time.monotonic() >= deadline:
                raise DeliveryFailure(
                    f"Agent Link plugin did not become active within {self.ready_timeout:.0f}s "
                    f"(fail closed, C6); log tail: {self._log_tail()}"
                )
            time.sleep(0.25)

    def _create_session(self) -> None:
        payload = {
            "id": self.session_id,
            "location": {"directory": str(self.cwd)},
        }
        if self.model_ref is not None:
            payload["model"] = self.model_ref
        status, body = _api(self.endpoint, self.password, "POST", "/api/session", payload)
        data = _unwrap(body)
        if status != 200 or not isinstance(data, dict) or data.get("id") != self.session_id:
            raise DeliveryFailure(
                f"opencode session pre-create failed for {self.session_id!r} "
                f"(HTTP {status}); log tail: {self._log_tail()}"
            )

    def _write_descriptors(self) -> None:
        protocol = push.PROTOCOL
        server_descriptor = {
            "protocol": protocol,
            "framework": "opencode",
            "serve_id": self.serve_id,
            "endpoint": self.endpoint,
            "pid": self.serve_pid,
            "supervisor_pid": os.getpid(),
            "managed": True,
            "credential_kind": "inline_password",
            "password": self.password,
        }
        session_descriptor = {
            "protocol": protocol,
            "framework": "opencode",
            "session_id": self.session_id,
            "endpoint": self.endpoint,
            "pid": self.serve_pid,
            "serve_id": self.serve_id,
            "managed": True,
            "credential_kind": "server_descriptor",
        }
        try:
            self._server_path = push.write_server_descriptor(server_descriptor)
            try:
                self._session_path = push.write_session_descriptor(session_descriptor)
            except FileExistsError:
                push.remove_descriptor_if_matches(
                    self._server_path, serve_id=self.serve_id, pid=self.serve_pid
                )
                raise ConflictError(
                    f"session descriptor already exists: "
                    f"{push.session_descriptor_path('opencode', self.session_id)}"
                ) from None
        except FileExistsError:
            raise ConflictError(
                f"serve descriptor already exists for serve_id {self.serve_id!r}"
            ) from None

    def request_stop(self) -> None:
        self._stop_requested = True
        self.stop_event.set()

    def run_until_stopped(self) -> int:
        """Block until SIGTERM/SIGINT or an unexpected child exit; then clean up."""
        while not self.stop_event.wait(0.5):
            if self.process is not None and self.process.poll() is not None:
                self._child_exit_code = self.process.returncode
                break
        requested = self._stop_requested
        self.stop()
        if self._child_exit_code is not None and not requested:
            raise DeliveryFailure(
                f"opencode serve exited unexpectedly (code {self._child_exit_code}); "
                f"see {self.log_path}"
            )
        return 0

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self.stop_event.set()
        process = self.process
        if process is not None and process.poll() is None:
            self._terminate(process)
        if self._session_path is not None:
            push.remove_descriptor_if_matches(
                self._session_path, serve_id=self.serve_id, pid=self.serve_pid or -1
            )
        if self._server_path is not None:
            push.remove_descriptor_if_matches(
                self._server_path, serve_id=self.serve_id, pid=self.serve_pid or -1
            )
        if self._log_stream is not None:
            try:
                self._log_stream.close()
            except OSError:
                pass
            self._log_stream = None

    def _terminate(self, process: subprocess.Popen) -> None:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)  # type: ignore[name-defined]
        except (OSError, ProcessLookupError, PermissionError):
            try:
                process.terminate()
            except OSError:
                pass
        try:
            process.wait(timeout=self.stop_grace)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)  # type: ignore[name-defined]
        except (OSError, ProcessLookupError, PermissionError):
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            pass


# ---------------------------------------------------------------------------
# client commands (status / send / stop)
# ---------------------------------------------------------------------------

def _load_session_descriptor(root, binding_id: str) -> tuple[dict, dict, Path]:
    """Read the binding + descriptors needed by the client commands."""
    binding = _read_binding(root, binding_id)
    if binding.get("child_framework") == "opencode":
        role = "child"
    elif binding.get("parent_framework") == "opencode":
        role = "parent"
    else:
        raise ValidationError(
            f"binding {binding_id} has no opencode side (child={binding.get('child_framework')!r}, "
            f"parent={binding.get('parent_framework')!r})"
        )
    session_id = validate_identifier(binding[f"{role}_session_id"], f"{role}_session_id")
    session_path = push.session_descriptor_path("opencode", session_id)
    if not session_path.is_file():
        raise NotFoundError(f"no opencode session descriptor for {session_id!r}; run serve first")
    session_descriptor = push.read_session_descriptor("opencode", session_id)
    serve_id = session_descriptor.get("serve_id")
    try:
        validate_identifier(serve_id, "serve_id")
    except ValidationError:
        raise DeliveryFailure(
            f"opencode session descriptor has an invalid serve_id: {session_path}"
        ) from None
    server_descriptor = push.read_server_descriptor("opencode", serve_id)
    if (
        server_descriptor.get("endpoint") != session_descriptor.get("endpoint")
        or server_descriptor.get("pid") != session_descriptor.get("pid")
    ):
        raise DeliveryFailure("opencode serve descriptor does not match the session descriptor")
    return binding, {"session": session_descriptor, "server": server_descriptor}, session_path


def status_binding(root, binding_id: str) -> dict:
    """Report the runner state without printing any credential."""
    binding = _read_binding(root, binding_id)
    role = "child" if binding.get("child_framework") == "opencode" else "parent"
    if binding.get(f"{role}_framework") != "opencode":
        raise ValidationError(
            f"binding {binding_id} has no opencode side "
            f"(child={binding.get('child_framework')!r}, parent={binding.get('parent_framework')!r})"
        )
    session_id = validate_identifier(binding[f"{role}_session_id"], f"{role}_session_id")
    if not push.session_descriptor_path("opencode", session_id).is_file():
        return {"binding_id": binding_id, "runner": "absent", "session_id": session_id}
    _binding, descriptors, _path = _load_session_descriptor(root, binding_id)
    session_descriptor = descriptors["session"]
    server_descriptor = descriptors["server"]
    endpoint = session_descriptor["endpoint"]
    pid = session_descriptor["pid"]
    result = {
        "binding_id": binding_id,
        "runner": "ready",
        "serve_id": session_descriptor["serve_id"],
        "session_id": session_id,
        "endpoint": endpoint,
        "serve_pid": pid,
        "process": "alive" if push._pid_alive(pid) else "dead",
    }
    try:
        if not push._pid_alive(pid):
            result["runner"] = "stale"
            result["session"] = "unknown"
            result["plugin"] = "unknown"
            return result
        result["session"] = (
            "present"
            if _session_present(endpoint, server_descriptor["password"], session_id)
            else "absent"
        )
        result["plugin"] = (
            "active"
            if _plugin_active(endpoint, server_descriptor["password"])
            else "inactive"
        )
    except Exception:  # noqa: BLE001 - status must not raise on a dead server
        result["runner"] = "unreachable"
        result["session"] = "unknown"
        result["plugin"] = "unknown"
    return result


def send_binding(root, binding_id: str, text: str, *, message_id: str | None = None,
                 timeout: float = push.TIMEOUT_SECONDS) -> dict:
    """Send raw prompt text through the frozen descriptors (no binding/role render)."""
    binding = _read_binding(root, binding_id)
    role = "child" if binding.get("child_framework") == "opencode" else "parent"
    if binding.get(f"{role}_framework") != "opencode":
        raise ValidationError(
            f"binding {binding_id} has no opencode side "
            f"(child={binding.get('child_framework')!r}, parent={binding.get('parent_framework')!r})"
        )
    session_id = validate_identifier(binding[f"{role}_session_id"], f"{role}_session_id")
    if not push.session_descriptor_path("opencode", session_id).is_file():
        raise NotFoundError(
            f"no opencode session descriptor for {session_id!r}; run serve first"
        )
    if message_id is None:
        message_id = f"send-{uuid.uuid4().hex}"
    detail = push.post_prompt(
        "opencode", session_id, text, message_id=message_id, timeout=timeout
    )
    return {
        "binding_id": binding_id,
        "session_id": session_id,
        "message_id": message_id,
        "accepted": True,
        "detail": detail,
    }


def _verify_our_process(pid: int, endpoint: str) -> None:
    """Refuse to stop a pid whose command line is not this opencode serve."""
    cmdline_path = Path(f"/proc/{pid}/cmdline")
    if not cmdline_path.is_file():
        return  # non-Linux: descriptor identity + pid liveness is all we have
    try:
        cmdline = cmdline_path.read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
    except OSError as error:
        raise DeliveryFailure(f"cannot read the serve command line: {error}") from None
    port = urllib.parse.urlsplit(endpoint).port
    if "opencode" not in cmdline or "serve" not in cmdline or str(port) not in cmdline:
        raise DeliveryFailure(
            f"refusing to stop pid {pid}: command line does not look like this opencode serve"
        )


def _is_our_supervisor(pid: int) -> bool:
    """True when the pid looks like this runner's supervisor process."""
    cmdline_path = Path(f"/proc/{pid}/cmdline")
    if not cmdline_path.is_file():
        return True  # non-Linux: trust the descriptor's supervisor_pid
    try:
        cmdline = cmdline_path.read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
    except OSError:
        return False
    return "agent_link.hosts.opencode.serve" in cmdline


def stop_binding(root, binding_id: str, *, stop_grace: float = DEFAULT_STOP_GRACE) -> dict:
    """Stop the serve named by the descriptors and remove only those descriptors.

    The runner supervisor (``supervisor_pid``) is signalled first so it can
    stop its child and remove the descriptors itself (graceful path); if no
    live supervisor is recorded, the serve pid is signalled directly and the
    descriptors are removed only when they still name this serve (E1).
    """
    _binding, descriptors, session_path = _load_session_descriptor(root, binding_id)
    session_descriptor = descriptors["session"]
    server_descriptor = descriptors["server"]
    pid = session_descriptor["pid"]
    endpoint = session_descriptor["endpoint"]
    serve_id = session_descriptor["serve_id"]
    server_path = push.server_descriptor_path("opencode", serve_id)
    supervisor_pid = server_descriptor.get("supervisor_pid")
    target = None
    if (
        isinstance(supervisor_pid, int)
        and not isinstance(supervisor_pid, bool)
        and supervisor_pid > 0
        and push._pid_alive(supervisor_pid)
        and _is_our_supervisor(supervisor_pid)
    ):
        target = supervisor_pid
    if not push._pid_alive(pid) and target is None:
        push.remove_descriptor_if_matches(session_path, serve_id=serve_id, pid=pid)
        push.remove_descriptor_if_matches(server_path, serve_id=serve_id, pid=pid)
        return {"binding_id": binding_id, "stopped": False, "pid": pid, "reason": "process already gone"}
    if target is None:
        _verify_our_process(pid, endpoint)
    try:
        os.kill(target if target is not None else pid, signal.SIGTERM)  # type: ignore[name-defined]
    except OSError as error:
        raise DeliveryFailure(f"cannot signal opencode serve pid {target or pid}: {error}") from None
    deadline = time.monotonic() + stop_grace
    while time.monotonic() < deadline:
        supervisor_alive = target is not None and push._pid_alive(target)
        serve_alive = push._pid_alive(pid)
        if not supervisor_alive and not serve_alive:
            break
        time.sleep(0.1)
    for candidate in (target, pid):
        if candidate is not None and push._pid_alive(candidate):
            try:
                os.kill(candidate, signal.SIGKILL)  # type: ignore[name-defined]
            except OSError:
                pass
    time.sleep(0.2)
    push.remove_descriptor_if_matches(session_path, serve_id=serve_id, pid=pid)
    push.remove_descriptor_if_matches(server_path, serve_id=serve_id, pid=pid)
    return {
        "binding_id": binding_id,
        "stopped": True,
        "pid": pid,
        "supervisor_pid": target,
        "serve_id": serve_id,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m agent_link.hosts.opencode.serve",
        description=(
            "OpenCode self-managed serve runner: serve a dedicated opencode server for one "
            "binding (password + isolated DB + descriptor contract); status/send/stop talk to it."
        ),
    )
    parser.add_argument("--root", required=True, metavar="ABS_DIR", help="runtime directory holding the binding")
    parser.add_argument("--binding", required=True, metavar="ID", help="binding_id served/queried")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the dedicated opencode serve until stopped")
    p.add_argument("--role", choices=("child", "parent"), default="child")
    p.add_argument("--opencode-bin", default=DEFAULT_OPENCODE_BIN, metavar="PATH")
    p.add_argument("--cwd", required=True, metavar="ABS_WORK", help="working directory for the session")
    p.add_argument("--credentials", default=None, metavar="FILE", help="child HTTP credentials for the plugin tools")
    p.add_argument("--extension", default=None, metavar="FILE", help="Agent Link OpenCode plugin")
    p.add_argument("--config", default=None, metavar="FILE", help="user opencode.json passed read-only")
    p.add_argument("--model", default=None, metavar="PROVIDER/MODEL", help="session model override")
    p.add_argument("--port", type=int, default=0, help="fixed port (0 picks a free one)")
    p.add_argument("--ready-timeout", type=float, default=DEFAULT_READY_TIMEOUT)
    p.add_argument("--stop-grace", type=float, default=DEFAULT_STOP_GRACE)
    p.add_argument("--db", default=None, metavar="FILE", help="session database (default under root)")

    sub.add_parser("status", help="report the runner/session/plugin state")

    p = sub.add_parser("send", help="send raw prompt text through the descriptors")
    p.add_argument("--text", default=None)
    p.add_argument("--text-file", default=None)
    p.add_argument("--message-id", default=None, dest="message_id")
    p.add_argument("--timeout", type=float, default=push.TIMEOUT_SECONDS)

    p = sub.add_parser("stop", help="stop the dedicated serve and remove its descriptors")
    p.add_argument("--stop-grace", type=float, default=DEFAULT_STOP_GRACE)
    return parser


def _resolve_text(args) -> str:
    if (args.text is None) == (args.text_file is None):
        raise ValidationError("pass exactly one of --text or --text-file")
    if args.text is not None:
        if not args.text.strip():
            raise ValidationError("--text must not be empty")
        return args.text
    path = Path(args.text_file).expanduser()
    if not path.is_file():
        raise ValidationError(f"text file does not exist: {path}")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ValidationError(f"cannot read text file {path}: {error}") from None
    if not text.strip():
        raise ValidationError("text file is empty")
    return text


def _print_result(result) -> None:
    print(json.dumps(result, ensure_ascii=False), flush=True)


def _print_error(error: AgentLinkError) -> None:
    print(f"{error.code}: {error}", file=sys.stderr)
    _print_result({"error": error.code, "message": str(error)})


def _run_serve(args) -> int:
    service = OpenCodeServe(
        args.root,
        args.binding,
        role=args.role,
        opencode_bin=args.opencode_bin,
        cwd=args.cwd,
        credentials=args.credentials,
        extension=args.extension,
        config=args.config,
        model=args.model,
        port=args.port,
        ready_timeout=args.ready_timeout,
        stop_grace=args.stop_grace,
        db=args.db,
    )
    ready = service.start()
    _print_result(ready)
    previous_handlers = {}
    for name in ("SIGTERM", "SIGINT"):
        if hasattr(signal, name):  # type: ignore[name-defined]
            previous_handlers[name] = signal.signal(  # type: ignore[name-defined]
                getattr(signal, name), lambda *_a: service.request_stop()
            )
    try:
        return service.run_until_stopped()
    finally:
        service.stop()
        for name, handler in previous_handlers.items():
            signal.signal(getattr(signal, name), handler)  # type: ignore[name-defined]


def _run_client(args) -> int:
    binding_id = validate_identifier(args.binding, "binding_id")
    if args.command == "status":
        _print_result(status_binding(args.root, binding_id))
        return 0
    if args.command == "send":
        text = _resolve_text(args)
        _print_result(
            send_binding(
                args.root,
                binding_id,
                text,
                message_id=args.message_id,
                timeout=args.timeout,
            )
        )
        return 0
    if args.command == "stop":
        _print_result(stop_binding(args.root, binding_id, stop_grace=args.stop_grace))
        return 0
    raise ValidationError(f"unknown command {args.command!r}")


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            return _run_serve(args)
        return _run_client(args)
    except AgentLinkError as error:
        _print_error(error)
        return error.exit_code
    except Exception as error:  # noqa: BLE001 - top-level guard mirrors the core CLI
        traceback.print_exc()
        _print_result({"error": "internal", "message": f"{type(error).__name__}: {error}"})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
