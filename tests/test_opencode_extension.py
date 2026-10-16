"""OpenCode plugin acceptance: no-model registration, real HTTP loopback
closed loop, and the removed-watcher residue check.

Child side: loads the real ``extensions/opencode/agent-link.ts`` (OpenCode v2
plugin API) with Bun or Node (type stripping) and calls the five tools against
a real ``agent_link.host`` service whose OpenCode parent adapter pushes through
the native prompt HTTP endpoint (fake serve); the parent stand-in replies
through the ordinary store API.  Registration side: boots a real
``opencode serve`` in an isolated HOME and confirms the plugin loads and
registers the five tools.  Slice 2 removed the plugin bridge watcher, so no
bridge/lock/cursor test suite remains.

Never calls a model, never touches a real OpenCode session or the user's real
opencode configuration.  Skipped when neither Bun nor a type-stripping Node
is available; the registration test additionally needs the ``opencode``
executable.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path

from agent_link.errors import AgentLinkError
from tests import isolate_registry
from tests.opencode_server import MockOpenCodeServe, write_descriptor_pair
from agent_link.storage import Store

REPO_ROOT = Path(__file__).resolve().parents[1]
EXTENSION = REPO_ROOT / "extensions" / "opencode" / "agent-link.ts"
TOOL_LOOP = REPO_ROOT / "tests" / "opencode" / "tool_loop.mjs"

NOTIFIER_STUB = '''\
"""Unused by OpenCodeHost; kept so the binding passes notifier_path validation."""
def bridge(payload):
    return {"accepted": True}
'''

EXPECTED_TOOLS = {
    "report_parent",
    "ask_parent",
    "check_parent_request",
    "answer_parent",
    "poll_parent_requests",
}


def _find_bun() -> str | None:
    found = shutil.which("bun")
    if found:
        return found
    fallback = Path.home() / ".bun" / "bin" / "bun"
    return str(fallback) if fallback.is_file() else None


def _find_node_with_type_stripping() -> str | None:
    node = shutil.which("node")
    if not node:
        return None
    try:
        probe = subprocess.run(
            [node, "-p", "String(process.features.typescript)"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return node if probe.stdout.strip() in ("strip", "transform", "true") else None


def _find_opencode() -> str | None:
    return shutil.which("opencode")


BUN = _find_bun()
NODE = _find_node_with_type_stripping()
RUNTIME = BUN or NODE
OPENCODE = _find_opencode()


class OpenCodeChildE2ECase(unittest.TestCase):
    """Real HTTP loopback: child tools against a real host service."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        isolate_registry(self)
        self.base = Path(self.temp.name)
        self.root = self.base / "state"
        self.stub = self.base / "notifier_stub.py"
        self.stub.write_text(NOTIFIER_STUB, encoding="utf-8")
        self.credentials = self.base / "credentials.json"
        self.binding = "e2e-b"
        store = Store(self.root)
        try:
            store.bind(
                {
                    "binding_id": self.binding,
                    "run_id": "e2e-run",
                    "native_task_id": None,
                    "parent": {"framework": "opencode", "session_id": "e2e-parent"},
                    "child": {"framework": "opencode", "session_id": "e2e-child"},
                    "notifier_path": str(self.stub),
                }
            )
        finally:
            store.close()
        self.bridge_file = self.root / "omp-bridge" / f"{self.binding}.jsonl"
        self.sessions = self.base / "sessions"
        previous = os.environ.get("AGENT_LINK_SESSIONS")
        os.environ["AGENT_LINK_SESSIONS"] = str(self.sessions)

        def restore_env():
            os.environ.pop("AGENT_LINK_SESSIONS", None)
            if previous is not None:
                os.environ["AGENT_LINK_SESSIONS"] = previous

        self.addCleanup(restore_env)
        self.serve = MockOpenCodeServe("e2e-parent")
        self.addCleanup(self.serve.close)
        write_descriptor_pair(self.sessions, self.serve)
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
                "opencode",
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

    @unittest.skipUnless(RUNTIME, "bun or a type-stripping node is required")
    def test_real_http_loopback_closed_loop(self):
        ids = {
            "report_message_id": "oc-e2e-report-001",
            "request_id": "oc-e2e-request-001",
            "question_message_id": "oc-e2e-question-001",
            "parent_request_id": "oc-e2e-parent-req-001",
            "parent_answer_message_id": "oc-e2e-parent-answer-001",
        }
        host = self.start_host()
        # A parent-initiated request is already pending before the child starts.
        store = Store(self.root)
        try:
            store.prepare_parent_ask(
                self.binding,
                ids["parent_request_id"],
                "oc-e2e-parent-msg-001",
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
                [RUNTIME, str(TOOL_LOOP), "child-happy"],
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
            store.reply(self.binding, ids["request_id"], "oc-e2e-reply-001", "parent decision")
        finally:
            store.close()
        worker.join(timeout=180)
        process = child.get("process")
        self.assertIsNotNone(process, "the child driver never finished")
        self.assertEqual(
            process.returncode, 0, f"child-happy failed:\n{process.stdout}\n{process.stderr}"
        )
        happy = json.loads([line for line in process.stdout.splitlines() if line.strip()][-1])

        self.assertEqual(set(happy["registeredTools"]), EXPECTED_TOOLS)
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
        self.assertEqual(len(self.serve.prompts), 2)
        self.assertEqual(
            [prompt["id"] for prompt in self.serve.prompts],
            [ids["report_message_id"], ids["question_message_id"]],
        )
        self.assertEqual([prompt["delivery"] for prompt in self.serve.prompts], ["queue", "queue"])
        self.assertIn("[agent-link:report]", self.serve.prompts[0]["text"])
        self.assertIn("[agent-link:ask]", self.serve.prompts[1]["text"])
        self.assertFalse(self.bridge_file.exists(), "the push transport must not write a bridge file")

        self.stop_host(host)
        self.assertFalse(self.credentials.exists(), "graceful shutdown removes its own credentials")


class OpenCodeRegistrationCase(unittest.TestCase):
    """The real opencode 2.x server loads the plugin and registers the tools.

    Runs ``opencode serve`` in an isolated HOME with a wrapper plugin that
    delegates to the real extension and records the registered tool ids.
    No model is configured or called; the only API read is /api/plugin.
    """

    WRAPPER = """\
import agentLink from %s
import { writeFileSync } from "node:fs"
export default {
  id: "agent-link-registration-probe",
  async setup(ctx) {
    const cleanup = await agentLink.setup(ctx)
    const tools = await ctx.tool.list()
    writeFileSync(process.env.AGENT_LINK_TEST_OUT, JSON.stringify(tools.map((tool) => tool.id)))
    return cleanup
  },
}
"""

    @unittest.skipUnless(OPENCODE, "the opencode executable is required")
    def test_real_opencode_loads_plugin_and_tools_without_a_model(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        home = base / "home"
        work = base / "work"
        plugin_dir = home / ".config" / "opencode" / "plugin"
        plugin_dir.mkdir(parents=True)
        work.mkdir()
        (home / ".config" / "opencode" / "opencode.json").write_text("{}", encoding="utf-8")
        out_file = base / "registered-tools.json"
        wrapper = plugin_dir / "agent-link-probe.ts"
        wrapper.write_text(self.WRAPPER % json.dumps(str(EXTENSION)), encoding="utf-8")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        env = dict(os.environ)
        env["HOME"] = str(home)
        env["AGENT_LINK_TEST_OUT"] = str(out_file)
        for name in (
            "AGENT_LINK_CREDENTIALS",
            "AGENT_LINK_BRIDGE_FILE",
            "AGENT_LINK_BINDING",
            "XDG_CONFIG_HOME",
            "XDG_DATA_HOME",
        ):
            env.pop(name, None)
        process = subprocess.Popen(
            [
                OPENCODE,
                "serve",
                "--hostname",
                "127.0.0.1",
                "--port",
                str(port),
                "--print-logs",
                "--log-level",
                "info",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(work),
            env=env,
        )
        password = None
        watchdog = threading.Timer(90, process.kill)
        watchdog.start()
        try:
            deadline = time.time() + 60
            while time.time() < deadline and password is None:
                line = process.stdout.readline()
                if not line:
                    break
                if line.startswith("server password "):
                    password = line.split()[-1].strip()
            self.assertIsNotNone(password, "opencode serve never printed its password")

            status = None
            deadline = time.time() + 60
            while time.time() < deadline:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{port}/api/plugin?directory={work}&workspaceID=",
                    headers={
                        "Authorization": "Basic "
                        + __import__("base64").b64encode(f"opencode:{password}".encode()).decode()
                    },
                )
                try:
                    with urllib.request.urlopen(request, timeout=5) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                except (OSError, json.JSONDecodeError):
                    time.sleep(0.5)
                    continue
                for entry in payload.get("data", []):
                    if entry.get("id") == "agent-link-registration-probe":
                        status = entry.get("state", {})
                if status is not None:
                    break
                time.sleep(0.5)
            self.assertIsNotNone(status, "the probe plugin never appeared in /api/plugin")
            self.assertEqual(status.get("status"), "active", status)
            self.assertTrue(out_file.is_file(), "the probe plugin never wrote its tool list")
            registered = set(json.loads(out_file.read_text(encoding="utf-8")))
            self.assertTrue(
                EXPECTED_TOOLS <= registered,
                f"registered tools: {sorted(registered)}",
            )
        finally:
            watchdog.cancel()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
            process.stdout.close()
        # Isolation proof: the user's real config was never touched.
        self.assertTrue((home / ".config" / "opencode" / "opencode.json").is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
