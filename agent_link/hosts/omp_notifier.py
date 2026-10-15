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
import sqlite3
from pathlib import Path

from agent_link.hosts.omp import BRIDGE_DIR_NAME, EVENT_VERSION, append_event, utc_now_iso
from agent_link.storage import DB_FILENAME


def _binding_identity(target: Path, binding_id: str) -> dict:
    """Best-effort structured identity fields from the binding record.

    The bridge file convention is ``<root>/omp-bridge/<binding>.jsonl``; when
    the layout matches, the binding row is read from the root's database.
    Enrichment never breaks delivery: any failure yields an empty dict.
    """
    try:
        if target.parent.name != BRIDGE_DIR_NAME:
            return {}
        db_path = target.parent.parent / DB_FILENAME
        if not db_path.is_file():
            return {}
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = connection.execute(
                "SELECT parent_framework, child_framework, child_session_id"
                " FROM bindings WHERE binding_id = ?",
                (binding_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return {}
        return {
            "parent_framework": row[0],
            "child_framework": row[1],
            "child_session_id": row[2],
        }
    except Exception:
        return {}


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
    target_path = Path(target)
    event = {
        "v": EVENT_VERSION,
        "ts": utc_now_iso(),
        "source": "notifier",
        "binding_id": binding,
        "thread_id": thread_id,
        "prompt": str(payload.get("prompt", "")),
        **_binding_identity(target_path, binding),
    }
    append_event(target_path, event)
    return f"omp_bridge:{target}"
