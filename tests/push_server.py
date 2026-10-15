"""Mock session-owned push listener for no-model tests.

A tiny loopback HTTP server that validates the bearer token, records the
notification payloads it receives and acknowledges the bound session.  Tests
write a matching session descriptor with :func:`write_descriptor` so the
production client (``agent_link.hosts.push.deliver``) can reach it.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DEFAULT_TOKEN = "t" * 32
PROTOCOL = "agent-link-push/1"


class MockPushListener:
    """One fake recipient session listener."""

    def __init__(
        self,
        framework: str,
        session_id: str,
        *,
        token: str = DEFAULT_TOKEN,
        validate_token: bool = True,
        redirect_to: str | None = None,
        respond_status: int | None = None,
        respond_body: dict | None = None,
    ):
        self.framework = framework
        self.session_id = session_id
        self.token = token
        self.validate_token = validate_token
        self.redirect_to = redirect_to
        self.respond_status = respond_status
        self.respond_body = respond_body
        self.events: list[dict | None] = []
        self.requests: list[dict] = []
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
                expected = "Bearer " + outer.token
                if outer.validate_token and (self.headers.get("Authorization") or "") != expected:
                    self._send(403, {"error": "unauthorized"})
                    return
                if outer.redirect_to is not None:
                    self.send_response(302)
                    self.send_header("Location", outer.redirect_to)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                try:
                    outer.events.append(json.loads(body.decode("utf-8")))
                except ValueError:
                    outer.events.append(None)
                if outer.respond_status is not None:
                    self._send(
                        outer.respond_status,
                        outer.respond_body if outer.respond_body is not None else {},
                    )
                    return
                self._send(200, {"accepted": True, "session_id": outer.session_id})

            def _send(self, status: int, payload: dict):
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

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

    def descriptor(self, **overrides) -> dict:
        descriptor = {
            "protocol": PROTOCOL,
            "framework": self.framework,
            "session_id": self.session_id,
            "endpoint": self.endpoint,
            "token": self.token,
            "pid": 1,
        }
        descriptor.update(overrides)
        return descriptor

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def write_descriptor(sessions_dir, descriptor: dict) -> Path:
    """Write ``AGENT_LINK_SESSIONS/<framework>/<session_id>.json``."""
    directory = Path(sessions_dir) / descriptor["framework"]
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{descriptor['session_id']}.json"
    target.write_text(json.dumps(descriptor), encoding="utf-8")
    return target
