"""OMP extension acceptance: no-model registration, real HTTP loopback closed
loop, and the session-owned push listener suite.

Child side: loads the real ``extensions/omp/agent-link.ts`` with Bun (its
native loader) and calls the tools against a real ``agent_link.host`` service
whose OmpParentAdapter delivers parent notifications to a mock loopback push
listener; the parent stand-in replies through the ordinary store API.  Push
side: drives the same extension's lifecycle with a fake ExtensionAPI over the
real loopback listener (descriptor discovery, token auth, identity rejection,
injection semantics, failure recovery, session switching, shutdown and
duplicate-descriptor refusal).

Never calls a model, never touches a real OMP session.  Skipped when Bun is
unavailable; the registration test additionally needs the ``omp`` executable.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from agent_link.errors import AgentLinkError
from tests import isolate_registry
from tests.push_server import MockPushListener, write_descriptor
from agent_link.storage import Store

REPO_ROOT = Path(__file__).resolve().parents[1]
EXTENSION = REPO_ROOT / "extensions" / "omp" / "agent-link.ts"
TOOL_LOOP = REPO_ROOT / "tests" / "omp" / "tool_loop.mjs"

NOTIFIER_STUB = '''\
"""Unused by the push parent adapter; kept so the binding passes notifier_path validation."""
def bridge(payload):
    return {"accepted": True}
'''


def _find_bun() -> str | None:
    found = shutil.which("bun")
    if found:
        return found
    fallback = Path.home() / ".bun" / "bin" / "bun"
    return str(fallback) if fallback.is_file() else None


def _find_omp() -> str | None:
    found = shutil.which("omp")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "omp"
    return str(fallback) if fallback.is_file() else None


BUN = _find_bun()
OMP = _find_omp()
BUN_READY = BUN is not None


class SessionsMixin:
    """Pin AGENT_LINK_SESSIONS to a per-test temp directory."""

    def setUpSessions(self):
        self.sessions = Path(self.temp.name) / "sessions"
        previous = os.environ.get("AGENT_LINK_SESSIONS")
        os.environ["AGENT_LINK_SESSIONS"] = str(self.sessions)

        def restore():
            os.environ.pop("AGENT_LINK_SESSIONS", None)
            if previous is not None:
                os.environ["AGENT_LINK_SESSIONS"] = previous

        self.addCleanup(restore)


class OmpChildE2ECase(SessionsMixin, unittest.TestCase):
    """Real HTTP loopback: Bun-loaded child tools against a real host service
    whose parent notifications land on a mock push listener."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        isolate_registry(self)
        self.setUpSessions()
        self.base = Path(self.temp.name)
        self.root = self.base / "state"
        self.stub = self.base / "notifier_stub.py"
        self.stub.write_text(NOTIFIER_STUB, encoding="utf-8")
        self.credentials = self.base / "credentials.json"
        self.binding = "e2e-b"
        self.parent_session = "e2e-parent"
        store = Store(self.root)
        try:
            store.bind(
                {
                    "binding_id": self.binding,
                    "run_id": "e2e-run",
                    "native_task_id": None,
                    "parent": {"framework": "omp", "session_id": self.parent_session},
                    "child": {"framework": "omp", "session_id": "e2e-child"},
                    "notifier_path": str(self.stub),
                }
            )
        finally:
            store.close()
        self.listener = MockPushListener("omp", self.parent_session)
        self.addCleanup(self.listener.close)
        write_descriptor(self.sessions, self.listener.descriptor())
        self.hosts: list[subprocess.Popen] = []
        self.addCleanup(self.stop_all_hosts)

    def stop_all_hosts(self):
        for process in list(self.hosts):
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        self.hosts.clear()

    def start_host(self) -> subprocess.Popen:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agent_link.host",
                "--root",
                str(self.root),
                "--binding",
                self.binding,
                "--credentials",
                str(self.credentials),
                "--framework",
                "omp",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(REPO_ROOT),
            env=env,
        )
        ready_line = process.stdout.readline()
        if not ready_line:
            raise AssertionError("host service did not become ready")
        self.assertTrue(json.loads(ready_line)["ready"])
        self.hosts.append(process)
        return process

    def stop_host(self, process: subprocess.Popen):
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=15)
        self.hosts.remove(process)
        stdout, stderr = process.communicate()
        self.assertEqual(
            process.returncode, 0, f"host shutdown failed: stdout={stdout} stderr={stderr}"
        )

    @unittest.skipUnless(BUN_READY, "bun is required")
    def test_real_http_loopback_closed_loop(self):
        ids = {
            "report_message_id": "omp-e2e-report-001",
            "request_id": "omp-e2e-request-001",
            "question_message_id": "omp-e2e-question-001",
            "parent_request_id": "omp-e2e-parent-req-001",
            "parent_answer_message_id": "omp-e2e-parent-answer-001",
        }
        host = self.start_host()
        # A parent-initiated request is already pending before the child starts.
        store = Store(self.root)
        try:
            store.prepare_parent_ask(
                self.binding,
                ids["parent_request_id"],
                "omp-e2e-parent-msg-001",
                "child, report back",
                60.0,
            )
        finally:
            store.close()
        child: dict = {}

        def run_child():
            env = dict(os.environ)
            env["AGENT_LINK_CREDENTIALS"] = str(self.credentials)
            env["AGENT_LINK_TEST_IDS"] = json.dumps(ids)
            env.pop("AGENT_LINK_BRIDGE_FILE", None)
            env.pop("AGENT_LINK_BINDING", None)
            process = subprocess.run(
                [BUN, str(TOOL_LOOP), "child-happy"],
                capture_output=True,
                text=True,
                cwd=str(REPO_ROOT),
                env=env,
                timeout=180,
            )
            child["process"] = process

        worker = threading.Thread(target=run_child)
        worker.start()
        # Parent stand-in: answer as soon as the child's ask is persisted.
        deadline = time.time() + 30
        found = False
        while time.time() < deadline:
            store = Store(self.root)
            try:
                row = store.get_request(self.binding, ids["request_id"])
            except AgentLinkError:
                row = None
            finally:
                store.close()
            if row is not None:
                found = True
                break
            time.sleep(0.05)
        self.assertTrue(found, "the child's ask was never persisted")
        store = Store(self.root)
        try:
            store.reply(self.binding, ids["request_id"], "omp-e2e-reply-001", "parent decision")
        finally:
            store.close()
        worker.join(timeout=180)
        process = child.get("process")
        self.assertIsNotNone(process, "the child driver never finished")
        self.assertEqual(
            process.returncode, 0, f"bun child-happy failed:\n{process.stdout}\n{process.stderr}"
        )
        happy = json.loads([line for line in process.stdout.splitlines() if line.strip()][-1])

        self.assertTrue(happy["report"]["ok"], happy["report"])
        self.assertEqual(happy["report"]["details"]["delivery_state"], "host_accepted")
        self.assertFalse(happy["report"]["details"]["idempotent"])
        self.assertTrue(happy["ask"]["ok"], happy["ask"])
        self.assertEqual(happy["ask"]["details"]["status"], "replied")
        self.assertEqual(happy["ask"]["details"]["answer"], "parent decision")
        self.assertIn("parent decision", happy["ask"]["text"])
        self.assertTrue(happy["check"]["ok"], happy["check"])
        self.assertEqual(happy["check"]["details"]["status"], "replied")
        # Parent->child channel via the child tools: poll + answer.
        self.assertTrue(happy["poll"]["ok"], happy["poll"])
        polled = [row["request_id"] for row in happy["poll"]["details"]["parent_requests"]]
        self.assertEqual(polled, [ids["parent_request_id"]])
        self.assertTrue(happy["answer"]["ok"], happy["answer"])
        self.assertEqual(happy["answer"]["details"]["status"], "answered")
        store = Store(self.root)
        try:
            parent_row = store.get_parent_request(self.binding, ids["parent_request_id"])
        finally:
            store.close()
        self.assertEqual(parent_row["status"], "answered")
        self.assertEqual(parent_row["answer"], "child answer")
        self.assertEqual(parent_row["answer_message_id"], ids["parent_answer_message_id"])
        # The host delivered both parent notifications over the push listener.
        events = self.listener.events
        self.assertEqual(len(events), 2)
        self.assertEqual([event["role"] for event in events], ["parent", "parent"])
        self.assertEqual(events[0]["framework"], "omp")
        self.assertEqual(events[0]["session_id"], self.parent_session)
        self.assertIn("[agent-link:report]", events[0]["prompt"])
        self.assertIn("[agent-link:ask]", events[1]["prompt"])
        self.assertIn("auto-reply", events[1]["prompt"])
        self.assertFalse((self.root / "omp-bridge").exists(), "no bridge file is written")

        self.stop_host(host)
        self.assertFalse(self.credentials.exists(), "graceful shutdown removes its own credentials")


