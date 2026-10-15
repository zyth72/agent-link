"""OMP driver tests: session-owned push schema, service closed loop, failure
mapping and the CLI dispatch path.  No real OMP session or model is involved.

The recipient session is a real loopback HTTP mock listener; the parent adapter
delivers an authenticated JSON payload to ``AGENT_LINK_SESSIONS/omp/<session>.json``.
No bridge file is read or written.
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
import urllib.error
import urllib.request
from pathlib import Path

from agent_link.errors import AgentLinkError
from agent_link.host import HostService, load_driver
from agent_link.hosts.omp.parent import OmpParentAdapter
from agent_link.hosts.push import PROTOCOL
from agent_link.storage import Store
from tests import isolate_registry
from tests.push_server import MockPushListener, write_descriptor

REPO_ROOT = Path(__file__).resolve().parents[1]


class OmpHostTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        isolate_registry(self)
        self.root = Path(self.temp.name) / "state"
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
                    "parent": {"framework": "omp", "session_id": "parent"},
                    "child": {"framework": "omp", "session_id": "child-b"},
                    "notifier_path": str(notifier),
                }
            )
        finally:
            store.close()
        self.listener = MockPushListener("omp", "parent")
        self.addCleanup(self.listener.close)
        self.descriptor_path = write_descriptor(self.sessions, self.listener.descriptor())
        self.service = self.start_service(OmpParentAdapter())

    # -- helpers -------------------------------------------------------
    def start_service(self, driver):
        server = HostService(self.root, "b", "test-token", driver)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(self.stop_service, server, thread)
        return server

    def stop_service(self, server, thread):
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    def call(self, path, body, token="test-token"):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.service.server_port}/v1/{path}",
            data=json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as result:
                return result.status, json.load(result)
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.load(error)
            finally:
                error.close()

    # -- tests ---------------------------------------------------------
    def test_driver_capabilities_and_push_schema(self):
        driver = load_driver("agent_link.hosts.omp.parent:OmpParentAdapter")
        capabilities = driver.capabilities()
        self.assertEqual(capabilities["host"], "omp")
        self.assertEqual(capabilities["driver"], "agent_link.hosts.omp.parent:OmpParentAdapter")
        self.assertEqual(capabilities["notification_transport"], "session_push_http")
        self.assertEqual(capabilities["receive_mode"], "session_injection")
        self.assertEqual(capabilities["busy_behavior"], "aside")
        self.assertFalse(capabilities["polling"])
        store = Store(self.root)
        try:
            binding = store.get_binding("b")
            detail = driver.notify(
                self.root,
                binding,
                {"kind": "report", "message_id": "m-r", "text": "progress text"},
            )
            driver.notify(
                self.root,
                binding,
                {
                    "kind": "ask",
                    "message_id": "m-q",
                    "request_id": "q-1",
                    "question": "why?",
                    "timeout_seconds": 5.0,
                    "deadline_epoch": time.time() + 5,
                },
            )
        finally:
            store.close()
        self.assertEqual(detail, "push:omp/parent#m-r")
        report_event, ask_event = self.listener.events
        self.assertEqual(report_event["protocol"], PROTOCOL)
        self.assertEqual(report_event["framework"], "omp")
        self.assertEqual(report_event["session_id"], "parent")
        self.assertEqual(report_event["binding_id"], "b")
        self.assertEqual(report_event["run_id"], "r-b")
        self.assertEqual(report_event["role"], "parent")
        self.assertEqual(report_event["message_id"], "m-r")
        self.assertIn("[agent-link:report]", report_event["prompt"])
        self.assertIn("progress text", report_event["prompt"])
        self.assertNotIn("request_id", report_event)
        self.assertEqual(ask_event["message_id"], "m-q")
        self.assertIn("[agent-link:ask]", ask_event["prompt"])
        self.assertIn("request_id: q-1", ask_event["prompt"])
        self.assertIn("why?", ask_event["prompt"])
        self.assertIn("The child process is waiting for a reply.", ask_event["prompt"])

    def test_service_closed_loop_with_omp_driver(self):
        status, payload = self.call("report", {"message_id": "m-1", "text": "progress"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["exit_code"], 0)
        self.assertEqual(payload["result"]["delivery_state"], "host_accepted")
        self.assertFalse(payload["result"]["idempotent"])
        self.assertEqual(len(self.listener.events), 1)
        self.assertIn("progress", self.listener.events[0]["prompt"])
        replay = self.call("report", {"message_id": "m-1", "text": "progress"})[1]
        self.assertTrue(replay["result"]["idempotent"])
        self.assertEqual(len(self.listener.events), 1, "a replay must not re-deliver")

        # The parent answers through the ordinary reply path while ask waits.
        def answer_when_persisted():
            deadline = time.time() + 10
            while time.time() < deadline:
                store = Store(self.root)
                try:
                    row = store.get_request("b", "q-1")
                except AgentLinkError:
                    row = None
                finally:
                    store.close()
                if row is not None:
                    store = Store(self.root)
                    try:
                        store.reply("b", "q-1", "r-1", "parent decision")
                    finally:
                        store.close()
                    return
                time.sleep(0.05)

        replier = threading.Thread(target=answer_when_persisted)
        replier.start()
        status, payload = self.call(
            "ask", {"request_id": "q-1", "message_id": "m-q", "question": "choose", "timeout": 10}
        )
        replier.join(timeout=10)
        self.assertEqual(status, 200)
        self.assertEqual(payload["exit_code"], 0)
        self.assertEqual(payload["result"]["status"], "replied")
        self.assertEqual(payload["result"]["answer"], "parent decision")
        self.assertEqual(payload["result"]["delivery_state"], "host_accepted")
        self.assertEqual(len(self.listener.events), 2)
        self.assertIn("choose", self.listener.events[1]["prompt"])

    def test_missing_descriptor_fails_without_automatic_retry(self):
        self.descriptor_path.unlink()
        status, payload = self.call("report", {"message_id": "m-fail", "text": "x"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["exit_code"], 7)
        self.assertEqual(payload["result"]["delivery_state"], "failed_or_unknown")
        self.assertEqual(self.listener.events, [])
        self.assertFalse((self.root / "omp-bridge").exists(), "no bridge fallback")
        store = Store(self.root)
        try:
            message = store.get_message("b", "m-fail")
        finally:
            store.close()
        self.assertEqual(message["delivery_state"], "failed_or_unknown")
        self.assertIn("session push unavailable", message["delivery_detail"])
        # Replaying the same message_id reports the persisted failure and never
        # attempts a second delivery.
        status, replay = self.call("report", {"message_id": "m-fail", "text": "x"})
        self.assertEqual(status, 200)
        self.assertEqual(replay["result"]["delivery_state"], "failed_or_unknown")
        self.assertTrue(replay["result"]["idempotent"])
        self.assertEqual(self.listener.events, [], "a failed message is never auto-retried")

    def test_stale_and_identity_mismatch_descriptors_fail(self):
        for overrides in (
            {"protocol": "agent-link-push/0"},
            {"framework": "pi"},
            {"session_id": "someone-else"},
        ):
            with self.subTest(overrides=overrides):
                write_descriptor(self.sessions, self.listener.descriptor(**overrides))
                status, payload = self.call(
                    "report", {"message_id": "m-" + sorted(overrides)[0], "text": "x"}
                )
                self.assertEqual(payload["exit_code"], 7)
                self.assertEqual(payload["result"]["delivery_state"], "failed_or_unknown")
        self.assertEqual(self.listener.requests, [], "mismatched descriptors are never contacted")

    def test_parent_adapter_cli_path_uses_push_without_bridge_env(self):
        """The registered OMP parent adapter delivers over push; the old
        notifier_path/env-based bridge path is gone."""
        listener = MockPushListener("omp", "parent-cli")
        self.addCleanup(listener.close)
        write_descriptor(self.sessions, listener.descriptor())
        config = {
            "binding_id": "cli-b",
            "run_id": "r-cli",
            "native_task_id": None,
            "parent": {"framework": "omp", "session_id": "parent-cli"},
            "child": {"framework": "omp", "session_id": "child-cli"},
            "notifier_path": str(REPO_ROOT / "agent_link" / "shared.py"),
        }
        config_path = Path(self.temp.name) / "bind-cli.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        env.pop("AGENT_LINK_BRIDGE_FILE", None)
        env.pop("AGENT_LINK_BINDING", None)
        env.pop("AGENT_LINK_CREDENTIALS", None)

        def run_cli(*args):
            return subprocess.run(
                [sys.executable, "-m", "agent_link", "--root", str(self.root), *args],
                capture_output=True,
                text=True,
                cwd=str(REPO_ROOT),
                env=env,
                timeout=30,
            )

        bound = run_cli("bind", "--config", str(config_path))
        self.assertEqual(bound.returncode, 0, bound.stderr)
        report = run_cli("report", "--binding", "cli-b", "--message-id", "m-cli", "--text", "hello")
        self.assertEqual(report.returncode, 0, report.stderr)
        self.assertEqual(len(listener.events), 1)
        event = listener.events[0]
        self.assertEqual(event["framework"], "omp")
        self.assertEqual(event["session_id"], "parent-cli")
        self.assertEqual(event["binding_id"], "cli-b")
        self.assertEqual(event["role"], "parent")
        self.assertIn("[agent-link:report]", event["prompt"])
        self.assertIn("hello", event["prompt"])
        replay = run_cli("report", "--binding", "cli-b", "--message-id", "m-cli", "--text", "hello")
        self.assertEqual(replay.returncode, 0)
        self.assertTrue(json.loads(replay.stdout)["idempotent"])
        self.assertEqual(len(listener.events), 1, "a replay must not re-deliver")


if __name__ == "__main__":
    unittest.main(verbosity=2)
