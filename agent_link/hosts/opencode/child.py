"""OpenCode child delivery over the native prompt HTTP endpoint.

``deliver()`` pushes the rendered parent question into the bound OpenCode
session with ``POST /api/session/<session_id>/prompt`` instead of appending to
the legacy child inbox file.  It uses the same frozen descriptor contract and
failure mapping as the parent adapter; no file fallback exists.  Whether a
busy child accepts the queued prompt and whether an idle child wakes are
unverified, so the capability labels stay ``unknown``.
"""
from __future__ import annotations

from ..base import ChildAdapter
from .push import deliver


class OpenCodeChildAdapter(ChildAdapter):
    framework = "opencode"

    def capabilities(self):
        return {
            "child": "opencode",
            "driver": "agent_link.hosts.opencode.child:OpenCodeChildAdapter",
            "delivery_transport": "opencode_prompt_http",
            "delivery": "queue",
            "resume": "framework_default_schedules",
            "wake_child": "native_injection",
            "busy_behavior": "queue_after_turn",
            "polling": False,
            "processed_confirmation": "answer_parent persisted state",
        }

    def deliver(self, root, binding, message):
        return deliver(root, binding, message, role="child")
