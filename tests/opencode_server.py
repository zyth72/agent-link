"""Fake OpenCode serve for no-model prompt-delivery tests.

Implements the slice-1 surface of the de-polling migration against the real
OpenCode v2 shape: ``POST /api/session/<session_id>/prompt`` with HTTP Basic
auth (``opencode:<password>``), the ``Session.Inbox.User`` response, per-id
idempotency (first admission wins), the ``POST .../interrupt`` route with its
``SessionInterruptResponse`` and configurable status/redirect/delay for
failure mapping.  No real ``opencode`` process, plugin or model is involved.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROTOCOL = "agent-link-opencode-push/1"
DEFAULT_PASSWORD = "p" * 32


class MockOpenCodeServe:
    """One fake integration-managed serve bound to a single target session."""

    def __init__(
        self,
        session_id: str,
        *,
        password: str = DEFAULT_PASSWORD,
        validate_auth: bool = True,
        redirect_to: str | None = None,
        respond_status: int | None = None,
        respond_body: dict | None = None,
        delay_seconds: float = 0.0,
        interrupt_result: bool = False,
    ):
        self.session_id = session_id
        self.password = password
        self.validate_auth = validate_auth
        self.redirect_to = redirect_to
        self.respond_status = respond_status
        self.respond_body = respond_body
        self.delay_seconds = delay_seconds
        self.interrupt_result = interrupt_result
        self.requests: list[dict] = []
        self.prompts: list[dict] = []
        self.interrupts: list[dict] = []
        self.responses: list[dict] = []
        self.admissions: dict[str, dict] = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # noqa: D102 - silence access logs
                pass

            def do_POST(self):  # noqa: N802 - http.server naming
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                outer.requests.append(
                    {
                        "path": self.path,
                        "authorization": self.headers.get("Authorization"),
                        "body": body,
                    }
                )
                if outer.delay_seconds:
                    time.sleep(outer.delay_seconds)
                expected = "Basic " + base64.b64encode(
                    ("opencode:" + outer.password).encode("utf-8")
                ).decode("ascii")
                if outer.validate_auth and (self.headers.get("Authorization") or "") != expected:
                    self._send(401, {"_tag": "UnauthorizedError"})
                    return
                if outer.redirect_to is not None:
                    self.send_response(302)
                    self.send_header("Location", outer.redirect_to)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path == f"/api/session/{outer.session_id}/interrupt":
                    # Native interrupt route: no request body, and the
                    # response is the SessionInterruptResponse (or the
                    # configured failure for mapping tests).
                    outer.interrupts.append({"path": self.path, "body": body})
                    if outer.respond_status is not None:
                        self._send(
                            outer.respond_status,
                            outer.respond_body if outer.respond_body is not None else {},
                        )
                        return
                    self._send(200, {"data": {"interrupted": outer.interrupt_result}})
                    return
                try:
                    payload = json.loads(body.decode("utf-8"))
                    if not isinstance(payload, dict):
                        raise ValueError("not an object")
                except ValueError:
                    payload = None
                if self.path != f"/api/session/{outer.session_id}/prompt" or payload is None:
                    self._send(404, {"_tag": "SessionNotFoundError"})
                    return
                if outer.respond_status is not None:
                    self._send(
                        outer.respond_status,
                        outer.respond_body if outer.respond_body is not None else {},
                    )
                    return
                outer.prompts.append(payload)
                prompt_id = payload.get("id")
                admission = outer.admissions.get(prompt_id)
                if admission is None:
                    admission = {
                        "id": prompt_id,
                        "sessionID": outer.session_id,
                        "time": "2026-01-01T00:00:00.000Z",
                        "type": "user",
                        "payload": {"text": payload.get("text", "")},
                        "delivery": payload.get("delivery", "steer"),
                    }
                    outer.admissions[prompt_id] = admission
                outer.responses.append(admission)
                # OpenCode v2 wraps every response in {"data": ...}.
                self._send(200, {"data": admission})

            def _send(self, status: int, payload: dict):
                data = json.dumps(payload).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    # The client gave up (timeout test); the response was recorded.
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return self.server.server_port

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    # -- descriptor builders ------------------------------------------------
    def session_descriptor(self, **overrides) -> dict:
        descriptor = {
            "protocol": PROTOCOL,
            "framework": "opencode",
            "session_id": self.session_id,
            "endpoint": self.endpoint,
            "pid": os.getpid(),
            "serve_id": "serve-1",
            "managed": True,
            "credential_kind": "server_descriptor",
        }
        descriptor.update(overrides)
        return descriptor

    def server_descriptor(self, **overrides) -> dict:
        descriptor = {
            "protocol": PROTOCOL,
            "framework": "opencode",
            "serve_id": "serve-1",
            "endpoint": self.endpoint,
            "pid": os.getpid(),
            "managed": True,
            "credential_kind": "inline_password",
            "password": self.password,
        }
        descriptor.update(overrides)
        return descriptor


def _write_private(target: Path, payload: dict, mode: int = 0o600) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(target.parent, 0o700)
    target.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(target, mode)
    return target


def write_session_descriptor(sessions_dir, descriptor: dict, *, mode: int = 0o600) -> Path:
    """Write ``AGENT_LINK_SESSIONS/opencode/<session_id>.json`` with 0600."""
    target = Path(sessions_dir) / "opencode" / f"{descriptor['session_id']}.json"
    return _write_private(target, descriptor, mode)


def write_server_descriptor(sessions_dir, descriptor: dict, *, mode: int = 0o600) -> Path:
    """Write ``AGENT_LINK_SESSIONS/opencode/servers/<serve_id>.json`` with 0600."""
    target = Path(sessions_dir) / "opencode" / "servers" / f"{descriptor['serve_id']}.json"
    return _write_private(target, descriptor, mode)


def write_descriptor_pair(sessions_dir, serve: MockOpenCodeServe, **overrides) -> tuple[Path, Path]:
    """Write a matching session + server descriptor pair for one fake serve."""
    session_overrides = overrides.get("session", {})
    server_overrides = overrides.get("server", {})
    session_path = write_session_descriptor(
        sessions_dir, serve.session_descriptor(**session_overrides)
    )
    server_path = write_server_descriptor(
        sessions_dir, serve.server_descriptor(**server_overrides)
    )
    return session_path, server_path
