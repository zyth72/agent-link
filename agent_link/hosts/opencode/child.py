"""OpenCode child adapter: legacy parent -> child inbox transport.

Identical inbox semantics to the OMP child adapter; only the capability labels
differ.  The OpenCode extension's child inbox watcher injects the events.
"""
from __future__ import annotations

from ...shared import append_inbox
from ..base import ChildAdapter


class OpenCodeChildAdapter(ChildAdapter):
    framework = "opencode"

    def capabilities(self):
        return {
            "child": "opencode",
            "driver": "agent_link.hosts.opencode.child:OpenCodeChildAdapter",
            "delivery_transport": "inbox_file",
            "delivery_bridge": "<root>/inbox/<binding_id>.jsonl + child inbox watcher",
            "wake_child": "watcher_polling",
            "processed_confirmation": "answer_parent persisted state",
        }

    def deliver(self, root, binding, message):
        return append_inbox(root, binding, message)
