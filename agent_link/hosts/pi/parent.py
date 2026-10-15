"""Pi interactive parent-side notification adapter.

Pi's parent role consumes the shared parent bridge file
(``<root>/omp-bridge/<binding_id>.jsonl``) through
``extensions/pi/agent-link.ts``: an idle session gets a plain
``sendUserMessage`` (starting a turn) and a busy session queues the event with
``deliverAs:"followUp"``.  The adapter only appends the event under the shared
lock+fsync helper; whether the model processed it is never asserted here.
"""
from __future__ import annotations

from ...errors import DeliveryFailure
from ...shared import append_parent_bridge
from ..base import ParentAdapter


class PiParentAdapter(ParentAdapter):
    framework = "pi"

    def capabilities(self):
        return {
            "host": "pi",
            "driver": "agent_link.hosts.pi.parent:PiParentAdapter",
            "notification_transport": "session_bridge_file",
            "notification_bridge": (
                "<root>/omp-bridge/<binding_id>.jsonl + AGENT_LINK_BRIDGE_FILE watcher "
                "(extensions/pi/agent-link.ts)"
            ),
            "receive_mode": "session_injection",
            "busy_behavior": "followup_queue",
            "wake_after_turn": "unknown",
        }

    def notify(self, root, binding, message):
        try:
            return append_parent_bridge(root, binding, message)
        except OSError as error:
            raise DeliveryFailure(
                f"cannot append Pi bridge event for {binding['binding_id']}: {error}"
            ) from error
