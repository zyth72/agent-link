"""OMP parent-side notification adapter.

``notify()`` appends one JSONL event per notification to
``<root>/omp-bridge/<binding_id>.jsonl`` (the shared parent bridge file).  The
parent OMP session runs ``extensions/omp/agent-link.ts`` with
``AGENT_LINK_BRIDGE_FILE`` (the bridge file) and ``AGENT_LINK_BINDING`` (the
expected binding_id); that extension is the single consumer of the file and
injects validated events into the session.

Each event is appended under an ``flock`` and fsynced before the service
records ``host_accepted``, so an accepted event is readable by the watcher.
A returned transport string only means the event reached the bridge file;
whether the model processed it is never asserted here.
"""
from __future__ import annotations

from ...errors import DeliveryFailure
from ...shared import append_parent_bridge
from ..base import ParentAdapter


class OmpParentAdapter(ParentAdapter):
    """OMP parent adapter: one shared bridge file per binding.

    ``notify()`` never fails silently: any append error is raised as
    ``DeliveryFailure`` so the service records ``failed_or_unknown`` instead of
    a false ``host_accepted``.  Busy/idle session semantics and whether an
    injection wakes an idle session are unknown until measured.
    """

    framework = "omp"

    def capabilities(self):
        return {
            "host": "omp",
            "driver": "agent_link.hosts.omp.parent:OmpParentAdapter",
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
        try:
            return append_parent_bridge(root, binding, message)
        except OSError as error:
            raise DeliveryFailure(
                f"cannot append OMP bridge event for {binding['binding_id']}: {error}"
            ) from error
