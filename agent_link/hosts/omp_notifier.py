"""Legacy CLI notifier bridge for OMP sessions.

An OMP parent can bind a child through the plain CLI (no host service) by
pointing ``notifier_path`` at this file.  The environment of the parent process
running the CLI must provide ``AGENT_LINK_BRIDGE_FILE`` (the bridge JSONL that
``extensions/omp/agent-link.ts`` polls) and ``AGENT_LINK_BINDING`` (the
binding_id that extension expects).

The Codex driver loads this file by path with ``spec_from_file_location``, so
it uses only the standard library plus absolute imports of the installed
``agent_link`` package -- never relative imports.
"""
from __future__ import annotations

import os
from pathlib import Path

from agent_link.hosts.omp import EVENT_VERSION, append_event, utc_now_iso


def bridge(payload: dict) -> str:
    target = os.environ.get("AGENT_LINK_BRIDGE_FILE")
    if not target:
        raise RuntimeError("AGENT_LINK_BRIDGE_FILE is not set; cannot deliver to an OMP session")
    binding = os.environ.get("AGENT_LINK_BINDING")
    if not binding:
        raise RuntimeError("AGENT_LINK_BINDING is not set; cannot deliver to an OMP session")
    thread_id = payload.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        raise RuntimeError("payload.thread_id must be a non-empty string")
    event = {
        "v": EVENT_VERSION,
        "ts": utc_now_iso(),
        "source": "notifier",
        "binding_id": binding,
        "thread_id": thread_id,
        "prompt": str(payload.get("prompt", "")),
    }
    append_event(Path(target), event)
    return f"omp_bridge:{target}"
