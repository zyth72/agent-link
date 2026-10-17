"""Typed errors with stable exit codes for the agent_link CLI."""

from __future__ import annotations

import json

DELIVERY_DETAIL_PROTOCOL = "agent-link-delivery/1"


class AgentLinkError(Exception):
    code = "error"
    exit_code = 1

    def __init__(self, message: str, **details):
        super().__init__(message)
        self.details = details


class ValidationError(AgentLinkError):
    code = "validation"
    exit_code = 5


class NotFoundError(AgentLinkError):
    code = "not_found"
    exit_code = 6


class ConflictError(AgentLinkError):
    code = "conflict"
    exit_code = 4


class RequestTimeout(AgentLinkError):
    code = "request_timeout"
    exit_code = 3


class DeliveryFailure(AgentLinkError):
    code = "delivery_failed"
    exit_code = 7


class NotificationNotSent(DeliveryFailure):
    """A notification failed before the bound bridge was invoked.

    This is intentionally narrower than "delivery failed": it is only raised
    for failures proven before the external call (invalid/missing connection
    descriptor, missing transport value, or a bridge that cannot accept the
    per-call environment).  Errors raised by the bridge itself, including
    ENOENT/timeouts after invocation, remain unknown and are never classified
    by matching error strings.
    """

    code = "notification_not_sent"

    def __init__(self, message: str, *, reason: str, **details):
        super().__init__(message, reason=reason, **details)
        self.reason = reason

    def delivery_detail(self) -> str:
        return json.dumps(
            {
                "protocol": DELIVERY_DETAIL_PROTOCOL,
                "certainty": "not_sent",
                "reason": self.reason,
                "error": str(self),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
