"""Loopback host-service tests: protocol limits, replay safety, driver failures.

All notifications go to an in-process fake driver; no real desktop message or
background-tasks bridge is involved.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from agent_link.errors import ValidationError
from tests import isolate_registry
from agent_link.host import (
    MAX_BODY_BYTES,
    MAX_HTTP_ASK_TIMEOUT_SECONDS,
    Handler,
    HostService,
    load_driver,
    remove_credentials_if_ours,
    write_credentials,
)
from agent_link.storage import Store

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeHost:
    """Parent-side driver double: records calls and can fail or reply."""

    def __init__(self):
        self.calls = []
        self.fail = False
        self.delay = 0.0
        self.answer = "parent decision"
        self._lock = threading.Lock()

    def capabilities(self):
        return {"host": "fake", "wake_after_turn": "unknown"}

    def notify(self, root, binding, message):
        with self._lock:
            self.calls.append(message)
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("simulated driver failure")
        if message["kind"] == "ask":
            store = Store(root)
            try:
                store.reply(
                    binding["binding_id"],
                    message["request_id"],
                    "stub-reply-" + message["request_id"],
                    self.answer,
                )
            finally:
                store.close()
        return "accepted"


class HostTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        isolate_registry(self)
        self.root = Path(self.temp.name) / "state"
        notifier = Path(self.temp.name) / "stub.py"
        notifier.write_text("def bridge(p):\n    return {}\n", encoding="utf-8")
        store = Store(self.root)
        try:
            for binding_id in ("b", "b2"):
                store.bind(
                    {
                        "binding_id": binding_id,
                        "run_id": "r-" + binding_id,
                        "native_task_id": None,
                        "parent": {"framework": "codex", "session_id": "parent"},
                        "child": {"framework": "pi", "session_id": "child-" + binding_id},
                        "notifier_path": str(notifier),
                    }
                )
        finally:
            store.close()
        self.driver = FakeHost()
        self.services = []
        self.service = self.start_service(self.driver)

    # -- helpers -------------------------------------------------------
    def start_service(self, driver):
        server = HostService(self.root, "b", "test-token", driver)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        self.addCleanup(self.stop_service, server, thread)
        self.services.append((server, thread))
        return server

    def stop_service(self, server, thread):
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    def call(self, path, body, token="test-token", server=None, method="POST", raw_body=None):
        server = server or self.service
        data = raw_body if raw_body is not None else json.dumps(body).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_port}/v1/{path}",
            data=data,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as result:
                return result.status, json.load(result)
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.load(error)
            finally:
                error.close()

    def parallel_calls(self, path, body):
        results = [None, None]
        barrier = threading.Barrier(2)

        def worker(index):
            barrier.wait(timeout=5)
            results[index] = self.call(path, body)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        return results

    # -- protocol basics ----------------------------------------------
    def test_scoped_report_and_replay(self):
        body = {"message_id": "m", "text": "progress"}
        first = self.call("report", body)[1]
        self.assertEqual(first["result"]["delivery_state"], "host_accepted")
        self.assertFalse(first["result"]["idempotent"])
        replay = self.call("report", body)[1]
        self.assertTrue(replay["result"]["idempotent"])
        self.assertEqual(replay["result"]["delivery_state"], "host_accepted")
        self.assertEqual(len(self.driver.calls), 1)
        self.assertEqual(self.call("report", dict(body, text="changed"))[0], 409)
        status, error = self.call("report", dict(body, text="changed"))
        self.assertEqual(status, 409)
        self.assertEqual(error["error"], "conflict")

    def test_reply_roundtrip(self):
        status, result = self.call(
            "ask", {"request_id": "q", "message_id": "m", "question": "choose", "timeout": 2}
        )
        self.assertEqual(status, 200)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["result"]["answer"], "parent decision")
        self.assertEqual(result["result"]["status"], "replied")

    def test_no_parent_privileges_or_rebinding(self):
        self.assertEqual(self.call("reply", {})[0], 404)
        self.assertEqual(self.call("report", {"binding": "other", "message_id": "m", "text": "x"})[0], 400)
        self.assertEqual(self.call("capabilities", {}, token="wrong")[0], 401)
        self.assertEqual(self.driver.calls, [])

    def test_timeout_persists(self):
        # Driver does not answer: child observes timeout and can query it later.
        self.driver.notify = lambda *args: "accepted"
        result = self.call(
            "ask", {"request_id": "q", "message_id": "m", "question": "choose", "timeout": 0.05}
        )[1]
        self.assertEqual(result["exit_code"], 3)
        got = self.call("get-request", {"request_id": "q"})[1]["result"]
        self.assertEqual(got["status"], "timed_out")
        self.assertIsNone(got["answer"])

    def test_capabilities_and_unknown_operations_are_json(self):
        status, payload = self.call("capabilities", {})
        self.assertEqual(status, 200)
        self.assertEqual(payload["result"]["host"], "fake")
        status, payload = self.call("bind", {})
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "not_found")
        self.assertEqual(payload["exit_code"], 6)
        status, payload = self.call("get-request", {"request_id": "nope"})
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "not_found")
        status, payload = self.call("report", {"message_id": "m", "text": "x"}, server=self.service, method="GET")
        self.assertEqual(status, 405)
        self.assertEqual(payload["error"], "method_not_allowed")

    def test_unknown_and_missing_fields_rejected(self):
        bad_bodies = (
            {"message_id": "m", "text": "x", "extra": 1},
            {"message_id": "m"},
            {"request_id": "q", "message_id": "m", "question": "q", "timeout": 1, "binding": "b2"},
            {"request_id": "q"},
        )
        paths = ("report", "report", "ask", "ask")
        for path, body in zip(paths, bad_bodies):
            with self.subTest(path=path, body=body):
                status, payload = self.call(path, body)
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"], "validation")
        self.assertEqual(self.driver.calls, [])

    def test_malformed_and_empty_bodies_are_json_errors(self):
        status, payload = self.call("report", None, raw_body=b"{not json")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "validation")
        status, payload = self.call("report", None, raw_body=b"")
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "validation")

    def test_http_ask_timeout_is_bounded(self):
        self.assertEqual(MAX_BODY_BYTES, 65536)
        self.assertEqual(MAX_HTTP_ASK_TIMEOUT_SECONDS, 3600.0)
        for bad in (0, -1, 3600.5, 1e9, "nan", "inf", True, "abc", None):
            with self.subTest(timeout=bad):
                status, payload = self.call(
                    "ask",
                    {
                        "request_id": "q-bad",
                        "message_id": "m-bad",
                        "question": "q",
                        "timeout": bad,
                    },
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"], "validation")
        self.assertEqual(self.driver.calls, [])
        # The bounded value itself is accepted and uses the ordinary wait path.
        status, payload = self.call(
            "ask", {"request_id": "q-max", "message_id": "m-max", "question": "q", "timeout": 3600}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["exit_code"], 0)

    # -- replay / concurrency / failure --------------------------------
    def test_driver_failure_records_failed_and_is_not_retried(self):
        self.driver.fail = True
        body = {"message_id": "m-fail", "text": "x"}
        first = self.call("report", body)[1]
        self.assertEqual(first["exit_code"], 7)
        self.assertEqual(first["result"]["delivery_state"], "failed_or_unknown")
        replay = self.call("report", body)[1]
        self.assertEqual(replay["exit_code"], 7)
        self.assertTrue(replay["result"]["idempotent"])
        self.assertEqual(len(self.driver.calls), 1)
        # ask with a failing driver times out but keeps the failed delivery visible.
        asked = self.call(
            "ask", {"request_id": "q-fail", "message_id": "m-q-fail", "question": "q", "timeout": 0.05}
        )[1]
        self.assertEqual(asked["exit_code"], 3)
        self.assertEqual(asked["result"]["delivery_state"], "failed_or_unknown")
        self.assertEqual(len(self.driver.calls), 2)

    def test_concurrent_same_report_delivers_once(self):
        self.driver.delay = 0.4
        body = {"message_id": "m-conc", "text": "x"}
        results = self.parallel_calls("report", body)
        for status, payload in results:
            self.assertEqual(status, 200)
            self.assertEqual(payload["exit_code"], 0)
            self.assertEqual(payload["result"]["delivery_state"], "host_accepted")
        self.assertEqual(sorted(r[1]["result"]["idempotent"] for r in results), [False, True])
        self.assertEqual(len(self.driver.calls), 1)

    def test_concurrent_same_ask_delivers_once(self):
        self.driver.delay = 0.3
        body = {"request_id": "q-conc", "message_id": "m-q-conc", "question": "q", "timeout": 5}
        results = self.parallel_calls("ask", body)
        for status, payload in results:
            self.assertEqual(status, 200)
            self.assertEqual(payload["exit_code"], 0)
            self.assertEqual(payload["result"]["answer"], "parent decision")
        self.assertEqual(len(self.driver.calls), 1)

    def test_restart_server_replays_without_redispatch(self):
        body = {"message_id": "m-restart", "text": "x"}
        self.assertEqual(self.call("report", body)[1]["result"]["delivery_state"], "host_accepted")
        first_driver = self.driver
        self.assertEqual(len(first_driver.calls), 1)
        self.stop_service(*self.services.pop(0))
        second_driver = FakeHost()
        second = self.start_service(second_driver)
        status, payload = self.call("report", body, server=second)
        self.assertEqual(status, 200)
        self.assertTrue(payload["result"]["idempotent"])
        self.assertEqual(payload["result"]["delivery_state"], "host_accepted")
        self.assertEqual(second_driver.calls, [])

    def test_cross_binding_request_and_field(self):
        # A request persisted under another binding is not visible through b's service.
        store = Store(self.root)
        try:
            store.prepare_ask("b2", "q-other", "m-other", "q", 30.0)
            store.reply("b2", "q-other", "r-other", "other answer")
        finally:
            store.close()
        status, payload = self.call("get-request", {"request_id": "q-other"})
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "not_found")
        # The service rejects attempts to target another binding.
        status, payload = self.call(
            "report", {"binding": "b2", "message_id": "m", "text": "x"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "validation")
        self.assertEqual(self.driver.calls, [])

    # -- resource limits -----------------------------------------------
    def test_body_read_timeout_returns_json_408(self):
        request = (
            b"POST /v1/report HTTP/1.1\r\n"
            b"Host: 127.0.0.1\r\n"
            b"Authorization: Bearer test-token\r\n"
            b"Content-Length: 10\r\n\r\n"
        )
        with mock.patch.object(Handler, "timeout", 0.5):
            with socket.create_connection(("127.0.0.1", self.service.server_port), timeout=5) as conn:
                conn.sendall(request)  # headers only, body never sent
                conn.settimeout(5)
                # The response closes the connection; headers and body may
                # arrive in separate TCP segments, so read until EOF instead
                # of trusting a single recv to contain both.
                chunks = []
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
                response = b"".join(chunks)
        self.assertIn(b"408", response.split(b"\r\n", 1)[0])
        self.assertIn(b"request_timeout", response)
        self.assertIn(b"application/json", response)

    def test_credentials_file_is_never_overwritten(self):
        target = Path(self.temp.name) / "nested" / "credentials.json"
        credentials = {"protocol": "agent-link/1", "endpoint": "http://127.0.0.1:1", "token": "x" * 32}
        write_credentials(target, credentials)
        self.assertEqual(json.loads(target.read_text()), credentials)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            write_credentials(target, dict(credentials, token="y" * 32))
        self.assertEqual(json.loads(target.read_text()), credentials)

    def test_remove_credentials_only_when_still_ours(self):
        target = Path(self.temp.name) / "credentials.json"
        credentials = {"protocol": "agent-link/1", "endpoint": "http://127.0.0.1:1", "token": "x" * 32}
        write_credentials(target, credentials)
        remove_credentials_if_ours(target, dict(credentials, token="y" * 32))
        self.assertTrue(target.exists(), "a different service's file must survive")
        remove_credentials_if_ours(target, credentials)
        self.assertFalse(target.exists())

    def test_host_cli_refuses_existing_credentials(self):
        credentials_path = Path(self.temp.name) / "cli-credentials.json"
        credentials_path.write_text('{"kept": true}', encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "agent_link.host",
                "--root",
                str(self.root),
                "--binding",
                "b",
                "--credentials",
                str(credentials_path),
            ],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            env=env,
            timeout=30,
        )
        self.assertNotEqual(process.returncode, 0)
        self.assertIn("credentials", process.stderr)
        self.assertEqual(credentials_path.read_text(encoding="utf-8"), '{"kept": true}')

    def test_host_cli_loads_custom_driver(self):
        """--driver MODULE:OBJECT is the documented host-extension point."""
        credentials_path = Path(self.temp.name) / "driver-credentials.json"
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT)
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agent_link.host",
                "--root",
                str(self.root),
                "--binding",
                "b",
                "--credentials",
                str(credentials_path),
                "--driver",
                "tests.fake_host_driver:RecordingDriver",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(REPO_ROOT),
            env=env,
        )
        try:
            ready_line = process.stdout.readline()
            if not ready_line:
                raise AssertionError(f"host service did not become ready: {process.stderr.read()}")
            endpoint = json.loads(ready_line)["endpoint"]
            token = json.loads(credentials_path.read_text(encoding="utf-8"))["token"]
            request = urllib.request.Request(
                endpoint + "/v1/capabilities",
                data=b"{}",
                headers={"Authorization": "Bearer " + token},
            )
            with urllib.request.urlopen(request, timeout=10) as response:
                payload = json.load(response)
            self.assertEqual(payload["result"]["host"], "recording-fake")
        finally:
            process.terminate()
            process.wait(timeout=15)
            process.stdout.close()
            process.stderr.close()

    def test_load_driver_accepts_class_instance_and_factory(self):
        driver = load_driver("agent_link.hosts.codex.parent:CodexParentAdapter")
        self.assertTrue(callable(driver.notify))
        self.assertTrue(callable(driver.capabilities))
        module = __import__("types").SimpleNamespace(
            instance=FakeHost(), factory=lambda: FakeHost(), not_a_driver=object()
        )
        with mock.patch("agent_link.host.importlib.import_module", return_value=module):
            self.assertIs(load_driver("x:instance"), module.instance)
            self.assertEqual(load_driver("x:factory").capabilities()["host"], "fake")
            with self.assertRaises(ValidationError):
                load_driver("x:not_a_driver")
        with self.assertRaises(ValidationError):
            load_driver("agent_link.hosts.codex.parent")
        with self.assertRaises(ValidationError):
            load_driver("agent_link.hosts.codex.parent:missing_attribute")


if __name__ == "__main__":
    unittest.main(verbosity=2)
