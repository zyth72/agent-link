"""No-model tests for the OpenCode native prompt push transport.

Every test runs against the fake OpenCode serve in ``tests/opencode_server.py``
or against descriptor validation only: no real ``opencode`` process, plugin,
session or model is involved.  The tests pin the frozen descriptor contract
(managed mark, single password copy, loopback-only origin, private files), the
prompt route and body, Basic auth, idempotent same-id replay, failure mapping
(401 configuration error, 404 failed_or_unknown, timeout unknown), descriptor
re-read per delivery and credential hygiene in errors, logs and the audit
database.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_link.errors import DeliveryFailure
from agent_link.hosts.opencode import push
from agent_link.hosts.opencode.child import OpenCodeChildAdapter
from agent_link.hosts.opencode.parent import OpenCodeParentAdapter
from agent_link.storage import Store
from tests import isolate_registry
from tests.opencode_server import (
    DEFAULT_PASSWORD,
    MockOpenCodeServe,
    write_descriptor_pair,
    write_server_descriptor,
    write_session_descriptor,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
CORE_CLI = [sys.executable, "-m", "agent_link"]


class OpenCodePushCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        isolate_registry(self)
        self.root = Path(self.temp.name) / "root"
        self.sessions = Path(self.temp.name) / "sessions"
        previous = os.environ.get("AGENT_LINK_SESSIONS")
        os.environ["AGENT_LINK_SESSIONS"] = str(self.sessions)

        def restore_env():
            os.environ.pop("AGENT_LINK_SESSIONS", None)
            if previous is not None:
                os.environ["AGENT_LINK_SESSIONS"] = previous

        self.addCleanup(restore_env)
        notifier = Path(self.temp.name) / "stub.py"
        notifier.write_text("def bridge(p):\n    return {}\n", encoding="utf-8")
        store = Store(self.root)
        try:
            store.bind(
                {
                    "binding_id": "b",
                    "run_id": "r-b",
                    "native_task_id": None,
                    "parent": {"framework": "opencode", "session_id": "parent-1"},
                    "child": {"framework": "opencode", "session_id": "child-1"},
                    "notifier_path": str(notifier),
                }
            )
        finally:
            store.close()
        self.serves: list[MockOpenCodeServe] = []
        self.addCleanup(self._close_serves)

    def _close_serves(self):
        for serve in reversed(self.serves):
            serve.close()
        self.serves.clear()

    # -- helpers ------------------------------------------------------------
    def serve(self, session_id: str, **kwargs) -> MockOpenCodeServe:
        server = MockOpenCodeServe(session_id, **kwargs)
        self.serves.append(server)
        return server

    def write_pair(self, serve: MockOpenCodeServe, *, session=None, server=None):
        overrides = {}
        if session:
            overrides["session"] = session
        if server:
            overrides["server"] = server
        return write_descriptor_pair(self.sessions, serve, **overrides)

    def binding(self) -> dict:
        store = Store(self.root)
        try:
            return store.get_binding("b")
        finally:
            store.close()

    def report(self, *, text: str = "progress text", message_id: str = "m-r") -> dict:
        return {"kind": "report", "message_id": message_id, "text": text}

    def ask_child(self, *, message_id: str = "m-c") -> dict:
        return {
            "kind": "ask_child",
            "message_id": message_id,
            "request_id": "rq-1",
            "question": "child, report",
            "timeout_seconds": 5.0,
            "deadline_epoch": time.time() + 5,
        }

    def notify(self, message=None):
        return OpenCodeParentAdapter().notify(self.root, self.binding(), message or self.report())

    def run_cli(self, *args):
        return subprocess.run(
            CORE_CLI + ["--root", str(self.root), *args],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env=dict(os.environ),
            timeout=60,
        )

    @staticmethod
    def dead_pid() -> int:
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        process.wait(timeout=10)
        return process.pid

    def assert_no_file_fallback(self):
        self.assertFalse((self.root / "omp-bridge").exists(), "no bridge file fallback")
        self.assertFalse((self.root / "inbox").exists(), "no inbox file fallback")

    # -- happy path ---------------------------------------------------------
    def test_parent_dispatch_posts_queue_prompt_over_basic_auth(self):
        server = self.serve("parent-1")
        self.write_pair(server)
        detail = self.notify()
        self.assertEqual(detail, "push:opencode/parent-1#m-r")
        self.assertNotIn(DEFAULT_PASSWORD, detail)
        self.assertEqual(len(server.requests), 1)
        request = server.requests[0]
        self.assertEqual(request["path"], "/api/session/parent-1/prompt")
        expected = "Basic " + base64.b64encode(
            b"opencode:" + DEFAULT_PASSWORD.encode("utf-8")
        ).decode("ascii")
        self.assertEqual(request["authorization"], expected)
        prompt = server.prompts[0]
        self.assertEqual(set(prompt), {"id", "text", "delivery"}, "no resume field is sent")
        self.assertEqual(prompt["id"], "msg_m-r")
        self.assertEqual(prompt["delivery"], "queue")
        self.assertIn("[agent-link:report]", prompt["text"])
        self.assertIn("message_id: m-r", prompt["text"])
        self.assertIn("child: opencode/child-1", prompt["text"])
        self.assertIn("progress text", prompt["text"])

    def test_child_dispatch_posts_parent_ask_prompt(self):
        server = self.serve("child-1")
        self.write_pair(server)
        detail = OpenCodeChildAdapter().deliver(self.root, self.binding(), self.ask_child())
        self.assertEqual(detail, "push:opencode/child-1#m-c")
        prompt = server.prompts[0]
        self.assertEqual(prompt["id"], "msg_m-c")
        self.assertEqual(prompt["delivery"], "queue")
        self.assertIn("[agent-link:parent-ask]", prompt["text"])
        self.assertIn("request_id: rq-1", prompt["text"])
        self.assertIn("child, report", prompt["text"])

    def test_same_message_id_replays_idempotently(self):
        server = self.serve("parent-1")
        self.write_pair(server)
        first = self.notify(self.report(text="first text", message_id="m-dup"))
        second = self.notify(self.report(text="second text", message_id="m-dup"))
        self.assertEqual(first, second)
        self.assertEqual(len(server.requests), 2, "the retry is sent with the same id")
        self.assertEqual(len(server.prompts), 2)
        self.assertEqual(server.prompts[0]["id"], "msg_m-dup")
        self.assertEqual(server.prompts[1]["id"], "msg_m-dup")
        self.assertIn("first text", server.admissions["msg_m-dup"]["payload"]["text"])
        self.assertEqual(
            server.responses[0],
            server.responses[1],
            "upstream first admission wins for the same prompt id",
        )

    # -- descriptors and identity ------------------------------------------
    def test_missing_descriptors_fail_without_file_fallback(self):
        with self.assertRaises(DeliveryFailure) as caught:
            self.notify()
        self.assertIn("no file fallback", str(caught.exception))
        self.assert_no_file_fallback()

        server = self.serve("parent-1")
        write_session_descriptor(self.sessions, server.session_descriptor())
        with self.assertRaises(DeliveryFailure):
            self.notify()
        self.assertEqual(server.requests, [], "a missing serve descriptor must not be contacted")
        self.assert_no_file_fallback()

    def test_descriptor_identity_mismatches_fail_without_contact(self):
        server = self.serve("parent-1")
        cases = (
            {"session": {"protocol": "agent-link-opencode-push/0"}},
            {"session": {"framework": "pi"}},
            {"session": {"session_id": "other-session"}},
            {"session": {"serve_id": "serve-missing"}},
            {"session": {"endpoint": "http://127.0.0.1:9"}},
            {"session": {"pid": os.getppid()}},
            {"server": {"protocol": "agent-link-opencode-push/0"}},
            {"server": {"framework": "pi"}},
            {"server": {"serve_id": "serve-missing"}},
            {"server": {"endpoint": "http://127.0.0.1:9"}},
            {"server": {"pid": os.getppid()}},
        )
        for case in cases:
            with self.subTest(case=case):
                self.write_pair(server, **case)
                with self.assertRaises(DeliveryFailure):
                    self.notify()
        self.assertEqual(server.requests, [], "mismatched descriptors must never be contacted")

    def test_unmanaged_descriptors_are_rejected(self):
        server = self.serve("parent-1")
        for case in (
            {"session": {"managed": False}},
            {"session": {"managed": "true"}},
            {"server": {"managed": False}},
        ):
            with self.subTest(case=case):
                self.write_pair(server, **case)
                with self.assertRaises(DeliveryFailure) as caught:
                    self.notify()
                self.assertIn("managed", str(caught.exception))
        self.assertEqual(server.requests, [])

        # A descriptor without the mark at all is the attach path: rejected.
        descriptor = server.session_descriptor()
        del descriptor["managed"]
        write_session_descriptor(self.sessions, descriptor)
        with self.assertRaises(DeliveryFailure) as caught:
            self.notify()
        self.assertIn("managed", str(caught.exception))
        self.assertEqual(server.requests, [])

    def test_dead_or_invalid_pid_is_rejected(self):
        server = self.serve("parent-1")
        dead = self.dead_pid()
        for pid in (dead, 0, -1, "1", None, True):
            with self.subTest(pid=pid):
                self.write_pair(server, session={"pid": pid})
                with self.assertRaises(DeliveryFailure) as caught:
                    self.notify()
                self.assertIn("not running", str(caught.exception))
        self.assertEqual(server.requests, [])

    def test_session_descriptor_must_not_carry_password(self):
        server = self.serve("parent-1")
        self.write_pair(server, session={"password": "s" * 32})
        with self.assertRaises(DeliveryFailure) as caught:
            self.notify()
        self.assertIn("must not carry a password", str(caught.exception))
        self.assertNotIn("s" * 32, str(caught.exception))
        self.assertEqual(server.requests, [])

    def test_reserved_credential_kinds_fail_closed(self):
        server = self.serve("parent-1")
        for case in (
            {"session": {"credential_kind": "session_push_token"}},
            {"session": {"credential_kind": None}},
            {"server": {"credential_kind": "server_descriptor"}},
            {"server": {"credential_kind": "bearer_token"}},
        ):
            with self.subTest(case=case):
                self.write_pair(server, **case)
                with self.assertRaises(DeliveryFailure):
                    self.notify()
        self.assertEqual(server.requests, [])

    def test_malformed_descriptor_json_fails(self):
        server = self.serve("parent-1")
        self.write_pair(server)
        session_path = push.session_descriptor_path("opencode", "parent-1")
        original = session_path.read_text(encoding="utf-8")
        for content in ("not json", "[]", '"text"'):
            with self.subTest(content=content):
                session_path.write_text(content, encoding="utf-8")
                os.chmod(session_path, 0o600)
                with self.assertRaises(DeliveryFailure):
                    self.notify()
        session_path.write_text(original, encoding="utf-8")
        os.chmod(session_path, 0o600)
        self.assertEqual(server.requests, [])

    def test_private_descriptor_files_are_required(self):
        server = self.serve("parent-1")
        for case in ("session", "server"):
            with self.subTest(case=case):
                if case == "session":
                    write_session_descriptor(
                        self.sessions, server.session_descriptor(), mode=0o644
                    )
                    write_server_descriptor(self.sessions, server.server_descriptor())
                else:
                    write_session_descriptor(self.sessions, server.session_descriptor())
                    write_server_descriptor(
                        self.sessions, server.server_descriptor(), mode=0o644
                    )
                with self.assertRaises(DeliveryFailure) as caught:
                    self.notify()
                self.assertIn("0600", str(caught.exception))
        self.assertEqual(server.requests, [])

    def test_non_loopback_endpoints_are_rejected(self):
        server = self.serve("parent-1")
        port = server.port
        bad_endpoints = (
            "http://192.0.2.10:1234",
            "http://localhost:1234",
            "https://127.0.0.1:%d" % port,
            "http://127.0.0.1:%d/path" % port,
            "http://127.0.0.1:%d/?x=1" % port,
            "http://127.0.0.1:%d/#fragment" % port,
            "http://user@127.0.0.1:%d" % port,
            "http://127.0.0.1",
            "http://[::1]:%d" % port,
            "unix:///tmp/socket",
        )
        for endpoint in bad_endpoints:
            with self.subTest(endpoint=endpoint):
                self.write_pair(server, session={"endpoint": endpoint})
                with self.assertRaises(DeliveryFailure):
                    self.notify()
        self.assertEqual(server.requests, [], "non-loopback endpoints must never be contacted")

    def test_descriptors_are_reread_for_each_delivery(self):
        first = self.serve("parent-1", password="a" * 32)
        second = self.serve("parent-1", password="b" * 32)
        self.write_pair(first)
        self.notify()
        self.assertEqual(len(first.requests), 1)
        # Repoint the session descriptor at the second serve: no cached endpoint,
        # password or pid is reused across deliveries.
        self.write_pair(second)
        self.notify(self.report(message_id="m-2"))
        self.assertEqual(len(first.requests), 1, "the old serve must not be reused")
        self.assertEqual(len(second.requests), 1)
        self.assertEqual(second.requests[0]["path"], "/api/session/parent-1/prompt")

    # -- failure mapping ----------------------------------------------------
    def test_redirect_is_not_followed(self):
        target = self.serve("parent-1")
        redirector = self.serve("parent-1", redirect_to=target.endpoint + "/evil")
        self.write_pair(redirector)
        with self.assertRaises(DeliveryFailure) as caught:
            self.notify()
        self.assertIn("302", str(caught.exception))
        self.assertEqual(target.requests, [], "the redirect target must not be contacted")

    def test_401_is_configuration_error_without_retry(self):
        server = self.serve("parent-1", password="c" * 32)
        self.write_pair(server, server={"password": "d" * 32})
        with self.assertRaises(DeliveryFailure) as caught:
            self.notify()
        message = str(caught.exception)
        self.assertIn("401", message)
        self.assertIn("configuration error", message)
        self.assertIn("no retry", message)
        self.assertNotIn("d" * 32, message)
        self.assertNotIn("c" * 32, message)
        self.assertEqual(len(server.requests), 1, "401 is not retried automatically")

    def test_404_maps_to_failed_or_unknown_without_resend(self):
        server = self.serve("other-session")
        self.write_pair(server, session={"session_id": "parent-1"})
        self.assertEqual(server.session_id, "other-session")
        with self.assertRaises(DeliveryFailure) as caught:
            self.notify()
        message = str(caught.exception)
        self.assertIn("404", message)
        self.assertIn("failed_or_unknown", message)
        self.assertEqual(len(server.requests), 1)

    def test_timeout_is_unknown_without_file_fallback(self):
        server = self.serve("parent-1", delay_seconds=2.0)
        self.write_pair(server)
        with mock.patch.object(push, "TIMEOUT_SECONDS", 0.2):
            with self.assertRaises(DeliveryFailure) as caught:
                self.notify()
        message = str(caught.exception)
        self.assertIn("timed out or is unavailable", message)
        self.assertIn("outcome unknown", message)
        self.assert_no_file_fallback()

    def test_server_error_status_is_failed_or_unknown(self):
        server = self.serve("parent-1", respond_status=500, respond_body={"error": "boom"})
        self.write_pair(server)
        with self.assertRaises(DeliveryFailure) as caught:
            self.notify()
        self.assertIn("500", str(caught.exception))
        self.assertIn("failed_or_unknown", str(caught.exception))

    def test_response_session_echo_mismatch_is_not_accepted(self):
        server = self.serve(
            "parent-1",
            respond_status=200,
            respond_body={"data": {"id": "m-r", "sessionID": "someone-else", "type": "user"}},
        )
        self.write_pair(server)
        with self.assertRaises(DeliveryFailure) as caught:
            self.notify()
        self.assertIn("acknowledged", str(caught.exception))

        server = self.serve(
            "parent-1",
            respond_status=200,
            respond_body={"data": {"id": "different", "sessionID": "parent-1", "type": "user"}},
        )
        self.write_pair(server)
        with self.assertRaises(DeliveryFailure) as caught:
            self.notify()
        self.assertIn("different prompt id", str(caught.exception))

    def test_unwrapped_response_envelope_is_also_accepted(self):
        server = self.serve(
            "parent-1",
            respond_status=200,
            respond_body={"id": "msg_m-r", "sessionID": "parent-1", "type": "user"},
        )
        self.write_pair(server)
        detail = self.notify()
        self.assertEqual(detail, "push:opencode/parent-1#m-r")

    def test_oversized_prompt_is_rejected_before_contact(self):
        server = self.serve("parent-1")
        self.write_pair(server)
        with self.assertRaises(DeliveryFailure) as caught:
            self.notify(self.report(text="x" * 70000))
        self.assertIn("exceeds", str(caught.exception))
        self.assertEqual(server.requests, [])

    # -- credential hygiene -------------------------------------------------
    def test_password_never_in_errors_logs_or_audit(self):
        password = "secret" * 8
        server = self.serve("parent-1", password="server" * 8)
        self.write_pair(server, server={"password": password})
        with self.assertRaises(DeliveryFailure) as caught:
            self.notify()
        self.assertNotIn(password, str(caught.exception))
        self.assertNotIn(password, repr(caught.exception))

        # The CLI must not print it and the audit row must not store it.
        proc = self.run_cli("report", "--binding", "b", "--message-id", "m-cli", "--text", "hi")
        self.assertEqual(proc.returncode, 7, proc.stdout + proc.stderr)
        self.assertNotIn(password, proc.stdout)
        self.assertNotIn(password, proc.stderr)
        store = Store(self.root)
        try:
            row = store.get_message("b", "m-cli")
        finally:
            store.close()
        self.assertEqual(row["delivery_state"], "failed_or_unknown")
        self.assertNotIn(password, row["delivery_detail"] or "")
        self.assertIn("401", row["delivery_detail"])

        # A successful audit detail contains only the transport identity.
        good = self.serve("child-1", password=password)
        self.write_pair(good)
        detail = OpenCodeChildAdapter().deliver(self.root, self.binding(), self.ask_child())
        self.assertNotIn(password, detail)
        self.assertEqual(detail, "push:opencode/child-1#m-c")


if __name__ == "__main__":
    unittest.main(verbosity=2)
