"""Command-line interface for agent_link (SPEC v0.1 section 7).

Usage: python3 -m agent_link --root ABS_DIR <command> [options]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback

from . import __version__
from .adapter import dispatch_notification
from .errors import AgentLinkError, ValidationError
from .storage import Store, require_text, validate_identifier, validate_timeout_seconds

CAPABILITIES = {
    "implementation": "agent_link",
    "version": __version__,
    "notification_transport": "desktop_bridge",
    "receive_mode": "host_injection",
    "reply_mode": "blocking_tool_file_poll",
    "busy_behavior": "unknown",
    "wake_after_turn": "unknown",
    "spawn": False,
    "cancel": False,
    "adapters": {
        "codex-desktop": {
            "notification_transport": "desktop_bridge",
            "receive_mode": "host_injection",
            "busy_behavior": "unknown",
            "wake_after_turn": "unknown",
        },
        "pi": {
            "reply_mode": "blocking_tool_file_poll",
            "spawn": False,
            "cancel": False,
        },
    },
}


def parse_timeout(value, max_seconds: float | None = None) -> float:
    return validate_timeout_seconds(value, field="timeout", max_seconds=max_seconds)


def _dispatch(store: Store, binding_id: str, message: dict):
    """Persist dispatching state, run the external call, record the outcome."""
    binding = store.get_binding(binding_id)
    store.mark_dispatching(binding_id, message["message_id"])
    try:
        detail = dispatch_notification(store.root, binding, message)
    except Exception as exc:  # noqa: BLE001 - external transport failure
        error = f"{type(exc).__name__}: {exc}"
        store.mark_delivery(binding_id, message["message_id"], "failed_or_unknown", error)
        return None, error
    except BaseException:
        store.mark_delivery(binding_id, message["message_id"], "failed_or_unknown", "interrupted")
        raise
    store.mark_delivery(binding_id, message["message_id"], "host_accepted", detail)
    return detail, None


def cmd_bind(store: Store, args) -> tuple[dict, int]:
    import os

    if not os.path.isfile(args.config):
        raise ValidationError(f"config file does not exist: {args.config}")
    with open(args.config, "r", encoding="utf-8") as stream:
        raw = json.load(stream)
    result = store.bind(raw)
    return result, 0


def cmd_capabilities(store, args) -> tuple[dict, int]:  # pragma: no cover - handled in main
    return CAPABILITIES, 0


def cmd_report(store: Store, args) -> tuple[dict, int]:
    binding_id = validate_identifier(args.binding, "binding_id")
    message_id = validate_identifier(args.message_id, "message_id")
    text = require_text(args.text, "text")
    prepared = store.prepare_report(binding_id, message_id, text)
    if prepared["new"]:
        _, error = _dispatch(
            store, binding_id, {"message_id": message_id, "kind": "report", "text": text}
        )
        state = store.get_delivery_state(binding_id, message_id)
        result = {
            "binding_id": binding_id,
            "message_id": message_id,
            "kind": "report",
            "delivery_state": state,
            "idempotent": False,
        }
        if error:
            result["error"] = error
        return result, 0 if state == "host_accepted" else 7
    state = store.get_delivery_state(binding_id, message_id)
    return (
        {
            "binding_id": binding_id,
            "message_id": message_id,
            "kind": "report",
            "delivery_state": state,
            "idempotent": True,
        },
        0 if state == "host_accepted" else 7,
    )


def cmd_ask(store: Store, args) -> tuple[dict, int]:
    binding_id = validate_identifier(args.binding, "binding_id")
    request_id = validate_identifier(args.request_id, "request_id")
    message_id = validate_identifier(args.message_id, "message_id")
    question = require_text(args.question, "question")
    timeout = parse_timeout(args.timeout)
    prepared = store.prepare_ask(binding_id, request_id, message_id, question, timeout)
    request = prepared["request"]
    if prepared["new"]:
        _dispatch(
            store,
            binding_id,
            {
                "message_id": message_id,
                "kind": "ask",
                "text": question,
                "request_id": request_id,
                "question": question,
                "timeout_seconds": request["timeout_seconds"],
                "deadline_epoch": request["deadline_epoch"],
            },
        )
    delivery_state = store.get_delivery_state(binding_id, message_id)
    # Map the persisted UTC deadline onto the monotonic clock using a single
    # reference pair measured after the notification.  The previous code kept
    # start_mono from before the notification but recomputed "remaining" after
    # it, so the notification duration was subtracted twice and the real
    # deadline was shortened.  The mapping below preserves the persisted
    # deadline; for a replay it only waits out the time left on the original
    # deadline instead of restarting the timeout.
    ref_wall = time.time()
    ref_mono = time.monotonic()
    mono_deadline = ref_mono + (request["deadline_epoch"] - ref_wall)
    final = store.wait_for_request(binding_id, request_id, mono_deadline, ref_wall, ref_mono)
    result = {
        "binding_id": binding_id,
        "request_id": request_id,
        "message_id": message_id,
        "status": final["status"],
        "answer": final["answer"],
        "late": bool(final["late"]),
        "delivery_state": delivery_state,
        "idempotent": not prepared["new"],
    }
    return result, 0 if final["status"] == "replied" else 3


def cmd_reply(store: Store, args) -> tuple[dict, int]:
    binding_id = validate_identifier(args.binding, "binding_id")
    request_id = validate_identifier(args.request_id, "request_id")
    message_id = validate_identifier(args.message_id, "message_id")
    answer = require_text(args.answer, "answer")
    outcome = store.reply(binding_id, request_id, message_id, answer)
    result = {
        "binding_id": binding_id,
        "request_id": outcome["request_id"],
        "request_message_id": outcome["request_message_id"],
        "reply_message_id": message_id,
        "status": outcome["status"],
        "late": outcome["late"],
        "answer": outcome["answer"],
        "idempotent": outcome["idempotent"],
    }
    return result, 0


def cmd_get_request(store: Store, args) -> tuple[dict, int]:
    binding_id = validate_identifier(args.binding, "binding_id")
    request_id = validate_identifier(args.request_id, "request_id")
    row = store.get_request(binding_id, request_id)
    result = {
        "binding_id": binding_id,
        "request_id": row["request_id"],
        "message_id": row["message_id"],
        "question": row["question"],
        "status": row["status"],
        "late": row["late"],
        "answer": row["answer"],
        "reply_message_id": row["reply_message_id"],
        "deadline_epoch": row["deadline_epoch"],
        "timeout_seconds": row["timeout_seconds"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "reply_at": row["reply_at"],
        "delivery_state": row.get("delivery_state"),
    }
    return result, 0


HANDLERS = {
    "bind": cmd_bind,
    "capabilities": cmd_capabilities,
    "report": cmd_report,
    "ask": cmd_ask,
    "reply": cmd_reply,
    "get-request": cmd_get_request,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m agent_link",
        description="Thin cross-framework binding / reporting / decision CLI (SPEC v0.1).",
    )
    parser.add_argument("--root", required=True, metavar="ABS_DIR", help="runtime directory (0700)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("bind", help="create a binding from a JSON config")
    p.add_argument("--config", required=True, metavar="FILE")

    sub.add_parser("capabilities", help="print the capability declaration")

    p = sub.add_parser("report", help="persist and notify a progress report")
    p.add_argument("--binding", required=True)
    p.add_argument("--message-id", required=True, dest="message_id")
    p.add_argument("--text", required=True)

    p = sub.add_parser("ask", help="persist, notify and wait for a reply")
    p.add_argument("--binding", required=True)
    p.add_argument("--request-id", required=True, dest="request_id")
    p.add_argument("--message-id", required=True, dest="message_id")
    p.add_argument("--question", required=True)
    p.add_argument("--timeout", required=True)

    p = sub.add_parser("reply", help="answer an existing request")
    p.add_argument("--binding", required=True)
    p.add_argument("--request-id", required=True, dest="request_id")
    p.add_argument("--message-id", required=True, dest="message_id")
    p.add_argument("--answer", required=True)

    p = sub.add_parser("get-request", help="read the persisted request state")
    p.add_argument("--binding", required=True)
    p.add_argument("--request-id", required=True, dest="request_id")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "capabilities":
        print(json.dumps(CAPABILITIES, ensure_ascii=False))
        return 0

    store = None
    try:
        store = Store(args.root)
        result, exit_code = HANDLERS[args.command](store, args)
    except AgentLinkError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        print(json.dumps({"error": exc.code, "message": str(exc)}, ensure_ascii=False))
        return exc.exit_code
    except Exception as exc:  # noqa: BLE001 - top-level guard
        traceback.print_exc()
        print(
            json.dumps(
                {"error": "internal", "message": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            )
        )
        return 1
    finally:
        if store is not None:
            store.close()

    print(json.dumps(result, ensure_ascii=False))
    return exit_code
