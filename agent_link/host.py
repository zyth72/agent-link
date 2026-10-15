"""Binding-scoped loopback host service.  Run with ``python -m agent_link.host``.

The parent process owns the binding, the host driver and the bearer token; the
child receives only a loopback endpoint plus token (the credentials file) and
may call ``report``, ``ask``, ``get-request`` and ``capabilities``.  It cannot
choose a parent session, create bindings or reply.  See
``docs/HOST-PROTOCOL.md`` for the full protocol and lifecycle.
"""
from __future__ import annotations

import argparse
from argparse import Namespace
import hmac
import importlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import signal
import sys
import threading
from typing import Any

from . import cli
from .errors import AgentLinkError, NotFoundError, ValidationError
from .hosts.base import load_adapters
from .registry import entry_from_row, write_entry
from .storage import Store

#: Requests larger than this are rejected before the body is read.
MAX_BODY_BYTES = 65536
#: An HTTP ask may not occupy a server thread longer than this.  This is a
#: resource bound of the loopback service, not a change to core ask semantics.
MAX_HTTP_ASK_TIMEOUT_SECONDS = 3600.0
#: Read timeout for request headers and body of one connection.  A stalled
#: client cannot pin a handler thread beyond this; the ask wait itself uses the
#: persisted deadline (bounded by MAX_HTTP_ASK_TIMEOUT_SECONDS).
REQUEST_READ_TIMEOUT_SECONDS = 10.0
#: Exact request field sets per operation.  Unknown or missing fields fail with
#: a JSON validation error instead of being silently ignored.
ALLOWED_FIELDS = {
    "/v1/report": {"message_id", "text"},
    "/v1/ask": {"request_id", "message_id", "question", "timeout"},
    "/v1/get-request": {"request_id"},
    "/v1/answer": {"request_id", "message_id", "answer"},
    "/v1/child-requests": set(),
    "/v1/get-parent-request": {"request_id"},
    "/v1/capabilities": set(),
}


class HostService(ThreadingHTTPServer):
    """Threaded loopback HTTP server scoped to exactly one binding and token."""

    daemon_threads = True
    request_queue_size = 16

    def __init__(self, root, binding_id, token, driver=None):
        self.root = str(root)
        self.binding_id = binding_id
        self.token = token
        if driver is None:
            adapter = load_adapters("codex-desktop").parent
            if adapter is None:  # pragma: no cover - registered adapter set
                raise ValidationError("codex-desktop has no parent adapter")
            driver = adapter
        self.driver = driver
        for method in ("capabilities", "notify"):
            if not callable(getattr(self.driver, method, None)):
                raise ValidationError(f"host driver must implement {method}()")
        # Re-entrant because do_POST holds it across prepare+dispatch while
        # dispatch() itself also takes it for direct callers.
        self.dispatch_lock = threading.RLock()
        super().__init__(("127.0.0.1", 0), Handler)

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"

    def dispatch(self, store: Store, message: dict) -> None:
        """Persist dispatching, call the host driver, record the outcome.

        The caller normally holds ``dispatch_lock`` (do_POST does, so a
        concurrent replay of the same message waits for the outcome instead of
        observing ``persisted`` and reporting a false failure).  Exceptions are
        recorded as ``failed_or_unknown`` and are never retried automatically.
        """
        binding = store.get_binding(self.binding_id)
        store.mark_dispatching(self.binding_id, message["message_id"])
        try:
            detail = self.driver.notify(store.root, binding, message)
        except Exception as error:  # noqa: BLE001 - external driver failure
            store.mark_delivery(
                self.binding_id,
                message["message_id"],
                "failed_or_unknown",
                f"{type(error).__name__}: {error}",
            )
        else:
            store.mark_delivery(self.binding_id, message["message_id"], "host_accepted", detail)


