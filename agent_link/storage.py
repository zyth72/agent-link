"""SQLite persistence and state machine for agent_link (SPEC v0.1 sections 4-5, 8).

Key rules implemented here:

* binding is created once and can never be redirected; identical config is
  idempotent, different config conflicts;
* messages share one idempotency rule: same message_id + same content is a
  replay, same message_id + different content/kind is a conflict;
* request states are pending -> replied | timed_out.  A late answer is stored
  with late=true and keeps status timed_out, even if the waiting process has
  not updated the state yet;
* delivery states are persisted -> dispatching -> host_accepted |
  failed_or_unknown.  External notification never runs inside a write
  transaction, and a replay never re-dispatches;
* cross-process safety uses BEGIN IMMEDIATE + busy_timeout, never in-process
  locks alone.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .errors import ConflictError, NotFoundError, ValidationError

ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
WALL_CLOCK_JUMP_TOLERANCE_SECONDS = 2.0

SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS bindings (
        binding_id TEXT PRIMARY KEY,
        config_json TEXT NOT NULL,
        config_digest TEXT NOT NULL,
        run_id TEXT NOT NULL,
        native_task_id TEXT,
        parent_framework TEXT NOT NULL,
        parent_session_id TEXT NOT NULL,
        child_framework TEXT NOT NULL,
        child_session_id TEXT NOT NULL,
        notifier_path TEXT NOT NULL,
        notifier_sha256 TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        binding_id TEXT NOT NULL,
        message_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        text TEXT NOT NULL,
        created_at TEXT NOT NULL,
        delivery_state TEXT NOT NULL,
        delivery_detail TEXT,
        delivery_updated_at TEXT,
        PRIMARY KEY (binding_id, message_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS requests (
        binding_id TEXT NOT NULL,
        request_id TEXT NOT NULL,
        message_id TEXT NOT NULL,
        question TEXT NOT NULL,
        timeout_seconds REAL NOT NULL,
        status TEXT NOT NULL,
        deadline_epoch REAL NOT NULL,
        late INTEGER NOT NULL DEFAULT 0,
        answer TEXT,
        reply_message_id TEXT,
        reply_at TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (binding_id, request_id)
    )
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS idx_requests_binding_message
        ON requests (binding_id, message_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        binding_id TEXT,
        message_id TEXT,
        request_id TEXT,
        event TEXT NOT NULL,
        detail TEXT
    )
    """,
)


def canonical_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def validate_identifier(value, field: str) -> str:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise ValidationError(
            f"{field} must be 1..128 characters of [A-Za-z0-9_-], got {value!r}"
        )
    return value


