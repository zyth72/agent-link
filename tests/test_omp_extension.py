"""OMP extension acceptance: no-model registration, real HTTP loopback closed
loop, and the parent bridge watcher suite.

Child side: loads the real ``extensions/omp/agent-link.ts`` with Bun (its
native loader) and calls the tools against a real ``agent_link.host`` service
whose OmpHost driver writes the bridge file; the parent stand-in replies
through the ordinary Agent Link CLI.  Parent side: drives the same extension's
bridge watcher with a fake ExtensionAPI over real bridge files (restart resume,
a failing injection, a second consumer, session switching, file replacement and
oversized/invalid events).

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
from agent_link.storage import Store

REPO_ROOT = Path(__file__).resolve().parents[1]
EXTENSION = REPO_ROOT / "extensions" / "omp" / "agent-link.ts"
TOOL_LOOP = REPO_ROOT / "tests" / "omp" / "tool_loop.mjs"

NOTIFIER_STUB = '''\
"""Unused by OmpHost; kept so the binding passes notifier_path validation."""
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


class OmpChildE2ECase(unittest.TestCase):
    """Real HTTP loopback: Bun-loaded child tools against a real host service."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
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
                    "parent": {"framework": "omp", "session_id": "e2e-parent"},
                    "child": {"framework": "omp", "session_id": "e2e-child"},
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
                "agent_link.hosts.omp:OmpHost",
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
        }
        host = self.start_host()
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


class OmpBridgeCase(unittest.TestCase):
    """Parent-side bridge watcher acceptance, no service and no model."""

    binding = "bridge-b"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.bridge_file = self.base / "omp-bridge" / f"{self.binding}.jsonl"

    # -- helpers -------------------------------------------------------
    def bridge_env(self, extra: dict | None = None) -> dict:
        env = dict(os.environ)
        env["AGENT_LINK_BRIDGE_FILE"] = str(self.bridge_file)
        env["AGENT_LINK_BINDING"] = self.binding
        env.pop("AGENT_LINK_CREDENTIALS", None)
        env.update(extra or {})
        return env

    def write_events(self, prompts: list[str], binding: str | None = None) -> list[bytes]:
        self.bridge_file.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            json.dumps(
                {
                    "v": 1,
                    "ts": "2026-01-01T00:00:00.000Z",
                    "source": "service",
                    "binding_id": self.binding if binding is None else binding,
                    "thread_id": "parent",
                    "kind": "report",
                    "message_id": f"m-{index}",
                    "prompt": prompt,
                }
            )
            + "\n"
            for index, prompt in enumerate(prompts, start=1)
        ]
        with self.bridge_file.open("a", encoding="utf-8") as stream:
            stream.write("".join(lines))
        return [line.encode("utf-8") for line in lines]

    def run_bridge(self, scenario: str = "bridge", extra_env: dict | None = None, timeout: int = 180) -> dict:
        process = subprocess.run(
            [BUN, str(TOOL_LOOP), scenario],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            env=self.bridge_env(extra_env),
            timeout=timeout,
        )
        self.assertEqual(
            process.returncode, 0, f"bun {scenario} failed:\n{process.stdout}\n{process.stderr}"
        )
        lines = [line for line in process.stdout.splitlines() if line.strip()]
        self.assertTrue(lines, f"bun {scenario} printed nothing")
        return json.loads(lines[-1])

    def cursor_offset(self) -> int | None:
        path = Path(str(self.bridge_file) + ".cursor")
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))["offset"]

    # -- tests ---------------------------------------------------------
    @unittest.skipUnless(BUN_READY, "bun is required")
    def test_resume_across_restarts(self):
        self.write_events(["prompt-A", "prompt-B"])
        size = self.bridge_file.stat().st_size
        first = self.run_bridge()
        self.assertEqual([entry["text"] for entry in first["sent"]], ["prompt-A", "prompt-B"])
        for entry in first["sent"]:
            self.assertEqual(entry["options"], {"deliverAs": "aside", "attribution": "agent"})
        self.assertEqual(first["cursor"]["offset"], size)
        stat = self.bridge_file.stat()
        self.assertEqual((first["cursor"]["dev"], first["cursor"]["ino"]), (stat.st_dev, stat.st_ino))
        self.assertTrue(first["notifies"], "each injection is notified")
        self.assertFalse(first["lockExists"], "shutdown must release the lock")
        second = self.run_bridge()
        self.assertEqual(second["sent"], [], "a restart must not replay consumed events")
        self.assertEqual(second["cursor"]["offset"], size)

    @unittest.skipUnless(BUN_READY, "bun is required")
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
        # A UI notify failure must not affect injection or cursor advancement.
        self.write_events(["event-D"])
        third = self.run_bridge(extra_env={"AGENT_LINK_TEST_NOTIFY_THROW": "1"})
        self.assertEqual([entry["text"] for entry in third["sent"]], ["event-D"])
        self.assertEqual(third["notifies"], [])
        self.assertEqual(third["cursor"]["offset"], self.bridge_file.stat().st_size)

    @unittest.skipUnless(BUN_READY, "bun is required")
    def test_second_consumer_is_refused_then_reclaimed(self):
        self.write_events(["event-1"])
        holder = subprocess.Popen(
            [BUN, str(TOOL_LOOP), "bridge-hold"],
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

    @unittest.skipUnless(BUN_READY, "bun is required")
    def test_session_switch_reclaims_the_lock_once(self):
        self.write_events(["switch-event"])
        out = self.run_bridge(scenario="bridge-switch")
        self.assertEqual([entry["text"] for entry in out["sent"]], ["switch-event"])
        self.assertEqual(out["injectedBeforeSwitch"], 1)
        self.assertGreaterEqual(out["firstCleared"], 1, "the old ctx timer must be cleared")
        self.assertEqual(out["secondTimers"], 1, "the new session owns exactly one timer")
        first_token, second_token = out["lockTokens"]
        self.assertNotEqual(first_token, second_token)
        self.assertTrue(first_token.split(":")[0].isdigit())
        self.assertTrue(second_token.split(":")[0].isdigit())
        self.assertFalse(out["lockExists"], "shutdown releases the lock")

    @unittest.skipUnless(BUN_READY, "bun is required")
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

    @unittest.skipUnless(BUN_READY, "bun is required")
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


class OmpRegistrationCase(unittest.TestCase):
    """The real OMP runtime loads the extension and registers the three tools."""

    @unittest.skipUnless(BUN_READY and OMP, "bun and the omp executable are required")
    def test_real_omp_registers_tools_without_a_model(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = dict(os.environ)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
