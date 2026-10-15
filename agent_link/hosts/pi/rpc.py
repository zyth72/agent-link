"""Pi RPC child controller (PI-RPC v1).

A long-lived controller owns a dedicated ``pi --mode rpc`` subprocess and
exposes a loopback HTTP control channel described by an exclusive descriptor
under ``<root>/pi-rpc/<binding_id>.json``.  The parent CLI (``ask-child`` /
``send-child``) and the Codex helper drive it; nothing polls a file inbox.

Protocol and lifecycle: see ``docs/PI-RPC.md``.  Key invariants:

- stdout is read continuously and split **only** on byte LF (never U+2028);
- responses complete pending waits strictly by ``id``; async events never do;
- a malformed/oversized frame fails explicitly, wakes every waiter and stops
  the child process group;
- ``ready`` waits for a real ``get_state`` response whose ``sessionId``
  matches the binding before the descriptor becomes usable;
- ``stop`` closes stdin, waits, then signals the whole process group so no
  orphan Pi survives a controller SIGTERM.
"""

from __future__ import annotations

import argparse
from collections import deque
import hmac
import json
import os
from pathlib import Path
import secrets
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ...errors import (
    AgentLinkError,
    ConflictError,
    DeliveryFailure,
    NotFoundError,
    RequestTimeout,
    ValidationError,
)
from ...shared import utc_now_iso
from ...storage import DB_FILENAME, require_text, validate_identifier

PROTOCOL = "agent-link-pi-rpc/1"
DESCRIPTOR_DIR_NAME = "pi-rpc"
DEFAULT_EXTENSION_PATH = Path(__file__).resolve().parents[3] / "extensions" / "pi" / "agent-link.ts"

#: One RPC record may not exceed this many bytes (LF excluded).
MAX_FRAME_BYTES = 8 * 1024 * 1024
#: HTTP control request/response body bound.
MAX_BODY_BYTES = 65536
#: Read timeout for one control HTTP request (headers + body).
REQUEST_READ_TIMEOUT_SECONDS = 10.0
#: Default client-side timeout for one control operation.
DEFAULT_CLIENT_TIMEOUT_SECONDS = 30.0
#: Bounded async-event history kept per controller.
EVENT_HISTORY = 256
#: Bounded stderr tail kept per controller.
STDERR_HISTORY = 200

SEND_MODES = {"prompt": "prompt", "steer": "steer", "follow-up": "follow-up"}
OPERATIONS = {
    "/v1/prompt": {"text"},
    "/v1/steer": {"text"},
    "/v1/follow-up": {"text"},
    "/v1/status": set(),
    "/v1/abort": set(),
    "/v1/stop": set(),
}


class RpcTimeout(RequestTimeout):
    code = "rpc_timeout"


class RpcProtocolError(DeliveryFailure):
    code = "rpc_protocol"


class RpcExited(DeliveryFailure):
    code = "rpc_exited"


class RpcCommandFailure(DeliveryFailure):
    code = "rpc_command_failed"


class _Pending:
    __slots__ = ("event", "response", "error")

    def __init__(self):
        self.event = threading.Event()
        self.response = None
        self.error = None


