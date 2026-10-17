"""Codex desktop parent-side notification adapter.

``notify()`` loads only the ``notifier_path`` recorded in the binding and calls
its bridge with the freshest per-session connection environment (or the legacy
``bridge(payload)`` when no connection is registered).  Message content is
passed to the notifier as data; it is never imported or executed.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import shlex
import sys
from pathlib import Path

from ...errors import DeliveryFailure, NotificationNotSent
from ...shared import build_parent_prompt
from ..base import ParentAdapter
from . import connection

MODULE_NAME = "agent_link_bound_notifier"


def _load_bridge(notifier_path: str):
    path = Path(notifier_path)
    if not path.is_file():
        raise DeliveryFailure(f"bound notifier_path does not exist: {path}")
    spec = importlib.util.spec_from_file_location(MODULE_NAME, path)
    if spec is None or spec.loader is None:
        raise DeliveryFailure(f"cannot load bound notifier: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    bridge = getattr(module, "bridge", None)
    if not callable(bridge):
        raise DeliveryFailure(f"bound notifier {path} does not expose bridge(payload)")
    return bridge


def _bridge_accepts_env(bridge) -> bool:
    """Whether the bound bridge can accept an explicit per-call environment.

    The bundled background-tasks notifier exposes ``bridge(payload, env=None)``.
    Legacy stubs expose only ``bridge(payload)``.  Inspect the callable before
    invoking it so an unsupported signature is a proven pre-invocation failure
    instead of a TypeError from inside the bridge.
    """
    try:
        parameters = inspect.signature(bridge).parameters.values()
    except (TypeError, ValueError):
        return False
    for parameter in parameters:
        if parameter.kind == inspect.Parameter.VAR_KEYWORD:
            return True
        if parameter.name == "env" and parameter.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            return True
    return False


def _delivery_environment(descriptor: dict) -> dict:
    """Copy the current environment and overlay only registered transport keys."""
    env = dict(os.environ)
    for key in connection.TRANSPORT_ENV_ALLOWLIST:
        env.pop(key, None)
    env.update(descriptor["transport_env"])
    return env


def _codex_hint_block(root, binding: dict, message: dict) -> str:
    """Codex-desktop-only helper hints appended to the shared prompt."""
    session_arg = shlex.quote(binding["parent_session_id"])
    binding_arg = shlex.quote(binding["binding_id"])
    lines = [
        "Codex parent helper for this chat (discovery reads the user-level",
        "registry, then verifies the binding in its own database; pass --binding",
        "whenever more than one binding matches this session):",
        f"python3 -m agent_link.hosts.codex.cli --session-id {session_arg} list",
    ]
    if message["kind"] == "ask":
        request_arg = shlex.quote(message["request_id"])
        lines += [
            f"python3 -m agent_link.hosts.codex.cli --session-id {session_arg} "
            f"--binding {binding_arg} get-request --request-id {request_arg}",
            f"python3 -m agent_link.hosts.codex.cli --session-id {session_arg} "
            f"--binding {binding_arg} reply --request-id {request_arg} "
            f"--message-id <new_message_id> --answer '<answer text>'",
        ]
    if binding.get("child_framework") == "pi":
        lines += [
            "For a Pi RPC child, the helper can also drive the controller:",
            f"python3 -m agent_link.hosts.codex.cli --session-id {session_arg} "
            f"--binding {binding_arg} child-status",
            f"python3 -m agent_link.hosts.codex.cli --session-id {session_arg} "
            f"--binding {binding_arg} send-child --request-id <new_request_id> "
            "--message-id <new_message_id> --question '<question>' --timeout <seconds>",
        ]
    return "\n".join(lines)


def build_codex_parent_prompt(root, binding: dict, message: dict) -> str:
    """Codex desktop prompt: shared renderer plus Codex-only helper hints."""
    prompt = build_parent_prompt(root, binding, message)
    if binding.get("parent_framework") != "codex-desktop":
        return prompt
    return prompt + "\n" + _codex_hint_block(root, binding, message)


def _invoke_bridge(bridge, payload, **kwargs):
    # Once invocation begins, submission is uncertain even if a third-party
    # bridge raises our preflight exception type. Never make it replayable.
    try:
        return bridge(payload, **kwargs)
    except NotificationNotSent as error:
        raise DeliveryFailure("notifier raised after invocation; submission unknown") from error


def dispatch_notification(root, binding: dict, message: dict) -> str:
    """Call the bound notifier's bridge with the freshest session transport.

    Descriptor lookup happens on every notification, so a long-lived host
    started by an old app instance picks up a newly registered pipe without a
    restart.  The process environment is never mutated: only a copy is passed
    to an env-aware bridge.  A corrupt descriptor or an env-unaware bridge with
    a registered connection fails closed instead of falling back to a stale
    inherited pipe.  Without a descriptor the legacy ``bridge(payload)`` call
    keeps existing custom notifiers and inherited desktop environments working.
    """
    bridge = _load_bridge(binding["notifier_path"])
    try:
        descriptor = connection.read_descriptor(binding["parent_session_id"])
    except connection.ConnectionDescriptorError as error:
        raise NotificationNotSent(
            f"registered Codex connection is unusable: {error}",
            reason="descriptor_invalid",
        )
    payload = {
        "thread_id": binding["parent_session_id"],
        "prompt": build_codex_parent_prompt(root, binding, message),
    }
    env_aware = _bridge_accepts_env(bridge)
    if descriptor is None:
        # An env-aware desktop bridge with neither a registered descriptor nor
        # an inherited pipe has no environment it could use.  This is known
        # before invocation.  Legacy bridge(payload) notifiers stay supported
        # even without a pipe because they may not use the desktop transport.
        if env_aware and not os.environ.get(connection.PIPE_ENV):
            raise NotificationNotSent(
                "no registered Codex connection and no inherited"
                f" {connection.PIPE_ENV}; the notification was not submitted",
                reason="missing_pipe_env",
            )
        result = _invoke_bridge(bridge, payload)
    else:
        if not env_aware:
            raise NotificationNotSent(
                "a Codex connection is registered for this session, but the bound notifier"
                " does not accept bridge(payload, env=...); refusing to fall back to the"
                " stale inherited environment",
                reason="bridge_env_unsupported",
            )
        result = _invoke_bridge(bridge, payload, env=_delivery_environment(descriptor))
    if result is None:
        detail = "bridge returned None"
    elif isinstance(result, (dict, list)):
        detail = json.dumps(result, ensure_ascii=False)
    else:
        detail = str(result)
    return detail[:2000]


class CodexParentAdapter(ParentAdapter):
    """Codex desktop parent adapter.

    ``notify()`` loads only the ``notifier_path`` recorded in the binding and
    calls its bridge with the freshest per-session connection descriptor.  On
    this machine that notifier is the background-tasks ``notifier.py`` WSL
    bridge, which needs a desktop parent environment
    (``CODEX_APP_TOOLS_PIPE_PATH``).  A returned value only means the bridge
    accepted the call; registration and acceptance are not health or model
    processing evidence, which require a real parent reply.
    """

    framework = "codex-desktop"

    def capabilities(self):
        return {
            "host": "codex-desktop",
            "driver": "agent_link.hosts.codex.parent:CodexParentAdapter",
            "notification_transport": "desktop_bridge",
            "notification_bridge": "binding.notifier_path (background-tasks notifier.py)",
            "connection_registration": "session_start_hook_or_manual_per_session",
            "connection_liveness": "unknown_registration_is_not_health",
            "receive_mode": "host_injection",
            "busy_behavior": "unknown",
            "wake_after_turn": "unknown",
        }

    def build_prompt(self, root, binding, message):
        """Full Codex-parent prompt (shared renderer + Codex helper hints)."""
        return build_codex_parent_prompt(root, binding, message)

    def notify(self, root, binding, message):
        return dispatch_notification(root, binding, message)
