"""Parent -> child channel tests (ask-child/answer CLI, inbox bridge, service ops)."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from agent_link.host import HostService
from agent_link.shared import inbox_path
from agent_link.storage import Store
from tests import isolate_registry

REPO_ROOT = Path(__file__).resolve().parent.parent


class StubDriver:
    def capabilities(self):
        return {"host": "stub"}

    def notify(self, root, binding, message):
        return "stub"


class ParentChildCliCase(unittest.TestCase):
    """CLI closed loop: parent ask-child writes the inbox; child answers via CLI."""

    def setUp(self):
        isolate_registry(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "runtime"
        self.notifier = Path(self.tmp.name) / "stub.py"
        self.notifier.write_text("def bridge(payload):\n    return {'accepted': True}\n", encoding="utf-8")
        self.binding = "pc-b"
        config = {
            "binding_id": self.binding,
            "run_id": "r-pc",
            "native_task_id": None,
            "parent": {"framework": "omp", "session_id": "parent-1"},
            "child": {"framework": "omp", "session_id": "child-1"},
            "notifier_path": str(self.notifier),
        }
        config_path = Path(self.tmp.name) / "bind.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        proc = self.run_cli("bind", "--config", str(config_path))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.inbox = inbox_path(self.root, self.binding)

    def run_cli(self, *args, timeout=60):
        return subprocess.run(
            [sys.executable, "-m", "agent_link", "--root", str(self.root), *args],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            timeout=timeout,
        )

    def start_ask_child(self, request_id, message_id, question, timeout):
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agent_link",
                "--root",
                str(self.root),
                "ask-child",
                "--binding",
                self.binding,
                "--request-id",
                request_id,
                "--message-id",
                message_id,
                "--question",
                question,
                "--timeout",
                str(timeout),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=REPO_ROOT,
        )

        def cleanup():
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)

        self.addCleanup(cleanup)
        return proc

    def answer(self, request_id, message_id, answer):
        return self.run_cli(
            "answer",
            "--binding",
            self.binding,
            "--request-id",
            request_id,
            "--message-id",
            message_id,
            "--answer",
            answer,
        )

    def read_inbox(self):
        if not self.inbox.exists():
            return []
        return [json.loads(line) for line in self.inbox.read_text("utf-8").splitlines() if line.strip()]

    def wait_for_inbox(self, count: int, timeout: float = 10) -> list[dict]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            events = self.read_inbox()
            if len(events) >= count:
                return events
            time.sleep(0.05)
        return self.read_inbox()

    def test_ask_child_closed_loop(self):
        asker = self.start_ask_child("rq-1", "pm-1", "which option?", 30)
        events = self.wait_for_inbox(1)
        self.assertEqual(len(events), 1, "ask-child must append one inbox event before waiting")
        event = events[0]
        self.assertEqual(event["v"], 1)
        self.assertEqual(event["kind"], "ask_child")
        self.assertEqual(event["binding_id"], self.binding)
        self.assertEqual(event["source"], "parent-cli")
        self.assertEqual(event["parent_session_id"], "parent-1")
        self.assertEqual(event["child_framework"], "omp")
        self.assertEqual(event["child_session_id"], "child-1")
        self.assertEqual(event["request_id"], "rq-1")
        self.assertEqual(event["message_id"], "pm-1")
        self.assertIn("[agent-link:parent-ask]", event["prompt"])
        self.assertIn("which option?", event["prompt"])
        self.assertIn("answer --binding", event["prompt"])

        proc = self.answer("rq-1", "am-1", "option A")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out, err = asker.communicate(timeout=30)
        self.assertEqual(asker.returncode, 0, err)
        result = json.loads(out)
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["answer"], "option A")
        self.assertFalse(result["late"])
        self.assertEqual(result["delivery_state"], "host_accepted")
        self.assertFalse(result["idempotent"])

    def test_timeout_and_late_answer(self):
        asker = self.start_ask_child("rq-t", "pm-t", "too slow", 1)
        out, err = asker.communicate(timeout=20)
        self.assertEqual(asker.returncode, 3, err)
        result = json.loads(out)
        self.assertEqual(result["status"], "timed_out")
        self.assertIsNone(result["answer"])

        proc = self.answer("rq-t", "am-t", "late answer")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        stored = json.loads(proc.stdout)
        self.assertEqual(stored["status"], "timed_out")
        self.assertTrue(stored["late"])

        got = self.run_cli("get-parent-request", "--binding", self.binding, "--request-id", "rq-t")
        self.assertEqual(got.returncode, 0, got.stderr)
        row = json.loads(got.stdout)
        self.assertEqual(row["status"], "timed_out")
        self.assertEqual(row["answer"], "late answer")
        self.assertTrue(row["late"])
        self.assertEqual(row["answer_message_id"], "am-t")

        # A second different late answer conflicts.
        again = self.answer("rq-t", "am-t2", "another")
        self.assertEqual(again.returncode, 4)

    def test_replay_and_conflicts(self):
        asker = self.start_ask_child("rq-c", "pm-c", "pick", 30)
        self.assertEqual(len(self.wait_for_inbox(1)), 1)
        self.assertEqual(self.answer("rq-c", "am-c", "A").returncode, 0)
        out, err = asker.communicate(timeout=30)
        self.assertEqual(asker.returncode, 0, err)

        replay = self.run_cli(
            "ask-child",
            "--binding",
            self.binding,
            "--request-id",
            "rq-c",
            "--message-id",
            "pm-c",
            "--question",
            "pick",
            "--timeout",
            "30",
        )
        self.assertEqual(replay.returncode, 0, replay.stderr)
        result = json.loads(replay.stdout)
        self.assertTrue(result["idempotent"])
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["answer"], "A")
        self.assertEqual(len(self.read_inbox()), 1, "a replay must not re-append the inbox event")

        changed = self.run_cli(
            "ask-child",
            "--binding",
            self.binding,
            "--request-id",
            "rq-c",
            "--message-id",
            "pm-c",
            "--question",
            "other",
            "--timeout",
            "30",
        )
        self.assertEqual(changed.returncode, 4)

        overwrite = self.answer("rq-c", "am-c2", "B")
        self.assertEqual(overwrite.returncode, 4)

        same = self.answer("rq-c", "am-c", "A")
        self.assertEqual(same.returncode, 0)
        self.assertTrue(json.loads(same.stdout)["idempotent"])

        reused = self.run_cli(
            "ask-child",
            "--binding",
            self.binding,
            "--request-id",
            "rq-x",
            "--message-id",
            "pm-c",
            "--question",
            "x",
            "--timeout",
            "1",
        )
        self.assertEqual(reused.returncode, 4, "one message_id cannot serve two parent requests")

        other = self.start_ask_child("rq-c2", "pm-c2", "pick2", 1)
        out2, err2 = other.communicate(timeout=20)
        self.assertEqual(other.returncode, 3, err2)
        cross = self.answer("rq-c2", "am-c", "A")
        self.assertEqual(cross.returncode, 4, "one answer message cannot answer two requests")

    def test_list_parent_requests_pending(self):
        asker = self.start_ask_child("rq-a", "pm-a", "?", 30)
        self.assertEqual(len(self.wait_for_inbox(1)), 1)
        self.answer("rq-a", "am-a", "ok")
        asker.communicate(timeout=30)

        pending = self.start_ask_child("rq-p", "pm-p", "?", 30)
        self.assertEqual(len(self.wait_for_inbox(2)), 2)

        listed = self.run_cli("list-parent-requests", "--binding", self.binding, "--pending")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        payload = json.loads(listed.stdout)
        self.assertEqual([row["request_id"] for row in payload["parent_requests"]], ["rq-p"])

        self.answer("rq-p", "am-p", "ok")
        pending.communicate(timeout=30)

        everything = self.run_cli("list-parent-requests", "--binding", self.binding)
        rows = json.loads(everything.stdout)["parent_requests"]
        self.assertEqual([row["request_id"] for row in rows], ["rq-a", "rq-p"])

    def test_unknown_parent_request_is_not_found(self):
        proc = self.run_cli("get-parent-request", "--binding", self.binding, "--request-id", "nope")
        self.assertEqual(proc.returncode, 6)
        proc = self.run_cli(
            "answer",
            "--binding",
            self.binding,
            "--request-id",
            "nope",
            "--message-id",
            "x",
            "--answer",
            "y",
        )
        self.assertEqual(proc.returncode, 6)


class ParentChildServiceCase(unittest.TestCase):
    """Child-side HTTP ops: poll pending parent requests and submit answers."""

    def setUp(self):
        isolate_registry(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
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
        self.service = HostService(self.root, "b", "test-token", StubDriver())

        def stop():
            self.service.shutdown()
            self.service.server_close()
            self.thread.join(timeout=5)

        self.addCleanup(stop)
        self.thread = threading.Thread(target=self.service.serve_forever, daemon=True)
        self.thread.start()

    def call(self, path, body):
        request = urllib.request.Request(
            self.service.endpoint + path,
            data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": "Bearer test-token", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read().decode("utf-8"))
            finally:
                error.close()

    def test_child_requests_and_answer_over_http(self):
        store = Store(self.root)
        try:
            store.prepare_parent_ask("b", "rq-1", "pm-1", "do X", 60.0)
        finally:
            store.close()

        status, payload = self.call("/v1/child-requests", {})
        self.assertEqual(status, 200)
        self.assertEqual(payload["exit_code"], 0)
        self.assertEqual(
            [row["request_id"] for row in payload["result"]["parent_requests"]], ["rq-1"]
        )

        status, payload = self.call("/v1/child-requests", {"extra": 1})
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "validation")
        self.assertEqual(payload["exit_code"], 5)

        status, payload = self.call(
            "/v1/answer", {"request_id": "rq-1", "message_id": "am-1", "answer": "done"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["result"]["status"], "answered")
        self.assertFalse(payload["result"]["late"])

        status, payload = self.call("/v1/child-requests", {})
        self.assertEqual(payload["result"]["parent_requests"], [])

        status, payload = self.call("/v1/get-parent-request", {"request_id": "rq-1"})
        self.assertEqual(status, 200)
        self.assertEqual(payload["result"]["status"], "answered")
        self.assertEqual(payload["result"]["answer"], "done")

        status, payload = self.call("/v1/get-parent-request", {"request_id": "nope"})
        self.assertEqual(status, 404)
        self.assertEqual(payload["exit_code"], 6)

        status, payload = self.call(
            "/v1/answer", {"request_id": "rq-1", "message_id": "am-2", "answer": "other"}
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["exit_code"], 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
