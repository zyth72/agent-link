"""Typed errors with stable exit codes for the agent_link CLI."""

from __future__ import annotations


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
