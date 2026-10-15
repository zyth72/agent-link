"""Pi extension acceptance: no-model loading plus a real HTTP loopback closed loop.

These tests load the real ``extensions/pi/agent-link.ts`` with jiti (the Pi
runtime's loader) and exercise it against a real ``agent_link.host`` service
whose bound notifier acts as the parent.  They never call a model and never
touch the background-tasks desktop bridge; the notifier stub replies through
the ordinary Agent Link CLI.

The tests are skipped when Node.js or a local Pi installation is unavailable.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_link.storage import Store

REPO_ROOT = Path(__file__).resolve().parents[1]
EXTENSION = REPO_ROOT / "extensions" / "pi" / "agent-link.ts"

NOTIFIER_STUB = '''\
"""E2E parent stand-in: answers asks through the normal Agent Link CLI."""
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path


def _reply(root, binding, request_id):
    subprocess.run(
        [sys.executable, "-m", "agent_link", "--root", root, "reply",
         "--binding", binding, "--request-id", request_id,
         "--message-id", "stub-reply-" + request_id,
         "--answer", "stub-parent-answer"],
        check=True, capture_output=True, text=True,
    )


def bridge(payload):
    log = Path(os.environ["STUB_NOTIFIER_LOG"])
    prompt = payload.get("prompt", "")
    with log.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"prompt": prompt}, ensure_ascii=False) + "\\n")
    match = re.search(r"^request_id: ([A-Za-z0-9_-]+)$", prompt, re.M)
    if not match:
        return {"accepted": True}
    request_id = match.group(1)
    root = os.environ["AGENT_LINK_TEST_ROOT"]
    binding = os.environ["AGENT_LINK_TEST_BINDING"]
    mode = os.environ.get("STUB_NOTIFIER_MODE", "auto")
    if mode == "auto":
        _reply(root, binding, request_id)
    elif mode == "late":
        threading.Thread(
            target=lambda: (time.sleep(1.5), _reply(root, binding, request_id)),
            daemon=True,
        ).start()
    return {"accepted": True}
'''


def _find_pi_root() -> Path | None:
    override = os.environ.get("PI_CODING_AGENT_ROOT")
    if override and (Path(override) / "dist" / "index.js").is_file():
        return Path(override)
    search_path = os.environ.get("PATH", "") + os.pathsep + str(Path.home() / ".npm-global" / "bin")
    executable = shutil.which("pi", path=search_path)
    if executable:
        for parent in [Path(executable).resolve(), *Path(executable).resolve().parents]:
            package_json = parent / "package.json"
            if not package_json.is_file():
                continue
            try:
                data = json.loads(package_json.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if data.get("name") == "@earendil-works/pi-coding-agent":
                return parent
    fallback = Path.home() / ".npm-global" / "lib" / "node_modules" / "@earendil-works" / "pi-coding-agent"
    if (fallback / "dist" / "index.js").is_file():
        return fallback
    return None


NODE = shutil.which("node")
PI_ROOT = _find_pi_root()
PI_EXECUTABLE = shutil.which(
    "pi", path=os.environ.get("PATH", "") + os.pathsep + str(Path.home() / ".npm-global" / "bin")
)
NODE_READY = bool(NODE and PI_ROOT and (PI_ROOT / "node_modules" / "jiti" / "lib" / "jiti.mjs").is_file())


class PiExtensionE2ECase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "state"
        self.stub = self.base / "notifier_stub.py"
        self.stub.write_text(NOTIFIER_STUB, encoding="utf-8")
        self.log = self.base / "notifier.log"
        self.credentials = self.base / "credentials.json"
        self.binding = "e2e-b"
        store = Store(self.root)
        try:
            store.bind(
                {
                    "binding_id": self.binding,
                    "run_id": "e2e-run",
                    "native_task_id": None,
                    "parent": {"framework": "codex-desktop", "session_id": "e2e-parent"},
                    "child": {"framework": "pi", "session_id": "e2e-child"},
                    "notifier_path": str(self.stub),
                }
            )
        finally:
            store.close()
        self.hosts: list[subprocess.Popen] = []
        self.addCleanup(self.stop_all_hosts)

    # -- helpers -------------------------------------------------------
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

    def host_env(self, mode: str) -> dict:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        env["STUB_NOTIFIER_LOG"] = str(self.log)
        env["STUB_NOTIFIER_MODE"] = mode
        env["AGENT_LINK_TEST_ROOT"] = str(self.root)
        env["AGENT_LINK_TEST_BINDING"] = self.binding
        return env

    def start_host(self, mode: str = "auto") -> subprocess.Popen:
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
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(REPO_ROOT),
            env=self.host_env(mode),
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
        self.assertEqual(process.returncode, 0, f"host shutdown failed: stdout={stdout} stderr={stderr}")

    def run_node(self, scenario: str, ids: dict) -> dict:
        env = dict(os.environ)
        env["PI_CODING_AGENT_ROOT"] = str(PI_ROOT)
        env["AGENT_LINK_CREDENTIALS"] = str(self.credentials)
        env["AGENT_LINK_TEST_IDS"] = json.dumps(ids)
        process = subprocess.run(
            [str(NODE), str(REPO_ROOT / "tests" / "pi" / "tool_loop.mjs"), scenario],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            env=env,
            timeout=180,
        )
        self.assertEqual(process.returncode, 0, f"node {scenario} failed:\n{process.stdout}\n{process.stderr}")
        lines = [line for line in process.stdout.splitlines() if line.strip()]
        self.assertTrue(lines, f"node {scenario} printed nothing")
        return json.loads(lines[-1])

    def notifier_calls(self) -> int:
        if not self.log.exists():
            return 0
        return len([line for line in self.log.read_text(encoding="utf-8").splitlines() if line.strip()])

    # -- tests ---------------------------------------------------------
    @unittest.skipUnless(NODE_READY, "Node.js and a local Pi installation are required")
    def test_real_http_loopback_closed_loop(self):
        ids = {
            "report_message_id": "pi-e2e-report-001",
            "request_id": "pi-e2e-request-001",
            "question_message_id": "pi-e2e-question-001",
            "late_request_id": "pi-e2e-late-001",
            "late_message_id": "pi-e2e-late-msg-001",
        }
        host_a = self.start_host("auto")
        happy = self.run_node("happy", ids)
        self.assertTrue(happy["report"]["ok"], happy["report"])
        self.assertEqual(happy["report"]["details"]["delivery_state"], "host_accepted")
        self.assertFalse(happy["report"]["details"]["idempotent"])
        self.assertTrue(happy["ask"]["ok"], happy["ask"])
        self.assertEqual(happy["ask"]["details"]["status"], "replied")
        self.assertEqual(happy["ask"]["details"]["answer"], "stub-parent-answer")
        self.assertFalse(happy["ask"]["details"]["late"])
        self.assertIn("stub-parent-answer", happy["ask"]["text"])
        self.assertTrue(happy["check"]["ok"], happy["check"])
        self.assertEqual(happy["check"]["details"]["status"], "replied")
        self.assertEqual(self.notifier_calls(), 2)
        self.stop_host(host_a)
        self.assertFalse(self.credentials.exists(), "graceful shutdown must remove its own credentials")

        # Same database, new service process: an idempotent replay must not redeliver.
        host_b = self.start_host("auto")
        replay = self.run_node("replay", ids)
        self.assertTrue(replay["report"]["ok"], replay["report"])
        self.assertTrue(replay["report"]["details"]["idempotent"])
        self.assertEqual(replay["report"]["details"]["delivery_state"], "host_accepted")
        self.assertEqual(self.notifier_calls(), 2, "replay after restart must not re-deliver")
        self.stop_host(host_b)

        # The child times out, then the parent's late answer is still persisted and readable.
        host_c = self.start_host("late")
        late = self.run_node("late", ids)
        self.assertTrue(late["ask"]["ok"], late["ask"])
        self.assertEqual(late["ask"]["details"]["status"], "timed_out")
        self.assertIsNone(late["ask"]["details"]["answer"])
        self.assertIn("timed_out", late["ask"]["text"])
        self.assertTrue(late["check"]["ok"], late["check"])
        self.assertEqual(late["check"]["details"]["status"], "timed_out")
        self.assertTrue(late["check"]["details"]["late"])
        self.assertEqual(late["check"]["details"]["answer"], "stub-parent-answer")
        self.assertEqual(self.notifier_calls(), 3)
        self.stop_host(host_c)

    @unittest.skipUnless(NODE_READY, "Node.js and a local Pi installation are required")
    def test_extension_unit_suite(self):
        """jiti-loads the real extension with a fake ExtensionAPI and a fake
        HTTP server: tool registration, credentials, HTTP errors, timeout,
        abort.  No model and no parent host are involved."""
        env = dict(os.environ)
        env["PI_CODING_AGENT_ROOT"] = str(PI_ROOT)
        process = subprocess.run(
            [str(NODE), str(REPO_ROOT / "tests" / "pi" / "extension_unit.test.mjs")],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            env=env,
            timeout=180,
        )
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)

    @unittest.skipUnless(PI_EXECUTABLE, "a local Pi executable is required")
    def test_pi_loads_extension_without_a_model(self):
        """`pi --help` loads registered extensions; a probe re-export that
        imports the real extension and registers a flag proves our file is
        imported by Pi's own jiti loader.  No model or session is started."""
        probe = self.base / "probe.ts"
        probe.write_text(
            "import type { ExtensionAPI } from '@earendil-works/pi-coding-agent';\n"
            f"import agentLink from {json.dumps(str(EXTENSION))};\n\n"
            "export default function (pi: ExtensionAPI) {\n"
            "  agentLink(pi);\n"
            "  pi.registerFlag('agent-link-probe', { type: 'boolean', description: 'agent-link probe flag' });\n"
            "}\n",
            encoding="utf-8",
        )
        process = subprocess.run(
            [str(PI_EXECUTABLE), "-ne", "--extension", str(probe), "--help"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            timeout=120,
        )
        self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
        self.assertIn("--agent-link-probe", process.stdout)
        self.assertNotIn("agent-link-probe", process.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