def require_text(value, field: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{field} must be a string")
    return value


def validate_timeout_seconds(
    value, field: str = "timeout_seconds", max_seconds: float | None = None
) -> float:
    """Validate a request timeout in the core API (not only in the CLI).

    A timeout is part of request semantics, so it must be a positive finite
    number; replaying an ask with a different timeout is a conflict.  Callers
    that hold a thread while waiting (the loopback host service) additionally
    pass ``max_seconds`` so an unbounded wait cannot pin a server thread.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValidationError(f"{field} must be a positive finite number of seconds, got {value!r}")
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{field} must be a positive finite number of seconds, got {value!r}")
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValidationError(f"{field} must be a positive finite number of seconds, got {value!r}")
    if max_seconds is not None and seconds > max_seconds:
        raise ValidationError(f"{field} must be <= {max_seconds:g} seconds, got {value!r}")
    return seconds


def _notifier_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def normalize_binding_config(raw) -> dict:
    """Validate a bind config and return the canonical version used for digesting."""
    if not isinstance(raw, dict):
        raise ValidationError("binding config must be a JSON object")
    config = dict(raw)
    binding_id = validate_identifier(raw.get("binding_id"), "binding_id")
    run_id = validate_identifier(raw.get("run_id"), "run_id")
    native_task_id = raw.get("native_task_id")
    if native_task_id is not None:
        validate_identifier(native_task_id, "native_task_id")
    parent = raw.get("parent")
    child = raw.get("child")
    if not isinstance(parent, dict):
        raise ValidationError("parent must be an object with framework and session_id")
    if not isinstance(child, dict):
        raise ValidationError("child must be an object with framework and session_id")
    parent_framework = validate_identifier(parent.get("framework"), "parent.framework")
    parent_session = validate_identifier(parent.get("session_id"), "parent.session_id")
    child_framework = validate_identifier(child.get("framework"), "child.framework")
    child_session = validate_identifier(child.get("session_id"), "child.session_id")
    notifier = raw.get("notifier_path")
    if not isinstance(notifier, str) or not notifier.strip():
        raise ValidationError("notifier_path must be a non-empty string")
    notifier_path = Path(notifier).expanduser()
    if not notifier_path.is_absolute():
        notifier_path = (Path.cwd() / notifier_path).resolve()
    else:
        notifier_path = notifier_path.resolve()
    if not notifier_path.is_file():
        raise ValidationError(f"notifier_path does not exist or is not a file: {notifier_path}")
    config.update(
        {
            "binding_id": binding_id,
            "run_id": run_id,
            "native_task_id": native_task_id,
            "parent": {"framework": parent_framework, "session_id": parent_session},
            "child": {"framework": child_framework, "session_id": child_session},
            "notifier_path": str(notifier_path),
        }
    )
    return config


class Store:
    """One SQLite database per --root runtime directory."""

    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        self.db_path = self.root / "agent_link.sqlite3"
        self.conn = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout = 30000")
        self._enable_wal()
        self.conn.execute("PRAGMA synchronous = FULL")
        self._init_schema()
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass

    def close(self) -> None:
        self.conn.close()

    # -- low level -----------------------------------------------------
    def _enable_wal(self) -> None:
        """Enable WAL, retrying across concurrent first-open of the same DB.

        PRAGMA journal_mode can return SQLITE_BUSY without invoking the busy
        handler while another process is initializing the same fresh database,
        so retry for the same 30s budget used by busy_timeout.
        """
        deadline = time.monotonic() + 30.0
        while True:
            try:
                self.conn.execute("PRAGMA journal_mode = WAL")
                return
            except sqlite3.OperationalError as exc:
                message = str(exc).lower()
                if ("locked" not in message and "busy" not in message) or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)

    def _init_schema(self) -> None:
        last_error = None
        for _ in range(120):
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                for statement in SCHEMA_STATEMENTS:
                    self.conn.execute(statement)
                self.conn.execute("COMMIT")
                return
            except sqlite3.OperationalError as exc:
                try:
                    self.conn.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                if "locked" in str(exc).lower() or "busy" in str(exc).lower():
                    last_error = exc
                    time.sleep(0.05)
                    continue
                raise
        raise last_error  # pragma: no cover - only on persistent lock contention

    @contextmanager
    def _write(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    def _event(self, event: str, binding_id=None, message_id=None, request_id=None, detail=None) -> None:
        self.conn.execute(
            "INSERT INTO events (at, binding_id, message_id, request_id, event, detail)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                utc_now_iso(),
                binding_id,
                message_id,
                request_id,
                event,
                None if detail is None else canonical_json(detail),
            ),
        )

    def _message_row(self, binding_id: str, message_id: str):
        return self.conn.execute(
            "SELECT * FROM messages WHERE binding_id = ? AND message_id = ?",
            (binding_id, message_id),
        ).fetchone()

    def _request_row(self, binding_id: str, request_id: str):
        return self.conn.execute(
            "SELECT * FROM requests WHERE binding_id = ? AND request_id = ?",
            (binding_id, request_id),
        ).fetchone()

    # -- bindings ------------------------------------------------------
    def bind(self, raw_config) -> dict:
        config = normalize_binding_config(raw_config)
        config_json = canonical_json(config)
        digest = hashlib.sha256(config_json.encode("utf-8")).hexdigest()
        binding_id = config["binding_id"]
        with self._write():
            row = self.conn.execute(
                "SELECT * FROM bindings WHERE binding_id = ?", (binding_id,)
            ).fetchone()
            if row is not None:
                if row["config_digest"] != digest:
                    raise ConflictError(
                        f"binding_id {binding_id!r} is already bound with a different configuration"
                    )
                self._event("binding_replay", binding_id=binding_id, detail={"digest": digest})
                return {
                    "binding_id": binding_id,
                    "created": False,
                    "idempotent": True,
                    "config_digest": digest,
                    "notifier_sha256": row["notifier_sha256"],
                }
            self.conn.execute(
                "INSERT INTO bindings (binding_id, config_json, config_digest, run_id,"
                " native_task_id, parent_framework, parent_session_id, child_framework,"
                " child_session_id, notifier_path, notifier_sha256, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    binding_id,
                    config_json,
                    digest,
                    config["run_id"],
                    config["native_task_id"],
                    config["parent"]["framework"],
                    config["parent"]["session_id"],
                    config["child"]["framework"],
                    config["child"]["session_id"],
                    config["notifier_path"],
                    _notifier_sha256(Path(config["notifier_path"])),
                    utc_now_iso(),
                ),
            )
            self._event("binding_created", binding_id=binding_id, detail={"digest": digest})
        return {
            "binding_id": binding_id,
            "created": True,
            "idempotent": False,
            "config_digest": digest,
        }

    def get_binding(self, binding_id: str) -> dict:
        validate_identifier(binding_id, "binding_id")
        row = self.conn.execute(
            "SELECT * FROM bindings WHERE binding_id = ?", (binding_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"unknown binding_id {binding_id!r}")
        return dict(row)

    # -- messages / delivery ------------------------------------------
    def get_message(self, binding_id: str, message_id: str):
        row = self._message_row(binding_id, message_id)
        return None if row is None else dict(row)

    def get_delivery_state(self, binding_id: str, message_id: str):
        row = self._message_row(binding_id, message_id)
        if row is None:
            raise NotFoundError(f"unknown message_id {message_id!r} for binding {binding_id!r}")
        return row["delivery_state"]

    def mark_dispatching(self, binding_id: str, message_id: str) -> None:
        with self._write():
            self.conn.execute(
                "UPDATE messages SET delivery_state = 'dispatching', delivery_updated_at = ?"
                " WHERE binding_id = ? AND message_id = ? AND delivery_state = 'persisted'",
                (utc_now_iso(), binding_id, message_id),
            )
            self._event("dispatch_started", binding_id=binding_id, message_id=message_id)

    def mark_delivery(self, binding_id: str, message_id: str, state: str, detail=None) -> None:
        with self._write():
            self.conn.execute(
                "UPDATE messages SET delivery_state = ?, delivery_detail = ?, delivery_updated_at = ?"
                " WHERE binding_id = ? AND message_id = ?",
                (state, detail, utc_now_iso(), binding_id, message_id),
            )
            self._event(
                "delivery_recorded",
                binding_id=binding_id,
                message_id=message_id,
                detail={"state": state, "detail": detail},
            )

    def prepare_report(self, binding_id: str, message_id: str, text: str) -> dict:
        validate_identifier(message_id, "message_id")
        require_text(text, "text")
        self.get_binding(binding_id)
        with self._write():
            row = self._message_row(binding_id, message_id)
            if row is None:
                self.conn.execute(
                    "INSERT INTO messages (binding_id, message_id, kind, text, created_at,"
                    " delivery_state, delivery_detail, delivery_updated_at)"
                    " VALUES (?, ?, 'report', ?, ?, 'persisted', NULL, ?)",
                    (binding_id, message_id, text, utc_now_iso(), utc_now_iso()),
                )
                self._event("report_persisted", binding_id=binding_id, message_id=message_id)
                return {"new": True, "delivery_state": "persisted", "message_id": message_id}
            if row["kind"] != "report" or row["text"] != text:
                raise ConflictError(
                    f"message_id {message_id!r} was already used with different content or kind"
                )
            self._event("report_replay", binding_id=binding_id, message_id=message_id)
            return {
                "new": False,
                "delivery_state": row["delivery_state"],
                "message_id": message_id,
            }

    def prepare_ask(
        self, binding_id: str, request_id: str, message_id: str, question: str, timeout_seconds: float
    ) -> dict:
        validate_identifier(request_id, "request_id")
        validate_identifier(message_id, "message_id")
        require_text(question, "question")
        timeout_seconds = validate_timeout_seconds(timeout_seconds)
        self.get_binding(binding_id)
        deadline_epoch = time.time() + timeout_seconds
        now = utc_now_iso()
        with self._write():
            message = self._message_row(binding_id, message_id)
            if message is not None:
                if message["kind"] != "ask" or message["text"] != question:
                    raise ConflictError(
                        f"message_id {message_id!r} was already used with different content or kind"
                    )
                request = self._request_row(binding_id, request_id)
                if request is not None:
                    if request["message_id"] != message_id:
                        raise ConflictError(
                            f"request_id {request_id!r} is already bound to message_id"
                            f" {request['message_id']!r}"
                        )
                    if (
                        request["question"] != question
                        or float(request["timeout_seconds"]) != timeout_seconds
                    ):
                        raise ConflictError(
                            f"request_id {request_id!r} was already created with a different"
                            " question or timeout"
                        )
                    self._event("ask_replay", binding_id=binding_id, request_id=request_id)
                    return {
                        "new": False,
                        "request": dict(request),
                        "delivery_state": message["delivery_state"],
                    }
                other = self.conn.execute(
                    "SELECT * FROM requests WHERE binding_id = ? AND message_id = ?",
                    (binding_id, message_id),
                ).fetchone()
                if other is not None:
                    raise ConflictError(
                        f"message_id {message_id!r} is already bound to request_id"
                        f" {other['request_id']!r}"
                    )
                self.conn.execute(
                    "INSERT INTO requests (binding_id, request_id, message_id, question,"
                    " timeout_seconds, status, deadline_epoch, late, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, ?, 'pending', ?, 0, ?, ?)",
                    (binding_id, request_id, message_id, question, timeout_seconds, deadline_epoch, now, now),
                )
                self._event(
                    "ask_persisted", binding_id=binding_id, message_id=message_id, request_id=request_id
                )
                return {
                    "new": False,
                    "request": dict(self._request_row(binding_id, request_id)),
                    "delivery_state": message["delivery_state"],
                }
            request = self._request_row(binding_id, request_id)
            if request is not None:
                raise ConflictError(
                    f"request_id {request_id!r} is already bound to message_id"
                    f" {request['message_id']!r}"
                )
            self.conn.execute(
                "INSERT INTO messages (binding_id, message_id, kind, text, created_at,"
                " delivery_state, delivery_detail, delivery_updated_at)"
                " VALUES (?, ?, 'ask', ?, ?, 'persisted', NULL, ?)",
                (binding_id, message_id, question, now, now),
            )
            self.conn.execute(
                "INSERT INTO requests (binding_id, request_id, message_id, question,"
                " timeout_seconds, status, deadline_epoch, late, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, 'pending', ?, 0, ?, ?)",
                (binding_id, request_id, message_id, question, timeout_seconds, deadline_epoch, now, now),
            )
            self._event(
                "ask_persisted", binding_id=binding_id, message_id=message_id, request_id=request_id
            )
            return {
                "new": True,
                "request": dict(self._request_row(binding_id, request_id)),
                "delivery_state": "persisted",
            }

    # -- request wait / reply -----------------------------------------
    def wait_for_request(
        self,
        binding_id: str,
        request_id: str,
        mono_deadline: float,
        start_wall: float,
        start_mono: float,
        poll_interval: float = 0.1,
    ) -> dict:
        jump_logged = False
        while True:
            row = self._request_row(binding_id, request_id)
            if row is None:
                raise NotFoundError(f"unknown request_id {request_id!r}")
            if row["status"] in ("replied", "timed_out"):
                return dict(row)
            now_mono = time.monotonic()
            if not jump_logged:
                drift = abs((time.time() - start_wall) - (now_mono - start_mono))
                if drift > WALL_CLOCK_JUMP_TOLERANCE_SECONDS:
                    with self._write():
                        self._event(
                            "wall_clock_jump_observed",
                            binding_id=binding_id,
                            request_id=request_id,
                            detail={"drift_seconds": round(drift, 3)},
                        )
                    jump_logged = True
            if now_mono >= mono_deadline:
                with self._write():
                    current = self._request_row(binding_id, request_id)
                    if current is not None and current["status"] == "pending":
                        self.conn.execute(
                            "UPDATE requests SET status = 'timed_out', updated_at = ?"
                            " WHERE binding_id = ? AND request_id = ? AND status = 'pending'",
                            (utc_now_iso(), binding_id, request_id),
                        )
                        self._event(
                            "request_timed_out", binding_id=binding_id, request_id=request_id
                        )
                    current = self._request_row(binding_id, request_id)
                return dict(current)
            time.sleep(min(poll_interval, max(0.01, mono_deadline - now_mono)))

    def get_request(self, binding_id: str, request_id: str) -> dict:
        validate_identifier(request_id, "request_id")
        self.get_binding(binding_id)
        row = self._request_row(binding_id, request_id)
        if row is None:
            raise NotFoundError(f"unknown request_id {request_id!r} for binding {binding_id!r}")
        # Reading an expired request applies the same deadline rule as waiting:
        # once the persisted wall-clock deadline has passed it is timed_out,
        # even if no waiter ever updated the row (for example after the child
        # disconnected or the host service restarted).  The common case stays
        # read-only; the write transaction only runs when the deadline passed.
        if row["status"] == "pending" and time.time() >= row["deadline_epoch"]:
            with self._write():
                current = self._request_row(binding_id, request_id)
                if (
                    current is not None
                    and current["status"] == "pending"
                    and time.time() >= current["deadline_epoch"]
                ):
                    self.conn.execute(
                        "UPDATE requests SET status = 'timed_out', updated_at = ?"
                        " WHERE binding_id = ? AND request_id = ? AND status = 'pending'",
                        (utc_now_iso(), binding_id, request_id),
                    )
                    self._event(
                        "request_timed_out", binding_id=binding_id, request_id=request_id
                    )
                row = self._request_row(binding_id, request_id)
        result = dict(row)
        result["late"] = bool(row["late"])
        message = self._message_row(binding_id, row["message_id"])
        result["delivery_state"] = None if message is None else message["delivery_state"]
        return result

    def reply(self, binding_id: str, request_id: str, message_id: str, answer: str) -> dict:
        validate_identifier(request_id, "request_id")
        validate_identifier(message_id, "message_id")
        require_text(answer, "answer")
        self.get_binding(binding_id)
        with self._write():
            request = self._request_row(binding_id, request_id)
            if request is None:
                raise NotFoundError(f"unknown request_id {request_id!r} for binding {binding_id!r}")
            # A reply message is identified by (message_id, target_request_id).
            # Reusing one message_id for a different request is a conflict even
            # when the answer text matches; the check runs inside this write
            # transaction so two independent connections cannot both win.
            owner = self.conn.execute(
                "SELECT request_id FROM requests WHERE binding_id = ? AND reply_message_id = ?",
                (binding_id, message_id),
            ).fetchone()
            if owner is not None and owner["request_id"] != request_id:
                raise ConflictError(
                    f"message_id {message_id!r} was already used as a reply to request_id"
                    f" {owner['request_id']!r}; one reply message cannot answer two requests"
                )
            message = self._message_row(binding_id, message_id)
            if message is not None and (message["kind"] != "reply" or message["text"] != answer):
                raise ConflictError(
                    f"message_id {message_id!r} was already used with different content or kind"
                )
            if request["status"] == "replied":
                if request["reply_message_id"] == message_id and request["answer"] == answer:
                    return {
                        "request_id": request_id,
                        "request_message_id": request["message_id"],
                        "status": request["status"],
                        "late": bool(request["late"]),
                        "answer": request["answer"],
                        "idempotent": True,
                    }
                raise ConflictError(
                    f"request_id {request_id!r} already has a reply; overwriting is not allowed"
                )
            if request["status"] == "timed_out" and request["answer"] is not None:
                if request["reply_message_id"] == message_id and request["answer"] == answer:
                    return {
                        "request_id": request_id,
                        "request_message_id": request["message_id"],
                        "status": request["status"],
                        "late": bool(request["late"]),
                        "answer": request["answer"],
                        "idempotent": True,
                    }
                raise ConflictError(
                    f"request_id {request_id!r} timed out and already has a stored late answer"
                )
            # An already timed-out request can never turn into an on-time reply,
            # even if a wall-clock rollback moves time before the deadline.
            # Arrival exactly at the deadline counts as timed out (>=).
            already_timed_out = request["status"] == "timed_out"
            late = 1 if already_timed_out or time.time() >= request["deadline_epoch"] else 0
            status = "timed_out" if late else "replied"
            if message is None:
                self.conn.execute(
                    "INSERT INTO messages (binding_id, message_id, kind, text, created_at,"
                    " delivery_state, delivery_detail, delivery_updated_at)"
                    " VALUES (?, ?, 'reply', ?, ?, 'local', NULL, ?)",
                    (binding_id, message_id, answer, utc_now_iso(), utc_now_iso()),
                )
            self.conn.execute(
                "UPDATE requests SET status = ?, late = ?, answer = ?, reply_message_id = ?,"
                " reply_at = ?, updated_at = ? WHERE binding_id = ? AND request_id = ?",
                (status, late, answer, message_id, utc_now_iso(), utc_now_iso(), binding_id, request_id),
            )
            self._event(
                "late_reply_recorded" if late else "reply_recorded",
                binding_id=binding_id,
                request_id=request_id,
                message_id=message_id,
            )
        return {
            "request_id": request_id,
            "request_message_id": request["message_id"],
            "status": status,
            "late": bool(late),
            "answer": answer,
            "idempotent": False,
        }