class RpcController:
    """Own one ``pi --mode rpc`` process and correlate its JSONL protocol."""

    def __init__(
        self,
        root,
        binding: dict,
        pi_bin: str,
        cwd,
        credentials,
        *,
        extension=None,
        provider=None,
        model=None,
        rpc_timeout: float = DEFAULT_CLIENT_TIMEOUT_SECONDS,
        stop_grace: float = 10.0,
    ):
        self.root = Path(root).expanduser().resolve()
        self.binding = binding
        self.binding_id = binding["binding_id"]
        self.session_id = binding["child_session_id"]
        self.pi_bin = str(pi_bin)
        self.cwd = Path(cwd).expanduser().resolve()
        self.credentials = Path(credentials).expanduser().resolve()
        self.extension = Path(extension) if extension else DEFAULT_EXTENSION_PATH
        self.provider = provider
        self.model = model
        self.rpc_timeout = float(rpc_timeout)
        self.stop_grace = float(stop_grace)
        self.process: subprocess.Popen | None = None
        self._counter = 0
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        self._events: deque = deque(maxlen=EVENT_HISTORY)
        self._events_condition = threading.Condition(self._lock)
        self._unmatched_responses: deque = deque(maxlen=16)
        self._stderr_tail: deque = deque(maxlen=STDERR_HISTORY)
        self._fatal: AgentLinkError | None = None
        self._exited = False
        self._stopping = False
        self._stop_lock = threading.Lock()
        self._stdout_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self.agent_settled_count = 0

    # -- process lifecycle ---------------------------------------------
    def argv(self) -> list[str]:
        """The exact child argv (never a shell string)."""
        argv = [
            self.pi_bin,
            "--mode",
            "rpc",
            "--session-id",
            self.session_id,
            "--extension",
            str(self.extension),
            "--no-extensions",
            "--no-skills",
            "--no-context-files",
        ]
        if self.provider:
            argv += ["--provider", str(self.provider)]
        if self.model:
            argv += ["--model", str(self.model)]
        return argv

    def start(self) -> dict:
        """Spawn Pi and complete the identity handshake; returns get_state data."""
        if self.process is not None:
            raise ValidationError("rpc controller already started")
        env = dict(os.environ)
        env["AGENT_LINK_CREDENTIALS"] = str(self.credentials)
        # The dedicated child must not double-consume the parent bridge.
        env.pop("AGENT_LINK_BRIDGE_FILE", None)
        env.pop("AGENT_LINK_BINDING", None)
        try:
            self.process = subprocess.Popen(
                self.argv(),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self.cwd),
                env=env,
                bufsize=0,
                start_new_session=True,
            )
        except OSError as error:
            raise DeliveryFailure(f"cannot start pi rpc process: {error}") from error
        self._stdout_thread = threading.Thread(
            target=self._read_stdout, name="agent-link-pi-rpc-stdout", daemon=True
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr, name="agent-link-pi-rpc-stderr", daemon=True
        )
        self._stdout_thread.start()
        self._stderr_thread.start()
        state = self.status(timeout=self.rpc_timeout)
        actual_session = state.get("sessionId")
        if actual_session != self.session_id:
            error = RpcProtocolError(
                f"pi rpc get_state sessionId {actual_session!r} does not match binding "
                f"child_session_id {self.session_id!r}"
            )
            self._fail_pending(error)
            self._signal_process_group(signal.SIGTERM)
            raise error
        return state

    def _signal_process_group(self, sig) -> None:
        process = self.process
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(process.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            # Fall back to the direct child only; never raise from cleanup.
            try:
                process.send_signal(sig)
            except OSError:
                pass

    def stop(self) -> None:
        """Graceful stdin close, then process-group termination after a grace."""
        with self._stop_lock:
            if self._stopping:
                return
            self._stopping = True
        process = self.process
        if process is None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
        except (OSError, ValueError):
            pass
        timed_out = False
        try:
            process.wait(timeout=self.stop_grace)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._signal_process_group(signal.SIGTERM)
            try:
                process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                self._signal_process_group(signal.SIGKILL)
                try:
                    process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:  # pragma: no cover - unkillable child
                    pass
        self._wake_on_exit(process.returncode, timed_out=timed_out)
        for thread in (self._stdout_thread, self._stderr_thread):
            if thread is not None:
                thread.join(timeout=5.0)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()

    def _wake_on_exit(self, returncode, *, timed_out=False) -> None:
        error = RpcExited(
            f"pi rpc process exited (code={returncode})"
            + (" after the stop grace elapsed" if timed_out else "")
        )
        self._fail_pending(error)

    def _fail_pending(self, error: AgentLinkError) -> None:
        with self._lock:
            self._exited = True
            pending = list(self._pending.values())
            self._pending.clear()
            if self._fatal is None and isinstance(error, RpcProtocolError):
                self._fatal = error
        for item in pending:
            item.error = error
            item.event.set()
        with self._events_condition:
            self._events_condition.notify_all()

    # -- protocol reader -----------------------------------------------
    def _read_stdout(self) -> None:
        process = self.process
        assert process is not None and process.stdout is not None
        fd = process.stdout.fileno()
        buffer = bytearray()
        try:
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                buffer.extend(chunk)
                while True:
                    index = buffer.find(b"\n")
                    if index < 0:
                        break
                    line = bytes(buffer[:index])
                    del buffer[:index + 1]
                    if line.endswith(b"\r"):
                        line = line[:-1]
                    if not line:
                        continue
                    if len(line) > MAX_FRAME_BYTES:
                        self._protocol_failure(
                            f"pi rpc frame exceeds {MAX_FRAME_BYTES} bytes ({len(line)})"
                        )
                        return
                    if not self._handle_line(line):
                        return
                if len(buffer) > MAX_FRAME_BYTES:
                    self._protocol_failure(
                        f"pi rpc frame exceeds {MAX_FRAME_BYTES} bytes without LF"
                    )
                    return
        except (OSError, ValueError):
            pass
        finally:
            self._process_gone()

    def _protocol_failure(self, message: str) -> None:
        error = RpcProtocolError(message)
        self._fail_pending(error)
        # A desynchronized stream cannot be trusted: terminate the child group.
        self._signal_process_group(signal.SIGTERM)

    def _handle_line(self, line: bytes) -> bool:
        """Parse one frame; return False when the stream must be abandoned."""
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError:
            self._protocol_failure("pi rpc frame is not valid UTF-8")
            return False
        try:
            record = json.loads(text)
        except ValueError as error:
            self._protocol_failure(f"pi rpc frame is not valid JSON: {error}")
            return False
        if not isinstance(record, dict):
            self._protocol_failure("pi rpc frame is not a JSON object")
            return False
        record_id = record.get("id")
        if record.get("type") == "response":
            if isinstance(record_id, str) and record_id:
                with self._lock:
                    pending = self._pending.pop(record_id, None)
                if pending is not None:
                    pending.response = record
                    pending.event.set()
                    return True
            with self._lock:
                self._unmatched_responses.append(record)
            return True
        with self._lock:
            self._events.append(record)
            if record.get("type") == "agent_settled":
                self.agent_settled_count += 1
            self._events_condition.notify_all()
        return True

    def _read_stderr(self) -> None:
        process = self.process
        assert process is not None and process.stderr is not None
        fd = process.stderr.fileno()
        try:
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                for line in chunk.split(b"\n"):
                    if line:
                        self._stderr_tail.append(line.decode("utf-8", "replace"))
        except (OSError, ValueError):
            pass

    def _process_gone(self) -> None:
        with self._lock:
            already = self._exited
        if already:
            return
        returncode = None
        process = self.process
        if process is not None:
            try:
                returncode = process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                pass
        self._wake_on_exit(returncode)

    # -- commands ------------------------------------------------------
    def request(self, command: dict, timeout: float | None = None) -> dict:
        """Send one command and wait for its matching response.

        A timeout never resends: the command may already have been accepted.
        """
        timeout = self.rpc_timeout if timeout is None else float(timeout)
        if not isinstance(command, dict) or not command.get("type"):
            raise ValidationError("rpc command must be an object with a type")
        with self._lock:
            if self._fatal is not None:
                raise self._fatal
            if self._exited:
                raise RpcExited("pi rpc process is not running")
            request_id = command.get("id")
            if request_id is None:
                self._counter += 1
                request_id = f"rpc-{self._counter}"
            if not isinstance(request_id, str) or not request_id:
                raise ValidationError("rpc command id must be a non-empty string")
            pending = _Pending()
            self._pending[request_id] = pending
        payload = dict(command)
        payload["id"] = request_id
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            process = self.process
            assert process is not None and process.stdin is not None
            with self._write_lock:
                view = memoryview(data)
                fd = process.stdin.fileno()
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError("write returned 0 bytes")
                    view = view[written:]
        except (OSError, ValueError) as error:
            with self._lock:
                self._pending.pop(request_id, None)
            raise RpcExited(f"cannot write to pi rpc stdin: {error}") from error
        if not pending.event.wait(timeout):
            with self._lock:
                self._pending.pop(request_id, None)
            raise RpcTimeout(
                f"pi rpc {payload['type']!r} did not answer within {timeout:g}s; "
                "it may still have been accepted -- do not resend automatically"
            )
        if pending.error is not None:
            raise pending.error
        record = pending.response or {}
        if record.get("success") is not True:
            raise RpcCommandFailure(
                f"pi rpc {payload['type']!r} failed: {record.get('error', 'unknown error')}"
            )
        return record

    def status(self, timeout: float | None = None) -> dict:
        """One on-demand ``get_state``; never a background poll."""
        record = self.request({"type": "get_state"}, timeout)
        data = record.get("data")
        process = self.process
        result = {
            "binding_id": self.binding_id,
            "run_id": self.binding["run_id"],
            "child_session_id": self.session_id,
            "pid": os.getpid(),
            "pi_pid": process.pid if process is not None else None,
            "alive": bool(process is not None and process.poll() is None),
        }
        if isinstance(data, dict):
            result.update(data)
        return result

    def prompt(self, text: str, timeout: float | None = None) -> dict:
        """Prompt with ``streamingBehavior=steer``: starts idle, queues busy."""
        return self.request(
            {"type": "prompt", "message": text, "streamingBehavior": "steer"}, timeout
        )

    def steer(self, text: str, timeout: float | None = None) -> dict:
        return self.request({"type": "steer", "message": text}, timeout)

    def follow_up(self, text: str, timeout: float | None = None) -> dict:
        return self.request({"type": "follow_up", "message": text}, timeout)

    def abort(self, timeout: float | None = None) -> dict:
        """Clear queued steering/follow-up texts, then abort the current run."""
        cleared = self.request({"type": "clear_queue"}, timeout)
        aborted = self.request({"type": "abort"}, timeout)
        return {
            "cleared": cleared.get("data") if isinstance(cleared.get("data"), dict) else {},
            "aborted": aborted.get("data") if isinstance(aborted.get("data"), dict) else {},
        }

    # -- introspection -------------------------------------------------
    def events(self) -> list[dict]:
        with self._lock:
            return list(self._events)

    def unmatched_responses(self) -> list[dict]:
        with self._lock:
            return list(self._unmatched_responses)

    def stderr_tail(self) -> list[str]:
        with self._lock:
            return list(self._stderr_tail)


# ---------------------------------------------------------------------------
# descriptor read / write
# ---------------------------------------------------------------------------

def descriptor_dir(root) -> Path:
    return Path(root).expanduser().resolve() / DESCRIPTOR_DIR_NAME


def descriptor_path(root, binding_id: str) -> Path:
    return descriptor_dir(root) / f"{binding_id}.json"


def write_descriptor_exclusive(path: Path, descriptor: dict) -> None:
    """Create the descriptor 0600 with O_EXCL; an existing one is never replaced."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as error:
        raise ConflictError(
            f"pi-rpc descriptor already exists: {path} (another controller may be running)"
        ) from error
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(descriptor, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def replace_descriptor(path: Path, descriptor: dict) -> None:
    """Atomically replace our own descriptor while keeping it 0600."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(descriptor, stream, ensure_ascii=False, sort_keys=True)
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


def remove_descriptor_if_ours(path: Path, descriptor: dict) -> None:
    """Delete only a descriptor that still describes this controller."""
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return
    if not isinstance(current, dict):
        return
    if (
        current.get("token") == descriptor.get("token")
        and current.get("binding_id") == descriptor.get("binding_id")
        and current.get("protocol") == descriptor.get("protocol")
    ):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def read_descriptor(root, binding_id: str) -> dict | None:
    """Return the raw descriptor for a binding, or ``None`` when absent.

    A present-but-unreadable descriptor raises ``DeliveryFailure``: callers
    must never mistake it for "no controller" and silently fall back.
    """
    path = descriptor_path(root, binding_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as error:
        raise DeliveryFailure(f"cannot read pi-rpc descriptor {path}: {error}") from error
    try:
        descriptor = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DeliveryFailure(f"pi-rpc descriptor is not valid JSON {path}: {error}") from error
    if not isinstance(descriptor, dict):
        raise DeliveryFailure(f"pi-rpc descriptor is not a JSON object: {path}")
    return descriptor


def validate_descriptor(descriptor: dict, binding: dict) -> dict:
    """Check protocol, identity, readiness and loopback endpoint shape."""
    if descriptor.get("protocol") != PROTOCOL:
        raise DeliveryFailure(
            f"unsupported pi-rpc descriptor protocol {descriptor.get('protocol')!r}"
        )
    for field, expected in (
        ("binding_id", binding["binding_id"]),
        ("run_id", binding["run_id"]),
        ("child_session_id", binding["child_session_id"]),
    ):
        if descriptor.get(field) != expected:
            raise DeliveryFailure(
                f"pi-rpc descriptor {field} mismatch: descriptor={descriptor.get(field)!r} "
                f"binding={expected!r}"
            )
    if descriptor.get("state") != "ready":
        raise DeliveryFailure(
            f"pi-rpc descriptor for {binding['binding_id']} is not ready "
            f"(state={descriptor.get('state')!r})"
        )
    _validate_endpoint(descriptor.get("endpoint"))
    token = descriptor.get("token")
    if not isinstance(token, str) or len(token) < 16:
        raise DeliveryFailure("pi-rpc descriptor token is missing or too short")
    return descriptor


def descriptor_for_binding(root, binding: dict) -> dict | None:
    """Validated descriptor or ``None`` when no descriptor exists at all."""
    descriptor = read_descriptor(root, binding["binding_id"])
    if descriptor is None:
        return None
    return validate_descriptor(descriptor, binding)


def _validate_endpoint(endpoint) -> urllib.parse.SplitResult:
    if not isinstance(endpoint, str) or not endpoint:
        raise DeliveryFailure("pi-rpc descriptor endpoint is missing")
    try:
        parsed = urllib.parse.urlsplit(endpoint)
        port = parsed.port
    except ValueError as error:
        raise DeliveryFailure(f"pi-rpc descriptor endpoint is invalid: {error}") from error
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or not (1 <= port <= 65535)
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise DeliveryFailure(
            "pi-rpc descriptor endpoint must be plain http://127.0.0.1:<port>/ (loopback only)"
        )
    return parsed


# ---------------------------------------------------------------------------
# control HTTP client
# ---------------------------------------------------------------------------

class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


def control_request(
    descriptor: dict, operation: str, payload: dict, timeout: float | None = None
) -> dict:
    """POST one control operation; loopback only, no proxies, no redirects."""
    operation = operation.strip("/")
    if operation.startswith("v1/"):
        operation = operation[3:]
    if f"/v1/{operation}" not in OPERATIONS:
        raise ValidationError(f"unknown pi-rpc operation {operation!r}")
    if not isinstance(payload, dict):
        raise ValidationError("pi-rpc control payload must be an object")
    parsed = _validate_endpoint(descriptor.get("endpoint"))
    token = descriptor.get("token")
    if not isinstance(token, str) or len(token) < 16:
        raise DeliveryFailure("pi-rpc descriptor token is missing or too short")
    url = f"{urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, '/v1/' + operation, '', ''))}"
    timeout = DEFAULT_CLIENT_TIMEOUT_SECONDS if timeout is None else float(timeout)
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _RejectRedirects()
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            return _parse_control_response(response.read(MAX_BODY_BYTES + 1))
    except urllib.error.HTTPError as error:
        try:
            body = error.read(MAX_BODY_BYTES + 1)
        finally:
            error.close()
        raise _http_error(operation, error.code, body) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        reason = getattr(error, "reason", None)
        if isinstance(error, TimeoutError) or isinstance(reason, TimeoutError):
            raise RpcTimeout(
                f"pi-rpc {operation} did not answer within {timeout:g}s; "
                "the command may still have been accepted -- do not resend automatically"
            ) from error
        raise DeliveryFailure(f"pi-rpc {operation} request failed: {error}") from error


def _parse_control_response(body: bytes) -> dict:
    if len(body) > MAX_BODY_BYTES:
        raise DeliveryFailure("pi-rpc control response exceeds the body limit")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise DeliveryFailure(f"pi-rpc control response is not JSON: {error}") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("result"), dict):
        raise DeliveryFailure("pi-rpc control response has an unexpected envelope")
    exit_code = payload.get("exit_code")
    if not isinstance(exit_code, int):
        raise DeliveryFailure("pi-rpc control response has no integer exit_code")
    if exit_code != 0:
        raise DeliveryFailure(
            f"pi-rpc control operation failed (exit {exit_code}): "
            f"{payload.get('result')}"
        )
    return payload["result"]


def _http_error(operation: str, status: int, body: bytes) -> AgentLinkError:
    message = body[:500].decode("utf-8", "replace")
    code = "http_error"
    try:
        parsed = json.loads(body.decode("utf-8"))
        if isinstance(parsed, dict):
            code = str(parsed.get("error") or code)
            message = str(parsed.get("message") or message)
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    detail = f"pi-rpc {operation} rejected (HTTP {status}, {code}): {message}"
    if status == 401:
        return DeliveryFailure(detail)
    if status == 404:
        return NotFoundError(detail)
    if status == 409:
        return ConflictError(detail)
    if status == 408:
        return RpcTimeout(detail)
    if status == 400:
        return ValidationError(detail)
    return DeliveryFailure(detail)


# ---------------------------------------------------------------------------
# control HTTP server
# ---------------------------------------------------------------------------

class _ControlHandler(BaseHTTPRequestHandler):
    timeout = REQUEST_READ_TIMEOUT_SECONDS

    def log_message(self, *args):  # noqa: D102 - never log bodies/tokens
        pass

    def send_json(self, status: int, value: dict) -> None:
        payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(payload)
        except OSError:
            pass
        finally:
            self.close_connection = True

    def send_error(self, code, message=None, explain=None):  # noqa: D102
        self.send_json(code, {"error": "http_error", "message": str(message or explain or "rejected")})

    def do_GET(self):  # noqa: N802
        self.send_json(405, {"error": "method_not_allowed", "message": "use POST"})

    def do_POST(self):  # noqa: N802
        service = self.server.service
        expected = "Bearer " + service.token
        if not hmac.compare_digest(self.headers.get("Authorization") or "", expected):
            self.send_json(401, {"error": "unauthorized", "message": "missing or invalid bearer token"})
            return
        try:
            operation = self.path
            if operation not in OPERATIONS:
                raise NotFoundError(f"unknown operation {operation!r}")
            data = self._read_json_body()
            if not isinstance(data, dict) or set(data) != OPERATIONS[operation]:
                raise ValidationError(
                    f"body fields must be exactly {sorted(OPERATIONS[operation])} for {operation}"
                )
            result, exit_code = service.handle_request(operation, data)
            self.send_json(200, {"result": result, "exit_code": exit_code})
            if operation == "/v1/stop":
                service.stop_event.set()
        except TimeoutError:
            self.send_json(408, {"error": "request_timeout", "message": "request body read timed out"})
        except ConnectionError:
            pass
        except AgentLinkError as error:
            self.send_json(_status_for(error), {"error": error.code, "message": str(error)})
        except (ValueError, TypeError) as error:
            self.send_json(400, {"error": "validation", "message": str(error)})
        except Exception:  # noqa: BLE001 - every error response stays JSON
            self.send_json(500, {"error": "controller_failure", "message": "internal controller failure"})

    def _read_json_body(self):
        length_header = self.headers.get("Content-Length")
        try:
            size = int(length_header) if length_header is not None else -1
        except (TypeError, ValueError):
            raise ValidationError("Content-Length must be an integer")
        if not 0 < size <= MAX_BODY_BYTES:
            raise ValidationError(f"body must be 1..{MAX_BODY_BYTES} bytes")
        body = self.rfile.read(size)
        if len(body) != size:
            raise ValidationError("request body ended before Content-Length bytes")
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValidationError(f"invalid JSON body: {error}")


def _status_for(error: AgentLinkError) -> int:
    if isinstance(error, NotFoundError):
        return 404
    if isinstance(error, ConflictError):
        return 409
    if isinstance(error, RpcTimeout):
        return 408
    return 400


class PiRpcService:
    """Own the descriptor, control HTTP server and one RpcController."""

    def __init__(
        self,
        root,
        binding_id: str,
        *,
        pi_bin: str,
        cwd,
        credentials,
        extension=None,
        provider=None,
        model=None,
        rpc_timeout: float = DEFAULT_CLIENT_TIMEOUT_SECONDS,
        stop_grace: float = 10.0,
    ):
        self.root = Path(root).expanduser().resolve()
        self.binding_id = validate_identifier(binding_id, "binding_id")
        self.pi_bin = pi_bin
        self.cwd = cwd
        self.credentials = credentials
        self.extension = extension
        self.provider = provider
        self.model = model
        self.rpc_timeout = rpc_timeout
        self.stop_grace = stop_grace
        self.stop_event = threading.Event()
        self.controller: RpcController | None = None
        self.descriptor: dict | None = None
        self.token = secrets.token_urlsafe(32)
        self.endpoint: str | None = None
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._serving = False
        self._stopped = False

    def start(self) -> dict:
        binding = _load_binding(self.root, self.binding_id)
        if binding["child_framework"] != "pi":
            raise ValidationError(
                f"binding {self.binding_id} child framework is {binding['child_framework']!r}, not pi"
            )
        self._binding = binding
        server = ThreadingHTTPServer(("127.0.0.1", 0), _ControlHandler)
        server.daemon_threads = True
        server.service = self
        server.stop_event = self.stop_event
        self._server = server
        self.endpoint = f"http://127.0.0.1:{server.server_port}"
        descriptor = {
            "protocol": PROTOCOL,
            "state": "starting",
            "binding_id": binding["binding_id"],
            "run_id": binding["run_id"],
            "child_session_id": binding["child_session_id"],
            "endpoint": self.endpoint,
            "token": self.token,
            "pid": os.getpid(),
            "created_at": utc_now_iso(),
            "updated_at": utc_now_iso(),
        }
        self.descriptor = descriptor
        path = descriptor_path(self.root, self.binding_id)
        try:
            write_descriptor_exclusive(path, descriptor)
        except BaseException:
            server.server_close()
            raise
        try:
            controller = RpcController(
                self.root,
                binding,
                self.pi_bin,
                self.cwd,
                self.credentials,
                extension=self.extension,
                provider=self.provider,
                model=self.model,
                rpc_timeout=self.rpc_timeout,
                stop_grace=self.stop_grace,
            )
            self.controller = controller
            state = controller.start()
            descriptor.update(
                {
                    "state": "ready",
                    "pi_pid": controller.process.pid if controller.process else None,
                    "pi_session_id": state.get("sessionId"),
                    "updated_at": utc_now_iso(),
                }
            )
            replace_descriptor(path, descriptor)
        except BaseException:
            if self.controller is not None:
                self.controller.stop()
            server.server_close()
            remove_descriptor_if_ours(path, descriptor)
            raise
        thread = threading.Thread(
            target=server.serve_forever, name="agent-link-pi-rpc-http", daemon=True
        )
        thread.start()
        self._thread = thread
        self._serving = True
        return {
            "ready": True,
            "binding_id": binding["binding_id"],
            "descriptor": str(path),
            "endpoint": self.endpoint,
            "pi_session_id": descriptor.get("pi_session_id"),
        }

    def handle_request(self, operation: str, data: dict) -> tuple[dict, int]:
        controller = self.controller
        if controller is None:
            raise ValidationError("pi rpc controller is not running")
        if operation == "/v1/prompt":
            return controller.prompt(require_text(data["text"], "text")), 0
        if operation == "/v1/steer":
            return controller.steer(require_text(data["text"], "text")), 0
        if operation == "/v1/follow-up":
            return controller.follow_up(require_text(data["text"], "text")), 0
        if operation == "/v1/status":
            result = controller.status()
            result["agent_settled_count"] = controller.agent_settled_count
            result["stderr_tail"] = controller.stderr_tail()[-10:]
            return result, 0
        if operation == "/v1/abort":
            return controller.abort(), 0
        if operation == "/v1/stop":
            return {"stopping": True}, 0
        raise NotFoundError(f"unknown operation {operation!r}")

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self.stop_event.set()
        server, controller = self._server, self.controller
        if server is not None:
            if self._serving:
                try:
                    server.shutdown()
                except Exception:  # noqa: BLE001 - shutdown must never raise
                    pass
            server.server_close()
        if controller is not None:
            controller.stop()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        if self.descriptor is not None:
            remove_descriptor_if_ours(
                descriptor_path(self.root, self.binding_id), self.descriptor
            )


def _load_binding(root, binding_id: str) -> dict:
    """Read the binding row through a read-only connection (never create a DB)."""
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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m agent_link.hosts.pi.rpc",
        description=(
            "Pi RPC child controller: serve a dedicated `pi --mode rpc` session with a "
            "loopback control descriptor; send/status/abort/stop talk to it."
        ),
    )
    parser.add_argument("--root", required=True, metavar="ABS_DIR", help="runtime directory holding the binding")
    parser.add_argument("--binding", required=True, metavar="ID", help="binding_id served/queried")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the controller and its dedicated Pi process")
    p.add_argument("--pi-bin", required=True, metavar="ABS_PI", help="pi executable (absolute recommended)")
    p.add_argument("--cwd", required=True, metavar="ABS_WORK", help="working directory for the Pi process")
    p.add_argument("--credentials", required=True, metavar="FILE", help="child HTTP credentials (host service)")
    p.add_argument("--extension", default=None, metavar="FILE", help="Pi extension to load")
    p.add_argument("--provider", default=None, help="optional Pi provider")
    p.add_argument("--model", default=None, help="optional Pi model")
    p.add_argument("--rpc-timeout", type=float, default=DEFAULT_CLIENT_TIMEOUT_SECONDS)
    p.add_argument("--stop-grace", type=float, default=10.0)

    p = sub.add_parser("send", help="send a prompt/steer/follow-up through the controller")
    p.add_argument("--mode", choices=sorted(SEND_MODES), default="prompt")
    p.add_argument("--text", default=None)
    p.add_argument("--text-file", default=None)
    p.add_argument("--timeout", type=float, default=DEFAULT_CLIENT_TIMEOUT_SECONDS)

    p = sub.add_parser("status", help="on-demand get_state through the controller")
    p.add_argument("--timeout", type=float, default=DEFAULT_CLIENT_TIMEOUT_SECONDS)

    p = sub.add_parser("abort", help="clear queued messages, then abort the current run")
    p.add_argument("--timeout", type=float, default=DEFAULT_CLIENT_TIMEOUT_SECONDS)

    p = sub.add_parser("stop", help="stop the controller and its Pi process")
    p.add_argument("--timeout", type=float, default=DEFAULT_CLIENT_TIMEOUT_SECONDS)
    return parser


