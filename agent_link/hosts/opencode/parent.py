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
            # No resume field is sent; scheduling follows the framework
            # default and busy/wake behavior is [推断], not verified.
            "resume": "framework_default_unknown",
            "busy_behavior": "unknown",
            "wake_after_turn": "unknown",
            "polling": False,
        }

    def notify(self, root, binding, message):
        return deliver(root, binding, message, role="parent")
