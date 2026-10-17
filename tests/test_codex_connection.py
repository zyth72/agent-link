"""Codex per-session connection, offline notification and SessionStart tests.

All transports are simulated: bound notifier stubs record calls and the
ConnectionStart hook is invoked with fake stdin and temporary env.  No desktop
message, real Codex config, installed cache, trust approval or model is touched.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_link.errors import NotificationNotSent, ValidationError
from agent_link.host import HostService
from agent_link.hosts.codex import connection
from agent_link.hosts.codex.parent import CodexParentAdapter
from agent_link.registry import entry_from_row, write_entry
from agent_link.storage import Store
from tests import isolate_codex_connections, isolate_registry

REPO_ROOT = Path(__file__).resolve().parent.parent
CORE_CLI = [sys.executable, "-m", "agent_link"]
CODEX_CLI = [sys.executable, "-m", "agent_link.hosts.codex.cli"]
HOOK = REPO_ROOT / "hooks" / "session_start.py"

ENV_AWARE_STUB = '''\
"""Simulated env-aware desktop bridge; records the effective pipe only."""

import json
import os
from pathlib import Path


def bridge(payload, env=None):
    env = dict(os.environ if env is None else env)
    log = Path(os.environ["STUB_NOTIFIER_LOG"])
    with log.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({
            "payload": payload,
            "pipe": env.get("CODEX_APP_TOOLS_PIPE_PATH"),
            "caller": env.get("CODEX_APP_TOOLS_CALLER_HOST_ID"),
            "env_is_os": env is os.environ,
        }, ensure_ascii=False) + "\\n")
    return {"accepted": True}
'''

LEGACY_STUB = '''\
"""Simulated legacy bridge(payload) with no explicit env parameter."""

import json
import os
from pathlib import Path


def bridge(payload):
    log = Path(os.environ["STUB_NOTIFIER_LOG"])
    with log.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"payload": payload}, ensure_ascii=False) + "\\n")
    return {"accepted": True}
'''

POST_INVOCATION_FAILURE_STUB = '''\
"""Logs the invocation, then raises after the bridge call has begun."""

import json
import os
from pathlib import Path


def bridge(payload, env=None):
    log = Path(os.environ["STUB_NOTIFIER_LOG"])
    with log.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"payload": payload}, ensure_ascii=False) + "\\n")
    raise FileNotFoundError("ENOENT simulated after bridge invocation")
'''


class CodexConnectionCase(unittest.TestCase):
    def setUp(self):
        self.registry = Path(isolate_registry(self))
        self.connections = Path(isolate_codex_connections(self))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.stub = self.work / "stub_notifier.py"
        self.write_stub(ENV_AWARE_STUB)
        self.log = self.work / "notifier-calls.jsonl"
        previous_log = os.environ.get("STUB_NOTIFIER_LOG")

        def restore_log():
            if previous_log is None:
                os.environ.pop("STUB_NOTIFIER_LOG", None)
            else:
                os.environ["STUB_NOTIFIER_LOG"] = previous_log

        self.addCleanup(restore_log)
        os.environ["STUB_NOTIFIER_LOG"] = str(self.log)
        self.roots = []

    # -- helpers -------------------------------------------------------
    def write_stub(self, source):
        self.stub.write_text(source, encoding="utf-8")

    def base_env(self, extra=None):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        env["STUB_NOTIFIER_LOG"] = str(self.log)
        env["AGENT_LINK_REGISTRY"] = str(self.registry)
        env["AGENT_LINK_CODEX_CONNECTIONS"] = str(self.connections)
        for name in (
            "CODEX_THREAD_ID",
            "CODEX_SESSION_ID",
            "CODEX_APP_TOOLS_PIPE_PATH",
            "CODEX_APP_TOOLS_CALLER_HOST_ID",
        ):
            env.pop(name, None)
        if extra:
            env.update(extra)
        return env

    def new_root(self, name):
        root = self.work / name
        self.roots.append(root)
        return root

    def bind(self, binding_id="b", *, session="session-1", root=None):
        root = root or self.new_root("root-" + binding_id)
        store = Store(root)
        try:
            store.bind(
                {
                    "binding_id": binding_id,
                    "run_id": "run-" + binding_id,
                    "native_task_id": None,
                    "parent": {"framework": "codex-desktop", "session_id": session},
                    "child": {"framework": "pi", "session_id": "child-" + binding_id},
                    "notifier_path": str(self.stub),
                }
            )
            row = store.get_binding(binding_id)
        finally:
            store.close()
        write_entry(entry_from_row(root, row))
        return root

    def binding(self, root, binding_id="b"):
        store = Store(root)
        try:
            return store.get_binding(binding_id)
        finally:
            store.close()

    def register(self, session="session-1", pipe="pipe-current", caller=None):
        env = dict(os.environ)
        env["CODEX_THREAD_ID"] = session
        env[connection.PIPE_ENV] = pipe
        env.pop("CODEX_APP_TOOLS_CALLER_HOST_ID", None)
        if caller is not None:
            env["CODEX_APP_TOOLS_CALLER_HOST_ID"] = caller
        return connection.register_connection(env=env, directory=self.connections)

    def run_core(self, *args, root, env=None, timeout=60):
        return subprocess.run(
            CORE_CLI + ["--root", str(root)] + [str(arg) for arg in args],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            env=env or self.base_env(),
            timeout=timeout,
        )

    def run_codex(self, *args, env=None, timeout=60):
        return subprocess.run(
            CODEX_CLI + [str(arg) for arg in args],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            env=env or self.base_env(),
            timeout=timeout,
        )

    def run_hook(self, payload, env, cwd=None):
        return subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            cwd=cwd or "/tmp",
            env=env,
            timeout=30,
        )

    @staticmethod
    def payload(proc):
        return json.loads(proc.stdout)

    def log_lines(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text("utf-8").splitlines() if line]

    def descriptor_path(self, session="session-1"):
        return self.connections / f"{session}.json"

    # -- descriptor registration --------------------------------------
    def test_registration_is_private_atomic_and_idempotent(self):
        first = self.register(pipe="pipe-one", caller="host-one")
        self.assertTrue(first["registered"])
        self.assertFalse(first["idempotent"])
        self.assertEqual(first["connection_generation"], 1)
        self.assertEqual(first["transport_env_keys"], sorted(connection.TRANSPORT_ENV_ALLOWLIST))
        self.assertNotIn("pipe-one", json.dumps(first))
        self.assertEqual(self.connections.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.descriptor_path().stat().st_mode & 0o777, 0o600)

        duplicate = self.register(pipe="pipe-one", caller="host-one")
        self.assertFalse(duplicate["registered"])
        self.assertTrue(duplicate["idempotent"])
        self.assertEqual(duplicate["connection_generation"], 1)
        before = self.descriptor_path().read_text("utf-8")

        newer = self.register(pipe="pipe-two")
        self.assertTrue(newer["registered"])
        self.assertEqual(newer["connection_generation"], 2)
        self.assertEqual(newer["transport_env_keys"], [connection.PIPE_ENV])
        raw = json.loads(self.descriptor_path().read_text("utf-8"))
        self.assertEqual(
            set(raw),
            {
                "protocol",
                "framework",
                "session_id",
                "connection_generation",
                "updated_at",
                "transport_env",
            },
        )
        self.assertEqual(raw["transport_env"], {connection.PIPE_ENV: "pipe-two"})
        self.assertNotIn("pipe-one", self.descriptor_path().read_text("utf-8"))
        self.assertNotEqual(before, self.descriptor_path().read_text("utf-8"))

        # Missing pipe never overwrites fresher data, explicitly or automatically.
        with mock.patch.dict(os.environ, {connection.CONNECTIONS_ENV: str(self.connections)}, clear=False):
            os.environ.pop(connection.PIPE_ENV, None)
            skipped = connection.maybe_register_connection(
                "session-1", env={"CODEX_THREAD_ID": "session-1"}
            )
        self.assertEqual(skipped["reason"], "missing_pipe_env")
        self.assertEqual(
            connection.read_descriptor("session-1", self.connections)["connection_generation"], 2
        )

        # An explicit session cannot redirect a different current chat.
        with self.assertRaises(ValidationError):
            connection.resolve_session_id(
                "session-other", {"CODEX_THREAD_ID": "session-1"}
            )

    def test_corrupt_descriptor_fails_closed_and_env_unaware_bridge_is_rejected(self):
        root = self.bind()
        binding = self.binding(root)
        self.register(pipe="pipe-current")
        self.write_stub(LEGACY_STUB)
        with self.assertRaises(NotificationNotSent) as raised:
            CodexParentAdapter().notify(
                root, binding, {"kind": "report", "message_id": "m1", "text": "hello"}
            )
        self.assertEqual(raised.exception.reason, "bridge_env_unsupported")
        self.assertEqual(self.log_lines(), [])

        self.descriptor_path().write_text("{not json", encoding="utf-8")
        os.chmod(self.descriptor_path(), 0o600)
        self.write_stub(ENV_AWARE_STUB)
        with self.assertRaises(NotificationNotSent) as raised:
            CodexParentAdapter().notify(
                root, binding, {"kind": "report", "message_id": "m2", "text": "hello"}
            )
        self.assertEqual(raised.exception.reason, "descriptor_invalid")
        self.assertEqual(self.log_lines(), [])

    def test_descriptor_absent_keeps_legacy_bridge_working(self):
        root = self.bind()
        binding = self.binding(root)
        self.write_stub(LEGACY_STUB)
        detail = CodexParentAdapter().notify(
            root, binding, {"kind": "report", "message_id": "m", "text": "legacy"}
        )
        self.assertIn("accepted", detail)
        self.assertEqual(len(self.log_lines()), 1)

    # -- per-call overlay / isolation / old host -----------------------
    def test_overlay_does_not_mutate_environment_and_is_session_isolated(self):
        root = self.bind("b1", session="session-1")
        root2 = self.bind("b2", session="session-2", root=self.new_root("root-b2"))
        self.register(session="session-1", pipe="pipe-A", caller="caller-A")
        self.register(session="session-2", pipe="pipe-B")

        old_env = {
            connection.PIPE_ENV: "pipe-OLD",
            "CODEX_APP_TOOLS_CALLER_HOST_ID": "caller-OLD",
        }
        with mock.patch.dict(os.environ, old_env, clear=False):
            adapter = CodexParentAdapter()
            adapter.notify(
                root,
                self.binding(root, "b1"),
                {"kind": "report", "message_id": "m1", "text": "one"},
            )
            self.assertEqual(os.environ[connection.PIPE_ENV], "pipe-OLD")
            self.assertEqual(os.environ["CODEX_APP_TOOLS_CALLER_HOST_ID"], "caller-OLD")
            # The descriptor omitted caller host, so the stale inherited value
            # must not leak into the per-call environment.
            adapter.notify(
                root2,
                self.binding(root2, "b2"),
                {"kind": "report", "message_id": "m2", "text": "two"},
            )

        lines = self.log_lines()
        self.assertEqual([line["pipe"] for line in lines], ["pipe-A", "pipe-B"])
        self.assertEqual(lines[0]["caller"], "caller-A")
        self.assertIsNone(lines[1]["caller"])
        self.assertFalse(any(line["env_is_os"] for line in lines))

    def test_host_dispatch_preserves_typed_not_sent_detail(self):
        root = self.bind()
        self.register(pipe="pipe-current")
        self.write_stub(LEGACY_STUB)
        service = HostService(root, "b", "test-token", CodexParentAdapter())
        store = Store(root)
        try:
            store.prepare_report("b", "m-host-not-sent", "offline host message")
            service.dispatch(
                store,
                {"kind": "report", "message_id": "m-host-not-sent", "text": "offline host message"},
            )
            row = store.get_message("b", "m-host-not-sent")
            self.assertEqual(row["delivery_state"], "failed_or_unknown")
            self.assertEqual(
                store._delivery_metadata(row["delivery_detail"]),
                ("not_sent", "bridge_env_unsupported"),
            )
        finally:
            store.close()
            service.server_close()
        self.assertEqual(self.log_lines(), [])

    def test_long_lived_host_uses_new_registration_without_restart(self):
        root = self.bind()
        self.register(pipe="pipe-NEW")
        old_env = {connection.PIPE_ENV: "pipe-OLD"}
        with mock.patch.dict(os.environ, old_env, clear=False):
            service = HostService(root, "b", "test-token", CodexParentAdapter())
            store = Store(root)
            try:
                store.prepare_report("b", "m-host", "after reconnect")
                service.dispatch(
                    store,
                    {"kind": "report", "message_id": "m-host", "text": "after reconnect"},
                )
                self.assertEqual(
                    store.get_delivery_state("b", "m-host"), "host_accepted"
                )
            finally:
                store.close()
                service.server_close()
            self.assertEqual(os.environ[connection.PIPE_ENV], "pipe-OLD")
        self.assertEqual(self.log_lines()[0]["pipe"], "pipe-NEW")

    # -- offline persistence / safe recovery ---------------------------
    def test_not_sent_is_persisted_listed_and_recovered_once(self):
        root = self.bind()
        self.register(pipe="pipe-current")
        self.write_stub(LEGACY_STUB)
        report = self.run_core(
            "report",
            "--binding",
            "b",
            "--message-id",
            "offline-report",
            "--text",
            "original report text",
            root=root,
        )
        self.assertEqual(report.returncode, 7, report.stderr)
        self.assertEqual(self.log_lines(), [])

        store = Store(root)
        try:
            row = store.get_message("b", "offline-report")
            self.assertEqual(row["delivery_state"], "failed_or_unknown")
            self.assertEqual(row["text"], "original report text")
            self.assertEqual(store._delivery_metadata(row["delivery_detail"])[0], "not_sent")
        finally:
            store.close()

        listed = self.run_codex(
            "--session-id", "session-1", "--binding", "b", "notifications"
        )
        self.assertEqual(listed.returncode, 0, listed.stderr)
        listed_body = self.payload(listed)
        self.assertEqual(len(listed_body["messages"]), 1)
        message = listed_body["messages"][0]
        self.assertEqual(message["text"], "original report text")
        self.assertEqual(message["delivery_certainty"], "not_sent")
        self.assertNotIn("pipe-current", listed.stdout)
        self.assertNotIn("pipe-current", listed.stderr)

        self.write_stub(ENV_AWARE_STUB)
        recovered = self.run_codex(
            "--session-id", "session-1", "--binding", "b", "recover-notifications"
        )
        self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
        body = self.payload(recovered)
        self.assertEqual(body["attempted"][0]["delivery_state"], "host_accepted")
        self.assertTrue(body["attempted"][0]["recovered"])
        self.assertEqual(len(self.log_lines()), 1)
        self.assertNotIn("pipe-current", recovered.stdout)
        self.assertNotIn("pipe-current", recovered.stderr)

        again = self.run_codex(
            "--session-id", "session-1", "--binding", "b", "recover-notifications"
        )
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(self.payload(again)["attempted"], [])
        self.assertEqual(len(self.log_lines()), 1, "host_accepted must never replay")

    def test_missing_inherited_pipe_with_env_aware_bridge_is_not_sent(self):
        root = self.bind()
        # No descriptor and no inherited pipe: the env-aware bridge cannot be
        # called at all, so this is a proven pre-invocation failure.
        failed = self.run_core(
            "report",
            "--binding",
            "b",
            "--message-id",
            "m-no-pipe",
            "--text",
            "not submitted",
            root=root,
        )
        self.assertEqual(failed.returncode, 7, failed.stderr)
        self.assertEqual(self.log_lines(), [])
        store = Store(root)
        try:
            row = store.get_message("b", "m-no-pipe")
            self.assertEqual(store._delivery_metadata(row["delivery_detail"]), ("not_sent", "missing_pipe_env"))
            self.assertEqual(row["text"], "not submitted")
        finally:
            store.close()

        self.register(pipe="pipe-after-registration")
        recovered = self.run_codex(
            "--session-id", "session-1", "--binding", "b", "recover-notifications"
        )
        self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
        self.assertEqual(len(self.log_lines()), 1)
        self.assertEqual(self.log_lines()[0]["pipe"], "pipe-after-registration")

    def test_post_invocation_enoent_stays_unknown_and_is_not_recovered(self):
        root = self.bind()
        self.register(pipe="pipe-current")
        self.write_stub(POST_INVOCATION_FAILURE_STUB)
        failed = self.run_core(
            "report", "--binding", "b", "--message-id", "m-unknown", "--text", "unknown", root=root
        )
        self.assertEqual(failed.returncode, 7, failed.stderr)
        self.assertEqual(len(self.log_lines()), 1, "the bridge was in fact invoked")
        store = Store(root)
        try:
            detail = store.get_message("b", "m-unknown")["delivery_detail"]
            self.assertIn("FileNotFoundError", detail)
            self.assertEqual(store._delivery_metadata(detail), (None, None))
        finally:
            store.close()

        self.register(pipe="pipe-current")
        self.write_stub(ENV_AWARE_STUB)
        listed = self.run_codex("--session-id", "session-1", "--binding", "b", "notifications")
        self.assertEqual(self.payload(listed)["messages"][0]["delivery_certainty"], None)
        recovered = self.run_codex(
            "--session-id", "session-1", "--binding", "b", "recover-notifications"
        )
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        body = self.payload(recovered)
        self.assertEqual(body["attempted"], [])
        self.assertEqual(body["skipped"][0]["reason"], "submission_certainty_unknown")
        self.assertEqual(len(self.log_lines()), 1, "unknown delivery must not be retried")

    def test_bridge_cannot_mark_post_invocation_failure_as_not_sent(self):
        stub = POST_INVOCATION_FAILURE_STUB.replace(
            'raise FileNotFoundError("ENOENT simulated after bridge invocation")',
            'from agent_link.errors import NotificationNotSent\n    raise NotificationNotSent("after invocation", reason="untrusted")',
        )
        root = self.bind()
        self.register(pipe="pipe-current")
        self.write_stub(stub)
        failed = self.run_core(
            "report", "--binding", "b", "--message-id", "m-typed", "--text", "test", root=root
        )
        self.assertEqual(failed.returncode, 7, failed.stderr)
        self.assertEqual(len(self.log_lines()), 1)
        store = Store(root)
        try:
            detail = store.get_message("b", "m-typed")["delivery_detail"]
            self.assertEqual(store._delivery_metadata(detail), (None, None))
            self.assertEqual(store.prepare_recovery("b")["claimed"], [])
        finally:
            store.close()

    def test_recovery_checks_request_after_acquiring_write_lock(self):
        from contextlib import contextmanager

        root = self.bind()
        store = Store(root)
        other = Store(root)
        try:
            store.prepare_ask("b", "race-request", "race-message", "question", 30.0)
            store.mark_dispatching("b", "race-message")
            store.mark_delivery(
                "b", "race-message", "failed_or_unknown",
                NotificationNotSent("not sent", reason="missing_pipe_env").delivery_detail(),
            )
            original_write = store._write

            @contextmanager
            def reply_before_lock():
                other.reply("b", "race-request", "race-reply", "answered")
                with original_write():
                    yield

            with mock.patch.object(store, "_write", reply_before_lock):
                outcome = store.prepare_recovery("b", ["race-message"])
            self.assertEqual(outcome["claimed"], [])
            self.assertEqual(outcome["skipped"][0]["reason"], "request_replied")
        finally:
            other.close()
            store.close()

    def test_expired_and_answered_asks_are_skipped_without_deadline_reset(self):
        root = self.bind()
        self.register(pipe="pipe-current")
        self.write_stub(LEGACY_STUB)
        asked = self.run_core(
            "ask",
            "--binding",
            "b",
            "--request-id",
            "rq-expired",
            "--message-id",
            "m-expired",
            "--question",
            "expire me",
            "--timeout",
            "0.05",
            root=root,
        )
        self.assertEqual(asked.returncode, 3, asked.stderr)
        store = Store(root)
        try:
            before = store.get_request("b", "rq-expired")
        finally:
            store.close()
        self.write_stub(ENV_AWARE_STUB)
        recovered = self.run_codex(
            "--session-id", "session-1", "--binding", "b", "recover-notifications"
        )
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        body = self.payload(recovered)
        self.assertEqual(body["attempted"], [])
        self.assertEqual(body["skipped"][0]["reason"], "expired_ask")
        self.assertEqual(self.log_lines(), [])
        store = Store(root)
        try:
            after = store.get_request("b", "rq-expired")
            self.assertEqual(after["status"], "timed_out")
            self.assertEqual(after["deadline_epoch"], before["deadline_epoch"])
            # Answered requests are skipped even if their message was proven not sent.
            store.prepare_ask("b", "rq-answered", "m-answered", "answer me", 30.0)
            store.reply("b", "rq-answered", "reply-1", "already answered")
            store.mark_dispatching("b", "m-answered")
            store.mark_delivery(
                "b",
                "m-answered",
                "failed_or_unknown",
                NotificationNotSent("not sent", reason="bridge_env_unsupported").delivery_detail(),
            )
            outcome = store.prepare_recovery("b", ["m-answered"])
            self.assertEqual(outcome["claimed"], [])
            self.assertEqual(outcome["skipped"][0]["reason"], "request_replied")
            self.assertEqual(store.get_request("b", "rq-answered")["answer"], "already answered")
        finally:
            store.close()

    def test_safe_replay_claims_once_across_concurrent_workers(self):
        root = self.bind()
        self.register(pipe="pipe-current")
        self.write_stub(LEGACY_STUB)
        for index in range(6):
            proc = self.run_core(
                "report",
                "--binding",
                "b",
                "--message-id",
                f"m-concurrent-{index}",
                "--text",
                f"offline-{index}",
                root=root,
            )
            self.assertEqual(proc.returncode, 7, proc.stderr)
        self.write_stub(ENV_AWARE_STUB)

        env = self.base_env()
        args = CODEX_CLI + [
            "--session-id",
            "session-1",
            "--binding",
            "b",
            "recover-notifications",
        ]
        workers = [
            subprocess.Popen(
                args,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(REPO_ROOT),
                env=env,
            )
            for _ in range(2)
        ]
        for worker in workers:
            out, err = worker.communicate(timeout=60)
            self.assertEqual(worker.returncode, 0, out + err)
        self.assertEqual(len(self.log_lines()), 6, "each message must be sent exactly once")
        store = Store(root)
        try:
            for index in range(6):
                self.assertEqual(
                    store.get_delivery_state("b", f"m-concurrent-{index}"),
                    "host_accepted",
                )
        finally:
            store.close()
        # A third invocation has nothing safe left to claim.
        final = self.run_codex(
            "--session-id", "session-1", "--binding", "b", "recover-notifications"
        )
        self.assertEqual(final.returncode, 0, final.stderr)
        self.assertEqual(self.payload(final)["attempted"], [])
        self.assertEqual(len(self.log_lines()), 6)

    # -- helper commands / hook ---------------------------------------
    def test_helper_registers_without_binding_and_status_hides_pipe(self):
        env = self.base_env(
            {
                "CODEX_THREAD_ID": "session-helper",
                connection.PIPE_ENV: "pipe-helper-secret",
            }
        )
        registered = self.run_codex("register-connection", env=env)
        self.assertEqual(registered.returncode, 0, registered.stderr)
        body = self.payload(registered)
        self.assertTrue(body["registered"])
        self.assertEqual(body["connection_generation"], 1)
        self.assertNotIn("pipe-helper-secret", registered.stdout)
        self.assertNotIn("pipe-helper-secret", registered.stderr)
        self.assertTrue(self.descriptor_path("session-helper").is_file())

        status = self.run_codex("connection-status", env=env)
        self.assertEqual(status.returncode, 0, status.stderr)
        status_body = self.payload(status)
        self.assertTrue(status_body["present"])
        self.assertEqual(status_body["transport_liveness"], "unknown")
        self.assertNotIn("pipe-helper-secret", status.stdout)
        self.assertNotIn("pipe-helper-secret", status.stderr)

        missing = self.run_codex(
            "register-connection", env=self.base_env({"CODEX_THREAD_ID": "session-helper"})
        )
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("CODEX_APP_TOOLS_PIPE_PATH", missing.stderr)
        self.assertEqual(
            connection.read_descriptor("session-helper", self.connections)[
                "connection_generation"
            ],
            1,
        )

        mismatch = self.run_codex(
            "--session-id",
            "different-chat",
            "register-connection",
            env=env,
        )
        self.assertEqual(mismatch.returncode, 5)
        self.assertIn("refusing to register another chat", mismatch.stderr)
        self.assertFalse(self.descriptor_path("different-chat").exists())

    def test_session_start_hook_registers_and_never_overwrites_without_pipe(self):
        env = self.base_env({connection.PIPE_ENV: "hook-pipe"})
        payload = {
            "hook_event_name": "SessionStart",
            "session_id": "hook-session",
            "source": "resume",
        }
        proc = self.run_hook(payload, env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("connection registered", proc.stderr)
        self.assertNotIn("hook-pipe", proc.stderr)
        self.assertTrue(self.descriptor_path("hook-session").is_file())
        generation = connection.read_descriptor("hook-session", self.connections)[
            "connection_generation"
        ]

        no_pipe = self.run_hook(payload, self.base_env())
        self.assertEqual(no_pipe.returncode, 0, no_pipe.stderr)
        self.assertIn("CODEX_APP_TOOLS_PIPE_PATH is absent", no_pipe.stderr)
        self.assertIn("compatibility unknown", no_pipe.stderr)
        self.assertNotIn("hook-pipe", no_pipe.stderr)
        self.assertEqual(
            connection.read_descriptor("hook-session", self.connections)[
                "connection_generation"
            ],
            generation,
        )

        mismatch_env = self.base_env(
            {
                connection.PIPE_ENV: "hook-pipe",
                "CODEX_THREAD_ID": "other-chat",
            }
        )
        mismatch = self.run_hook(payload, mismatch_env)
        self.assertEqual(mismatch.returncode, 0, mismatch.stderr)
        self.assertIn("refusing to register another chat", mismatch.stderr)
        self.assertEqual(
            connection.read_descriptor("hook-session", self.connections)[
                "connection_generation"
            ],
            generation,
        )
        self.assertFalse(self.descriptor_path("other-chat").exists())

    def test_hooks_manifest_command_works_from_quoted_plugin_root(self):
        manifest = json.loads((REPO_ROOT / ".codex-plugin" / "plugin.json").read_text("utf-8"))
        self.assertEqual(manifest["hooks"], "./hooks/hooks.json")
        hook_config = json.loads((REPO_ROOT / "hooks" / "hooks.json").read_text("utf-8"))
        entry = hook_config["hooks"]["SessionStart"][0]
        self.assertIn("resume", entry["matcher"])
        command = entry["hooks"][0]["command"]
        self.assertIn('"$PLUGIN_ROOT/hooks/session_start.py"', command)

        plugin_root = self.work / "plugin root with spaces"
        plugin_root.symlink_to(REPO_ROOT, target_is_directory=True)
        env = self.base_env(
            {
                "PLUGIN_ROOT": str(plugin_root),
                connection.PIPE_ENV: "shell-hook-pipe",
            }
        )
        proc = subprocess.run(
            command,
            shell=True,
            input=json.dumps(
                {
                    "hook_event_name": "SessionStart",
                    "session_id": "shell-hook-session",
                    "source": "startup",
                }
            ),
            capture_output=True,
            text=True,
            cwd="/tmp",
            env=env,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("connection registered", proc.stderr)
        self.assertTrue(self.descriptor_path("shell-hook-session").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
