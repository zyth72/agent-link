"""Pi child adapter: RPC prompt when a controller descriptor exists.

Dispatch policy (see ``docs/PI-RPC.md`` §5):

- descriptor present and valid: render the parent question with the shared
  ``build_child_prompt`` and deliver it through the controller's
  ``/v1/prompt`` (RPC ``prompt`` with ``streamingBehavior:"steer"``).  The
  parent records the RPC acceptance only; the model answer arrives later
  through ``answer_parent`` / ``/v1/answer``.  No inbox file is written.
- descriptor present but unusable (old protocol, identity mismatch, state not
  ready, transport/auth failure): fail explicitly; never fall back silently.
- descriptor absent: legacy inbox compatibility.  The returned detail says so
  explicitly because that transport does not wake a Pi RPC session.
"""
from __future__ import annotations

import json

from ...shared import append_inbox, build_child_prompt
from ..base import ChildAdapter
from .rpc import DEFAULT_CLIENT_TIMEOUT_SECONDS, control_request, descriptor_for_binding


class PiChildAdapter(ChildAdapter):
    framework = "pi"

    def capabilities(self):
        return {
            "child": "pi",
            "driver": "agent_link.hosts.pi.child:PiChildAdapter",
            "delivery_transport": "rpc_prompt",
            "delivery_bridge": (
                "<root>/pi-rpc/<binding_id>.json + loopback /v1/prompt "
                "(pi --mode rpc); inbox only when no descriptor exists"
            ),
            "wake_child": "rpc_stdin",
            "processed_confirmation": "answer_parent persisted state",
            "legacy_without_descriptor": "inbox_file_does_not_wake_rpc",
        }

    def deliver(self, root, binding, message):
        prompt = build_child_prompt(root, binding, message)
        descriptor = descriptor_for_binding(root, binding)
        if descriptor is None:
            detail = append_inbox(root, binding, message)
            return (
                f"{detail} (legacy inbox; no pi-rpc descriptor, this does not wake "
                "a Pi RPC session)"
            )
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
