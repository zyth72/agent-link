"""OpenCode parent-side notification adapter.

The transport is the shared parent bridge file: ``notify()`` appends one JSONL
event per notification to ``<root>/omp-bridge/<binding_id>.jsonl``.  The
OpenCode session runs ``extensions/opencode/agent-link.ts`` (OpenCode v2 plugin
API), which is the single consumer of the file and injects validated events
into the tracked session with ``ctx.session.prompt``.

Only the capability labels differ from the OMP adapter; append semantics,
locking and failure mapping (``DeliveryFailure`` -> ``failed_or_unknown``) are
identical because both call the neutral shared bridge helpers.
"""
from __future__ import annotations

from ...errors import DeliveryFailure
from ...shared import append_parent_bridge
from ..base import ParentAdapter


class OpenCodeParentAdapter(ParentAdapter):
    framework = "opencode"

    def capabilities(self):
        return {
            "host": "opencode",
            "driver": "agent_link.hosts.opencode.parent:OpenCodeParentAdapter",
            "notification_transport": "session_bridge_file",
            "notification_bridge": (
                "<root>/omp-bridge/<binding_id>.jsonl + AGENT_LINK_BRIDGE_FILE watcher "
                "(extensions/opencode/agent-link.ts)"
            ),
            "receive_mode": "session_injection",
            "busy_behavior": "unknown",
            "wake_after_turn": "unknown",
        }

    def notify(self, root, binding, message):
        try:
            return append_parent_bridge(root, binding, message)
        except OSError as error:
            raise DeliveryFailure(
                f"cannot append OpenCode bridge event for {binding['binding_id']}: {error}"
            ) from error
