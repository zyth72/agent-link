"""Codex desktop parent-side notification adapter.

``notify()`` loads only the ``notifier_path`` recorded in the binding and calls
its ``bridge(payload)``.  Message content is passed to the notifier as data; it
is never imported or executed.
"""

from __future__ import annotations

import importlib.util
import json
import shlex
import sys
from pathlib import Path

from ...errors import DeliveryFailure
from ...shared import build_parent_prompt
from ..base import ParentAdapter

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


def dispatch_notification(root, binding: dict, message: dict) -> str:
    """Call the bound notifier's bridge(payload).  Returns an audit detail string."""
    bridge = _load_bridge(binding["notifier_path"])
    payload = {
        "thread_id": binding["parent_session_id"],
        "prompt": build_codex_parent_prompt(root, binding, message),
    }
    result = bridge(payload)
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
    calls its ``bridge(payload)``.  On this machine that notifier is the
    background-tasks ``notifier.py`` WSL bridge, which needs a desktop parent
    environment (``CODEX_APP_TOOLS_PIPE_PATH``); the host service must
    therefore be started by the parent session, not by the child.  A returned
    value only means the bridge accepted the call; whether the model processed
    it is proven by an actual parent reply, never by the bridge result.
    """

    framework = "codex-desktop"

    def capabilities(self):
        return {
            "host": "codex-desktop",
            "driver": "agent_link.hosts.codex.parent:CodexParentAdapter",
            "notification_transport": "desktop_bridge",
            "notification_bridge": "binding.notifier_path (background-tasks notifier.py)",
            "receive_mode": "host_injection",
            "busy_behavior": "unknown",
            "wake_after_turn": "unknown",
        }

    def build_prompt(self, root, binding, message):
        """Full Codex-parent prompt (shared renderer + Codex helper hints)."""
        return build_codex_parent_prompt(root, binding, message)

    def notify(self, root, binding, message):
        return dispatch_notification(root, binding, message)
