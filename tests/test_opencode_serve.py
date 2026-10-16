"""Real OpenCode serve tests for the self-managed runner (slice 3b, no model).

Starts the real ``opencode`` executable through the runner CLI in an isolated
HOME/XDG environment (never touching the user's config, credentials or
database) and reproduces the B-group gates: plugin-active readiness (C6),
session pre-creation, descriptor permissions, 401/404, same-id idempotency,
loopback-only listening, restart/session recovery and explicit descriptor
residue failure.  The whole module is skipped when ``opencode`` is missing.
"""

from __future__ import annotations

import json
import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_link.errors import ConflictError, DeliveryFailure
from agent_link.hosts.opencode import push, serve

REPO_ROOT = Path(__file__).resolve().parents[1]
CORE_CLI = [sys.executable, "-m", "agent_link"]
RUNNER_CLI = [sys.executable, "-m", "agent_link.hosts.opencode.serve"]
OPENCODE = shutil.which("opencode")
NOTIFIER_STUB = "def bridge(payload):\n    return {}\n"


def _read_ready(process: subprocess.Popen, timeout: float = 120.0) -> dict:
    """Read the runner's first stdout line with a timeout (select on POSIX)."""
    ready, _writable, _error = select.select([process.stdout], [], [], timeout)
    if not ready:
        raise AssertionError("runner did not print a ready line in time")
    line = process.stdout.readline()
    if not line:
        stderr = process.stderr.read() if process.stderr else ""
        raise AssertionError(f"runner exited before ready:\n{stderr}")
    return json.loads(line)


def _stop_process(process: subprocess.Popen, timeout: float = 30.0) -> tuple[int, str]:
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
    try:
        _stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        _stdout, stderr = process.communicate(timeout=10)
    return process.returncode, stderr or ""


