"""Parent-side notification adapter.

Loads only the notifier.py path recorded in the binding.  Message content is
passed to the notifier as data; it is never imported or executed.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from ..errors import DeliveryFailure

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


def build_prompt(root, binding: dict, message: dict) -> str:
    kind = message["kind"]
    lines = [
        f"[agent-link:{kind}]",
        f"binding_id: {binding['binding_id']}",
        f"run_id: {binding['run_id']}",
        f"native_task_id: {binding['native_task_id'] or 'unknown'}",
        f"child: {binding['child_framework']}/{binding['child_session_id']}",
        f"parent: {binding['parent_framework']}/{binding['parent_session_id']}",
        f"message_id: {message['message_id']}",
    ]
    if kind == "report":
        lines += ["", "progress:", message["text"]]
    else:
        lines += [
            f"request_id: {message['request_id']}",
            f"timeout_seconds: {message['timeout_seconds']}",
            f"deadline_epoch: {message['deadline_epoch']:.3f}",
            f"question: {message['question']}",
            "",
            "The child process is waiting for a reply.",
            f"python3 -m agent_link --root {Path(root).resolve()} reply "
            f"--binding {binding['binding_id']} --request-id {message['request_id']} "
            f"--message-id <new_message_id> --answer '<answer text>'",
        ]
    return "\n".join(lines)


def dispatch_notification(root, binding: dict, message: dict) -> str:
    """Call the bound notifier's bridge(payload).  Returns an audit detail string."""
    bridge = _load_bridge(binding["notifier_path"])
    payload = {
        "thread_id": binding["parent_session_id"],
        "prompt": build_prompt(root, binding, message),
    }
    result = bridge(payload)
    if result is None:
        detail = "bridge returned None"
    elif isinstance(result, (dict, list)):
        detail = json.dumps(result, ensure_ascii=False)
    else:
        detail = str(result)
    return detail[:2000]


class CodexHost:
    """Codex desktop driver.

    ``notify()`` loads only the ``notifier_path`` recorded in the binding and
    calls its ``bridge(payload)``.  On this machine that notifier is the
    background-tasks ``notifier.py`` WSL bridge, which needs a desktop parent
    environment (``CODEX_APP_TOOLS_PIPE_PATH``); the service must therefore be
    started by the parent session, not by the child.  A returned value only
    means the bridge accepted the call; whether the model processed it is
    proven by an actual parent reply, never by the bridge result.
    """

    name = "codex-desktop"

    def capabilities(self):
        return {
            "host": "codex-desktop",
            "driver": "agent_link.hosts.codex:CodexHost",
            "notification_transport": "desktop_bridge",
            "notification_bridge": "binding.notifier_path (background-tasks notifier.py)",
            "receive_mode": "host_injection",
            "busy_behavior": "unknown",
            "wake_after_turn": "unknown",
        }

    def notify(self, root, binding, message):
        return dispatch_notification(root, binding, message)
