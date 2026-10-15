"""End-to-end tests for the agent_link CLI (SPEC v0.1 section 9).

All transport calls use a simulated notifier written into the test temporary
directory; the suite never sends a real desktop message.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_link.errors import ConflictError, ValidationError
from agent_link.storage import Store

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = [sys.executable, "-m", "agent_link"]
STUB_SOURCE = '''\
"""Simulated notifier used by tests. Records every bridge() attempt."""
import json
import os
import time
from pathlib import Path


def bridge(payload):
    log = Path(os.environ["STUB_NOTIFIER_LOG"])
    fail = os.environ.get("STUB_NOTIFIER_FAIL") == "1"
    delay = float(os.environ.get("STUB_NOTIFIER_SLEEP", "0") or "0")
    with log.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"payload": payload, "fail": fail}, ensure_ascii=False) + "\\n")
    if delay > 0:
        time.sleep(delay)
    done = os.environ.get("STUB_NOTIFIER_DONE")
    if done:
        Path(done).write_text("done", encoding="utf-8")
    if fail:
        raise RuntimeError("simulated notifier failure")
    return {"accepted": True}
'''


def base_env(extra=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    if extra:
        env.update(extra)
    return env


class CliCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "runtime"
        self.stub = self.tmp / "stub_notifier.py"
        self.stub.write_text(STUB_SOURCE, encoding="utf-8")
        self.log = self.tmp / "notifier-calls.jsonl"
        self.env = base_env({"STUB_NOTIFIER_LOG": str(self.log), "STUB_NOTIFIER_FAIL": "0"})

    # -- helpers -------------------------------------------------------
    def run_cli(self, *args, env=None, timeout=60, root=None):
        return subprocess.run(
            CLI + ["--root", str(root or self.root), *[str(a) for a in args]],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            env=env or self.env,
            timeout=timeout,
        )

    def popen_cli(self, *args, env=None, root=None):
        return subprocess.Popen(
            CLI + ["--root", str(root or self.root), *[str(a) for a in args]],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(REPO_ROOT),
            env=env or self.env,
        )

    @staticmethod
    def payload(proc):
        return json.loads(proc.stdout)

    def write_config(self, binding_id="bind-a", run_id="run-a", notifier=None, config_overrides=None):
        config = {
            "binding_id": binding_id,
            "run_id": run_id,
            "native_task_id": None,
            "parent": {"framework": "codex-desktop", "session_id": "parent-session-1"},
            "child": {"framework": "pi", "session_id": "child-session-1"},
            "notifier_path": str(notifier or self.stub),
        }
        if config_overrides:
            config.update(config_overrides)
        path = self.tmp / f"{binding_id}-{run_id}.json"
        path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        return path, config

    def bind(self, binding_id="bind-a", run_id="run-a", **kwargs):
        path, config = self.write_config(binding_id, run_id, **kwargs)
        proc = self.run_cli("bind", "--config", path)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc, config

    def call_log(self):
        if not self.log.exists():
            return []
        lines = self.log.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def wait_for(self, predicate, timeout=15.0, interval=0.05, message="condition"):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(interval)
        self.fail(f"timed out waiting for {message}")

    def wait_for_request(self, binding_id, request_id, timeout=15.0):
        def ready():
            proc = self.run_cli("get-request", "--binding", binding_id, "--request-id", request_id)
            return proc.returncode == 0

        self.wait_for(ready, timeout=timeout, message=f"request {request_id}")

    # -- tests ---------------------------------------------------------
    def test_bind_idempotent_conflict_and_no_redirect(self):
        proc, _ = self.bind()
        first = self.payload(proc)
        self.assertTrue(first["created"])
        self.assertFalse(first["idempotent"])
        replay = self.run_cli("bind", "--config", self.tmp / "bind-a-run-a.json")
        self.assertEqual(replay.returncode, 0)
        self.assertTrue(self.payload(replay)["idempotent"])
        conflict_path, _ = self.write_config("bind-a", "run-b")
        conflict = self.run_cli("bind", "--config", conflict_path)
        self.assertEqual(conflict.returncode, 4, conflict.stdout + conflict.stderr)
        self.assertEqual(self.payload(conflict)["error"], "conflict")
        # original binding survived and was not redirected
        missing = self.run_cli("get-request", "--binding", "bind-a", "--request-id", "missing")
        self.assertEqual(missing.returncode, 6)
        report = self.run_cli("report", "--binding", "bind-a", "--message-id", "m-after", "--text", "ok")
        self.assertEqual(report.returncode, 0, report.stderr)

    def test_bind_rejects_missing_notifier(self):
        path, _ = self.write_config("bind-n", "run-n", notifier=self.tmp / "does-not-exist.py")
        proc = self.run_cli("bind", "--config", path)
        self.assertEqual(proc.returncode, 5)
        self.assertEqual(self.payload(proc)["error"], "validation")

    def test_identifier_validation(self):
        self.bind()
        for bad_id in ("../escape", "has space", "a" * 129, ""):
            proc = self.run_cli(
                "report", "--binding", "bind-a", "--message-id", bad_id, "--text", "x"
            )
            self.assertEqual(proc.returncode, 5, f"expected rejection for {bad_id!r}")
            self.assertEqual(self.payload(proc)["error"], "validation")
        bad_binding = self.run_cli(
            "report", "--binding", "bad/../id", "--message-id", "m-1", "--text", "x"
        )
        self.assertEqual(bad_binding.returncode, 5)
        path, _ = self.write_config("bind-x", "run-x", config_overrides={"run_id": "run/x"})
        proc = self.run_cli("bind", "--config", path)
        self.assertEqual(proc.returncode, 5)

    def test_capabilities_declares_all_fields(self):
        proc = self.run_cli("capabilities", root=self.tmp / "unused-root")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        caps = self.payload(proc)
        for key in (
            "notification_transport",
            "receive_mode",
            "reply_mode",
            "busy_behavior",
            "wake_after_turn",
            "spawn",
            "cancel",
        ):
            self.assertIn(key, caps)
        self.assertEqual(caps["notification_transport"], "desktop_bridge")
        self.assertEqual(caps["receive_mode"], "host_injection")
        self.assertEqual(caps["reply_mode"], "blocking_tool_file_poll")
        self.assertEqual(caps["busy_behavior"], "unknown")
        self.assertEqual(caps["wake_after_turn"], "unknown")
        self.assertFalse(caps["spawn"])
        self.assertFalse(caps["cancel"])

    def test_report_idempotent_same_conflict_different(self):
        self.bind()
        first = self.run_cli("report", "--binding", "bind-a", "--message-id", "m-1", "--text", "hello")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(self.payload(first)["delivery_state"], "host_accepted")
        self.assertEqual(len(self.call_log()), 1)
        replay = self.run_cli("report", "--binding", "bind-a", "--message-id", "m-1", "--text", "hello")
        self.assertEqual(replay.returncode, 0)
        self.assertTrue(self.payload(replay)["idempotent"])
        self.assertEqual(len(self.call_log()), 1, "replay must not redeliver")
        conflict = self.run_cli("report", "--binding", "bind-a", "--message-id", "m-1", "--text", "changed")
        self.assertEqual(conflict.returncode, 4)
        self.assertEqual(len(self.call_log()), 1)

    def test_ask_waits_and_receives_reply(self):
        self.bind()
        ask = self.popen_cli(
            "ask", "--binding", "bind-a", "--request-id", "req-1", "--message-id", "q-1",
            "--question", "pick one", "--timeout", "20",
        )
        self.wait_for_request("bind-a", "req-1")
        self.wait_for(lambda: len(self.call_log()) == 1, message="ask dispatch")
        reply = self.run_cli(
            "reply", "--binding", "bind-a", "--request-id", "req-1",
            "--message-id", "a-1", "--answer", "answer-abc",
        )
        self.assertEqual(reply.returncode, 0, reply.stderr)
        reply_payload = self.payload(reply)
        self.assertEqual(reply_payload["status"], "replied")
        self.assertFalse(reply_payload["late"])
        out, err = ask.communicate(timeout=30)
        self.assertEqual(ask.returncode, 0, err)
        result = json.loads(out)
        self.assertEqual(result["request_id"], "req-1")
        self.assertEqual(result["answer"], "answer-abc")
        self.assertFalse(result["late"])
        self.assertEqual(result["status"], "replied")
        self.assertEqual(len(self.call_log()), 1)

    def test_slow_notifier_does_not_shorten_real_deadline(self):
        """Notification time is consumed inside the persisted deadline and must
        not be subtracted from it a second time (REVIEW-FIXES item 1)."""
        self.bind()
        done_marker = self.tmp / "slow-notifier-done"
        slow_env = base_env(
            {
                "STUB_NOTIFIER_LOG": str(self.log),
                "STUB_NOTIFIER_FAIL": "0",
                "STUB_NOTIFIER_SLEEP": "1.0",
                "STUB_NOTIFIER_DONE": str(done_marker),
            }
        )
        started = time.monotonic()
        ask = self.popen_cli(
            "ask", "--binding", "bind-a", "--request-id", "req-slow", "--message-id", "q-slow",
            "--question", "q", "--timeout", "3.0", env=slow_env,
        )
        self.wait_for_request("bind-a", "req-slow")
        self.wait_for(lambda: len(self.call_log()) == 1, message="slow ask dispatch")
        # The done marker is written only after the notifier's 1.0s sleep, so
        # its presence proves the notification really was slow.
        self.wait_for(done_marker.exists, message="slow notifier to finish sleeping")
        # The old start_mono + (deadline - time.time()) calculation expired at
        # about started+2.0s; the persisted deadline is about started+3.1s.
        target = started + 2.5
        delay = target - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self.assertIsNone(ask.poll(), "ask exited before the persisted deadline")
        reply = self.run_cli(
            "reply", "--binding", "bind-a", "--request-id", "req-slow",
            "--message-id", "r-slow", "--answer", "on-time-after-slow-notify",
        )
        self.assertEqual(reply.returncode, 0, reply.stderr)
        self.assertFalse(self.payload(reply)["late"])
        out, err = ask.communicate(timeout=15)
        self.assertEqual(ask.returncode, 0, err)
        result = json.loads(out)
        self.assertEqual(result["status"], "replied")
        self.assertFalse(result["late"])
        self.assertEqual(result["answer"], "on-time-after-slow-notify")
        self.assertEqual(len(self.call_log()), 1)

    def test_ask_timeout_change_conflicts(self):
        """timeout is part of ask semantics: same ids with a different timeout
        conflict and the persisted value is preserved (REVIEW-FIXES item 3)."""
        self.bind()
        ask = self.popen_cli(
            "ask", "--binding", "bind-a", "--request-id", "req-to", "--message-id", "q-to",
            "--question", "same question", "--timeout", "20",
        )
        self.wait_for_request("bind-a", "req-to")
        conflict = self.run_cli(
            "ask", "--binding", "bind-a", "--request-id", "req-to", "--message-id", "q-to",
            "--question", "same question", "--timeout", "200",
        )
        self.assertEqual(conflict.returncode, 4, conflict.stdout + conflict.stderr)
        self.assertEqual(self.payload(conflict)["error"], "conflict")
        got = self.payload(
            self.run_cli("get-request", "--binding", "bind-a", "--request-id", "req-to")
        )
        self.assertEqual(got["timeout_seconds"], 20.0)
        self.assertEqual(got["status"], "pending")
        reply = self.run_cli(
            "reply", "--binding", "bind-a", "--request-id", "req-to",
            "--message-id", "r-to", "--answer", "answer-to",
        )
        self.assertEqual(reply.returncode, 0, reply.stderr)
        out, err = ask.communicate(timeout=30)
        self.assertEqual(ask.returncode, 0, err)
        replay = self.run_cli(
            "ask", "--binding", "bind-a", "--request-id", "req-to", "--message-id", "q-to",
            "--question", "same question", "--timeout", "20",
        )
        self.assertEqual(replay.returncode, 0, replay.stderr)
        self.assertTrue(self.payload(replay)["idempotent"])
        self.assertEqual(len(self.call_log()), 1, "replay must not re-dispatch")

    def test_invalid_timeout_rejected(self):
        self.bind()
        for bad in ("0", "-1", "nan", "inf"):
            proc = self.run_cli(
                "ask", "--binding", "bind-a", "--request-id", "req-bad", "--message-id", "q-bad",
                "--question", "q", "--timeout", bad,
            )
            self.assertEqual(proc.returncode, 5, f"timeout={bad}: {proc.stdout}{proc.stderr}")
            self.assertEqual(self.payload(proc)["error"], "validation")
        self.assertEqual(len(self.call_log()), 0)

    def test_reply_message_id_cannot_answer_two_requests(self):
        """A reply message_id is scoped to its target request_id; reusing it for
        another request conflicts even when the answer text matches
        (REVIEW-FIXES item 2)."""
        self.bind()
        ask1 = self.popen_cli(
            "ask", "--binding", "bind-a", "--request-id", "req-sc-1", "--message-id", "q-sc-1",
            "--question", "q1", "--timeout", "30",
        )
        self.wait_for_request("bind-a", "req-sc-1")
        ask2 = self.popen_cli(
            "ask", "--binding", "bind-a", "--request-id", "req-sc-2", "--message-id", "q-sc-2",
            "--question", "q2", "--timeout", "30",
        )
        self.wait_for_request("bind-a", "req-sc-2")
        first = self.run_cli(
            "reply", "--binding", "bind-a", "--request-id", "req-sc-1",
            "--message-id", "shared-reply", "--answer", "yes",
        )
        self.assertEqual(first.returncode, 0, first.stderr)
        cross = self.run_cli(
            "reply", "--binding", "bind-a", "--request-id", "req-sc-2",
            "--message-id", "shared-reply", "--answer", "yes",
        )
        self.assertEqual(cross.returncode, 4, cross.stdout + cross.stderr)
        self.assertEqual(self.payload(cross)["error"], "conflict")
        got = self.payload(
            self.run_cli("get-request", "--binding", "bind-a", "--request-id", "req-sc-2")
        )
        self.assertEqual(got["status"], "pending")
        self.assertIsNone(got["answer"])
        cleanup = self.run_cli(
            "reply", "--binding", "bind-a", "--request-id", "req-sc-2",
            "--message-id", "shared-reply-2", "--answer", "yes",
        )
        self.assertEqual(cleanup.returncode, 0, cleanup.stderr)
        for ask in (ask1, ask2):
            out, err = ask.communicate(timeout=30)
            self.assertEqual(ask.returncode, 0, err)

    def test_concurrent_same_reply_message_two_requests_only_one_wins(self):
        """Two independent connections reusing one reply message_id for two
        pending requests: exactly one commits, the other conflicts."""
        self.bind()
        ask1 = self.popen_cli(
            "ask", "--binding", "bind-a", "--request-id", "req-cc-1", "--message-id", "q-cc-1",
            "--question", "q1", "--timeout", "30",
        )
        self.wait_for_request("bind-a", "req-cc-1")
        ask2 = self.popen_cli(
            "ask", "--binding", "bind-a", "--request-id", "req-cc-2", "--message-id", "q-cc-2",
            "--question", "q2", "--timeout", "30",
        )
        self.wait_for_request("bind-a", "req-cc-2")
        first = self.popen_cli(
            "reply", "--binding", "bind-a", "--request-id", "req-cc-1",
            "--message-id", "cc-shared", "--answer", "yes",
        )
        second = self.popen_cli(
            "reply", "--binding", "bind-a", "--request-id", "req-cc-2",
            "--message-id", "cc-shared", "--answer", "yes",
        )
        out1, err1 = first.communicate(timeout=30)
        out2, err2 = second.communicate(timeout=30)
        self.assertEqual(
            sorted([first.returncode, second.returncode]), [0, 4], (out1, err1, out2, err2)
        )
        got1 = self.payload(
            self.run_cli("get-request", "--binding", "bind-a", "--request-id", "req-cc-1")
        )
        got2 = self.payload(
            self.run_cli("get-request", "--binding", "bind-a", "--request-id", "req-cc-2")
        )
        self.assertEqual(sorted([got1["status"], got2["status"]]), ["pending", "replied"])
        pending = "req-cc-1" if got1["status"] == "pending" else "req-cc-2"
        cleanup = self.run_cli(
            "reply", "--binding", "bind-a", "--request-id", pending,
            "--message-id", pending + "-reply", "--answer", "yes",
        )
        self.assertEqual(cleanup.returncode, 0, cleanup.stderr)
        for ask in (ask1, ask2):
            out, err = ask.communicate(timeout=30)
            self.assertEqual(ask.returncode, 0, err)

    def test_duplicate_reply_cannot_overwrite(self):
        self.bind()
        ask = self.popen_cli(
            "ask", "--binding", "bind-a", "--request-id", "req-dup", "--message-id", "q-dup",
            "--question", "q", "--timeout", "20",
        )
        self.wait_for_request("bind-a", "req-dup")
        self.assertEqual(
            self.run_cli(
                "reply", "--binding", "bind-a", "--request-id", "req-dup",
                "--message-id", "r-dup-1", "--answer", "first",
            ).returncode,
            0,
        )
        overwrite = self.run_cli(
            "reply", "--binding", "bind-a", "--request-id", "req-dup",
            "--message-id", "r-dup-2", "--answer", "second",
        )
        self.assertEqual(overwrite.returncode, 4)
        same = self.run_cli(
            "reply", "--binding", "bind-a", "--request-id", "req-dup",
            "--message-id", "r-dup-1", "--answer", "first",
        )
        self.assertEqual(same.returncode, 0)
        self.assertTrue(self.payload(same)["idempotent"])
        ask.communicate(timeout=30)
        got = self.payload(
            self.run_cli("get-request", "--binding", "bind-a", "--request-id", "req-dup")
        )
        self.assertEqual(got["answer"], "first")
        self.assertEqual(got["status"], "replied")

    def test_unknown_binding_and_request(self):
        self.bind()
        for binding, request in (("bind-a", "nope"), ("missing", "nope")):
            proc = self.run_cli("get-request", "--binding", binding, "--request-id", request)
            self.assertEqual(proc.returncode, 6)
            self.assertEqual(self.payload(proc)["error"], "not_found")
        reply = self.run_cli(
            "reply", "--binding", "bind-a", "--request-id", "nope",
            "--message-id", "r-x", "--answer", "x",
        )
        self.assertEqual(reply.returncode, 6)

    def test_timeout_then_late_reply_is_stored(self):
        self.bind()
        ask = self.popen_cli(
            "ask", "--binding", "bind-a", "--request-id", "req-t", "--message-id", "q-t",
            "--question", "q", "--timeout", "1",
        )
        self.wait_for_request("bind-a", "req-t")
        out, err = ask.communicate(timeout=15)
        self.assertEqual(ask.returncode, 3, err)
        result = json.loads(out)
        self.assertEqual(result["status"], "timed_out")
        self.assertIsNone(result["answer"])
        self.assertFalse(result["late"])
        late = self.run_cli(
            "reply", "--binding", "bind-a", "--request-id", "req-t",
            "--message-id", "r-late-1", "--answer", "late-answer",
        )
        self.assertEqual(late.returncode, 0, late.stderr)
        late_payload = self.payload(late)
        self.assertEqual(late_payload["status"], "timed_out")
        self.assertTrue(late_payload["late"])
        got = self.payload(self.run_cli("get-request", "--binding", "bind-a", "--request-id", "req-t"))
        self.assertEqual(got["status"], "timed_out")
        self.assertTrue(got["late"])
        self.assertEqual(got["answer"], "late-answer")
        second_late = self.run_cli(
            "reply", "--binding", "bind-a", "--request-id", "req-t",
            "--message-id", "r-late-2", "--answer", "other",
        )
        self.assertEqual(second_late.returncode, 4)

    def test_late_reply_without_waiter_timeout_update(self):
        """Deadline passed but waiter state not yet updated: reply must still be late."""
        self.bind()
        ask = self.popen_cli(
            "ask", "--binding", "bind-a", "--request-id", "req-wj", "--message-id", "q-wj",
            "--question", "q", "--timeout", "30",
        )
        self.wait_for_request("bind-a", "req-wj")
        connection = sqlite3.connect(str(self.root / "agent_link.sqlite3"))
        connection.execute(
            "UPDATE requests SET deadline_epoch = deadline_epoch - 120"
            " WHERE binding_id = ? AND request_id = ?",
            ("bind-a", "req-wj"),
        )
        connection.commit()
        connection.close()
        reply = self.run_cli(
            "reply", "--binding", "bind-a", "--request-id", "req-wj",
            "--message-id", "r-wj-1", "--answer", "late-after-deadline",
        )
        self.assertEqual(reply.returncode, 0, reply.stderr)
        reply_payload = self.payload(reply)
        self.assertEqual(reply_payload["status"], "timed_out")
        self.assertTrue(reply_payload["late"])
        out, err = ask.communicate(timeout=15)
        self.assertEqual(ask.returncode, 3, err)
        result = json.loads(out)
        self.assertEqual(result["status"], "timed_out")
        self.assertTrue(result["late"])
        self.assertEqual(result["answer"], "late-after-deadline")

    def test_reopen_database_and_idempotent_replay(self):
        self.bind()
        ask = self.popen_cli(
            "ask", "--binding", "bind-a", "--request-id", "req-r", "--message-id", "q-r",
            "--question", "q", "--timeout", "30",
        )
        self.wait_for_request("bind-a", "req-r")
        self.wait_for(lambda: len(self.call_log()) == 1, message="ask dispatch")
        ask.kill()
        ask.wait()
        if ask.stdout:
            ask.stdout.close()
        if ask.stderr:
            ask.stderr.close()
        got = self.payload(self.run_cli("get-request", "--binding", "bind-a", "--request-id", "req-r"))
        self.assertEqual(got["status"], "pending")
        self.assertEqual(got["delivery_state"], "host_accepted")
        reply = self.run_cli(
            "reply", "--binding", "bind-a", "--request-id", "req-r",
            "--message-id", "r-r-1", "--answer", "recovered",
        )
        self.assertEqual(reply.returncode, 0, reply.stderr)
        replay = self.run_cli(
            "ask", "--binding", "bind-a", "--request-id", "req-r", "--message-id", "q-r",
            "--question", "q", "--timeout", "30",
        )
        self.assertEqual(replay.returncode, 0, replay.stderr)
        replay_payload = self.payload(replay)
        self.assertEqual(replay_payload["answer"], "recovered")
        self.assertTrue(replay_payload["idempotent"])
        self.assertEqual(len(self.call_log()), 1, "replay must not re-dispatch")

    def test_concurrent_replies_one_wins(self):
        self.bind()
        ask = self.popen_cli(
            "ask", "--binding", "bind-a", "--request-id", "req-c", "--message-id", "q-c",
            "--question", "q", "--timeout", "30",
        )
        self.wait_for_request("bind-a", "req-c")
        first = self.popen_cli(
            "reply", "--binding", "bind-a", "--request-id", "req-c",
            "--message-id", "r-c-1", "--answer", "winner-1",
        )
        second = self.popen_cli(
            "reply", "--binding", "bind-a", "--request-id", "req-c",
            "--message-id", "r-c-2", "--answer", "winner-2",
        )
        out1, err1 = first.communicate(timeout=30)
        out2, err2 = second.communicate(timeout=30)
        self.assertEqual(sorted([first.returncode, second.returncode]), [0, 4], (out1, err1, out2, err2))
        ask_out, ask_err = ask.communicate(timeout=30)
        self.assertEqual(ask.returncode, 0, ask_err)
        result = json.loads(ask_out)
        self.assertIn(result["answer"], ("winner-1", "winner-2"))
        got = self.payload(self.run_cli("get-request", "--binding", "bind-a", "--request-id", "req-c"))
        self.assertIn(got["answer"], ("winner-1", "winner-2"))
        self.assertEqual(got["status"], "replied")

    def test_concurrent_bind_conflict(self):
        path1, _ = self.write_config("bind-c", "run-1")
        path2, _ = self.write_config("bind-c", "run-2")
        first = self.popen_cli("bind", "--config", path1)
        second = self.popen_cli("bind", "--config", path2)
        out1, err1 = first.communicate(timeout=30)
        out2, err2 = second.communicate(timeout=30)
        self.assertEqual(sorted([first.returncode, second.returncode]), [0, 4], (out1, err1, out2, err2))

    def test_simulated_notifier_failure_no_retry(self):
        self.bind()
        fail_env = base_env({"STUB_NOTIFIER_LOG": str(self.log), "STUB_NOTIFIER_FAIL": "1"})
        first = self.run_cli(
            "report", "--binding", "bind-a", "--message-id", "m-fail", "--text", "x", env=fail_env
        )
        self.assertEqual(first.returncode, 7)
        self.assertEqual(self.payload(first)["delivery_state"], "failed_or_unknown")
        self.assertEqual(len(self.call_log()), 1)
        second = self.run_cli(
            "report", "--binding", "bind-a", "--message-id", "m-fail", "--text", "x", env=fail_env
        )
        self.assertEqual(second.returncode, 7)
        self.assertTrue(self.payload(second)["idempotent"])
        self.assertEqual(len(self.call_log()), 1, "failed delivery must not be retried automatically")
        ask = self.run_cli(
            "ask", "--binding", "bind-a", "--request-id", "req-f", "--message-id", "q-f",
            "--question", "q", "--timeout", "1", env=fail_env,
        )
        self.assertEqual(ask.returncode, 3)
        ask_payload = self.payload(ask)
        self.assertEqual(ask_payload["delivery_state"], "failed_or_unknown")
        self.assertEqual(ask_payload["status"], "timed_out")


class StoreUnitCase(unittest.TestCase):
    """Core-API tests without subprocesses: timeout validation and clock rules."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "runtime"
        self.stub = self.tmp / "stub_notifier.py"
        self.stub.write_text(
            "def bridge(payload):\n    return {'accepted': True}\n", encoding="utf-8"
        )
        self.store = Store(self.root)
        self.addCleanup(self.store.close)
        self.store.bind(
            {
                "binding_id": "bind-u",
                "run_id": "run-u",
                "native_task_id": None,
                "parent": {"framework": "codex-desktop", "session_id": "p-u"},
                "child": {"framework": "pi", "session_id": "c-u"},
                "notifier_path": str(self.stub),
            }
        )

    def test_core_rejects_non_positive_and_non_finite_timeout(self):
        for bad in (float("nan"), float("inf"), float("-inf"), 0, -1, 0.0, "abc", None, True):
            with self.subTest(timeout=bad):
                with self.assertRaises(ValidationError):
                    self.store.prepare_ask("bind-u", "req-bad", "q-bad", "q", bad)

    def test_core_ask_replay_requires_same_timeout(self):
        first = self.store.prepare_ask("bind-u", "req-t", "q-t", "q", 60.0)
        self.assertTrue(first["new"])
        self.assertEqual(first["request"]["timeout_seconds"], 60.0)
        with self.assertRaises(ConflictError):
            self.store.prepare_ask("bind-u", "req-t", "q-t", "q", 600.0)
        replay = self.store.prepare_ask("bind-u", "req-t", "q-t", "q", 60.0)
        self.assertFalse(replay["new"])
        self.assertEqual(replay["request"]["timeout_seconds"], 60.0)

    def test_core_deadline_boundary_is_timed_out(self):
        with mock.patch("agent_link.storage.time.time", return_value=1000.0):
            self.store.prepare_ask("bind-u", "req-at", "q-at", "q", 10.0)
            self.store.prepare_ask("bind-u", "req-before", "q-before", "q", 10.0)
        with mock.patch("agent_link.storage.time.time", return_value=1010.0):
            at_deadline = self.store.reply("bind-u", "req-at", "r-at", "answer")
        self.assertEqual(at_deadline["status"], "timed_out")
        self.assertTrue(at_deadline["late"])
        with mock.patch("agent_link.storage.time.time", return_value=1009.999):
            before_deadline = self.store.reply("bind-u", "req-before", "r-before", "answer")
        self.assertEqual(before_deadline["status"], "replied")
        self.assertFalse(before_deadline["late"])

    def test_core_timed_out_reply_stays_late_after_wall_clock_rollback(self):
        self.store.prepare_ask("bind-u", "req-rb", "q-rb", "q", 3600.0)
        # Move the request to timed_out through the real state machine without
        # waiting: a mono_deadline in the past is enough.
        outcome = self.store.wait_for_request(
            "bind-u", "req-rb", 0.0, time.time(), time.monotonic()
        )
        self.assertEqual(outcome["status"], "timed_out")
        deadline = outcome["deadline_epoch"]
        # Wall clock rolled back before the stored deadline: the already
        # timed-out request must still record the answer as late.
        with mock.patch("agent_link.storage.time.time", return_value=deadline - 120.0):
            stored = self.store.reply("bind-u", "req-rb", "r-rb", "late answer")
        self.assertEqual(stored["status"], "timed_out")
        self.assertTrue(stored["late"])
        row = self.store.get_request("bind-u", "req-rb")
        self.assertEqual(row["answer"], "late answer")
        self.assertTrue(row["late"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
