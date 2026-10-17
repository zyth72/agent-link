#!/usr/bin/env python3
"""Codex SessionStart hook: register this session's current desktop pipe env.

Codex invokes the hook with JSON on stdin containing at least
``hook_event_name``, ``session_id`` and ``source``.  The hook receives the
current inherited environment and, when private desktop transport variables
are available, calls the same registration routine as the manual helper.  No
model call is involved.

The hook must never break session startup.  It therefore writes diagnostics to
stderr and exits 0 even when registration is skipped or rejected.  It never
prints a pipe value, never mutates an existing descriptor when the current pipe
env is absent, and never claims transport/process health.  Whether a real Codex
lifecycle passes the private pipe env to hooks is not guaranteed by the public
hook documentation and remains unknown until verified on the actual app.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys


def _plugin_root() -> Path:
    """Locate the source/installed plugin root without depending on cwd."""
    return Path(__file__).resolve().parents[1]


def _diagnostic(message: str) -> None:
    print(f"agent-link SessionStart: {message}", file=sys.stderr)


def main() -> int:
    root = _plugin_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        from agent_link.hosts.codex import connection
    except Exception as error:  # noqa: BLE001 - a hook must not break startup
        _diagnostic(f"cannot import Agent Link connection module: {type(error).__name__}: {error}")
        return 0
    try:
        raw = sys.stdin.read()
    except OSError as error:
        _diagnostic(f"cannot read hook stdin: {error}")
        return 0
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as error:
        _diagnostic(f"invalid hook JSON: {error}")
        return 0
    if not isinstance(payload, dict):
        _diagnostic("hook payload is not a JSON object")
        return 0
    event = payload.get("hook_event_name")
    if event != "SessionStart":
        _diagnostic(f"ignored hook_event_name={event!r}")
        return 0
    session_id = payload.get("session_id")
    source = payload.get("source")
    try:
        result = connection.maybe_register_connection(session_id)
    except Exception as error:  # noqa: BLE001 - typed registration failure
        _diagnostic(
            f"connection not registered for source={source!r}: {type(error).__name__}: {error}"
        )
        return 0
    if not result.get("registered") and result.get("reason") == "missing_pipe_env":
        _diagnostic(
            "connection not registered: CODEX_APP_TOOLS_PIPE_PATH is absent;"
            " existing descriptor left unchanged; hook transport compatibility unknown"
        )
        return 0
    if result.get("idempotent"):
        _diagnostic(
            f"connection already current for source={source!r}"
            f" (generation {result.get('connection_generation')})"
        )
    else:
        _diagnostic(
            f"connection registered for source={source!r}"
            f" (generation {result.get('connection_generation')});"
            " registration is not transport liveness"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
