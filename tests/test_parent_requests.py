"""Parent -> child channel tests (ask-child/answer CLI, OpenCode prompt push, service ops)."""

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

from agent_link.host import HostService
from agent_link.storage import Store
from tests import isolate_registry
from tests.opencode_server import MockOpenCodeServe, write_descriptor_pair

REPO_ROOT = Path(__file__).resolve().parent.parent


class StubDriver:
    def capabilities(self):
        return {"host": "stub"}

    def notify(self, root, binding, message):
        return "stub"


class ParentChildCliCase(unittest.TestCase):
    """CLI closed loop for the OpenCode child: ask-child pushes one prompt
    through the native OpenCode HTTP endpoint; the child answers via the
    ordinary CLI.  No file inbox is written."""

    def setUp(self):
        isolate_registry(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "runtime"
        self.sessions = Path(self.tmp.name) / "sessions"
        previous = os.environ.get("AGENT_LINK_SESSIONS")
        os.environ["AGENT_LINK_SESSIONS"] = str(self.sessions)

        def restore_env():
            os.environ.pop("AGENT_LINK_SESSIONS", None)
            if previous is not None:
                os.environ["AGENT_LINK_SESSIONS"] = previous

        self.addCleanup(restore_env)
        self.notifier = Path(self.tmp.name) / "stub.py"
        self.notifier.write_text("def bridge(payload):\n    return {'accepted': True}\n", encoding="utf-8")
        self.binding = "pc-b"
        config = {
            "binding_id": self.binding,
            "run_id": "r-pc",
            "native_task_id": None,
            "parent": {"framework": "omp", "session_id": "parent-1"},
            "child": {"framework": "opencode", "session_id": "child-1"},
            "notifier_path": str(self.notifier),
        }
        config_path = Path(self.tmp.name) / "bind.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        proc = self.run_cli("bind", "--config", str(config_path))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.serve = MockOpenCodeServe("child-1")
        self.addCleanup(self.serve.close)
        write_descriptor_pair(self.sessions, self.serve)

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

    def send_child(self, request_id, message_id, question, timeout):
        return self.run_cli(
            "send-child",
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
        )

    def read_prompts(self):
        return list(self.serve.prompts)

    def wait_for_prompts(self, count: int, timeout: float = 10) -> list[dict]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            prompts = self.read_prompts()
            if len(prompts) >= count:
                return prompts
            time.sleep(0.05)
        return self.read_prompts()

    def test_ask_child_closed_loop(self):
        asker = self.start_ask_child("rq-1", "pm-1", "which option?", 30)
        prompts = self.wait_for_prompts(1)
        self.assertEqual(len(prompts), 1, "ask-child must push one prompt before waiting")
        prompt = prompts[0]
        self.assertEqual(prompt["id"], "pm-1")
        self.assertEqual(prompt["delivery"], "queue")
        self.assertIn("[agent-link:parent-ask]", prompt["text"])
        self.assertIn("binding_id: pc-b", prompt["text"])
        self.assertIn("request_id: rq-1", prompt["text"])
        self.assertIn("child: opencode/child-1", prompt["text"])
        self.assertIn("which option?", prompt["text"])
        self.assertIn("answer --binding", prompt["text"])
        self.assertFalse((self.root / "inbox").exists(), "no inbox fallback")

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
        self.assertEqual(len(self.wait_for_prompts(1)), 1)
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
        self.assertEqual(len(self.read_prompts()), 1, "a replay must not re-push the prompt")

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

    def test_send_child_is_non_blocking_idempotent_and_not_waiting(self):
        started = time.time()
        proc = self.send_child("rq-s", "pm-s", "fire and forget", 30)
        elapsed = time.time() - started
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result["delivery_state"], "host_accepted")
        self.assertEqual(result["status"], "pending")
        self.assertFalse(result["idempotent"])
        self.assertNotIn("answer", result, "send-child must not wait for an answer")
        self.assertLess(elapsed, 15, "send-child must return without waiting for the reply")
        prompts = self.wait_for_prompts(1)
        self.assertEqual(len(prompts), 1, "send-child must push exactly one prompt")
        self.assertEqual(prompts[0]["id"], "pm-s")
        self.assertEqual(prompts[0]["delivery"], "queue")
        self.assertIn("[agent-link:parent-ask]", prompts[0]["text"])
        self.assertIn("fire and forget", prompts[0]["text"])

        replay = self.send_child("rq-s", "pm-s", "fire and forget", 30)
        self.assertEqual(replay.returncode, 0, replay.stdout + replay.stderr)
        replay_result = json.loads(replay.stdout)
        self.assertTrue(replay_result["idempotent"])
        self.assertEqual(replay_result["delivery_state"], "host_accepted")
        self.assertEqual(len(self.read_prompts()), 1, "a replay must not re-push the prompt")

        answered = self.answer("rq-s", "am-s", "done")
        self.assertEqual(answered.returncode, 0, answered.stderr)
        after = self.send_child("rq-s", "pm-s", "fire and forget", 30)
        self.assertEqual(after.returncode, 0, after.stdout + after.stderr)
        after_result = json.loads(after.stdout)
        self.assertTrue(after_result["idempotent"])
        self.assertEqual(after_result["status"], "answered")
        self.assertEqual(len(self.read_prompts()), 1)

    def test_send_child_delivery_failure_is_exit_7_without_redispatch(self):
        (self.sessions / "opencode" / "child-1.json").unlink()
        proc = self.send_child("rq-f", "pm-f", "nobody home", 30)
        self.assertEqual(proc.returncode, 7, proc.stdout + proc.stderr)
        result = json.loads(proc.stdout)
        self.assertEqual(result["delivery_state"], "failed_or_unknown")
        self.assertFalse(result["idempotent"])
        self.assertIn("error", result)
        self.assertEqual(self.serve.requests, [], "a missing descriptor must not be contacted")
        self.assertFalse((self.root / "inbox").exists(), "no inbox fallback")

        replay = self.send_child("rq-f", "pm-f", "nobody home", 30)
        self.assertEqual(replay.returncode, 7)
        replay_result = json.loads(replay.stdout)
        self.assertTrue(replay_result["idempotent"])
        self.assertEqual(replay_result["delivery_state"], "failed_or_unknown")
        self.assertNotIn("error", replay_result, "a replay must not re-dispatch")

    def test_send_child_then_ask_child_shares_one_dispatch(self):
        sent = self.send_child("rq-sa", "pm-sa", "shared question", 30)
        self.assertEqual(sent.returncode, 0, sent.stdout + sent.stderr)
        self.assertEqual(len(self.wait_for_prompts(1)), 1)

        asker = self.start_ask_child("rq-sa", "pm-sa", "shared question", 30)
        time.sleep(0.3)
        self.assertEqual(
            len(self.read_prompts()), 1, "ask-child replay must not re-dispatch"
        )
        answered = self.answer("rq-sa", "am-sa", "shared answer")
        self.assertEqual(answered.returncode, 0, answered.stderr)
        out, err = asker.communicate(timeout=30)
        self.assertEqual(asker.returncode, 0, err)
        ask_result = json.loads(out)
        self.assertTrue(ask_result["idempotent"])
        self.assertEqual(ask_result["status"], "answered")
        self.assertEqual(ask_result["answer"], "shared answer")
        self.assertEqual(ask_result["delivery_state"], "host_accepted")
        self.assertEqual(len(self.read_prompts()), 1)

    def test_ask_child_then_send_child_replay_and_conflicts(self):
        asker = self.start_ask_child("rq-as", "pm-as", "pick", 30)
        self.assertEqual(len(self.wait_for_prompts(1)), 1)

        replay = self.send_child("rq-as", "pm-as", "pick", 30)
        self.assertEqual(replay.returncode, 0, replay.stdout + replay.stderr)
        replay_result = json.loads(replay.stdout)
        self.assertTrue(replay_result["idempotent"])
        self.assertEqual(replay_result["status"], "pending")
        self.assertEqual(replay_result["delivery_state"], "host_accepted")
        self.assertEqual(len(self.read_prompts()), 1, "send-child replay must not re-dispatch")

        self.assertEqual(self.send_child("rq-as", "pm-other", "pick", 30).returncode, 4)
        self.assertEqual(self.send_child("rq-as", "pm-as", "different", 30).returncode, 4)
        self.assertEqual(self.send_child("rq-as", "pm-as", "pick", 5).returncode, 4)
        self.assertEqual(self.send_child("rq-other", "pm-as", "another", 30).returncode, 4)
        self.assertEqual(len(self.read_prompts()), 1)

        answered = self.answer("rq-as", "am-as", "A")
        self.assertEqual(answered.returncode, 0, answered.stderr)
        out, err = asker.communicate(timeout=30)
        self.assertEqual(asker.returncode, 0, err)
        self.assertEqual(json.loads(out)["status"], "answered")

    def test_list_parent_requests_pending(self):
        asker = self.start_ask_child("rq-a", "pm-a", "?", 30)
        self.assertEqual(len(self.wait_for_prompts(1)), 1)
        self.answer("rq-a", "am-a", "ok")
        asker.communicate(timeout=30)

        pending = self.start_ask_child("rq-p", "pm-p", "?", 30)
        self.assertEqual(len(self.wait_for_prompts(2)), 2)

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