class RunnerEnvCase(unittest.TestCase):
    """Common isolated environment + binding helpers (no opencode required)."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "root"
        self.sessions = self.base / "sessions"
        self.work = self.base / "work"
        self.work.mkdir()
        for name in ("home", "xdg-config", "xdg-data", "xdg-state", "xdg-cache"):
            (self.base / name).mkdir()
        self.env = dict(os.environ)
        self.env["PYTHONPATH"] = str(REPO_ROOT)
        self.env["AGENT_LINK_SESSIONS"] = str(self.sessions)
        self.env["AGENT_LINK_REGISTRY"] = str(self.base / "registry")
        self.env.update(
            {
                "HOME": str(self.base / "home"),
                "XDG_CONFIG_HOME": str(self.base / "xdg-config"),
                "XDG_DATA_HOME": str(self.base / "xdg-data"),
                "XDG_STATE_HOME": str(self.base / "xdg-state"),
                "XDG_CACHE_HOME": str(self.base / "xdg-cache"),
                "OPENCODE_DISABLE_MODELS_FETCH": "1",
            }
        )
        self.processes: list[subprocess.Popen] = []
        self.addCleanup(self._cleanup_processes)

    def _cleanup_processes(self):
        for process in reversed(self.processes):
            if process.poll() is None:
                process.kill()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
        self.processes.clear()

    def bind(self, binding_id="runner-b", child_session="ses_runner_real_1"):
        notifier = self.base / f"stub-{binding_id}.py"
        notifier.write_text(NOTIFIER_STUB, encoding="utf-8")
        config = {
            "binding_id": binding_id,
            "run_id": "run-" + binding_id,
            "native_task_id": None,
            "parent": {"framework": "omp", "session_id": "parent-session"},
            "child": {"framework": "opencode", "session_id": child_session},
            "notifier_path": str(notifier),
        }
        config_path = self.base / f"bind-{binding_id}.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        proc = subprocess.run(
            CORE_CLI + ["--root", str(self.root), "bind", "--config", str(config_path)],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return child_session


class DescriptorWriterCase(RunnerEnvCase):
    """Writer/cleanup contract without any opencode process."""

    def test_writers_are_private_exclusive_and_cleanup_is_identity_bound(self):
        previous = os.environ.get("AGENT_LINK_SESSIONS")
        os.environ["AGENT_LINK_SESSIONS"] = str(self.sessions)

        def restore():
            os.environ.pop("AGENT_LINK_SESSIONS", None)
            if previous is not None:
                os.environ["AGENT_LINK_SESSIONS"] = previous

        self.addCleanup(restore)
        session_path = push.write_session_descriptor(
            {
                "protocol": push.PROTOCOL,
                "framework": "opencode",
                "session_id": "ses_writer_1",
                "endpoint": "http://127.0.0.1:1",
                "pid": os.getpid(),
                "serve_id": "serve-writer",
                "managed": True,
                "credential_kind": "server_descriptor",
            }
        )
        server_path = push.write_server_descriptor(
            {
                "protocol": push.PROTOCOL,
                "framework": "opencode",
                "serve_id": "serve-writer",
                "endpoint": "http://127.0.0.1:1",
                "pid": os.getpid(),
                "managed": True,
                "credential_kind": "inline_password",
                "password": "w" * 32,
            }
        )
        self.assertEqual(session_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(server_path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            push.write_session_descriptor(
                {
                    "protocol": push.PROTOCOL,
                    "framework": "opencode",
                    "session_id": "ses_writer_1",
                    "endpoint": "http://127.0.0.1:1",
                    "pid": os.getpid(),
                    "serve_id": "serve-other",
                    "managed": True,
                    "credential_kind": "server_descriptor",
                }
            )
        # A descriptor for another serve must never be removed.
        self.assertFalse(
            push.remove_descriptor_if_matches(server_path, serve_id="serve-other", pid=os.getpid())
        )
        self.assertTrue(server_path.is_file())
        self.assertTrue(
            push.remove_descriptor_if_matches(server_path, serve_id="serve-writer", pid=os.getpid())
        )
        self.assertFalse(server_path.exists())
        self.assertTrue(
            push.remove_descriptor_if_matches(session_path, serve_id="serve-writer", pid=os.getpid())
        )
        self.assertFalse(session_path.exists())


@unittest.skipUnless(OPENCODE, "the opencode executable is required")
class OpenCodeServeRealCase(unittest.TestCase):
    """One real isolated serve shared by the positive interface checks."""

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="oc-serve-real-")
        cls.base = Path(cls.temp.name)
        cls.root = cls.base / "root"
        cls.sessions = cls.base / "sessions"
        cls.work = cls.base / "work"
        cls.work.mkdir()
        for name in ("home", "xdg-config", "xdg-data", "xdg-state", "xdg-cache"):
            (cls.base / name).mkdir()
        cls.env = dict(os.environ)
        cls.env["PYTHONPATH"] = str(REPO_ROOT)
        cls.env["AGENT_LINK_SESSIONS"] = str(cls.sessions)
        cls.env["AGENT_LINK_REGISTRY"] = str(cls.base / "registry")
        cls.env.update(
            {
                "HOME": str(cls.base / "home"),
                "XDG_CONFIG_HOME": str(cls.base / "xdg-config"),
                "XDG_DATA_HOME": str(cls.base / "xdg-data"),
                "XDG_STATE_HOME": str(cls.base / "xdg-state"),
                "XDG_CACHE_HOME": str(cls.base / "xdg-cache"),
                "OPENCODE_DISABLE_MODELS_FETCH": "1",
            }
        )
        notifier = cls.base / "stub.py"
        notifier.write_text(NOTIFIER_STUB, encoding="utf-8")
        config = {
            "binding_id": "runner-b",
            "run_id": "run-runner-b",
            "native_task_id": None,
            "parent": {"framework": "omp", "session_id": "parent-session"},
            "child": {"framework": "opencode", "session_id": "ses_real_ready_1"},
            "notifier_path": str(notifier),
        }
        config_path = cls.base / "bind.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        bound = subprocess.run(
            CORE_CLI + ["--root", str(cls.root), "bind", "--config", str(config_path)],
            capture_output=True,
            text=True,
            env=cls.env,
            timeout=30,
        )
        assert bound.returncode == 0, bound.stderr
        cls.previous_sessions_env = os.environ.get("AGENT_LINK_SESSIONS")
        os.environ["AGENT_LINK_SESSIONS"] = str(cls.sessions)
        cls.process = subprocess.Popen(
            RUNNER_CLI
            + [
                "--root",
                str(cls.root),
                "--binding",
                "runner-b",
                "serve",
                "--cwd",
                str(cls.work),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=cls.env,
        )
        cls.ready = _read_ready(cls.process)
        cls.endpoint = cls.ready["endpoint"]
        cls.session_id = cls.ready["session_id"]
        cls.session_path = Path(cls.ready["session_descriptor"])
        cls.server_path = Path(cls.ready["descriptor"])
        cls.server_descriptor = json.loads(cls.server_path.read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        _stop_process(cls.process)
        if cls.previous_sessions_env is None:
            os.environ.pop("AGENT_LINK_SESSIONS", None)
        else:
            os.environ["AGENT_LINK_SESSIONS"] = cls.previous_sessions_env
        cls.temp.cleanup()

    def test_ready_handshake_session_and_descriptors(self):
        ready = self.ready
        self.assertTrue(ready["ready"])
        self.assertEqual(ready["plugin"], "active")
        self.assertEqual(ready["binding_id"], "runner-b")
        self.assertEqual(ready["role"], "child")
        self.assertEqual(ready["session_id"], "ses_real_ready_1")
        self.assertTrue(ready["endpoint"].startswith("http://127.0.0.1:"))
        self.assertNotIn(self.server_descriptor["password"], json.dumps(ready))
        self.assertTrue(push._pid_alive(ready["serve_pid"]))

        session = json.loads(self.session_path.read_text(encoding="utf-8"))
        self.assertEqual(session["protocol"], push.PROTOCOL)
        self.assertEqual(session["framework"], "opencode")
        self.assertEqual(session["session_id"], self.session_id)
        self.assertEqual(session["endpoint"], self.endpoint)
        self.assertEqual(session["pid"], ready["serve_pid"])
        self.assertEqual(session["serve_id"], ready["serve_id"])
        self.assertIs(session["managed"], True)
        self.assertEqual(session["credential_kind"], "server_descriptor")
        self.assertNotIn("password", session)
        self.assertEqual(self.session_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.server_path.stat().st_mode & 0o777, 0o600)

        server = self.server_descriptor
        self.assertEqual(server["protocol"], push.PROTOCOL)
        self.assertEqual(server["serve_id"], ready["serve_id"])
        self.assertEqual(server["endpoint"], self.endpoint)
        self.assertEqual(server["pid"], ready["serve_pid"])
        self.assertEqual(server.get("supervisor_pid"), self.process.pid)
        self.assertIs(server["managed"], True)
        self.assertEqual(server["credential_kind"], "inline_password")
        self.assertGreaterEqual(len(server["password"]), 16)

        status, payload = serve._api(
            self.endpoint, server["password"], "GET", f"/api/session/{self.session_id}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(serve._unwrap(payload)["id"], self.session_id)

    def test_prompt_idempotency_and_client(self):
        password = self.server_descriptor["password"]
        path = f"/api/session/{self.session_id}/prompt"
        first = {"id": "msg_real_1", "text": "first real text", "delivery": "queue", "resume": False}
        second = {"id": "msg_real_1", "text": "second real text", "delivery": "queue", "resume": False}
        status, body = serve._api(self.endpoint, password, "POST", path, first)
        self.assertEqual(status, 200)
        admission = serve._unwrap(body)
        self.assertEqual(admission["sessionID"], self.session_id)
        self.assertEqual(admission["payload"]["text"], "first real text")
        status, body = serve._api(self.endpoint, password, "POST", path, second)
        self.assertEqual(status, 200)
        self.assertEqual(serve._unwrap(body)["payload"]["text"], "first real text")

        # The production client goes through the frozen descriptors.
        detail = push.post_prompt(
            "opencode",
            self.session_id,
            "client text",
            message_id="msg_client_1",
            resume=False,
        )
        self.assertEqual(detail, f"push:opencode/{self.session_id}#msg_client_1")

        status, body = serve._api(
            self.endpoint, password, "GET", f"/api/session/{self.session_id}/inbox"
        )
        self.assertEqual(status, 200)
        texts = [item["payload"]["text"] for item in serve._unwrap(body)]
        self.assertEqual(texts, ["first real text", "client text"])

    def test_401_404_and_loopback_only_listening(self):
        status, _payload = serve._api(
            self.endpoint, "wrong-password-" + "x" * 20, "GET", f"/api/session/{self.session_id}"
        )
        self.assertEqual(status, 401)
        status, _payload = serve._api(
            self.endpoint,
            self.server_descriptor["password"],
            "POST",
            "/api/session/ses_missing_00000000/prompt",
            {"id": "msg_missing", "text": "x", "delivery": "queue", "resume": False},
        )
        self.assertEqual(status, 404)

        proc_tcp = Path("/proc/net/tcp")
        if not proc_tcp.is_file():
            self.skipTest("cannot inspect listeners without /proc/net/tcp")
        port = int(self.endpoint.rsplit(":", 1)[1])
        listeners = []
        for line in proc_tcp.read_text(encoding="utf-8").splitlines()[1:]:
            fields = line.split()
            if len(fields) < 4 or fields[3] != "0A":
                continue
            address, port_hex = fields[1].split(":")
            if int(port_hex, 16) == port:
                listeners.append(address)
        self.assertEqual(
            set(listeners), {"0100007F"}, "the serve must listen on 127.0.0.1 only"
        )

    def test_status_cli_reports_ready_session_and_plugin(self):
        proc = subprocess.run(
            RUNNER_CLI
            + ["--root", str(self.root), "--binding", "runner-b", "status"],
            capture_output=True,
            text=True,
            env=self.env,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        status = json.loads(proc.stdout)
        self.assertEqual(status["runner"], "ready")
        self.assertEqual(status["session"], "present")
        self.assertEqual(status["plugin"], "active")
        self.assertEqual(status["session_id"], self.session_id)
        self.assertNotIn(self.server_descriptor["password"], proc.stdout)


@unittest.skipUnless(OPENCODE, "the opencode executable is required")
class OpenCodeRestartCase(unittest.TestCase):
    """Stopping and restarting recovers the session from the same database."""

    def test_restart_adopts_the_same_session(self):
        with tempfile.TemporaryDirectory(prefix="oc-restart-") as raw:
            base = Path(raw)
            root, sessions, work = base / "root", base / "sessions", base / "work"
            work.mkdir()
            for name in ("home", "xdg-config", "xdg-data", "xdg-state", "xdg-cache"):
                (base / name).mkdir()
            env = dict(os.environ)
            env["PYTHONPATH"] = str(REPO_ROOT)
            env["AGENT_LINK_SESSIONS"] = str(sessions)
            env["AGENT_LINK_REGISTRY"] = str(base / "registry")
            env.update(
                {
                    "HOME": str(base / "home"),
                    "XDG_CONFIG_HOME": str(base / "xdg-config"),
                    "XDG_DATA_HOME": str(base / "xdg-data"),
                    "XDG_STATE_HOME": str(base / "xdg-state"),
                    "XDG_CACHE_HOME": str(base / "xdg-cache"),
                    "OPENCODE_DISABLE_MODELS_FETCH": "1",
                }
            )
            notifier = base / "stub.py"
            notifier.write_text(NOTIFIER_STUB, encoding="utf-8")
            config = {
                "binding_id": "restart-b",
                "run_id": "run-restart-b",
                "native_task_id": None,
                "parent": {"framework": "omp", "session_id": "parent-session"},
                "child": {"framework": "opencode", "session_id": "ses_restart_1"},
                "notifier_path": str(notifier),
            }
            config_path = base / "bind.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            bound = subprocess.run(
                CORE_CLI + ["--root", str(root), "bind", "--config", str(config_path)],
                capture_output=True,
                text=True,
                env=env,
                timeout=30,
            )
            self.assertEqual(bound.returncode, 0, bound.stderr)
            previous = os.environ.get("AGENT_LINK_SESSIONS")
            os.environ["AGENT_LINK_SESSIONS"] = str(sessions)

            def restore():
                os.environ.pop("AGENT_LINK_SESSIONS", None)
                if previous is not None:
                    os.environ["AGENT_LINK_SESSIONS"] = previous

            self.addCleanup(restore)

            def start():
                process = subprocess.Popen(
                    RUNNER_CLI
                    + [
                        "--root",
                        str(root),
                        "--binding",
                        "restart-b",
                        "serve",
                        "--cwd",
                        str(work),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                )
                self.addCleanup(lambda: process.poll() is None and process.kill())
                return process, _read_ready(process)

            first, first_ready = start()
            push.post_prompt(
                "opencode",
                first_ready["session_id"],
                "persisted before restart",
                message_id="msg_restart_1",
                resume=False,
            )
            # Stop through the runner client: it must signal the supervisor so
            # the supervisor stops its own child and cleans up its descriptors.
            stopped = subprocess.run(
                RUNNER_CLI
                + ["--root", str(root), "--binding", "restart-b", "stop"],
                capture_output=True,
                text=True,
                env=env,
                timeout=60,
            )
            self.assertEqual(stopped.returncode, 0, stopped.stdout + stopped.stderr)
            stop_result = json.loads(stopped.stdout)
            self.assertTrue(stop_result["stopped"])
            self.assertEqual(stop_result["supervisor_pid"], first.pid)
            first.wait(timeout=30)
            self.assertEqual(first.returncode, 0, first.stderr.read() if first.stderr else "")
            self.assertFalse(Path(first_ready["session_descriptor"]).exists())
            self.assertFalse(Path(first_ready["descriptor"]).exists())
            self.assertFalse(push._pid_alive(first_ready["serve_pid"]))

            second, second_ready = start()
            self.assertEqual(second_ready["session_id"], first_ready["session_id"])
            self.assertNotEqual(second_ready["serve_id"], first_ready["serve_id"])
            status, payload = serve._api(
                second_ready["endpoint"],
                json.loads(Path(second_ready["descriptor"]).read_text(encoding="utf-8"))["password"],
                "GET",
                f"/api/session/{second_ready['session_id']}/inbox",
            )
            self.assertEqual(status, 200)
            texts = [item["payload"]["text"] for item in serve._unwrap(payload)]
            self.assertIn("persisted before restart", texts)
            code, stderr = _stop_process(second)
            self.assertEqual(code, 0, stderr)


class OpenCodeFailureCase(RunnerEnvCase):
    """Explicit failures: descriptor residue, missing plugin, dead targets."""

    def _runner(self, *args, binding_id="fail-b"):
        process = subprocess.Popen(
            RUNNER_CLI
            + ["--root", str(self.root), "--binding", binding_id, "serve", "--cwd", str(self.work), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.env,
        )
        self.processes.append(process)
        try:
            stdout, stderr = process.communicate(timeout=120)
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = process.communicate(timeout=10)
        return process.returncode, stdout, stderr

    def test_session_descriptor_residue_fails_explicitly(self):
        self.bind("fail-b", child_session="ses_residue_1")
        session_path = push.write_session_descriptor(
            {
                "protocol": push.PROTOCOL,
                "framework": "opencode",
                "session_id": "ses_residue_1",
                "endpoint": "http://127.0.0.1:1",
                "pid": os.getpid(),
                "serve_id": "serve-residue",
                "managed": True,
                "credential_kind": "server_descriptor",
            },
            sessions=self.sessions,
        )
        before = session_path.read_text(encoding="utf-8")
        code, stdout, stderr = self._runner()
        self.assertEqual(code, 4, stdout + stderr)
        payload = json.loads(stdout.strip().splitlines()[-1])
        self.assertEqual(payload["error"], "conflict")
        self.assertIn("already exists", payload["message"])
        self.assertEqual(session_path.read_text(encoding="utf-8"), before)

    def test_missing_descriptors_fail_without_spawning(self):
        self.bind("fail-b", child_session="ses_absent_1")
        for command in ("status", "send", "stop"):
            with self.subTest(command=command):
                args = [command]
                if command == "send":
                    args += ["--text", "hello"]
                proc = subprocess.run(
                    RUNNER_CLI
                    + ["--root", str(self.root), "--binding", "fail-b", *args],
                    capture_output=True,
                    text=True,
                    env=self.env,
                    timeout=30,
                )
                if command == "status":
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertEqual(json.loads(proc.stdout)["runner"], "absent")
                else:
                    self.assertEqual(proc.returncode, 6, proc.stdout + proc.stderr)
                    self.assertEqual(json.loads(proc.stdout.strip().splitlines()[-1])["error"], "not_found")

    @unittest.skipUnless(OPENCODE, "the opencode executable is required")
    def test_plugin_not_loaded_fails_closed(self):
        self.bind("fail-b", child_session="ses_noplugin_1")
        dummy = self.base / "dummy-plugin.ts"
        dummy.write_text(
            'export default { id: "not-agent-link", async setup() {} };\n', encoding="utf-8"
        )
        code, stdout, stderr = self._runner(
            "--extension", str(dummy), "--ready-timeout", "6"
        )
        self.assertEqual(code, 7, stdout + stderr)
        payload = json.loads(stdout.strip().splitlines()[-1])
        self.assertEqual(payload["error"], "delivery_failed")
        self.assertIn("did not become active", payload["message"])
        self.assertFalse(
            push.session_descriptor_path("opencode", "ses_noplugin_1").exists()
        )

    def test_timeout_and_refused_mapping(self):
        previous = os.environ.get("AGENT_LINK_SESSIONS")
        os.environ["AGENT_LINK_SESSIONS"] = str(self.sessions)

        def restore():
            os.environ.pop("AGENT_LINK_SESSIONS", None)
            if previous is not None:
                os.environ["AGENT_LINK_SESSIONS"] = previous

        self.addCleanup(restore)

        class BlackHole:
            def __init__(self):
                self.socket = socket.socket()
                self.socket.bind(("127.0.0.1", 0))
                self.socket.listen(4)
                self.port = self.socket.getsockname()[1]
                self.connections = []
                self.thread = threading.Thread(target=self._accept, daemon=True)
                self.thread.start()

            def _accept(self):
                while True:
                    try:
                        connection, _address = self.socket.accept()
                    except OSError:
                        return
                    self.connections.append(connection)  # accept, never answer

            def close(self):
                self.socket.close()
                for connection in self.connections:
                    try:
                        connection.close()
                    except OSError:
                        pass

        blackhole = BlackHole()
        self.addCleanup(blackhole.close)
        blackhole_session = self.bind("fail-bh", child_session="ses_blackhole_1")
        blackhole_endpoint = f"http://127.0.0.1:{blackhole.port}"
        push.write_server_descriptor(
            {
                "protocol": push.PROTOCOL,
                "framework": "opencode",
                "serve_id": "serve-blackhole",
                "endpoint": blackhole_endpoint,
                "pid": os.getpid(),
                "managed": True,
                "credential_kind": "inline_password",
                "password": "b" * 32,
            }
        )
        push.write_session_descriptor(
            {
                "protocol": push.PROTOCOL,
                "framework": "opencode",
                "session_id": blackhole_session,
                "endpoint": blackhole_endpoint,
                "pid": os.getpid(),
                "serve_id": "serve-blackhole",
                "managed": True,
                "credential_kind": "server_descriptor",
            }
        )
        with mock.patch.object(push, "TIMEOUT_SECONDS", 0.3):
            with self.assertRaises(DeliveryFailure) as caught:
                push.post_prompt(
                    "opencode", blackhole_session, "anyone?", message_id="msg_bh", resume=False
                )
        self.assertIn("timed out or is unavailable", str(caught.exception))
        self.assertIn("outcome unknown", str(caught.exception))

        # A closed port maps to the same unknown-outcome failure, not a fake success.
        dead_session = self.bind("fail-dead", child_session="ses_dead_1")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            free_port = probe.getsockname()[1]
        dead_endpoint = f"http://127.0.0.1:{free_port}"
        push.write_server_descriptor(
            {
                "protocol": push.PROTOCOL,
                "framework": "opencode",
                "serve_id": "serve-dead",
                "endpoint": dead_endpoint,
                "pid": os.getpid(),
                "managed": True,
                "credential_kind": "inline_password",
                "password": "c" * 32,
            }
        )
        push.write_session_descriptor(
            {
                "protocol": push.PROTOCOL,
                "framework": "opencode",
                "session_id": dead_session,
                "endpoint": dead_endpoint,
                "pid": os.getpid(),
                "serve_id": "serve-dead",
                "managed": True,
                "credential_kind": "server_descriptor",
            }
        )
        with self.assertRaises(DeliveryFailure) as caught:
            push.post_prompt(
                "opencode", dead_session, "anyone?", message_id="msg_dead", resume=False
            )
        self.assertIn("outcome unknown", str(caught.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