class OmpPushCase(SessionsMixin, unittest.TestCase):
    """Session-owned push listener acceptance, no service and no model."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        isolate_registry(self)
        self.setUpSessions()
        self.base = Path(self.temp.name)

    def run_push(self, scenario: str, extra_env: dict | None = None, timeout: int = 180) -> dict:
        env = dict(os.environ)
        env.pop("AGENT_LINK_CREDENTIALS", None)
        env.pop("AGENT_LINK_BRIDGE_FILE", None)
        env.pop("AGENT_LINK_BINDING", None)
        env.update(extra_env or {})
        process = subprocess.run(
            [BUN, str(TOOL_LOOP), scenario],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            env=env,
            timeout=timeout,
        )
        self.assertEqual(
            process.returncode, 0, f"bun {scenario} failed:\n{process.stdout}\n{process.stderr}"
        )
        lines = [line for line in process.stdout.splitlines() if line.strip()]
        self.assertTrue(lines, f"bun {scenario} printed nothing")
        return json.loads(lines[-1])

    @unittest.skipUnless(BUN_READY, "bun is required")
    def test_push_auth_identity_and_injection(self):
        out = self.run_push("push-basic")
        self.assertEqual(out["valid"]["status"], 200)
        self.assertEqual(out["badToken"]["status"], 403)
        self.assertEqual(out["noToken"]["status"], 403)
        self.assertEqual(out["wrongSession"]["status"], 400)
        self.assertEqual(out["wrongFramework"]["status"], 400)
        self.assertEqual(out["malformed"]["status"], 400)
        self.assertEqual(out["oversized"]["status"], 413)
        self.assertEqual([entry["text"] for entry in out["sent"]], ["push-1"])
        self.assertEqual(
            out["sent"][0]["options"],
            {"deliverAs": "aside", "attribution": "agent"},
            "an OMP injection is an attributed aside",
        )
        self.assertEqual(out["descriptor"]["protocol"], "agent-link-push/1")
        self.assertEqual(out["descriptor"]["framework"], "omp")
        self.assertFalse(out["descriptorExists"], "shutdown removes the owned descriptor")

    @unittest.skipUnless(BUN_READY, "bun is required")
    def test_push_failure_recovers_and_preserves_order(self):
        marker = self.base / "fail.marker"
        out = self.run_push("push-failure", {"AGENT_LINK_TEST_FAIL_FILE": str(marker)})
        self.assertEqual(out["failure"]["status"], 502)
        self.assertEqual(out["recovered"]["status"], 200)
        self.assertEqual([entry["text"] for entry in out["sent"]], ["after-failure"])
        self.assertFalse(marker.exists(), "the failing injection consumes its marker")

    @unittest.skipUnless(BUN_READY, "bun is required")
    def test_push_session_switch_closes_the_old_listener(self):
        out = self.run_push("push-switch")
        self.assertEqual(out["firstAccepted"]["status"], 200)
        self.assertEqual(out["secondAccepted"]["status"], 200)
        self.assertTrue(out["firstRemoved"], "the previous descriptor is removed on switch")
        self.assertIsNotNone(out["staleError"], "the old endpoint no longer accepts events")
        self.assertNotEqual(out["firstEndpoint"], out["secondEndpoint"])
        self.assertEqual([entry["text"] for entry in out["sent"]], ["first", "second"])

    @unittest.skipUnless(BUN_READY, "bun is required")
    def test_duplicate_descriptor_is_refused(self):
        foreign = {
            "protocol": "agent-link-push/1",
            "framework": "omp",
            "session_id": "omp-dup",
            "endpoint": "http://127.0.0.1:1",
            "token": "f" * 64,
            "pid": 999,
        }
        write_descriptor(self.sessions, foreign)
        out = self.run_push("push-duplicate")
        self.assertIsNotNone(out["startError"])
        self.assertRegex(out["startError"], r"EEXIST|exist", out["startError"])
        self.assertEqual(out["descriptor"], foreign, "a refused listener leaves the foreign descriptor")


class OmpRegistrationCase(unittest.TestCase):
    """The real OMP runtime loads the extension and registers the tools."""

    @unittest.skipUnless(BUN_READY and OMP, "bun and the omp executable are required")
    def test_real_omp_registers_tools_without_a_model(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        sessions = Path(temp.name) / "sessions"
        env = dict(os.environ)
        env["AGENT_LINK_SESSIONS"] = str(sessions)
        env.pop("AGENT_LINK_CREDENTIALS", None)
        env.pop("AGENT_LINK_BRIDGE_FILE", None)
        env.pop("AGENT_LINK_BINDING", None)
        process = subprocess.Popen(
            [
                OMP,
                "--mode",
                "rpc",
                "--no-ui",
                "--no-extensions",
                "-e",
                str(EXTENSION),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=temp.name,
            env=env,
        )
        watchdog = threading.Timer(60, process.kill)
        watchdog.start()
        try:
            ready = json.loads(process.stdout.readline())
            self.assertEqual(ready["type"], "ready", ready)
            process.stdin.write(json.dumps({"id": "1", "type": "get_state"}) + "\n")
            process.stdin.flush()
            response = None
            while True:
                line = process.stdout.readline()
                if not line:
                    break
                message = json.loads(line)
                if message.get("type") == "response" and message.get("command") == "get_state":
                    response = message
                    break
            self.assertIsNotNone(response, "omp never answered get_state")
            self.assertTrue(response["success"], response)
            names = {tool["name"] for tool in response["data"]["dumpTools"]}
            self.assertTrue(
                {"report_parent", "ask_parent", "check_parent_request"} <= names,
                f"registered tools: {sorted(names)}",
            )
            process.stdin.close()
            process.wait(timeout=60)
            self.assertEqual(process.returncode, 0)
        finally:
            watchdog.cancel()
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
            process.stderr.close()
        # A clean runtime shutdown removes its own session descriptor.
        leftovers = list(sessions.glob("omp/*.json")) if sessions.exists() else []
        self.assertEqual(leftovers, [], f"descriptor left behind: {leftovers}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
