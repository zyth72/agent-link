"""OMP parent-side notification driver.

``notify()`` appends one JSONL event per notification to
``<root>/omp-bridge/<binding_id>.jsonl``.  The parent OMP session runs
``extensions/omp/agent-link.ts`` with ``AGENT_LINK_BRIDGE_FILE`` (the bridge
file) and ``AGENT_LINK_BINDING`` (the expected binding_id); that extension is
the single consumer of the file and injects validated events into the session.

Each event is appended under an ``flock`` and fsynced before the service
records ``host_accepted``, so an accepted event is readable by the watcher.
A returned transport string only means the event reached the bridge file;
whether the model processed it is never asserted here.
"""
from __future__ import annotations

import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from ..errors import DeliveryFailure
from .codex import build_prompt

BRIDGE_DIR_NAME = "omp-bridge"
EVENT_VERSION = 1


def bridge_path(root, binding_id) -> Path:
    return Path(root) / BRIDGE_DIR_NAME / f"{binding_id}.jsonl"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def append_event(target: Path, event: dict) -> None:
    """Append one JSONL event, atomically and durably, in a single ``flock``.

    The file is created 0600 inside its parent directory.  POSIX only: the
    O_APPEND + flock pair is what keeps concurrent writers from interleaving.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        view = memoryview(line)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("write returned 0 bytes")
            view = view[written:]
        os.fsync(fd)
    finally:
        # Closing the descriptor releases the lock.
        os.close(fd)


class OmpHost:
    """OMP driver: one bridge file per binding, consumed by the session.

    ``notify()`` never fails silently: any append error is raised as
    ``DeliveryFailure`` so the service records ``failed_or_unknown`` instead of
    a false ``host_accepted``.  Busy/idle session semantics and whether an
    injection wakes an idle session are unknown until measured.
    """

    name = "omp"

    def capabilities(self):
        return {
            "host": "omp",
            "driver": "agent_link.hosts.omp:OmpHost",
            "notification_transport": "session_bridge_file",
            "notification_bridge": (
                "<root>/omp-bridge/<binding_id>.jsonl + AGENT_LINK_BRIDGE_FILE watcher "
                "(extensions/omp/agent-link.ts)"
            ),
            "receive_mode": "session_injection",
            "busy_behavior": "unknown",
            "wake_after_turn": "unknown",
        }

    def notify(self, root, binding, message):
        target = bridge_path(root, binding["binding_id"])
        event = {
            "v": EVENT_VERSION,
            "ts": utc_now_iso(),
            "source": "service",
            "binding_id": binding["binding_id"],
            "thread_id": binding["parent_session_id"],
            "kind": message["kind"],
            "message_id": message["message_id"],
            "prompt": build_prompt(root, binding, message),
        }
        if message["kind"] == "ask":
            event["request_id"] = message["request_id"]
        try:
            append_event(target, event)
        except OSError as error:
            raise DeliveryFailure(f"cannot append OMP bridge event to {target}: {error}") from error
        return f"omp_bridge:{target}#{message['message_id']}"
