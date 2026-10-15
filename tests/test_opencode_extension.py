"""OpenCode plugin acceptance: no-model registration, real HTTP loopback
closed loop, and the bridge watcher suite.

Child side: loads the real ``extensions/opencode/agent-link.ts`` (OpenCode v2
plugin API) with Bun or Node (type stripping) and calls the tools against a
real ``agent_link.host`` service whose OpenCodeHost driver writes the bridge
file; the parent stand-in replies through the ordinary store API.  Watcher
side: drives the same plugin's bridge watchers with a fake plugin context over
real bridge files (restart resume, a failing injection, a second consumer,
session switching, file replacement, oversized/invalid events, registry
auto-claim).  Registration side: boots a real ``opencode serve`` in an
isolated HOME and confirms the plugin loads and registers the five tools.

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
from agent_link.registry import entry_from_row, write_entry
from tests import isolate_registry
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
                "--driver",
                "agent_link.hosts.opencode:OpenCodeHost",
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
        events = [
            json.loads(line)
            for line in self.bridge_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertEqual(len(events), 2)
        self.assertEqual([event["kind"] for event in events], ["report", "ask"])
        self.assertEqual([event["source"] for event in events], ["service", "service"])

        self.stop_host(host)
        self.assertFalse(self.credentials.exists(), "graceful shutdown removes its own credentials")


class OpenCodeBridgeCase(unittest.TestCase):
    """Bridge watcher acceptance, no service and no model."""

    binding = "bridge-b"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        isolate_registry(self)
        self.base = Path(self.temp.name)
        self.bridge_file = self.base / "omp-bridge" / f"{self.binding}.jsonl"
        self.inbox_file = self.base / "inbox" / f"{self.binding}.jsonl"

    # -- helpers -------------------------------------------------------
    def bridge_env(self, extra: dict | None = None) -> dict:
        env = dict(os.environ)
        env["AGENT_LINK_BRIDGE_FILE"] = str(self.bridge_file)
        env["AGENT_LINK_BINDING"] = self.binding
        env.pop("AGENT_LINK_CREDENTIALS", None)
        env.update(extra or {})
        return env

    def write_events(
        self,
        prompts: list[str],
        binding: str | None = None,
        thread_id: str = "parent",
        child_session_id: str = "child",
        path: Path | None = None,
    ) -> list[bytes]:
        target = path or self.bridge_file
        target.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps(
                {
                    "v": 1,
                    "ts": "2026-01-01T00:00:00.000Z",
                    "source": "service",
                    "binding_id": self.binding if binding is None else binding,
                    "thread_id": thread_id,
                    "child_session_id": child_session_id,
                    "kind": "report",
                    "message_id": f"m-{index}",
                    "prompt": prompt,
                }
            )
            + "\n"
            for index, prompt in enumerate(prompts, start=1)
        ]
        with target.open("a", encoding="utf-8") as stream:
            stream.write("".join(lines))
        return [line.encode("utf-8") for line in lines]

    def run_bridge(self, scenario: str = "bridge", extra_env: dict | None = None, timeout: int = 120) -> dict:
        process = subprocess.run(
            [RUNTIME, str(TOOL_LOOP), scenario],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            env=self.bridge_env(extra_env),
            timeout=timeout,
        )
        self.assertEqual(
            process.returncode, 0, f"{scenario} failed:\n{process.stdout}\n{process.stderr}"
        )
        lines = [line for line in process.stdout.splitlines() if line.strip()]
        self.assertTrue(lines, f"{scenario} printed nothing")
        return json.loads(lines[-1])

    def cursor_offset(self) -> int | None:
        path = Path(str(self.bridge_file) + ".cursor")
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))["offset"]

    # -- auto-claim helpers (session-id discovery) -------------------------
    def claim_env(
        self, session_id: str, extra: dict | None = None, bridge_file: Path | None = None
    ) -> dict:
        env = dict(os.environ)
        env.pop("AGENT_LINK_BRIDGE_FILE", None)
        env.pop("AGENT_LINK_BINDING", None)
        env.pop("AGENT_LINK_CREDENTIALS", None)
        env["AGENT_LINK_TEST_BRIDGE_FILE"] = str(bridge_file or self.bridge_file)
        env["AGENT_LINK_TEST_SESSION_ID"] = session_id
        env.update(extra or {})
        return env

    def write_claim(self, binding: str, parent_session: str, child_session: str = "child") -> None:
        write_entry(
            entry_from_row(
                self.base,
                {
                    "binding_id": binding,
                    "run_id": "r-" + binding,
                    "native_task_id": None,
                    "parent_framework": "opencode",
                    "parent_session_id": parent_session,
                    "child_framework": "opencode",
                    "child_session_id": child_session,
                    "config_digest": "d" * 64,
                },
            )
        )

    def run_claim(
        self, scenario: str, session_id: str, timeout: int = 120, bridge_file: Path | None = None
    ) -> dict:
        process = subprocess.run(
            [RUNTIME, str(TOOL_LOOP), scenario],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            env=self.claim_env(session_id, bridge_file=bridge_file),
            timeout=timeout,
        )
        self.assertEqual(
            process.returncode, 0, f"{scenario} failed:\n{process.stdout}\n{process.stderr}"
        )
        lines = [line for line in process.stdout.splitlines() if line.strip()]
        self.assertTrue(lines, f"{scenario} printed nothing")
        return json.loads(lines[-1])

    # -- auto-claim tests -------------------------------------------------
    @unittest.skipUnless(RUNTIME, "bun or a type-stripping node is required")
    def test_auto_claim_matches_session_id(self):
        session_id = "parent-auto"
        self.write_claim("bridge-b", session_id)
        self.write_events(["auto-1", "auto-2"], thread_id=session_id)
        out = self.run_claim("bridge-auto-claim", session_id)
        self.assertEqual([entry["text"] for entry in out["sent"]], ["auto-1", "auto-2"])
        for entry in out["sent"]:
            self.assertEqual(entry["sessionID"], session_id)
            self.assertEqual(entry["delivery"], "queue")
        self.assertTrue(out["lockHeld"])
        self.assertFalse(out["lockAfterShutdown"], "shutdown must release the lock")
        self.assertEqual(out["cursor"]["offset"], self.bridge_file.stat().st_size)

    @unittest.skipUnless(RUNTIME, "bun or a type-stripping node is required")
    def test_auto_claim_ignores_other_parents_and_foreign_thread(self):
        session_id = "parent-auto"
        self.write_claim("other-b", "someone-else")
        self.write_claim("bridge-b", session_id)
        self.write_events(["auto-wrong"], thread_id="not-me")
        self.write_events(["auto-ok"], thread_id=session_id)
        out = self.run_claim("bridge-auto-claim", session_id)
        self.assertEqual([entry["text"] for entry in out["sent"]], ["auto-ok"])
        self.assertTrue(
            any("skipping invalid event" in line for line in out["logs"]),
            out["logs"],
        )

    @unittest.skipUnless(RUNTIME, "bun or a type-stripping node is required")
    def test_auto_claim_discovers_late_binding(self):
        session_id = "parent-auto"
        process = subprocess.Popen(
            [RUNTIME, str(TOOL_LOOP), "bridge-auto-claim-late"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(REPO_ROOT),
            env=self.claim_env(session_id),
        )
        try:
            time.sleep(2.5)  # the plugin is up while the registry is still empty
            self.write_claim("bridge-b", session_id)
            self.write_events(["late-1"], thread_id=session_id)
            stdout, stderr = process.communicate(timeout=90)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
        self.assertEqual(process.returncode, 0, stderr)
        lines = [line for line in stdout.splitlines() if line.strip()]
        self.assertTrue(lines)
        out = json.loads(lines[-1])
        self.assertEqual([entry["text"] for entry in out["sent"]], ["late-1"])
        self.assertTrue(out["lockHeld"])

    @unittest.skipUnless(RUNTIME, "bun or a type-stripping node is required")
    def test_auto_claim_child_inbox(self):
        """A child session claims its inbox by child.session_id and gets woken."""
        session_id = "child-auto"
        self.write_claim("bridge-b", parent_session="parent-1", child_session=session_id)
        self.write_events(
            ["inbox-1", "inbox-2"],
            thread_id="parent-1",
            child_session_id=session_id,
            path=self.inbox_file,
        )
        self.write_events(
            ["inbox-wrong"],
            thread_id="parent-1",
            child_session_id="other-child",
            path=self.inbox_file,
        )
        out = self.run_claim("bridge-auto-claim", session_id, bridge_file=self.inbox_file)
        self.assertEqual([entry["text"] for entry in out["sent"]], ["inbox-1", "inbox-2"])
        self.assertTrue(
            any("skipping invalid event" in line for line in out["logs"]),
            out["logs"],
        )
        self.assertTrue(out["lockHeld"])
        self.assertFalse(out["lockAfterShutdown"])

    @unittest.skipUnless(RUNTIME, "bun or a type-stripping node is required")
    def test_failed_injection_retries_in_same_process(self):
        """Regression: the failed line must be restored byte-exact (with its newline)."""
        self.write_events(["event-A", "event-B", "event-C"])
        marker = self.base / "fail.marker"
        marker.write_text("event-B", encoding="utf-8")
        out = self.run_bridge(
            "bridge-fail-retry-same-process", extra_env={"AGENT_LINK_TEST_FAIL_FILE": str(marker)}
        )
        self.assertEqual(
            [entry["text"] for entry in out["sent"]],
            ["event-A", "event-B", "event-C"],
            "a same-process retry must restore the exact line and preserve order",
        )
        self.assertEqual(out["cursor"]["offset"], self.bridge_file.stat().st_size)

    # -- tests ---------------------------------------------------------
    @unittest.skipUnless(RUNTIME, "bun or a type-stripping node is required")
    def test_resume_across_restarts(self):
        self.write_events(["prompt-A", "prompt-B"])
        size = self.bridge_file.stat().st_size
        first = self.run_bridge()
        self.assertEqual([entry["text"] for entry in first["sent"]], ["prompt-A", "prompt-B"])
        for entry in first["sent"]:
            self.assertEqual(entry["sessionID"], "parent")
            self.assertEqual(entry["delivery"], "queue")
        self.assertEqual(first["cursor"]["offset"], size)
        stat = self.bridge_file.stat()
        self.assertEqual((first["cursor"]["dev"], first["cursor"]["ino"]), (stat.st_dev, stat.st_ino))
        self.assertFalse(first["lockExists"], "shutdown must release the lock")
        second = self.run_bridge()
        self.assertEqual(second["sent"], [], "a restart must not replay consumed events")
        self.assertEqual(second["cursor"]["offset"], size)

    @unittest.skipUnless(RUNTIME, "bun or a type-stripping node is required")
    def test_failure_blocks_and_preserves_order(self):
        byte_lines = self.write_events(["event-A", "event-B", "event-C"])
        marker = self.base / "fail.marker"
        marker.write_text("event-B", encoding="utf-8")
        first = self.run_bridge(extra_env={"AGENT_LINK_TEST_FAIL_FILE": str(marker)})
        self.assertEqual([entry["text"] for entry in first["sent"]], ["event-A"])
        self.assertIn("injection failed", "\n".join(first["logs"]))
        self.assertFalse(marker.exists(), "the failing injection consumes its marker")
        self.assertEqual(first["cursor"]["offset"], len(byte_lines[0]), "cursor stops before the failure")
        second = self.run_bridge()
        self.assertEqual(
            [entry["text"] for entry in second["sent"]],
            ["event-B", "event-C"],
            "the retry resumes at the failed event, not before it",
        )
        self.assertEqual(second["cursor"]["offset"], self.bridge_file.stat().st_size)

    @unittest.skipUnless(RUNTIME, "bun or a type-stripping node is required")
    def test_second_consumer_is_refused_then_reclaimed(self):
        self.write_events(["event-1"])
        holder = subprocess.Popen(
            [RUNTIME, str(TOOL_LOOP), "bridge-hold"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(REPO_ROOT),
            env=self.bridge_env(),
        )
        try:
            holding_line = holder.stdout.readline()
            self.assertTrue(holding_line, "bridge-hold never reported holding")
            self.assertTrue(json.loads(holding_line)["holding"])
            # The holder consumes event-1 on its first tick; wait for its cursor.
            deadline = time.time() + 15
            while time.time() < deadline and self.cursor_offset() is None:
                time.sleep(0.05)
            taken_offset = self.cursor_offset()
            self.assertIsNotNone(taken_offset, "the holder never consumed the first event")
            refused = self.run_bridge()
            self.assertTrue(refused["refused"], refused)
            self.assertEqual(refused["sent"], [])
            self.assertEqual(self.cursor_offset(), taken_offset, "a refused consumer writes no cursor")
            holder_token = json.loads(holding_line)["lock"]
            lock_text = Path(str(self.bridge_file) + ".lock").read_text(encoding="utf-8").strip()
            self.assertEqual(
                lock_text, holder_token, "a refused consumer leaves the owner's lock alone"
            )
        finally:
            holder.kill()
            holder.wait(timeout=15)
            holder.stdout.close()
            holder.stderr.close()
        # The stale lock of the dead holder is reclaimed and new events flow.
        self.write_events(["event-2"])
        reclaimed = self.run_bridge()
        self.assertFalse(reclaimed["refused"], reclaimed)
        self.assertEqual([entry["text"] for entry in reclaimed["sent"]], ["event-2"])
        self.assertEqual(reclaimed["cursor"]["offset"], self.bridge_file.stat().st_size)

    @unittest.skipUnless(RUNTIME, "bun or a type-stripping node is required")
    def test_session_switch_reclaims_the_lock_once(self):
        self.write_events(["switch-event"])
        out = self.run_bridge(scenario="bridge-switch")
        self.assertEqual([entry["text"] for entry in out["sent"]], ["switch-event"])
        self.assertEqual(out["injectedBeforeSwitch"], 1)
        self.assertGreaterEqual(out["clearedTimers"], 1, "the old watcher timer must be cleared")
        self.assertEqual(out["activeTimers"], 1, "the new session owns exactly one timer")
        first_token, second_token = out["lockTokens"]
        self.assertNotEqual(first_token, second_token)
        self.assertTrue(first_token.split(":")[0].isdigit())
        self.assertTrue(second_token.split(":")[0].isdigit())
        self.assertFalse(out["lockExists"], "shutdown releases the lock")

    @unittest.skipUnless(RUNTIME, "bun or a type-stripping node is required")
    def test_replace_and_truncate_restart_from_zero(self):
        self.write_events(["event-A"])
        first = self.run_bridge()
        self.assertEqual([entry["text"] for entry in first["sent"]], ["event-A"])
        # Replacement at the same path (new inode): replay from the beginning.
        self.bridge_file.unlink()
        self.write_events(["event-B"])
        replaced = self.run_bridge()
        self.assertEqual([entry["text"] for entry in replaced["sent"]], ["event-B"])
        # Truncation of the same file below the cursor: replay from zero.
        short = json.dumps(
            {"v": 1, "binding_id": self.binding, "prompt": "truncated-event", "kind": "report"}
        ) + "\n"
        self.assertTrue(len(short.encode("utf-8")) < replaced["cursor"]["offset"])
        self.bridge_file.write_text(short, encoding="utf-8")
        truncated = self.run_bridge()
        self.assertEqual([entry["text"] for entry in truncated["sent"]], ["truncated-event"])
        self.assertEqual(truncated["cursor"]["offset"], self.bridge_file.stat().st_size)

    @unittest.skipUnless(RUNTIME, "bun or a type-stripping node is required")
    def test_oversized_and_invalid_events_are_skipped(self):
        oversized = json.dumps(
            {"v": 1, "binding_id": self.binding, "prompt": "x" * (1024 * 1024 + 4096)}
        )
        lines = [
            oversized,
            json.dumps({"v": 1, "binding_id": self.binding}),  # missing prompt
            json.dumps({"v": 1, "binding_id": "other-binding", "prompt": "mismatch"}),
            json.dumps({"v": 1, "binding_id": self.binding, "prompt": "good-event"}),
        ]
        self.bridge_file.parent.mkdir(parents=True, exist_ok=True)
        self.bridge_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        out = self.run_bridge()
        self.assertEqual([entry["text"] for entry in out["sent"]], ["good-event"])
        skips = [line for line in out["logs"] if "skip" in line]
        self.assertEqual(len(skips), 3, out["logs"])
        self.assertEqual(out["cursor"]["offset"], self.bridge_file.stat().st_size)
        again = self.run_bridge()
        self.assertEqual(again["sent"], [], "skipped events must not be retried")


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
