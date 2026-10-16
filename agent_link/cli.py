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
from .errors import AgentLinkError, DeliveryFailure, ValidationError
from .hosts.base import load_adapters
from .registry import entry_from_row, list_bindings, registry_dir, write_entry
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
            "notification_transport": "session_push_http",
            "receive_mode": "session_injection",
            "polling": False,
            "reply_mode": "blocking_tool_file_poll",
            "busy_behavior": "followup_queue",
            "wake_after_turn": "unknown",
            "spawn": False,
            "cancel": False,
        },
        "omp": {
            "notification_transport": "session_push_http",
            "receive_mode": "session_injection",
            "polling": False,
            "reply_mode": "blocking_tool_file_poll",
            "busy_behavior": "aside",
            "wake_after_turn": "unknown",
            "spawn": False,
            "cancel": False,
        },
        "opencode": {
            "notification_transport": "opencode_prompt_http",
            "receive_mode": "session_injection",
            "reply_mode": "blocking_tool_file_poll",
            "polling": False,
            "delivery": "queue",
            "busy_behavior": "unknown",
            "wake_after_turn": "unknown",
            "spawn": False,
            "cancel": False,
        },
    },
}


def parse_timeout(value, max_seconds: float | None = None) -> float:
    return validate_timeout_seconds(value, field="timeout", max_seconds=max_seconds)


def _parent_adapter(binding: dict):
    """The registered parent adapter for this binding, or a clear failure."""
    adapters = load_adapters(binding["parent_framework"])
    if adapters.parent is None:
        raise DeliveryFailure(
            f"framework {binding['parent_framework']!r} has no parent adapter"
        )
    return adapters.parent


def _child_adapter(binding: dict):
    """The registered child adapter for this binding, or a clear failure."""
    adapters = load_adapters(binding["child_framework"])
    if adapters.child is None:
        raise DeliveryFailure(
            f"framework {binding['child_framework']!r} has no child adapter"
        )
    return adapters.child


def _dispatch(store: Store, binding_id: str, message: dict):
    """Persist dispatching state, run the external call, record the outcome."""
    binding = store.get_binding(binding_id)
    store.mark_dispatching(binding_id, message["message_id"])
    try:
        detail = _parent_adapter(binding).notify(store.root, binding, message)
    except Exception as exc:  # noqa: BLE001 - external transport failure
        error = f"{type(exc).__name__}: {exc}"
        store.mark_delivery(binding_id, message["message_id"], "failed_or_unknown", error)
        return None, error
    except BaseException:
        store.mark_delivery(binding_id, message["message_id"], "failed_or_unknown", "interrupted")
        raise
    store.mark_delivery(binding_id, message["message_id"], "host_accepted", detail)
    return detail, None


def dispatch_parent_request(store: Store, binding_id: str, message: dict):
    """Deliver one parent->child message through the registered child adapter.

    Mirrors _dispatch for the parent->child direction: the transport is chosen
    by ``binding.child_framework`` (Pi uses the RPC descriptor when present,
    otherwise the session push listener; OMP uses the session push listener;
    OpenCode pushes to its native prompt endpoint through the frozen session
    descriptor).  ``host_accepted`` only proves the transport accepted the
    message, never that the model processed it.  A replay never re-dispatches
    (the caller checks new).
    """
    store.mark_parent_dispatching(binding_id, message["request_id"])
    try:
        binding = store.get_binding(binding_id)
        detail = _child_adapter(binding).deliver(store.root, binding, message)
    except Exception as exc:  # noqa: BLE001 - external transport failure
        error = f"{type(exc).__name__}: {exc}"
        store.mark_parent_delivery(binding_id, message["request_id"], "failed_or_unknown", error)
        return None, error
    except BaseException:
        store.mark_parent_delivery(binding_id, message["request_id"], "failed_or_unknown", "interrupted")
        raise
    store.mark_parent_delivery(binding_id, message["request_id"], "host_accepted", detail)
    return detail, None


def _register_binding(store: Store, result: dict) -> None:
    """Write or refresh the user-level registry entry for this binding.

    The registry is a discovery aid only: a write failure is reported on
    stderr and never fails the bind itself; the entry can be regenerated by
    re-running bind or by starting the host service.
    """
    try:
        path = write_entry(entry_from_row(store.root, store.get_binding(result["binding_id"])))
    except OSError as error:
        print(f"agent-link: registry write failed: {error}", file=sys.stderr)
        return
    result["registry"] = str(path)


