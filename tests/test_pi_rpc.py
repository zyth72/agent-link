"""Pi RPC controller tests (docs/PI-RPC.md).

Everything runs against a fake ``pi --mode rpc`` script that simulates the
protocol from environment variables, or against a real local Pi executable for
the no-model get_state/stop check.  No model is ever called and no message
leaves the machine.
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
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from agent_link.errors import ConflictError, DeliveryFailure, NotFoundError, ValidationError
from agent_link.hosts.pi import rpc as pi_rpc
from agent_link.hosts.pi.child import PiChildAdapter
from agent_link.shared import inbox_path
from agent_link.storage import Store
from tests import isolate_registry

REPO_ROOT = Path(__file__).resolve().parents[1]
FAKE_PI = REPO_ROOT / "tests" / "pi" / "fake_rpc_pi.py"
EXTENSION = REPO_ROOT / "extensions" / "pi" / "agent-link.ts"
CORE_CLI = [sys.executable, "-m", "agent_link"]
RPC_CLI = [sys.executable, "-m", "agent_link.hosts.pi.rpc"]


def _find_pi() -> str | None:
    search_path = os.environ.get("PATH", "") + os.pathsep + str(Path.home() / ".npm-global" / "bin")
    return shutil.which("pi", path=search_path)


PI_EXECUTABLE = _find_pi()


class PiRpcCase(unittest.TestCase):
    binding_id = "pi-b"
    child_session = "pi-child-session"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        isolate_registry(self)
        self.base = Path(self.temp.name)
        self.root = self.base / "root"
        self.credentials = self.base / "credentials.json"
        self.credentials.write_text(
            json.dumps(
                {
                    "protocol": "agent-link/1",
                    "endpoint": "http://127.0.0.1:9",
                    "token": "c" * 32,
                    "binding_id": self.binding_id,
                }
            ),
            encoding="utf-8",
        )
        notifier = self.base / "stub_notifier.py"
        notifier.write_text("def bridge(payload):\n    return {'accepted': True}\n", encoding="utf-8")
        self.notifier = notifier
        store = Store(self.root)
        try:
            self._bind(store, self.binding_id, "pi", self.child_session)
        finally:
            store.close()
        self.fake_log = self.base / "fake-pi.log"
        self.argv_log = self.base / "fake-pi-argv.json"
        self.services: list = []
        self.processes: list[subprocess.Popen] = []
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for process in list(self.processes):
            if process.poll() is None:
                process.kill()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        self.processes.clear()
        for service in reversed(self.services):
            service.stop()
        self.services.clear()

    def _bind(self, store, binding_id, child_framework, child_session):
        store.bind(
            {
                "binding_id": binding_id,
                "run_id": "run-" + binding_id,
                "native_task_id": None,
                "parent": {"framework": "codex-desktop", "session_id": "parent-session"},
                "child": {"framework": child_framework, "session_id": child_session},
                "notifier_path": str(self.notifier),
            }
        )

    def binding(self, binding_id=None):
        store = Store(self.root)
        try:
            return store.get_binding(binding_id or self.binding_id)
        finally:
            store.close()

    def start_service(self, scenario="normal", *, fake_session=None, binding_id=None,
                      child_session=None, rpc_timeout=5.0, stop_grace=3.0):
        env = {
            "FAKE_PI_LOG": str(self.fake_log),
            "FAKE_PI_ARGV_LOG": str(self.argv_log),
            "FAKE_PI_SCENARIO": scenario,
            "FAKE_PI_SESSION_ID": fake_session or child_session or self.child_session,
        }
        service = pi_rpc.PiRpcService(
            self.root,
            binding_id or self.binding_id,
            pi_bin=str(FAKE_PI),
            cwd=str(self.base),
            credentials=str(self.credentials),
            extension=str(EXTENSION),
            rpc_timeout=rpc_timeout,
            stop_grace=stop_grace,
        )
        self.services.append(service)
        with mock.patch.dict(os.environ, env, clear=False):
            ready = service.start()
        return service, ready

    def descriptor(self, binding_id=None) -> dict:
        return json.loads(
            pi_rpc.descriptor_path(self.root, binding_id or self.binding_id).read_text("utf-8")
        )

    def fake_commands(self) -> list[dict]:
        if not self.fake_log.exists():
            return []
        return [
            json.loads(line)
            for line in self.fake_log.read_text("utf-8").splitlines()
            if line.strip()
        ]

    def wait_for(self, predicate, timeout=10.0, message="condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail(f"timed out waiting for {message}")

    def run_core(self, *args, timeout=30, env=None):
        command_env = dict(os.environ)
        command_env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + command_env.get("PYTHONPATH", "")
        if env:
            command_env.update(env)
        return subprocess.run(
            CORE_CLI + ["--root", str(self.root), *[str(arg) for arg in args]],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            env=command_env,
            timeout=timeout,
        )

    def popen_core(self, *args):
        command_env = dict(os.environ)
        command_env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + command_env.get("PYTHONPATH", "")
        process = subprocess.Popen(
            CORE_CLI + ["--root", str(self.root), *[str(arg) for arg in args]],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(REPO_ROOT),
            env=command_env,
        )
        self.processes.append(process)
        return process

    @staticmethod
    def http_post(endpoint, path, body, token, method="POST"):
        request = urllib.request.Request(
            endpoint + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.load(error)
            finally:
                error.close()

    # -- handshake / descriptor ---------------------------------------
    def test_ready_requires_real_get_state_and_matching_session(self):
        service = pi_rpc.PiRpcService(
            self.root,
            self.binding_id,
            pi_bin=str(FAKE_PI),
            cwd=str(self.base),
            credentials=str(self.credentials),
            extension=str(EXTENSION),
            rpc_timeout=5.0,
        )
        self.services.append(service)
        with mock.patch.dict(os.environ, {"FAKE_PI_SESSION_ID": "someone-else"}, clear=False):
            with self.assertRaises(pi_rpc.RpcProtocolError):
                service.start()
        self.assertFalse(pi_rpc.descriptor_path(self.root, self.binding_id).exists())
        self.assertIsNotNone(service.controller.process.poll(), "the mismatched Pi must be stopped")

    def test_descriptor_ready_status_and_argv(self):
        service, ready = self.start_service()
        self.assertTrue(ready["ready"])
        self.assertEqual(ready["pi_session_id"], self.child_session)
        self.assertNotIn("token", ready, "the ready line never prints the token")
        path = pi_rpc.descriptor_path(self.root, self.binding_id)
        mode = path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)
        descriptor = self.descriptor()
        self.assertEqual(descriptor["state"], "ready")
        self.assertEqual(descriptor["protocol"], pi_rpc.PROTOCOL)
        self.assertEqual(descriptor["binding_id"], self.binding_id)
        self.assertEqual(descriptor["child_session_id"], self.child_session)
        self.assertTrue(descriptor["endpoint"].startswith("http://127.0.0.1:"))
        self.assertGreaterEqual(len(descriptor["token"]), 16)
        binding = self.binding()
        validated = pi_rpc.validate_descriptor(descriptor, binding)
        status = pi_rpc.control_request(validated, "status", {})
        self.assertEqual(status["sessionId"], self.child_session)
        self.assertTrue(status["alive"])
        argv_info = json.loads(self.argv_log.read_text("utf-8"))
        argv = argv_info["argv"]
        self.assertEqual(argv[0], str(FAKE_PI))
        for flag in ("--mode", "rpc", "--session-id", "--extension", "--no-extensions",
                     "--no-skills", "--no-context-files"):
            self.assertIn(flag, argv)
        self.assertIn(self.child_session, argv)
        self.assertEqual(argv_info["env"]["AGENT_LINK_CREDENTIALS"], str(self.credentials.resolve()))
        self.assertIsNone(argv_info["env"]["AGENT_LINK_BRIDGE_FILE"])
        self.assertIsNone(argv_info["env"]["AGENT_LINK_BINDING"])

    def test_duplicate_serve_does_not_overwrite(self):
        service_a, _ = self.start_service()
        before = pi_rpc.descriptor_path(self.root, self.binding_id).read_bytes()
        service_b = pi_rpc.PiRpcService(
            self.root,
            self.binding_id,
            pi_bin=str(FAKE_PI),
            cwd=str(self.base),
            credentials=str(self.credentials),
            extension=str(EXTENSION),
        )
        with mock.patch.dict(os.environ, {"FAKE_PI_SESSION_ID": self.child_session}, clear=False):
            with self.assertRaises(ConflictError):
                service_b.start()
        self.assertEqual(pi_rpc.descriptor_path(self.root, self.binding_id).read_bytes(), before)
        self.assertTrue(service_a.controller.process.poll() is None)

    def test_cleanup_deletes_only_its_own_descriptor(self):
        service, _ = self.start_service()
        path = pi_rpc.descriptor_path(self.root, self.binding_id)
        foreign = dict(self.descriptor(), token="f" * 32, pid=1)
        path.write_text(json.dumps(foreign), encoding="utf-8")
        service.stop()
        self.assertTrue(path.exists(), "a descriptor replaced by someone else must survive")
        path.unlink()

    def test_client_rejects_old_or_mismatched_descriptors(self):
        binding = self.binding()
        self.assertIsNone(pi_rpc.descriptor_for_binding(self.root, binding))
        path = pi_rpc.descriptor_path(self.root, self.binding_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        good = {
            "protocol": pi_rpc.PROTOCOL,
            "state": "ready",
            "binding_id": self.binding_id,
            "run_id": binding["run_id"],
            "child_session_id": self.child_session,
            "endpoint": "http://127.0.0.1:12345",
            "token": "t" * 32,
        }
        cases = (
            dict(good, protocol="agent-link-pi-rpc/0"),
            dict(good, binding_id="other"),
            dict(good, run_id="other"),
            dict(good, child_session_id="other"),
            dict(good, state="starting"),
            dict(good, endpoint="http://192.0.2.10:12345"),
            dict(good, endpoint="http://localhost:12345"),
            dict(good, token="short"),
        )
        for case in cases:
            with self.subTest(case=case):
                path.write_text(json.dumps(case), encoding="utf-8")
                with self.assertRaises(DeliveryFailure):
                    pi_rpc.descriptor_for_binding(self.root, binding)
        # A bad descriptor must never silently fall back to the inbox.
        path.write_text(json.dumps(dict(good, protocol="agent-link-pi-rpc/0")), encoding="utf-8")
        message = {
            "kind": "ask_child",
            "request_id": "rq-old",
            "message_id": "m-old",
            "question": "?",
            "timeout_seconds": 5.0,
            "deadline_epoch": time.time() + 5,
        }
        with self.assertRaises(DeliveryFailure):
            PiChildAdapter().deliver(self.root, binding, message)
        self.assertFalse(inbox_path(self.root, self.binding_id).exists())
        path.unlink()

    # -- protocol framing ---------------------------------------------
    def test_out_of_order_ids_events_and_u2028(self):
        service, _ = self.start_service("out_of_order")
        controller = service.controller
        results = {}

        def call(name, command):
            try:
                results[name] = controller.request(command)
            except Exception as error:  # noqa: BLE001 - surfaced in the assertion
                results[name] = error

        threads = [
            threading.Thread(target=call, args=("state", {"type": "get_state"})),
            threading.Thread(target=call, args=("messages", {"type": "get_messages"})),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertIsInstance(results["state"], dict, results)
        self.assertIsInstance(results["messages"], dict, results)
        orders = {results["state"]["data"]["order"], results["messages"]["data"]["order"]}
        self.assertEqual(orders, {"first", "second"})
        self.assertEqual(results["state"]["data"]["sessionId"], self.child_session)
        self.assertEqual(controller.unmatched_responses(), [])
        events = controller.events()
        deltas = [
            event["assistantMessageEvent"]["delta"]
            for event in events
            if event.get("type") == "message_update"
        ]
        self.assertIn("async\u2028event", deltas)

    def test_async_event_with_same_id_is_not_a_response(self):
        service, _ = self.start_service("event_same_id")
        started = time.monotonic()
        response = service.controller.request({"type": "get_messages"})
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.1, "the event must not resolve the pending request")
        self.assertEqual(response["data"]["messages"][0]["content"], "fake\u2028message")
        self.assertTrue(
            any(event.get("type") == "message_update" for event in service.controller.events())
        )

    def test_lf_only_framing_keeps_u2028_inside_strings(self):
        service, _ = self.start_service("u2028")
        controller = service.controller
        status = controller.status()
        self.assertIn("\u2028", status["sessionName"])
        messages = controller.request({"type": "get_messages"})
        self.assertIn("\u2028", messages["data"]["messages"][0]["content"])
        events = controller.events()
        self.assertTrue(
            any("\u2028" in json.dumps(event, ensure_ascii=False) for event in events)
        )

    def test_command_false_timeout_no_resend_and_exit(self):
        service, _ = self.start_service("false")
        from agent_link.hosts.pi.rpc import RpcCommandFailure

        with self.assertRaises(RpcCommandFailure):
            service.controller.prompt("hello")
        service.stop()

        service, _ = self.start_service("no_response")
        before = len(self.fake_commands())
        with self.assertRaises(pi_rpc.RpcTimeout):
            service.controller.prompt("hello", timeout=0.3)
        prompts = [
            cmd
            for cmd in self.fake_commands()[before:]
            if cmd.get("type") == "prompt"
        ]
        self.assertEqual(len(prompts), 1, "a timeout must never auto-resend")
        self.assertEqual(prompts[0]["streamingBehavior"], "steer")
        service.stop()

        service, _ = self.start_service("exit")
        with self.assertRaises(pi_rpc.RpcExited):
            service.controller.prompt("hello", timeout=5.0)

    def test_bad_and_oversized_frames_fail_explicitly(self):
        for scenario in ("bad_frame", "long_frame"):
            with self.subTest(scenario=scenario):
                self.fake_log.unlink(missing_ok=True)
                service, _ = self.start_service(scenario)
                if service.controller._fatal is None:  # noqa: SLF001 - race with the reader
                    self.wait_for(
                        lambda: service.controller._fatal is not None,  # noqa: SLF001
                        message="protocol failure",
                    )
                with self.assertRaises(pi_rpc.RpcProtocolError):
                    service.controller.status(timeout=2.0)
                self.wait_for(
                    lambda: service.controller.process.poll() is not None,
                    message="pi process termination",
                )
                service.stop()

    # -- control HTTP --------------------------------------------------
    def test_control_auth_fields_methods_and_limits(self):
        service, ready = self.start_service()
        endpoint = ready["endpoint"]
        token = self.descriptor()["token"]
        status, payload = self.http_post(endpoint, "/v1/status", {}, "wrong-token")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"], "unauthorized")
        status, payload = self.http_post(endpoint, "/v1/status", {}, token)
        self.assertEqual(status, 200)
        self.assertEqual(payload["result"]["sessionId"], self.child_session)
        status, payload = self.http_post(endpoint, "/v1/prompt", {"text": "x", "extra": 1}, token)
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"], "validation")
        status, payload = self.http_post(endpoint, "/v1/nope", {}, token)
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "not_found")
        status, payload = self.http_post(endpoint, "/v1/status", None, token, method="GET")
        self.assertEqual(status, 405)
        request = urllib.request.Request(
            endpoint + "/v1/prompt",
            data=b"x" * (pi_rpc.MAX_BODY_BYTES + 1),
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            method="POST",
        )
        try:
            urllib.request.urlopen(request, timeout=10)
            self.fail("oversized body must be rejected")
        except urllib.error.HTTPError as error:
            self.assertEqual(error.code, 400)
            error.close()

    def test_client_ignores_proxy_env_and_rejects_redirects(self):
        with mock.patch.dict(
            os.environ,
            {
                "HTTP_PROXY": "http://127.0.0.1:1",
                "HTTPS_PROXY": "http://127.0.0.1:1",
                "ALL_PROXY": "http://127.0.0.1:1",
                "NO_PROXY": "",
            },
            clear=False,
        ):
            service, _ = self.start_service()
            descriptor = self.descriptor()
            status = pi_rpc.control_request(descriptor, "status", {})
            self.assertEqual(status["sessionId"], self.child_session)

        class Redirect(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):  # noqa: N802
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:9/evil")
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            fake_descriptor = {
                "protocol": pi_rpc.PROTOCOL,
                "state": "ready",
                "endpoint": f"http://127.0.0.1:{server.server_port}",
                "token": "t" * 32,
            }
            with self.assertRaises(DeliveryFailure) as caught:
                pi_rpc.control_request(fake_descriptor, "status", {}, timeout=5.0)
            self.assertIn("302", str(caught.exception))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_abort_clears_queue_before_aborting(self):
        service, _ = self.start_service()
        descriptor = self.descriptor()
        result = pi_rpc.control_request(descriptor, "abort", {})
        self.assertEqual(result["cleared"]["steering"], ["queued-steer"])
        self.assertEqual(result["cleared"]["followUp"], ["queued-follow"])
        commands = [cmd["type"] for cmd in self.fake_commands()]
        self.assertLess(commands.index("clear_queue"), commands.index("abort"))

    # -- CLI -----------------------------------------------------------
    def test_send_cli_modes_and_status_stop(self):
        service, _ = self.start_service()
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")

        def run_rpc(*args, timeout=30):
            return subprocess.run(
                RPC_CLI + ["--root", str(self.root), "--binding", self.binding_id, *args],
                capture_output=True,
                text=True,
                cwd=str(REPO_ROOT),
                env=env,
                timeout=timeout,
            )

        for mode in ("prompt", "steer", "follow-up"):
            proc = run_rpc("send", "--mode", mode, "--text", f"hello-{mode}")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertTrue(json.loads(proc.stdout)["accepted"])
        prompt = [cmd for cmd in self.fake_commands() if cmd.get("type") == "prompt"]
        self.assertEqual(prompt[-1]["streamingBehavior"], "steer")
        self.assertEqual(prompt[-1]["message"], "hello-prompt")
        steer = [cmd for cmd in self.fake_commands() if cmd.get("type") == "steer"]
        self.assertEqual(steer[-1]["message"], "hello-steer")
        follow = [cmd for cmd in self.fake_commands() if cmd.get("type") == "follow_up"]
        self.assertEqual(follow[-1]["message"], "hello-follow-up")

        text_file = self.base / "long text.txt"
        text_file.write_text("from-file", encoding="utf-8")
        proc = run_rpc("send", "--text-file", str(text_file))
        self.assertEqual(proc.returncode, 0, proc.stderr)

        status = run_rpc("status")
        self.assertEqual(status.returncode, 0, status.stderr)
        status_payload = json.loads(status.stdout)
        self.assertEqual(status_payload["controller"], "ready")
        self.assertEqual(status_payload["status"]["sessionId"], self.child_session)

        stop = run_rpc("stop")
        self.assertEqual(stop.returncode, 0, stop.stderr)
        self.assertTrue(json.loads(stop.stdout)["stopped"])
        self.wait_for(lambda: service.stop_event.is_set(), message="controller stop event")

    def test_serve_cli_sigterm_leaves_no_orphan(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        env.update(
            {
                "FAKE_PI_LOG": str(self.fake_log),
                "FAKE_PI_SCENARIO": "normal",
                "FAKE_PI_SESSION_ID": self.child_session,
            }
        )
        process = subprocess.Popen(
            RPC_CLI
            + [
                "--root", str(self.root),
                "--binding", self.binding_id,
                "serve",
                "--pi-bin", str(FAKE_PI),
                "--cwd", str(self.base),
                "--credentials", str(self.credentials),
                "--extension", str(EXTENSION),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(REPO_ROOT),
            env=env,
        )
        self.processes.append(process)
        ready_line = process.stdout.readline()
        self.assertTrue(ready_line, "controller exited without a ready response")
        ready = json.loads(ready_line)
        self.assertTrue(ready["ready"])
        descriptor = json.loads(
            pi_rpc.descriptor_path(self.root, self.binding_id).read_text("utf-8")
        )
        pi_pid = descriptor["pi_pid"]
        process.send_signal(signal.SIGTERM)
        self.assertEqual(process.wait(timeout=20), 0)
        self.assertFalse(pi_rpc.descriptor_path(self.root, self.binding_id).exists())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                os.kill(pi_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            self.fail("the dedicated Pi process survived the controller SIGTERM")

    # -- ask-child integration -----------------------------------------
    def _ask_child(self, request_id, message_id, question, timeout="20"):
        return self.popen_core(
            "ask-child",
            "--binding",
            self.binding_id,
            "--request-id",
            request_id,
            "--message-id",
            message_id,
            "--question",
            question,
            "--timeout",
            timeout,
        )

    def test_ask_child_uses_rpc_without_inbox_and_answer_is_idempotent(self):
        service, _ = self.start_service()
        asker = self._ask_child("rq-rpc", "pm-rpc", "what now?")
        self.wait_for(
            lambda: any(cmd.get("type") == "prompt" for cmd in self.fake_commands()),
            message="rpc prompt",
        )
        self.assertFalse(
            inbox_path(self.root, self.binding_id).exists(),
            "a valid descriptor must not produce an inbox event",
        )
        prompt = [cmd for cmd in self.fake_commands() if cmd.get("type") == "prompt"][-1]
        self.assertEqual(prompt["streamingBehavior"], "steer")
        self.assertIn("[agent-link:parent-ask]", prompt["message"])
        self.assertIn("request_id: rq-rpc", prompt["message"])

        answered = self.run_core(
            "answer", "--binding", self.binding_id, "--request-id", "rq-rpc",
            "--message-id", "am-rpc", "--answer", "child says yes",
        )
        self.assertEqual(answered.returncode, 0, answered.stderr)
        out, err = asker.communicate(timeout=30)
        self.assertEqual(asker.returncode, 0, err)
        result = json.loads(out)
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["answer"], "child says yes")
        self.assertEqual(result["delivery_state"], "host_accepted")

        prompts_before = len([cmd for cmd in self.fake_commands() if cmd.get("type") == "prompt"])
        replay = self.run_core(
            "ask-child", "--binding", self.binding_id, "--request-id", "rq-rpc",
            "--message-id", "pm-rpc", "--question", "what now?", "--timeout", "20",
        )
        self.assertEqual(replay.returncode, 0, replay.stderr)
        replay_payload = json.loads(replay.stdout)
        self.assertTrue(replay_payload["idempotent"])
        self.assertEqual(replay_payload["status"], "answered")
        prompts_after = len([cmd for cmd in self.fake_commands() if cmd.get("type") == "prompt"])
        self.assertEqual(prompts_before, prompts_after, "a replay must not re-prompt Pi")

    def test_ask_child_without_rpc_descriptor_uses_session_push(self):
        # With no pi-rpc controller descriptor the Pi child adapter falls back
        # to the interactive session's push listener; it never writes an inbox.
        from tests.push_server import MockPushListener, write_descriptor

        listener = MockPushListener("pi", self.child_session)
        self.addCleanup(listener.close)
        sessions = self.base / "sessions"
        previous = os.environ.get("AGENT_LINK_SESSIONS")
        os.environ["AGENT_LINK_SESSIONS"] = str(sessions)

        def restore_env():
            os.environ.pop("AGENT_LINK_SESSIONS", None)
            if previous is not None:
                os.environ["AGENT_LINK_SESSIONS"] = previous

        self.addCleanup(restore_env)
        write_descriptor(sessions, listener.descriptor())
        asker = self._ask_child("rq-push", "pm-push", "push?")
        self.wait_for(lambda: len(listener.events) == 1, message="child push event")
        event = listener.events[0]
        self.assertEqual(event["binding_id"], self.binding_id)
        self.assertEqual(event["role"], "child")
        self.assertEqual(event["framework"], "pi")
        self.assertEqual(event["session_id"], self.child_session)
        self.assertIn("[agent-link:parent-ask]", event["prompt"])
        self.assertIn("request_id: rq-push", event["prompt"])
        self.assertFalse(
            inbox_path(self.root, self.binding_id).exists(),
            "the push fallback must not write an inbox file",
        )
        answered = self.run_core(
            "answer", "--binding", self.binding_id, "--request-id", "rq-push",
            "--message-id", "am-push", "--answer", "push answer",
        )
        self.assertEqual(answered.returncode, 0, answered.stderr)
        out, err = asker.communicate(timeout=30)
        self.assertEqual(asker.returncode, 0, err)
        self.assertEqual(json.loads(out)["status"], "answered")

    def test_ask_child_with_old_descriptor_fails_without_inbox_fallback(self):
        path = pi_rpc.descriptor_path(self.root, self.binding_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        binding = self.binding()
        path.write_text(
            json.dumps(
                {
                    "protocol": "agent-link-pi-rpc/0",
                    "state": "ready",
                    "binding_id": self.binding_id,
                    "run_id": binding["run_id"],
                    "child_session_id": self.child_session,
                    "endpoint": "http://127.0.0.1:12345",
                    "token": "t" * 32,
                }
            ),
            encoding="utf-8",
        )
        asker = self._ask_child("rq-old", "pm-old", "old?")
        out, err = asker.communicate(timeout=30)
        self.assertEqual(asker.returncode, 3, out + err)
        result = json.loads(out)
        self.assertEqual(result["delivery_state"], "failed_or_unknown")
        self.assertEqual(result["status"], "timed_out")
        store = Store(self.root)
        try:
            row = store.get_parent_request(self.binding_id, "rq-old")
        finally:
            store.close()
        self.assertEqual(row["delivery_state"], "failed_or_unknown")
        self.assertIn("protocol", row["delivery_detail"])
        self.assertFalse(inbox_path(self.root, self.binding_id).exists())

    def test_ask_child_other_framework_still_uses_inbox(self):
        # OpenCode is the remaining legacy inbox child; OMP now uses the
        # session-owned push listener instead of an inbox file.
        store = Store(self.root)
        try:
            self._bind(store, "oc-b", "opencode", "oc-child")
        finally:
            store.close()
        asker = subprocess.Popen(
            CORE_CLI
            + [
                "--root", str(self.root), "ask-child", "--binding", "oc-b",
                "--request-id", "rq-oc", "--message-id", "pm-oc",
                "--question", "oc?", "--timeout", "20",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(REPO_ROOT),
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        )
        self.processes.append(asker)
        inbox = inbox_path(self.root, "oc-b")
        self.wait_for(lambda: inbox.exists(), message="opencode inbox event")
        event = json.loads(inbox.read_text("utf-8").splitlines()[0])
        self.assertEqual(event["kind"], "ask_child")
        self.assertEqual(event["child_framework"], "opencode")
        answered = self.run_core(
            "answer", "--binding", "oc-b", "--request-id", "rq-oc",
            "--message-id", "am-oc", "--answer", "ok",
        )
        self.assertEqual(answered.returncode, 0, answered.stderr)
        out, err = asker.communicate(timeout=30)
        self.assertEqual(asker.returncode, 0, err)
        self.assertEqual(json.loads(out)["status"], "answered")

    # -- real Pi without a model ---------------------------------------
    @unittest.skipUnless(PI_EXECUTABLE, "a local Pi executable is required")
    def test_real_pi_get_state_and_stop_without_model(self):
        session_dir = self.base / "pi-sessions"
        session_dir.mkdir()
        service = pi_rpc.PiRpcService(
            self.root,
            self.binding_id,
            pi_bin=PI_EXECUTABLE,
            cwd=str(self.base),
            credentials=str(self.credentials),
            extension=str(EXTENSION),
            rpc_timeout=60.0,
            stop_grace=15.0,
        )
        self.services.append(service)
        with mock.patch.dict(
            os.environ, {"PI_CODING_AGENT_SESSION_DIR": str(session_dir),
                         "AGENT_LINK_SESSIONS": str(self.base / "push-sessions")}, clear=False
        ):
            ready = service.start()
        self.assertEqual(ready["pi_session_id"], self.child_session)
        descriptor = self.descriptor()
        validated = pi_rpc.validate_descriptor(descriptor, self.binding())
        status = pi_rpc.control_request(validated, "status", {}, timeout=30.0)
        self.assertEqual(status["sessionId"], self.child_session)
        service.stop()
        self.assertFalse(pi_rpc.descriptor_path(self.root, self.binding_id).exists())
        self.assertIsNotNone(service.controller.process.poll())


if __name__ == "__main__":
    unittest.main(verbosity=2)