class Handler(BaseHTTPRequestHandler):
    # StreamRequestHandler applies this to the accepted socket, bounding reads
    # of the request line, headers and body.
    timeout = REQUEST_READ_TIMEOUT_SECONDS

    def log_message(self, *args):
        pass  # Credentials and message contents must never enter access logs.

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
            # The child disconnected while we were writing; the request state
            # stays persisted and can be read again with get-request/ask.
            pass
        finally:
            self.close_connection = True

    def send_error(self, code, message=None, explain=None):  # noqa: D102 - BaseHTTPRequestHandler hook
        # BaseHTTPRequestHandler would answer framework errors with an HTML
        # page; every host-service error is a JSON envelope instead.
        detail = message or explain or "request rejected"
        self.send_json(code, {"error": "http_error", "message": str(detail)})

    def do_GET(self):  # noqa: N802 - http.server naming
        self.send_json(405, {"error": "method_not_allowed", "message": "use POST"})

    def do_POST(self):  # noqa: N802 - http.server naming
        expected = "Bearer " + self.server.token
        if not hmac.compare_digest(self.headers.get("Authorization") or "", expected):
            self.send_json(401, {"error": "unauthorized", "message": "missing or invalid bearer token"})
            return
        store = None
        try:
            if self.path not in ALLOWED_FIELDS:
                raise NotFoundError(f"unknown operation {self.path!r}")
            data = self._read_json_body()
            if not isinstance(data, dict) or set(data) != ALLOWED_FIELDS[self.path]:
                raise ValidationError(
                    f"body fields must be exactly {sorted(ALLOWED_FIELDS[self.path])} for {self.path}"
                )
            if self.path == "/v1/capabilities":
                self.send_json(200, {"result": self.server.driver.capabilities(), "exit_code": 0})
                return
            store = Store(self.server.root)
            args = Namespace(binding=self.server.binding_id, **data)
            if self.path == "/v1/get-request":
                result, code = cli.cmd_get_request(store, args)
            elif self.path == "/v1/answer":
                result, code = cli.cmd_answer(store, args)
            elif self.path == "/v1/get-parent-request":
                result, code = cli.cmd_get_parent_request(store, args)
            elif self.path == "/v1/child-requests":
                args.pending = True
                result, code = cli.cmd_list_parent_requests(store, args)
            else:
                if self.path == "/v1/ask":
                    # Cap the HTTP wait before anything is persisted; the core
                    # CLI keeps its own (unbounded) semantics.
                    args.timeout = cli.parse_timeout(
                        args.timeout, max_seconds=MAX_HTTP_ASK_TIMEOUT_SECONDS
                    )
                # prepare+dispatch are serialized so a concurrent replay waits
                # for the delivery outcome and can never trigger a second
                # delivery for the same message_id.
                with self.server.dispatch_lock:
                    if self.path == "/v1/report":
                        prepared = store.prepare_report(args.binding, args.message_id, args.text)
                        message = {
                            "kind": "report",
                            "message_id": args.message_id,
                            "text": args.text,
                        }
                    else:
                        prepared = store.prepare_ask(
                            args.binding,
                            args.request_id,
                            args.message_id,
                            args.question,
                            args.timeout,
                        )
                        request = prepared["request"]
                        message = {
                            "kind": "ask",
                            "message_id": args.message_id,
                            "request_id": args.request_id,
                            "question": args.question,
                            "timeout_seconds": request["timeout_seconds"],
                            "deadline_epoch": request["deadline_epoch"],
                        }
                    if prepared["new"]:
                        self.server.dispatch(store, message)
                # The command handlers only query/wait on this replay; they
                # never dispatch again.
                handler = cli.cmd_report if self.path == "/v1/report" else cli.cmd_ask
                result, code = handler(store, args)
                result["idempotent"] = not prepared["new"]
            self.send_json(200, {"result": result, "exit_code": code})
        except TimeoutError:
            self.send_json(408, {"error": "request_timeout", "message": "request body read timed out"})
        except ConnectionError:
            pass  # client vanished; nothing can be delivered
        except AgentLinkError as error:
            self.send_json(
                _status_for(error),
                {"error": error.code, "message": str(error), "exit_code": error.exit_code},
            )
        except (ValueError, TypeError) as error:
            self.send_json(400, {"error": "validation", "message": str(error), "exit_code": 5})
        except Exception:  # noqa: BLE001 - keep every error response JSON
            self.send_json(500, {"error": "host_failure", "message": "internal host service failure"})
        finally:
            if store is not None:
                store.close()

    def _read_json_body(self) -> Any:
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
    """Map CLI error kinds onto HTTP status codes (documented in the protocol)."""
    if isinstance(error, NotFoundError):
        return 404
    if error.code == "conflict":
        return 409
    return 400


