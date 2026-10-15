"""OMP driver tests: bridge event schema, service closed loop, failure mapping,
and the legacy CLI notifier path.  No real OMP session is involved.
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
from agent_link.shared import parent_bridge_path
from agent_link.storage import Store
from tests import isolate_registry

REPO_ROOT = Path(__file__).resolve().parents[1]


class OmpHostTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        isolate_registry(self)
        self.root = Path(self.temp.name) / "state"
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
        self.bridge = parent_bridge_path(self.root, "b")
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

    def events(self, target=None):
        path = self.bridge if target is None else target
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    # -- tests ---------------------------------------------------------
    def test_driver_capabilities_and_event_schema(self):
        driver = load_driver("agent_link.hosts.omp.parent:OmpParentAdapter")
        capabilities = driver.capabilities()
        self.assertEqual(capabilities["host"], "omp")
        self.assertEqual(capabilities["driver"], "agent_link.hosts.omp.parent:OmpParentAdapter")
        self.assertEqual(capabilities["notification_transport"], "session_bridge_file")
        self.assertEqual(capabilities["receive_mode"], "session_injection")
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
        self.assertEqual(detail, f"omp-bridge:{self.bridge}#m-r")
        report_event, ask_event = self.events()
        self.assertEqual(report_event["v"], 1)
        self.assertEqual(report_event["source"], "service")
        self.assertEqual(report_event["kind"], "report")
        self.assertEqual(report_event["message_id"], "m-r")
        self.assertEqual(report_event["binding_id"], "b")
        self.assertEqual(report_event["thread_id"], "parent")
        self.assertEqual(report_event["parent_framework"], "omp")
        self.assertEqual(report_event["child_framework"], "omp")
        self.assertEqual(report_event["child_session_id"], "child-b")
        self.assertRegex(report_event["ts"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
        self.assertIn("[agent-link:report]", report_event["prompt"])
        self.assertIn("progress text", report_event["prompt"])
        self.assertNotIn("request_id", report_event)
        self.assertEqual(ask_event["request_id"], "q-1")
        self.assertIn("[agent-link:ask]", ask_event["prompt"])
        self.assertIn("request_id: q-1", ask_event["prompt"])
        self.assertIn("The child process is waiting for a reply.", ask_event["prompt"])

    def test_service_closed_loop_with_omp_driver(self):
        status, payload = self.call("report", {"message_id": "m-1", "text": "progress"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["exit_code"], 0)
        self.assertEqual(payload["result"]["delivery_state"], "host_accepted")
        self.assertFalse(payload["result"]["idempotent"])
        self.assertEqual(len(self.events()), 1)
        replay = self.call("report", {"message_id": "m-1", "text": "progress"})[1]
        self.assertTrue(replay["result"]["idempotent"])
        self.assertEqual(len(self.events()), 1, "a replay must not append a second event")

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
        self.assertEqual(len(self.events()), 2)

    def test_notify_failure_is_failed_or_unknown(self):
        # A plain file where the bridge directory belongs makes the append fail.
        (self.root / "omp-bridge").write_text("occupied", encoding="utf-8")
        status, payload = self.call("report", {"message_id": "m-fail", "text": "x"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["exit_code"], 7)
        self.assertEqual(payload["result"]["delivery_state"], "failed_or_unknown")
        # HTTP report replays do not repeat the error in the envelope; the
        # persisted delivery detail is the evidence that the append failed.
        store = Store(self.root)
        try:
            message = store.get_message("b", "m-fail")
        finally:
            store.close()
        self.assertEqual(message["delivery_state"], "failed_or_unknown")
        self.assertIn("omp-bridge", message["delivery_detail"])

    def test_parent_adapter_cli_path_appends_bridge_without_env(self):
        """The registered OMP parent adapter appends the shared bridge file
        directly; the old notifier_path/env-based path is gone."""
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
        # Deliberately no AGENT_LINK_BRIDGE_FILE / AGENT_LINK_BINDING: the
        # adapter resolves the path from the binding's root.
        env.pop("AGENT_LINK_BRIDGE_FILE", None)
        env.pop("AGENT_LINK_BINDING", None)

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
        events = self.events(parent_bridge_path(self.root, "cli-b"))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["source"], "service")
        self.assertEqual(events[0]["binding_id"], "cli-b")
        self.assertEqual(events[0]["thread_id"], "parent-cli")
        self.assertEqual(events[0]["parent_framework"], "omp")
        self.assertEqual(events[0]["child_session_id"], "child-cli")
        self.assertIn("[agent-link:report]", events[0]["prompt"])
        self.assertIn("hello", events[0]["prompt"])
        replay = run_cli("report", "--binding", "cli-b", "--message-id", "m-cli", "--text", "hello")
        self.assertEqual(replay.returncode, 0)
        self.assertTrue(json.loads(replay.stdout)["idempotent"])
        self.assertEqual(len(events), 1, "a replay must not append a second event")


if __name__ == "__main__":
    unittest.main(verbosity=2)