def _resolve_text(args) -> str:
    if (args.text is None) == (args.text_file is None):
        raise ValidationError("pass exactly one of --text or --text-file")
    if args.text is not None:
        return require_text(args.text, "text")
    path = Path(args.text_file).expanduser()
    if not path.is_file():
        raise ValidationError(f"text file does not exist: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        raise ValidationError(f"cannot read text file {path}: {error}") from error


def _print_result(result) -> None:
    print(json.dumps(result, ensure_ascii=False), flush=True)


def _print_error(error: AgentLinkError) -> None:
    print(f"{error.code}: {error}", file=sys.stderr)
    _print_result({"error": error.code, "message": str(error)})


def _run_serve(args) -> int:
    extension = Path(args.extension).expanduser() if args.extension else DEFAULT_EXTENSION_PATH
    if not extension.is_file():
        raise ValidationError(f"pi extension does not exist: {extension}")
    credentials = Path(args.credentials).expanduser()
    if not credentials.is_file():
        raise ValidationError(
            f"child credentials file does not exist yet: {credentials} "
            "(start the parent host service first)"
        )
    cwd = Path(args.cwd).expanduser()
    if not cwd.is_dir():
        raise ValidationError(f"cwd is not a directory: {cwd}")
    service = PiRpcService(
        args.root,
        args.binding,
        pi_bin=args.pi_bin,
        cwd=str(cwd.resolve()),
        credentials=str(credentials.resolve()),
        extension=str(extension.resolve()),
        provider=args.provider,
        model=args.model,
        rpc_timeout=args.rpc_timeout,
        stop_grace=args.stop_grace,
    )
    ready = service.start()
    _print_result(ready)
    previous_handlers = {}
    for name in ("SIGTERM", "SIGINT"):
        if hasattr(signal, name):
            previous_handlers[name] = signal.signal(
                getattr(signal, name), lambda *_args: service.stop_event.set()
            )
    try:
        service.stop_event.wait()
    finally:
        service.stop()
        for name, handler in previous_handlers.items():
            signal.signal(getattr(signal, name), handler)
    return 0


def _run_client(args) -> int:
    binding = _load_binding(args.root, validate_identifier(args.binding, "binding_id"))
    if args.command == "send":
        text = _resolve_text(args)
        descriptor = descriptor_for_binding(args.root, binding)
        if descriptor is None:
            raise NotFoundError(
                f"no pi-rpc descriptor for {binding['binding_id']}; run serve first"
            )
        operation = {"prompt": "prompt", "steer": "steer", "follow-up": "follow-up"}[args.mode]
        result = control_request(
            descriptor, operation, {"text": text}, timeout=args.timeout
        )
        _print_result(
            {
                "binding_id": binding["binding_id"],
                "mode": args.mode,
                "accepted": True,
                "result": result,
            }
        )
        return 0
    if args.command == "status":
        descriptor = read_descriptor(args.root, binding["binding_id"])
        if descriptor is None:
            _print_result(
                {"binding_id": binding["binding_id"], "controller": "absent"}
            )
            return 0
        descriptor = validate_descriptor(descriptor, binding)
        result = control_request(descriptor, "status", {}, timeout=args.timeout)
        _print_result(
            {"binding_id": binding["binding_id"], "controller": "ready", "status": result}
        )
        return 0
    if args.command == "abort":
        descriptor = descriptor_for_binding(args.root, binding)
        if descriptor is None:
            raise NotFoundError(f"no pi-rpc descriptor for {binding['binding_id']}")
        result = control_request(descriptor, "abort", {}, timeout=args.timeout)
        _print_result({"binding_id": binding["binding_id"], "aborted": True, "result": result})
        return 0
    if args.command == "stop":
        descriptor = descriptor_for_binding(args.root, binding)
        if descriptor is None:
            raise NotFoundError(f"no pi-rpc descriptor for {binding['binding_id']}")
        result = control_request(descriptor, "stop", {}, timeout=args.timeout)
        _print_result({"binding_id": binding["binding_id"], "stopped": True, "result": result})
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
