"""OMP child adapter: legacy parent -> child inbox transport.

The parent CLI (``ask-child``/``send-child``) appends one JSONL event per
parent-initiated request to ``<root>/inbox/<binding_id>.jsonl``; the OMP child
session's extension watches that file and injects validated events.  The
inbox is the wake-up transport only: request/answer state lives in SQLite, and
an appended event only proves the event is readable (``host_accepted``).
"""
from __future__ import annotations

from ...shared import append_inbox
from ..base import ChildAdapter


class OmpChildAdapter(ChildAdapter):
    framework = "omp"

    def capabilities(self):
        return {
            "child": "omp",
            "driver": "agent_link.hosts.omp.child:OmpChildAdapter",
            "delivery_transport": "inbox_file",
            "delivery_bridge": "<root>/inbox/<binding_id>.jsonl + child inbox watcher",
            "wake_child": "watcher_polling",
            "processed_confirmation": "answer_parent persisted state",
        }

    def deliver(self, root, binding, message):
        return append_inbox(root, binding, message)
