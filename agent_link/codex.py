"""Codex desktop parent-side thin CLI.

``python3 -m agent_link.codex`` is the parent helper for a Codex chat.  It
discovers the bindings registered for the current session in the user-level
registry, verifies every candidate against the binding's own SQLite database
opened **read-only**, and only then reuses the core CLI command handlers
(``agent_link.cli``) for the parent-side operations.  It never accepts a
``--root`` from the caller: the runtime root and the binding identity must come
from a registry entry that passed verification, so a root pasted into a chat
message can never route a parent reply.

Why the checks: the registry is a discovery index, not the source of truth.
A stale entry can keep pointing at a deleted root, a rewritten entry can name
another session, and a root copied from elsewhere can hold an unrelated
database.  This layer rejects such entries with explicit reasons instead of
silently replying through them.  It protects a trusted local user from
accidental misrouting; it is not a security boundary against a malicious
same-user process.

The session id comes from an explicit ``--session-id`` or, when running inside
the Codex chat, from ``CODEX_THREAD_ID`` / ``CODEX_SESSION_ID``.  No session id
is hardcoded anywhere.

Examples::

    python3 -m agent_link.codex --session-id CURRENT_THREAD list
    python3 -m agent_link.codex --session-id CURRENT_THREAD --binding B reply \\
        --request-id R --message-id M --answer 'decision'
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import traceback
from pathlib import Path

from . import cli
from .errors import AgentLinkError, NotFoundError, ValidationError
from .registry import REGISTRY_VERSION, registry_dir
from .storage import DB_FILENAME, Store, canonical_json, validate_identifier

#: Only bindings whose parent is the Codex desktop adapter are discovered.
PARENT_FRAMEWORK = "codex-desktop"
#: Trusted environment variables a Codex chat provides for the current thread.
SESSION_ENV_VARS = ("CODEX_THREAD_ID", "CODEX_SESSION_ID")
#: Parent-side operations; each reuses the exact core CLI handler.
OPERATION_HANDLERS = {
    "reply": cli.cmd_reply,
    "ask-child": cli.cmd_ask_child,
    "get-request": cli.cmd_get_request,
    "get-parent-request": cli.cmd_get_parent_request,
    "list-parent-requests": cli.cmd_list_parent_requests,
}


def _candidate(entry: dict) -> dict:
    """Registry-sourced fields shown in list/rejection output."""
    return {
        "binding_id": entry.get("binding_id"),
        "run_id": entry.get("run_id"),
        "native_task_id": entry.get("native_task_id"),
        "root": entry.get("root"),
        "parent": entry.get("parent"),
        "child": entry.get("child"),
        "config_digest": entry.get("config_digest"),
        "created_at": entry.get("created_at"),
        "updated_at": entry.get("updated_at"),
    }


def _rejected(candidate: dict, reason: str) -> dict:
    candidate["status"] = "rejected"
    candidate["reason"] = reason
    return candidate


def _read_binding_row(db_path: Path, binding_id: str):
    """Read one ``bindings`` row through a read-only SQLite connection.

    ``mode=ro`` is deliberate: a missing or invalid database must fail here
    instead of being auto-created by ``Store``.  The caller validates the file
    exists before calling.
    """
    uri = db_path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    try:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM bindings WHERE binding_id = ?", (binding_id,)
        ).fetchone()
        return None if row is None else dict(row)
    finally:
        connection.close()


def _check(mismatches: list, label: str, registered, stored) -> None:
    if registered != stored:
        mismatches.append(f"{label}: registry={registered!r} database={stored!r}")


def _inspect(entry: dict, session_id: str) -> dict:
    """Verify one registry entry against its own database (read-only).

    Returns a candidate dict with ``status`` ``accepted`` or ``rejected``; a
    rejected candidate always carries a human-readable ``reason``.  Store is
    never constructed here.
    """
    candidate = _candidate(entry)
    try:
        binding_id = validate_identifier(entry.get("binding_id"), "binding_id")
    except ValidationError as error:
        return _rejected(candidate, f"invalid binding_id: {error}")

    parent = entry.get("parent")
    child = entry.get("child")
    if not isinstance(parent, dict) or parent.get("framework") != PARENT_FRAMEWORK:
        return _rejected(candidate, "registry entry parent is not codex-desktop")
    if parent.get("session_id") != session_id:
        return _rejected(
            candidate,
            f"registry entry parent session {parent.get('session_id')!r} is not this session",
        )
    if not isinstance(child, dict):
        return _rejected(candidate, "registry entry has no child object")

    root_value = entry.get("root")
    if not isinstance(root_value, str) or not root_value.strip():
        return _rejected(candidate, "registry entry has no root path")
    root = Path(root_value).expanduser()
    if not root.is_absolute():
        return _rejected(candidate, f"root is not an absolute path: {root_value!r}")
    if not root.is_dir():
        return _rejected(candidate, f"root directory does not exist: {root}")
    root = root.resolve()
    db_path = root / DB_FILENAME
    if not db_path.is_file():
        return _rejected(candidate, f"binding database does not exist: {db_path}")
    try:
        row = _read_binding_row(db_path, binding_id)
    except sqlite3.Error as error:
        return _rejected(candidate, f"cannot read binding database {db_path}: {error}")
    if row is None:
        return _rejected(candidate, f"database {db_path} has no binding {binding_id!r}")

    mismatches: list = []
    _check(mismatches, "parent.framework", parent.get("framework"), row["parent_framework"])
    _check(mismatches, "parent.session_id", parent.get("session_id"), row["parent_session_id"])
    _check(mismatches, "child.framework", child.get("framework"), row["child_framework"])
    _check(mismatches, "child.session_id", child.get("session_id"), row["child_session_id"])
    _check(mismatches, "run_id", entry.get("run_id"), row["run_id"])
    _check(mismatches, "native_task_id", entry.get("native_task_id"), row["native_task_id"])
    _check(mismatches, "config_digest", entry.get("config_digest"), row["config_digest"])
    if mismatches:
        return _rejected(
            candidate,
            "database does not match the registry entry: " + "; ".join(mismatches),
        )
    try:
        stored_config = json.loads(row["config_json"])
        recomputed = hashlib.sha256(canonical_json(stored_config).encode("utf-8")).hexdigest()
    except (TypeError, ValueError) as error:
        return _rejected(candidate, f"database binding config_json is invalid: {error}")
    if recomputed != row["config_digest"]:
        return _rejected(candidate, "database config_digest does not match its stored config_json")

    return {
        "binding_id": binding_id,
        "run_id": row["run_id"],
        "native_task_id": row["native_task_id"],
        "root": str(root),
        "database": str(db_path),
        "parent": {"framework": row["parent_framework"], "session_id": row["parent_session_id"]},
        "child": {"framework": row["child_framework"], "session_id": row["child_session_id"]},
        "config_digest": row["config_digest"],
        "notifier_path": row["notifier_path"],
        "created_at": entry.get("created_at"),
        "updated_at": entry.get("updated_at"),
        "status": "accepted",
    }


def _registry_candidates(session_id: str):
    """Scan the registry for this session; verify each entry read-only.

    Entries that do not name this Codex session are skipped.  Files that cannot
    be parsed (and therefore cannot be attributed to a session) are reported as
    rejected so listing never silently hides a broken index.  Returns
    ``(candidates, registry_directory)``.
    """
    directory = registry_dir()
    candidates: list = []
    if not directory.exists():
        return candidates, directory
    if not directory.is_dir():
        raise ValidationError(f"registry path is not a directory: {directory}")
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError as error:
        raise ValidationError(f"cannot read registry directory {directory}: {error}")
    for path in paths:
        try:
            entry = json.loads(path.read_text("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            candidates.append(
                _rejected(
                    {"binding_id": path.stem, "registry_file": str(path)},
                    f"unreadable registry entry: {type(error).__name__}: {error}",
                )
            )
            continue
        if not isinstance(entry, dict):
            candidates.append(
                _rejected(
                    {"binding_id": path.stem, "registry_file": str(path)},
                    "registry entry is not a JSON object",
                )
            )
            continue
        if entry.get("version") != REGISTRY_VERSION:
            candidates.append(
                _rejected(
                    {"binding_id": entry.get("binding_id") or path.stem, "registry_file": str(path)},
                    f"unsupported registry entry version: {entry.get('version')!r}",
                )
            )
            continue
        if not _matches_session(entry, session_id):
            continue
        candidates.append(_inspect(entry, session_id))
    candidates.sort(
        key=lambda candidate: (
            str(candidate.get("created_at") or ""),
            str(candidate.get("binding_id") or ""),
        )
    )
    return candidates, directory


def _matches_session(entry: dict, session_id: str) -> bool:
    parent = entry.get("parent")
    return (
        isinstance(parent, dict)
        and parent.get("framework") == PARENT_FRAMEWORK
        and parent.get("session_id") == session_id
    )


def _resolve_session_id(explicit) -> str:
    if explicit:
        return validate_identifier(explicit, "session_id")
    for name in SESSION_ENV_VARS:
        value = os.environ.get(name)
        if value:
            return validate_identifier(value, "session_id")
    raise ValidationError(
        "a session id is required: pass --session-id or run inside the Codex chat where "
        + "/".join(SESSION_ENV_VARS)
        + " is set"
    )


def _resolve_binding(session_id: str, requested_binding):
    """Pick the verified binding for this session, never the newest one."""
    candidates, _directory = _registry_candidates(session_id)
    if requested_binding:
        requested = validate_identifier(requested_binding, "binding_id")
        for candidate in candidates:
            if candidate["binding_id"] == requested:
                if candidate["status"] != "accepted":
                    raise ValidationError(
                        f"binding {requested!r} is rejected: {candidate['reason']}",
                        bindings=[candidate],
                    )
                return candidate
        raise NotFoundError(
            f"binding {requested!r} is not registered for this codex-desktop session",
            bindings=candidates,
        )
    if not candidates:
        raise NotFoundError(f"no codex-desktop binding is registered for session {session_id!r}")
    accepted = [candidate for candidate in candidates if candidate["status"] == "accepted"]
    if len(candidates) == 1 and accepted:
        return accepted[0]
    if accepted:
        raise ValidationError(
            "more than one registry entry matches this session; pass --binding explicitly",
            bindings=candidates,
        )
    raise ValidationError(
        "no verified binding matches this session; every registered entry was rejected",
        bindings=candidates,
    )


def _verify_store_row(row: dict, candidate: dict) -> None:
    """Re-check ownership from the write connection before running a command."""
    expected = candidate
    mismatches: list = []
    _check(mismatches, "parent.framework", expected["parent"]["framework"], row["parent_framework"])
    _check(mismatches, "parent.session_id", expected["parent"]["session_id"], row["parent_session_id"])
    _check(mismatches, "child.framework", expected["child"]["framework"], row["child_framework"])
    _check(mismatches, "child.session_id", expected["child"]["session_id"], row["child_session_id"])
    _check(mismatches, "run_id", expected["run_id"], row["run_id"])
    _check(mismatches, "config_digest", expected["config_digest"], row["config_digest"])
    if mismatches:
        raise ValidationError(
            "binding changed after verification; refusing to run the operation: "
            + "; ".join(mismatches)
        )


def _run_operation(args, candidate: dict):
    """Create the Store only for a verified binding, then reuse the core handler."""
    store = Store(candidate["root"])
    try:
        row = store.get_binding(candidate["binding_id"])
        _verify_store_row(row, candidate)
        namespace = argparse.Namespace(**vars(args))
        namespace.binding = candidate["binding_id"]
        result, exit_code = OPERATION_HANDLERS[args.command](store, namespace)
    finally:
        store.close()
    result["binding_id"] = candidate["binding_id"]
    return result, exit_code


def command_list(session_id: str, requested_binding=None):
    """List this session's registry entries with their verification status."""
    candidates, directory = _registry_candidates(session_id)
    if requested_binding:
        requested = validate_identifier(requested_binding, "binding_id")
        candidates = [
            candidate for candidate in candidates if candidate["binding_id"] == requested
        ]
    accepted = [c["binding_id"] for c in candidates if c["status"] == "accepted"]
    rejected = [
        {"binding_id": c.get("binding_id"), "root": c.get("root"), "reason": c["reason"]}
        for c in candidates
        if c["status"] == "rejected"
    ]
    return {
        "session_id": session_id,
        "parent_framework": PARENT_FRAMEWORK,
        "registry": str(directory),
        "registry_present": directory.is_dir(),
        "bindings": candidates,
        "accepted": accepted,
        "rejected": rejected,
    }, 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m agent_link.codex",
        description=(
            "Codex desktop parent helper: discover this chat's bindings from the user-level "
            "registry (read-only database verification) and run parent-side operations. "
            "The runtime root always comes from a verified registry entry, never from argv."
        ),
    )
    parser.add_argument(
        "--session-id",
        dest="session_id",
        metavar="ID",
        help="Codex thread/session id; defaults to CODEX_THREAD_ID or CODEX_SESSION_ID",
    )
    parser.add_argument(
        "--binding",
        default=None,
        metavar="ID",
        help="binding_id to operate on; required when more than one binding matches the session",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="list and verify this session's registered bindings").add_argument(
        "--binding", default=argparse.SUPPRESS, metavar="ID"
    )
    p = sub.add_parser("reply", help="answer an existing child request with a fresh reply id")
    p.add_argument("--binding", default=argparse.SUPPRESS)
    p.add_argument("--request-id", required=True, dest="request_id")
    p.add_argument("--message-id", required=True, dest="message_id")
    p.add_argument("--answer", required=True)
    p = sub.add_parser(
        "ask-child",
        help="ask the bound child and wait for its answer (blocks; use background-tasks for long waits)",
    )
    p.add_argument("--binding", default=argparse.SUPPRESS)
    p.add_argument("--request-id", required=True, dest="request_id")
    p.add_argument("--message-id", required=True, dest="message_id")
    p.add_argument("--question", required=True)
    p.add_argument("--timeout", required=True)
    p = sub.add_parser("get-request", help="read a child request's persisted state")
    p.add_argument("--binding", default=argparse.SUPPRESS)
    p.add_argument("--request-id", required=True, dest="request_id")
    p = sub.add_parser("get-parent-request", help="read a parent-initiated request's state")
    p.add_argument("--binding", default=argparse.SUPPRESS)
    p.add_argument("--request-id", required=True, dest="request_id")
    p = sub.add_parser("list-parent-requests", help="list parent-initiated requests for the binding")
    p.add_argument("--binding", default=argparse.SUPPRESS)
    p.add_argument("--pending", action="store_true", help="only requests still waiting for an answer")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        session_id = _resolve_session_id(args.session_id)
        if args.command == "list":
            result, exit_code = command_list(session_id, args.binding)
        else:
            candidate = _resolve_binding(session_id, args.binding)
            result, exit_code = _run_operation(args, candidate)
    except AgentLinkError as error:
        print(f"{error.code}: {error}", file=sys.stderr)
        payload = {"error": error.code, "message": str(error)}
        if error.details:
            payload.update(error.details)
        print(json.dumps(payload, ensure_ascii=False))
        return error.exit_code
    except Exception as error:  # noqa: BLE001 - top-level guard mirrors the core CLI
        traceback.print_exc()
        print(
            json.dumps(
                {"error": "internal", "message": f"{type(error).__name__}: {error}"},
                ensure_ascii=False,
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
