"""Pi child adapter: RPC prompt when a controller descriptor exists.

Dispatch policy (see ``docs/PI-RPC.md`` §5):

- descriptor present and valid: render the parent question with the shared
  ``build_child_prompt`` and deliver it through the controller's
  ``/v1/prompt`` (RPC ``prompt`` with ``streamingBehavior:"steer"``).  The
  parent records the RPC acceptance only; the model answer arrives later
  through ``answer_parent`` / ``/v1/answer``.  No inbox file is written.
- descriptor present but unusable (old protocol, identity mismatch, state not
  ready, transport/auth failure): fail explicitly; never fall back silently.
- descriptor absent: deliver to the interactive Pi session's authenticated
  push listener. Missing/stale listeners fail; never append a file inbox.
"""
from __future__ import annotations

import json

from ...shared import build_child_prompt
from ..push import deliver as push_deliver
from ..base import ChildAdapter


class PiChildAdapter(ChildAdapter):
    framework = "pi"

    def capabilities(self):
        return {
            "child": "pi",
            "driver": "agent_link.hosts.pi.child:PiChildAdapter",
            "delivery_transport": "rpc_or_session_push",
            "delivery_bridge": (
                "<root>/pi-rpc/<binding_id>.json + loopback /v1/prompt "
                "(pi --mode rpc); otherwise the session push listener"
            ),
            "wake_child": "native_injection",
            "polling": False,
            "processed_confirmation": "answer_parent persisted state",
        }

    def deliver(self, root, binding, message):
        from .rpc import DEFAULT_CLIENT_TIMEOUT_SECONDS, control_request, descriptor_for_binding
        prompt = build_child_prompt(root, binding, message)
        descriptor = descriptor_for_binding(root, binding)
        if descriptor is None:
            return push_deliver(root, binding, message, role="child")
        result = control_request(
            descriptor,
            "prompt",
            {"text": prompt},
            timeout=DEFAULT_CLIENT_TIMEOUT_SECONDS,
        )
        return (
            f"pi-rpc:{descriptor['endpoint']}#{message['message_id']} "
            f"{json.dumps(result, ensure_ascii=False)[:1000]}"
        )