def write_credentials(target: Path, credentials: dict) -> None:
    """Create the child credentials file, refusing to overwrite an existing one.

    O_EXCL keeps a second host service (or any other file) from being replaced;
    on any failure the partial file created here is removed before re-raising.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(credentials, stream)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def remove_credentials_if_ours(target: Path, credentials: dict) -> None:
    """Delete the credentials file only while it still describes this service.

    If another process replaced the file, it is that process's file and must
    not be removed.  A read/parse failure is treated as "not ours".
    """
    try:
        current = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return
    if current == credentials:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass


def load_driver(spec: str):
    """Load a host driver from ``module:object`` (class/factory/instance).

    This is parent-side configuration only; message contents are never used to
    import code.  See docs/HOST-PROTOCOL.md for the HostDriver contract.
    """
    module_name, separator, attribute = spec.partition(":")
    if not separator or not module_name or not attribute:
        raise ValidationError(f"driver must be MODULE:OBJECT, got {spec!r}")
    try:
        module = importlib.import_module(module_name)
    except ImportError as error:
        raise ValidationError(f"cannot import driver module {module_name!r}: {error}")
    try:
        value = getattr(module, attribute)
    except AttributeError:
        raise ValidationError(f"driver module {module_name!r} has no attribute {attribute!r}")
    if isinstance(value, type):
        driver = value()
    elif callable(getattr(value, "notify", None)) and callable(getattr(value, "capabilities", None)):
        driver = value
    elif callable(value):
        driver = value()
    else:
        raise ValidationError(f"driver {spec!r} is not a HostDriver or zero-arg factory")
    for method in ("capabilities", "notify"):
        if not callable(getattr(driver, method, None)):
            raise ValidationError(f"driver {spec!r} does not implement {method}()")
    return driver


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m agent_link.host",
        description="Binding-scoped loopback host service for child Agent Link clients.",
    )
    parser.add_argument("--root", required=True, help="runtime directory (0700) holding the binding")
    parser.add_argument("--binding", required=True, help="binding_id served by this process")
    parser.add_argument(
        "--credentials",
        required=True,
        help="path for the child credentials JSON (created 0600, never overwritten)",
    )
    parser.add_argument(
        "--framework",
        default=None,
        help="registered parent adapter framework (default: codex-desktop)",
    )
    parser.add_argument(
        "--driver",
        default=None,
        help="custom host driver as MODULE:OBJECT (extension point; overrides --framework)",
    )
    args = parser.parse_args()

    if args.driver and args.framework:
        parser.error("--driver and --framework are mutually exclusive")

    try:
        if args.driver:
            driver = load_driver(args.driver)
        else:
            adapters = load_adapters(args.framework or "codex-desktop")
            if adapters.parent is None:
                parser.error(f"framework {args.framework or 'codex-desktop'!r} has no parent adapter")
            driver = adapters.parent
        store = Store(args.root)
        try:
            binding_row = store.get_binding(args.binding)
        finally:
            store.close()
    except AgentLinkError as error:
        parser.error(str(error))

    # Refresh the user-level registry entry so framework adapters can discover
    # this binding by session id (see agent_link/registry.py); a failure here
    # never blocks the service.
    try:
        write_entry(entry_from_row(args.root, binding_row))
    except OSError as error:
        print(f"agent-link: registry write failed: {error}", file=sys.stderr)

    service = HostService(args.root, args.binding, secrets.token_urlsafe(32), driver)
    target = Path(args.credentials)
    credentials = {
        "protocol": "agent-link/1",
        "endpoint": service.endpoint,
        "token": service.token,
        "binding_id": args.binding,
    }
    try:
        write_credentials(target, credentials)
    except OSError as error:
        # Do not leave a listening socket behind when the credentials file
        # cannot be created (for example because it already exists).
        service.server_close()
        parser.error(f"cannot create credentials file {target}: {error}")
    except BaseException:
        service.server_close()
        raise

    stopped = threading.Event()

    def request_stop(*_args):
        stopped.set()

    previous_handlers = {}
    for name in ("SIGTERM", "SIGINT"):
        if hasattr(signal, name):
            previous_handlers[name] = signal.signal(getattr(signal, name), request_stop)
    # serve_forever runs on a worker thread so SIGTERM/SIGINT can shut the
    # service down gracefully and remove its own credentials file.
    server_thread = threading.Thread(target=service.serve_forever, name="agent-link-host", daemon=True)
    server_thread.start()
    try:
        print(
            json.dumps(
                {"ready": True, "credentials": str(target), "endpoint": service.endpoint},
                ensure_ascii=False,
            ),
            flush=True,
        )
        stopped.wait()
    finally:
        service.shutdown()
        service.server_close()
        server_thread.join(timeout=5)
        remove_credentials_if_ours(target, credentials)
        for name, handler in previous_handlers.items():
            signal.signal(getattr(signal, name), handler)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