def cmd_bind(store: Store, args) -> tuple[dict, int]:
    import os

    if not os.path.isfile(args.config):
        raise ValidationError(f"config file does not exist: {args.config}")
    with open(args.config, "r", encoding="utf-8") as stream:
        raw = json.load(stream)
    result = store.bind(raw)
    _register_binding(store, result)
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


def cmd_ask_child(store: Store, args) -> tuple[dict, int]:
    binding_id = validate_identifier(args.binding, "binding_id")
    request_id = validate_identifier(args.request_id, "request_id")
    message_id = validate_identifier(args.message_id, "message_id")
    question = require_text(args.question, "question")
    timeout = parse_timeout(args.timeout)
    prepared = store.prepare_parent_ask(binding_id, request_id, message_id, question, timeout)
    request = prepared["request"]
    if prepared["new"]:
        dispatch_parent_request(
            store,
            binding_id,
            {
                "kind": "ask_child",
                "request_id": request_id,
                "message_id": message_id,
                "question": question,
                "timeout_seconds": request["timeout_seconds"],
                "deadline_epoch": request["deadline_epoch"],
            },
        )
    # Map the persisted UTC deadline onto the monotonic clock exactly like
    # cmd_ask: a replay waits only the time left on the original deadline.
    ref_wall = time.time()
    ref_mono = time.monotonic()
    mono_deadline = ref_mono + (request["deadline_epoch"] - ref_wall)
    final = store.wait_for_parent_request(binding_id, request_id, mono_deadline, ref_wall, ref_mono)
    result = {
        "binding_id": binding_id,
        "request_id": request_id,
        "message_id": message_id,
        "status": final["status"],
        "answer": final["answer"],
        "late": bool(final["late"]),
        "delivery_state": final["delivery_state"],
        "idempotent": not prepared["new"],
    }
    return result, 0 if final["status"] == "answered" else 3


def cmd_send_child(store: Store, args) -> tuple[dict, int]:
    """Persist and deliver a parent request without waiting for the child model.

    Same persistence and dispatch path as :func:`cmd_ask_child`, but the
    command returns as soon as the transport call finishes: ``host_accepted``
    maps to exit 0 and ``failed_or_unknown`` to exit 7.  This is the
    "message the child any time" operation; whether a busy or idle child model
    actually processes it follows the child framework's native scheduling and
    is not proven by the transport acceptance.  A replay with the same
    request_id/message_id never dispatches twice and reports the persisted
    request row instead.
    """
    binding_id = validate_identifier(args.binding, "binding_id")
    request_id = validate_identifier(args.request_id, "request_id")
    message_id = validate_identifier(args.message_id, "message_id")
    question = require_text(args.question, "question")
    timeout = parse_timeout(args.timeout)
    prepared = store.prepare_parent_ask(binding_id, request_id, message_id, question, timeout)
    request = prepared["request"]
    error = None
    if prepared["new"]:
        _, error = dispatch_parent_request(
            store,
            binding_id,
            {
                "kind": "ask_child",
                "request_id": request_id,
                "message_id": message_id,
                "question": question,
                "timeout_seconds": request["timeout_seconds"],
                "deadline_epoch": request["deadline_epoch"],
            },
        )
    row = store.get_parent_request(binding_id, request_id)
    result = {
        "binding_id": binding_id,
        "request_id": request_id,
        "message_id": message_id,
        "status": row["status"],
        "delivery_state": row["delivery_state"],
        "idempotent": not prepared["new"],
    }
    if error:
        result["error"] = error
    return result, 0 if row["delivery_state"] == "host_accepted" else 7


def cmd_answer(store: Store, args) -> tuple[dict, int]:
    binding_id = validate_identifier(args.binding, "binding_id")
    request_id = validate_identifier(args.request_id, "request_id")
    message_id = validate_identifier(args.message_id, "message_id")
    answer = require_text(args.answer, "answer")
    outcome = store.answer_parent_request(binding_id, request_id, message_id, answer)
    result = {
        "binding_id": binding_id,
        "request_id": outcome["request_id"],
        "request_message_id": outcome["request_message_id"],
        "answer_message_id": message_id,
        "status": outcome["status"],
        "late": outcome["late"],
        "answer": outcome["answer"],
        "idempotent": outcome["idempotent"],
    }
    return result, 0


