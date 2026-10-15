"""Parent -> child inbox bridge: ``<root>/inbox/<binding_id>.jsonl``.

The parent CLI (``ask-child``) appends one JSONL event per parent-initiated
request; a child session's framework adapter polls the file and injects
validated events into the child session.  The file is the wake-up transport
only: request/answer state lives in the SQLite database, and an appended event
only proves the event is readable by the child watcher (``host_accepted``),
never that the child processed it.

Writes go through :func:`append_event` (flock + fsync), so any process on this
machine can append safely and an accepted event is durable before the CLI
records ``host_accepted``.
"""

from __future__ import annotations

from pathlib import Path

from .omp import EVENT_VERSION, append_event, utc_now_iso

INBOX_DIR_NAME = "inbox"


def inbox_path(root, binding_id) -> Path:
    return Path(root) / INBOX_DIR_NAME / f"{binding_id}.jsonl"


def build_child_prompt(root, binding: dict, message: dict) -> str:
    lines = [
        "[agent-link:parent-ask]",
        f"binding_id: {binding['binding_id']}",
        f"run_id: {binding['run_id']}",
        f"parent: {binding['parent_framework']}/{binding['parent_session_id']}",
        f"child: {binding['child_framework']}/{binding['child_session_id']}",
        f"message_id: {message['message_id']}",
        f"request_id: {message['request_id']}",
        f"timeout_seconds: {message['timeout_seconds']}",
        f"deadline_epoch: {message['deadline_epoch']}",
        f"question: {message['question']}",
        "",
        "The parent agent is waiting for an answer.",
        "If this session provides the answer_parent tool, use it; otherwise run:",
        (
            f"python3 -m agent_link --root {root} answer --binding {binding['binding_id']}"
            f" --request-id {message['request_id']} --message-id <new_message_id>"
            " --answer '<answer text>'"
        ),
    ]
    return "\n".join(lines)


def build_inbox_event(root, binding: dict, message: dict) -> dict:
    event = {
        "v": EVENT_VERSION,
        "ts": utc_now_iso(),
        "source": "parent-cli",
        "binding_id": binding["binding_id"],
        "parent_framework": binding["parent_framework"],
        "parent_session_id": binding["parent_session_id"],
        "child_framework": binding["child_framework"],
        "child_session_id": binding["child_session_id"],
        "kind": message["kind"],
        "message_id": message["message_id"],
        "prompt": build_child_prompt(root, binding, message),
    }
    if message["kind"] == "ask_child":
        event["request_id"] = message["request_id"]
    return event


def append_inbox(root, binding: dict, message: dict) -> str:
    """Append one parent->child event; raises OSError on write failure."""
    target = inbox_path(root, binding["binding_id"])
    append_event(target, build_inbox_event(root, binding, message))
    return f"inbox:{target}#{message['message_id']}"
