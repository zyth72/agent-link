"""OpenCode parent notifications over the native prompt HTTP endpoint.

``notify()`` reads the frozen session/serve descriptors and injects the
rendered parent prompt into the bound OpenCode session with
``POST /api/session/<session_id>/prompt`` (``delivery:"queue"``, prompt ``id``
= Agent Link ``message_id``).  This replaces the former parent bridge file and
its plugin watcher; there is no file fallback.  Missing, unmanaged, stale or
mismatched descriptors and transport failures are explicit
``failed_or_unknown`` outcomes.
"""
from __future__ import annotations

from ..base import ParentAdapter
from .push import deliver


class OpenCodeParentAdapter(ParentAdapter):
    framework = "opencode"

    def capabilities(self):
        return {
            "host": "opencode",
            "driver": "agent_link.hosts.opencode.parent:OpenCodeParentAdapter",
            "notification_transport": "opencode_prompt_http",
            "receive_mode": "session_injection",
            "delivery": "queue",
            # No resume field is sent; C1/C4 observed the framework default
            # scheduling an idle session and processing a queued prompt after
            # the current turn (model end-to-end, slice 3c).
            "resume": "framework_default_schedules",
            "busy_behavior": "queue_after_turn",
            "wake_after_turn": "verified",
            "polling": False,
        }

    def notify(self, root, binding, message):
        return deliver(root, binding, message, role="parent")