def cmd_get_parent_request(store: Store, args) -> tuple[dict, int]:
    binding_id = validate_identifier(args.binding, "binding_id")
    request_id = validate_identifier(args.request_id, "request_id")
    row = store.get_parent_request(binding_id, request_id)
    result = {
        "binding_id": binding_id,
        "request_id": row["request_id"],
        "message_id": row["message_id"],
        "question": row["question"],
        "status": row["status"],
        "late": row["late"],
        "answer": row["answer"],
        "answer_message_id": row["answer_message_id"],
        "deadline_epoch": row["deadline_epoch"],
        "timeout_seconds": row["timeout_seconds"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "answered_at": row["answered_at"],
        "delivery_state": row["delivery_state"],
    }
    return result, 0


def cmd_list_parent_requests(store: Store, args) -> tuple[dict, int]:
    binding_id = validate_identifier(args.binding, "binding_id")
    entries = store.list_parent_requests(binding_id, pending_only=bool(args.pending))
    return {"binding_id": binding_id, "parent_requests": entries}, 0


def cmd_list_bindings(store, args) -> tuple[dict, int]:
    """List user-level registry entries; the required --root is accepted but unused.

    The registry (default ~/.agent-link/registry/, override AGENT_LINK_REGISTRY)
    is a discovery pointer written by bind and refreshed by the host service;
    each runtime root's SQLite database stays authoritative.
    """
    entries = list_bindings(
        parent_session_id=args.parent_session_id,
        child_session_id=args.child_session_id,
        parent_framework=args.parent_framework,
        child_framework=args.child_framework,
    )
    return {"registry": str(registry_dir()), "bindings": entries}, 0


HANDLERS = {
    "bind": cmd_bind,
    "capabilities": cmd_capabilities,
    "report": cmd_report,
    "ask": cmd_ask,
    "reply": cmd_reply,
    "get-request": cmd_get_request,
    "ask-child": cmd_ask_child,
    "send-child": cmd_send_child,
    "answer": cmd_answer,
    "get-parent-request": cmd_get_parent_request,
    "list-parent-requests": cmd_list_parent_requests,
    "list-bindings": cmd_list_bindings,
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

    p = sub.add_parser(
        "ask-child",
        help="parent asks the bound child and waits for its answer (blocks; child transport)",
    )
    p.add_argument("--binding", required=True)
    p.add_argument("--request-id", required=True, dest="request_id")
    p.add_argument("--message-id", required=True, dest="message_id")
    p.add_argument("--question", required=True)
    p.add_argument("--timeout", required=True)

    p = sub.add_parser(
        "send-child",
        help="persist and deliver a parent request without waiting for the child's answer",
    )
    p.add_argument("--binding", required=True)
    p.add_argument("--request-id", required=True, dest="request_id")
    p.add_argument("--message-id", required=True, dest="message_id")
    p.add_argument("--question", required=True)
    p.add_argument("--timeout", required=True)

    p = sub.add_parser("answer", help="child answers a parent-initiated request")
    p.add_argument("--binding", required=True)
    p.add_argument("--request-id", required=True, dest="request_id")
    p.add_argument("--message-id", required=True, dest="message_id")
    p.add_argument("--answer", required=True)

    p = sub.add_parser("get-parent-request", help="read a parent-initiated request state")
    p.add_argument("--binding", required=True)
    p.add_argument("--request-id", required=True, dest="request_id")

    p = sub.add_parser("list-parent-requests", help="list parent-initiated requests for a binding")
    p.add_argument("--binding", required=True)
    p.add_argument("--pending", action="store_true", help="only requests still waiting for an answer")

    p = sub.add_parser(
        "list-bindings",
        help="list user-level binding registry entries (session-id discovery; --root accepted but unused)",
    )
    p.add_argument("--parent-session-id", dest="parent_session_id")
    p.add_argument("--child-session-id", dest="child_session_id")
    p.add_argument("--parent-framework", dest="parent_framework")
    p.add_argument("--child-framework", dest="child_framework")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "capabilities":
        print(json.dumps(CAPABILITIES, ensure_ascii=False))
        return 0

    store = None
    try:
        if args.command == "list-bindings":
            result, exit_code = HANDLERS[args.command](None, args)
        else:
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
