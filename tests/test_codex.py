"""Codex parent helper tests: registry discovery, ownership checks, closed loops.

Everything is local and simulated: stub notifiers record ``bridge()`` calls
and the OpenCode child is a fake HTTP serve.  No desktop message is ever sent
and no model is called.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from agent_link.hosts.codex.parent import CodexParentAdapter, build_codex_parent_prompt
from agent_link.shared import build_parent_prompt
from agent_link.storage import Store
from tests import isolate_codex_connections, isolate_registry
from tests.opencode_server import MockOpenCodeServe, write_descriptor_pair

REPO_ROOT = Path(__file__).resolve().parent.parent
WRAPPER = REPO_ROOT / "skills" / "agent-link" / "scripts" / "link.py"
CODEX_CLI = [sys.executable, "-m", "agent_link.hosts.codex.cli"]
CORE_CLI = [sys.executable, "-m", "agent_link"]
STUB_SOURCE = '''\
"""Simulated notifier used by tests: records every bridge() attempt."""

import json
import os
from pathlib import Path


def bridge(payload):
    log = Path(os.environ["STUB_NOTIFIER_LOG"])
    with log.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"payload": payload}, ensure_ascii=False) + "\\n")
    return {"accepted": True}
'''


class CodexCase(unittest.TestCase):
    def setUp(self):
        self.registry = Path(isolate_registry(self))
        self.connections = Path(isolate_codex_connections(self))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name)
        self.stub = self.work / "stub_notifier.py"
        self.stub.write_text(STUB_SOURCE, encoding="utf-8")
        self.log = self.work / "notifier-calls.jsonl"
        self.roots: list = []
        self.bindings: list = []

    # -- helpers -------------------------------------------------------
    def base_env(self, extra=None):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        env["STUB_NOTIFIER_LOG"] = str(self.log)
        env.pop("AGENT_LINK_REPO", None)
        env.pop("CODEX_THREAD_ID", None)
        env.pop("CODEX_SESSION_ID", None)
        env.pop("CODEX_APP_TOOLS_PIPE_PATH", None)
        env.pop("CODEX_APP_TOOLS_CALLER_HOST_ID", None)
        if extra:
            env.update(extra)
        return env

    def new_root(self, name):
        root = self.work / name
        self.roots.append(root)
        return root

    def bind(
        self,
        binding_id,
        *,
        parent_session="session-1",
        parent_framework="codex-desktop",
        child_framework="pi",
        child_session=None,
        root=None,
        run_id=None,
    ):
        root = root or self.new_root("root-" + binding_id)
        config = {
            "binding_id": binding_id,
            "run_id": run_id or "run-" + binding_id,
            "native_task_id": None,
            "parent": {"framework": parent_framework, "session_id": parent_session},
            "child": {
                "framework": child_framework,
                "session_id": child_session or "child-" + binding_id,
            },
            "notifier_path": str(self.stub),
        }
        config_path = self.work / f"bind-{binding_id}.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        proc = subprocess.run(
            CORE_CLI + ["--root", str(root), "bind", "--config", str(config_path)],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env=self.base_env(),
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.bindings.append(binding_id)
        return root, config

    def run_codex(self, *args, env=None, timeout=60, cwd=None):
        return subprocess.run(
            CODEX_CLI + [str(arg) for arg in args],
            capture_output=True,
            text=True,
            cwd=cwd or REPO_ROOT,
            env=env or self.base_env(),
            timeout=timeout,
        )

    def run_core(self, *args, root=None, timeout=60):
        return subprocess.run(
            CORE_CLI + ["--root", str(root or self.roots[0])] + [str(arg) for arg in args],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            env=self.base_env(),
            timeout=timeout,
        )

    def popen_codex(self, *args):
        proc = subprocess.Popen(
            CODEX_CLI + [str(arg) for arg in args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=REPO_ROOT,
            env=self.base_env(),
        )
        self.add_process_cleanup(proc)
        return proc

    def add_process_cleanup(self, proc):
        def cleanup():
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
            for stream in (proc.stdout, proc.stderr):
                if stream is not None and not stream.closed:
                    stream.close()

        self.addCleanup(cleanup)
        return proc

    def opencode_child(self, name, child_session):
        """Start a fake OpenCode serve and write its descriptors for one test.

        Points AGENT_LINK_SESSIONS at a per-test directory so subprocesses
        inherit it through ``base_env()``; returns the fake serve.
        """
        sessions = self.work / f"{name}-sessions"
        previous = os.environ.get("AGENT_LINK_SESSIONS")
        os.environ["AGENT_LINK_SESSIONS"] = str(sessions)

        def restore_env():
            os.environ.pop("AGENT_LINK_SESSIONS", None)
            if previous is not None:
                os.environ["AGENT_LINK_SESSIONS"] = previous

        self.addCleanup(restore_env)
        serve = MockOpenCodeServe(child_session)
        self.addCleanup(serve.close)
        write_descriptor_pair(sessions, serve)
        return serve

    def write_registry_entry(self, binding_id, **overrides):
        entry = {
            "version": 1,
            "binding_id": binding_id,
            "run_id": "run-" + binding_id,
            "native_task_id": None,
            "root": str(self.work / "nowhere"),
            "parent": {"framework": "codex-desktop", "session_id": "session-1"},
            "child": {"framework": "pi", "session_id": "child-" + binding_id},
            "config_digest": "0" * 64,
            "created_at": "2026-01-01T00:00:00.000+00:00",
            "updated_at": "2026-01-01T00:00:00.000+00:00",
        }
        entry.update(overrides)
        path = self.registry / f"{binding_id}.json"
        path.write_text(json.dumps(entry), encoding="utf-8")
        return entry, path

    def edit_registry_entry(self, binding_id, **updates):
        path = self.registry / f"{binding_id}.json"
        entry = json.loads(path.read_text("utf-8"))
        entry.update(updates)
        path.write_text(json.dumps(entry), encoding="utf-8")
        return entry

    def wait_for_file_text(self, path: Path, timeout: float = 10.0) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if path.exists():
                text = path.read_text("utf-8")
                if text.strip():
                    return text
            time.sleep(0.05)
        return path.read_text("utf-8") if path.exists() else ""

    @staticmethod
    def payload(proc):
        return json.loads(proc.stdout)

    # -- discovery -----------------------------------------------------
    def test_list_reports_accepted_and_rejected_entries(self):
        self.bind("b-good")
        empty_root = self.new_root("empty-root")
        empty_root.mkdir()
        entry, _ = self.write_registry_entry(
            "b-no-db",
            run_id="run-b-no-db",
            root=str(empty_root),
            parent={"framework": "codex-desktop", "session_id": "session-1"},
        )
        self.write_registry_entry(
            "b-no-root",
            run_id="run-b-no-root",
            root=str(self.work / "does-not-exist"),
            parent={"framework": "codex-desktop", "session_id": "session-1"},
        )
        other_root = self.new_root("other-root")
        other_root.mkdir()
        self.write_registry_entry(
            "b-other-session",
            root=str(other_root),
            parent={"framework": "codex-desktop", "session_id": "session-other"},
        )
        self.write_registry_entry(
            "b-wrong-framework",
            root=str(other_root),
            parent={"framework": "omp", "session_id": "session-1"},
        )
        (self.registry / "broken.json").write_text("{not json", encoding="utf-8")

        proc = self.run_codex("--session-id", "session-1", "list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = self.payload(proc)
        self.assertEqual(payload["session_id"], "session-1")
        self.assertEqual(payload["accepted"], ["b-good"])
        listed = {entry["binding_id"]: entry for entry in payload["bindings"]}
        self.assertNotIn("b-other-session", listed)
        self.assertNotIn("b-wrong-framework", listed)
        rejected = {entry["binding_id"]: entry["reason"] for entry in payload["rejected"]}
        self.assertIn("b-no-db", rejected)
        self.assertIn("database does not exist", rejected["b-no-db"])
        self.assertIn("b-no-root", rejected)
        self.assertIn("does not exist", rejected["b-no-root"])
        self.assertIn("broken", rejected)
        self.assertIn("unreadable registry entry", rejected["broken"])
        # Discovery must never create a database through Store.
        self.assertFalse((empty_root / "agent_link.sqlite3").exists())
        # The good entry was verified against its own database over read-only sqlite.
        good = listed["b-good"]
        self.assertEqual(good["status"], "accepted")
        self.assertEqual(good["run_id"], "run-b-good")
        self.assertEqual(good["parent"]["session_id"], "session-1")
        self.assertEqual(good["database"], str((self.roots[0] / "agent_link.sqlite3").resolve()))

    def test_multiple_bindings_require_explicit_selection(self):
        root_b1, _ = self.bind("b1", child_framework="omp", child_session="child-b1")
        root_b2, _ = self.bind("b2", child_framework="omp", child_session="child-b2")
        store = Store(root_b1)
        try:
            store.prepare_ask("b1", "rq-b1", "pm-b1", "question-b1", 60.0)
        finally:
            store.close()

        ambiguous = self.run_codex("--session-id", "session-1", "get-request", "--request-id", "rq-b1")
        self.assertEqual(ambiguous.returncode, 5)
        body = self.payload(ambiguous)
        self.assertEqual(body["error"], "validation")
        self.assertIn("--binding", body["message"])
        self.assertEqual(
            sorted(entry["binding_id"] for entry in body["bindings"]), ["b1", "b2"]
        )

        selected = self.run_codex(
            "--session-id", "session-1", "--binding", "b1", "get-request", "--request-id", "rq-b1"
        )
        self.assertEqual(selected.returncode, 0, selected.stderr)
        self.assertEqual(self.payload(selected)["question"], "question-b1")

        # The other binding must not answer for b1 or leak its database.
        wrong = self.run_codex(
            "--session-id", "session-1", "--binding", "b2", "get-request", "--request-id", "rq-b1"
        )
        self.assertEqual(wrong.returncode, 6)
        unknown = self.run_codex(
            "--session-id", "session-1", "--binding", "nope", "get-request", "--request-id", "rq-b1"
        )
        self.assertEqual(unknown.returncode, 6)
        unknown_session = self.run_codex(
            "--session-id", "session-other", "--binding", "b1", "get-request", "--request-id", "rq-b1"
        )
        self.assertEqual(unknown_session.returncode, 6)
        self.assertTrue((root_b1 / "agent_link.sqlite3").is_file())
        self.assertTrue((root_b2 / "agent_link.sqlite3").is_file())

    def test_stale_digest_run_id_and_identity_are_rejected(self):
        root, _ = self.bind("b-stale")
        request = self.run_codex(
            "--session-id", "session-1", "--binding", "b-stale", "get-request", "--request-id", "x"
        )
        self.assertEqual(request.returncode, 6)

        self.edit_registry_entry("b-stale", config_digest="f" * 64)
        stale = self.run_codex(
            "--session-id", "session-1", "--binding", "b-stale", "get-request", "--request-id", "x"
        )
        self.assertEqual(stale.returncode, 5)
        self.assertIn("config_digest", self.payload(stale)["message"])
        listed = self.payload(self.run_codex("--session-id", "session-1", "list"))
        self.assertEqual(listed["accepted"], [])
        self.assertEqual([entry["binding_id"] for entry in listed["rejected"]], ["b-stale"])

        # A replay of bind restores the entry from the authoritative database.
        self.bind("b-stale")
        self.edit_registry_entry("b-stale", run_id="run-something-else")
        stale_run = self.run_codex(
            "--session-id", "session-1", "--binding", "b-stale", "get-request", "--request-id", "x"
        )
        self.assertEqual(stale_run.returncode, 5)
        self.assertIn("run_id", self.payload(stale_run)["message"])

        self.bind("b-stale")
        self.edit_registry_entry(
            "b-stale", parent={"framework": "codex-desktop", "session_id": "session-other"}
        )
        cross = self.run_codex(
            "--session-id", "session-1", "--binding", "b-stale", "get-request", "--request-id", "x"
        )
        self.assertEqual(cross.returncode, 6, "an entry for another session must not be usable")
        listed = self.payload(self.run_codex("--session-id", "session-1", "list"))
        self.assertEqual(listed["bindings"], [])

        self.bind("b-stale")
        self.edit_registry_entry("b-stale", parent={"framework": "omp", "session_id": "session-1"})
        wrong_framework = self.run_codex(
            "--session-id", "session-1", "--binding", "b-stale", "get-request", "--request-id", "x"
        )
        self.assertEqual(wrong_framework.returncode, 6)

        self.bind("b-stale")
        self.edit_registry_entry("b-stale", child={"framework": "omp", "session_id": "child-b-stale"})
        wrong_child = self.run_codex(
            "--session-id", "session-1", "--binding", "b-stale", "get-request", "--request-id", "x"
        )
        self.assertEqual(wrong_child.returncode, 5)
        self.assertIn("child.framework", self.payload(wrong_child)["message"])
        # The binding database itself was never modified by the tampering.
        self.assertTrue((root / "agent_link.sqlite3").is_file())

    def test_missing_database_is_rejected_and_not_created(self):
        empty_root = self.new_root("missing-db")
        empty_root.mkdir()
        self.write_registry_entry(
            "b-missing",
            run_id="run-b-missing",
            root=str(empty_root),
            parent={"framework": "codex-desktop", "session_id": "session-1"},
        )
        listed = self.payload(self.run_codex("--session-id", "session-1", "list"))
        self.assertEqual([entry["binding_id"] for entry in listed["rejected"]], ["b-missing"])
        self.assertIn("database does not exist", listed["rejected"][0]["reason"])
        operation = self.run_codex(
            "--session-id", "session-1", "--binding", "b-missing", "get-request", "--request-id", "x"
        )
        self.assertEqual(operation.returncode, 5)
        self.assertFalse(
            (empty_root / "agent_link.sqlite3").exists(),
            "a rejected discovery must not create a database via Store",
        )

    # -- session id handling ------------------------------------------
    def test_session_id_env_fallback_and_missing_session(self):
        self.bind("b-env")
        from_thread = self.run_codex("list", env=self.base_env({"CODEX_THREAD_ID": "session-1"}))
        self.assertEqual(from_thread.returncode, 0, from_thread.stderr)
        self.assertEqual(self.payload(from_thread)["accepted"], ["b-env"])

        from_session = self.run_codex("list", env=self.base_env({"CODEX_SESSION_ID": "session-1"}))
        self.assertEqual(from_session.returncode, 0)
        self.assertEqual(self.payload(from_session)["accepted"], ["b-env"])

        missing = self.run_codex("list")
        self.assertEqual(missing.returncode, 5)
        self.assertIn("session id", self.payload(missing)["message"])

        other = self.run_codex("list", env=self.base_env({"CODEX_THREAD_ID": "session-unused"}))
        self.assertEqual(other.returncode, 0)
        self.assertEqual(self.payload(other)["accepted"], [])

        explicit = self.run_codex(
            "--session-id", "session-1", "list", env=self.base_env({"CODEX_THREAD_ID": "session-unused"})
        )
        self.assertEqual(self.payload(explicit)["accepted"], ["b-env"])

    def test_root_argument_is_never_accepted(self):
        with_root = self.run_codex("--session-id", "session-1", "--root", "/tmp", "list")
        self.assertEqual(with_root.returncode, 2, "the helper must not accept a caller-supplied root")
        reply_root = self.run_codex(
            "--session-id",
            "session-1",
            "reply",
            "--root",
            "/tmp",
            "--request-id",
            "r",
            "--message-id",
            "m",
            "--answer",
            "a",
        )
        self.assertEqual(reply_root.returncode, 2)

    # -- closed loops --------------------------------------------------
    def test_reply_closed_loop_uses_temp_database(self):
        root, _ = self.bind("b-reply")
        asker = subprocess.Popen(
            CORE_CLI
            + [
                "--root",
                str(root),
                "ask",
                "--binding",
                "b-reply",
                "--request-id",
                "rq-1",
                "--message-id",
                "pm-1",
                "--question",
                "pick one",
                "--timeout",
                "30",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=REPO_ROOT,
            env=self.base_env(),
        )
        self.add_process_cleanup(asker)
        self.assertTrue(self.wait_for_file_text(self.log).strip(), "the ask must dispatch before reply")

        reply = self.run_codex(
            "--session-id",
            "session-1",
            "--binding",
            "b-reply",
            "reply",
            "--request-id",
            "rq-1",
            "--message-id",
            "rm-1",
            "--answer",
            "option A",
        )
        self.assertEqual(reply.returncode, 0, reply.stderr)
        result = self.payload(reply)
        self.assertEqual(result["status"], "replied")
        self.assertEqual(result["answer"], "option A")
        self.assertFalse(result["idempotent"])

        out, err = asker.communicate(timeout=30)
        self.assertEqual(asker.returncode, 0, err)
        asked = json.loads(out)
        self.assertEqual(asked["status"], "replied")
        self.assertEqual(asked["answer"], "option A")

        replay = self.run_codex(
            "--session-id",
            "session-1",
            "--binding",
            "b-reply",
            "reply",
            "--request-id",
            "rq-1",
            "--message-id",
            "rm-1",
            "--answer",
            "option A",
        )
        self.assertEqual(replay.returncode, 0)
        self.assertTrue(self.payload(replay)["idempotent"])
        overwrite = self.run_codex(
            "--session-id",
            "session-1",
            "--binding",
            "b-reply",
            "reply",
            "--request-id",
            "rq-1",
            "--message-id",
            "rm-2",
            "--answer",
            "option B",
        )
        self.assertEqual(overwrite.returncode, 4)

        # The prompt carried the Codex helper hints (no desktop message was sent).
        prompt = json.loads(self.wait_for_file_text(self.log).splitlines()[0])["payload"]["prompt"]
        self.assertIn("python3 -m agent_link.hosts.codex.cli --session-id session-1 list", prompt)
        self.assertIn("--binding b-reply get-request --request-id rq-1", prompt)
        self.assertIn("--binding b-reply reply --request-id rq-1", prompt)
        self.assertIn("The child process is waiting for a reply.", prompt)

    def test_ask_child_closed_loop_with_simulated_child(self):
        root, _ = self.bind("b-askchild", child_framework="opencode", child_session="child-oc")
        serve = self.opencode_child("askchild", "child-oc")
        asker = self.popen_codex(
            "--session-id",
            "session-1",
            "--binding",
            "b-askchild",
            "ask-child",
            "--request-id",
            "rq-c",
            "--message-id",
            "pm-c",
            "--question",
            "child, answer?",
            "--timeout",
            "30",
        )
        self.add_process_cleanup(asker)
        deadline = time.time() + 10
        while time.time() < deadline and not serve.prompts:
            time.sleep(0.05)
        self.assertTrue(serve.prompts, "ask-child must push one prompt before waiting")
        prompt = serve.prompts[0]
        self.assertEqual(prompt["id"], "msg_pm-c")
        self.assertEqual(prompt["delivery"], "queue")
        self.assertIn("[agent-link:parent-ask]", prompt["text"])
        self.assertIn("request_id: rq-c", prompt["text"])
        self.assertIn("answer --binding", prompt["text"])
        self.assertFalse((root / "inbox").exists(), "no inbox fallback")

        answered = self.run_core(
            "answer",
            "--binding",
            "b-askchild",
            "--request-id",
            "rq-c",
            "--message-id",
            "am-c",
            "--answer",
            "child answer",
            root=root,
        )
        self.assertEqual(answered.returncode, 0, answered.stderr)
        out, err = asker.communicate(timeout=30)
        self.assertEqual(asker.returncode, 0, err)
        result = json.loads(out)
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["answer"], "child answer")
        self.assertFalse(result["late"])
        self.assertEqual(result["delivery_state"], "host_accepted")

        fetched = self.run_codex(
            "--session-id", "session-1", "--binding", "b-askchild", "get-parent-request",
            "--request-id", "rq-c",
        )
        self.assertEqual(fetched.returncode, 0, fetched.stderr)
        self.assertEqual(self.payload(fetched)["status"], "answered")
        listed = self.run_codex(
            "--session-id", "session-1", "--binding", "b-askchild", "list-parent-requests"
        )
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(
            [row["request_id"] for row in self.payload(listed)["parent_requests"]], ["rq-c"]
        )

        timed_out = self.run_codex(
            "--session-id",
            "session-1",
            "--binding",
            "b-askchild",
            "ask-child",
            "--request-id",
            "rq-t",
            "--message-id",
            "pm-t",
            "--question",
            "nobody answers",
            "--timeout",
            "1",
            timeout=30,
        )
        self.assertEqual(timed_out.returncode, 3, timed_out.stderr)
        self.assertEqual(self.payload(timed_out)["status"], "timed_out")

    def test_send_child_closed_loop_reuses_core_handler(self):
        root, _ = self.bind("b-sendchild", child_framework="opencode", child_session="child-sc")
        serve = self.opencode_child("sendchild", "child-sc")
        send_args = (
            "--session-id", "session-1", "--binding", "b-sendchild", "send-child",
            "--request-id", "rq-s", "--message-id", "pm-s", "--question", "status?",
            "--timeout", "30",
        )

        sent = self.run_codex(*send_args, timeout=30)
        self.assertEqual(sent.returncode, 0, sent.stdout + sent.stderr)
        payload = self.payload(sent)
        self.assertEqual(payload["delivery_state"], "host_accepted")
        self.assertEqual(payload["status"], "pending")
        self.assertFalse(payload["idempotent"])
        deadline = time.time() + 10
        while time.time() < deadline and not serve.prompts:
            time.sleep(0.05)
        self.assertEqual(len(serve.prompts), 1, "send-child must push one prompt")
        self.assertEqual(serve.prompts[0]["id"], "msg_pm-s")
        self.assertIn("status?", serve.prompts[0]["text"])

        replay = self.run_codex(*send_args, timeout=30)
        self.assertEqual(replay.returncode, 0, replay.stdout + replay.stderr)
        self.assertTrue(self.payload(replay)["idempotent"])
        self.assertEqual(len(serve.prompts), 1, "the helper replay must not re-push")

        answered = self.run_core(
            "answer", "--binding", "b-sendchild", "--request-id", "rq-s",
            "--message-id", "am-s", "--answer", "all good", root=root,
        )
        self.assertEqual(answered.returncode, 0, answered.stderr)
        after = self.run_codex(*send_args, timeout=30)
        self.assertEqual(after.returncode, 0, after.stdout + after.stderr)
        after_payload = self.payload(after)
        self.assertTrue(after_payload["idempotent"])
        self.assertEqual(after_payload["status"], "answered")
        self.assertEqual(len(serve.prompts), 1)

        conflict = self.run_codex(
            "--session-id", "session-1", "--binding", "b-sendchild", "send-child",
            "--request-id", "rq-conflict", "--message-id", "pm-s", "--question", "reuse",
            "--timeout", "30", timeout=30,
        )
        self.assertEqual(conflict.returncode, 4, conflict.stdout + conflict.stderr)

    def test_send_child_delivery_failure_maps_to_exit_7(self):
        self.bind("b-sendfail", child_framework="opencode", child_session="child-missing")
        send_args = (
            "--session-id", "session-1", "--binding", "b-sendfail", "send-child",
            "--request-id", "rq-f", "--message-id", "pm-f", "--question", "anyone?",
            "--timeout", "30",
        )
        proc = self.run_codex(*send_args, timeout=30)
        self.assertEqual(proc.returncode, 7, proc.stdout + proc.stderr)
        payload = self.payload(proc)
        self.assertEqual(payload["delivery_state"], "failed_or_unknown")
        self.assertIn("error", payload)

        replay = self.run_codex(*send_args, timeout=30)
        self.assertEqual(replay.returncode, 7, replay.stdout + replay.stderr)
        replay_payload = self.payload(replay)
        self.assertTrue(replay_payload["idempotent"])
        self.assertNotIn("error", replay_payload, "a replay must not re-dispatch")

    def test_ask_child_rejected_binding_does_not_touch_root(self):
        empty_root = self.new_root("askchild-missing")
        empty_root.mkdir()
        self.write_registry_entry(
            "b-askchild-missing",
            run_id="run-b-askchild-missing",
            root=str(empty_root),
            parent={"framework": "codex-desktop", "session_id": "session-1"},
        )
        proc = self.run_codex(
            "--session-id",
            "session-1",
            "--binding",
            "b-askchild-missing",
            "ask-child",
            "--request-id",
            "rq",
            "--message-id",
            "pm",
            "--question",
            "?",
            "--timeout",
            "1",
        )
        self.assertEqual(proc.returncode, 5)
        self.assertFalse((empty_root / "agent_link.sqlite3").exists())
        self.assertFalse((empty_root / "inbox").exists())

    # -- prompt rendering ----------------------------------------------
    def test_prompt_hints_are_framework_scoped_and_shell_quoted(self):
        root = self.work / "root with spaces"
        root.mkdir()
        binding = {
            "binding_id": "b-quote",
            "run_id": "run-b-quote",
            "native_task_id": None,
            "parent_framework": "codex-desktop",
            "parent_session_id": "session-1",
            "child_framework": "pi",
            "child_session_id": "child-b-quote",
        }
        message = {
            "kind": "ask",
            "message_id": "m-1",
            "request_id": "rq-1",
            "question": "which one?",
            "timeout_seconds": 60.0,
            "deadline_epoch": 0.0,
        }
        shared = build_parent_prompt(root, binding, message)
        self.assertIn("The child process is waiting for a reply.", shared)
        self.assertIn("python3 -m agent_link --root", shared)
        self.assertNotIn("agent_link.hosts.codex", shared, "the shared renderer must stay framework-neutral")
        quoted = "python3 -m agent_link --root " + shlex_quote(str(root.resolve()))
        self.assertIn(quoted, shared)

        codex_prompt = build_codex_parent_prompt(root, binding, message)
        self.assertGreater(len(codex_prompt), len(shared))
        self.assertIn("agent_link.hosts.codex.cli", codex_prompt)
        self.assertIn("--session-id session-1", codex_prompt)
        self.assertIn("--binding b-quote get-request --request-id rq-1", codex_prompt)
        self.assertTrue(CodexParentAdapter().build_prompt(root, binding, message).endswith(codex_prompt))

        omp = dict(binding, parent_framework="omp")
        self.assertEqual(build_codex_parent_prompt(root, omp, message), build_parent_prompt(root, omp, message))
        self.assertNotIn("agent_link.hosts.codex.cli", build_codex_parent_prompt(root, omp, message))

    # -- wrapper -------------------------------------------------------
    def test_wrapper_dispatches_from_tmp_both_entry_points(self):
        self.bind("b-wrapper")
        env = self.base_env()
        legacy = subprocess.run(
            [sys.executable, str(WRAPPER), "--root", str(self.roots[0]), "list-bindings",
             "--parent-session-id", "session-1"],
            capture_output=True,
            text=True,
            cwd="/tmp",
            env=env,
            timeout=30,
        )
        self.assertEqual(legacy.returncode, 0, legacy.stderr)
        self.assertEqual(
            [entry["binding_id"] for entry in json.loads(legacy.stdout)["bindings"]], ["b-wrapper"]
        )

        codex = subprocess.run(
            [sys.executable, str(WRAPPER), "--codex", "--session-id", "session-1", "list"],
            capture_output=True,
            text=True,
            cwd="/tmp",
            env=env,
            timeout=30,
        )
        self.assertEqual(codex.returncode, 0, codex.stderr)
        self.assertEqual(json.loads(codex.stdout)["accepted"], ["b-wrapper"])

    def test_wrapper_uses_repo_location_file_for_standalone_copy(self):
        self.bind("b-standalone")
        install = self.work / "installed-skill" / "skills" / "agent-link" / "scripts"
        install.mkdir(parents=True)
        script = install / "link.py"
        script.write_text(WRAPPER.read_text("utf-8"), encoding="utf-8")
        (install / "repo-location.json").write_text(
            json.dumps({"repo": str(REPO_ROOT)}), encoding="utf-8"
        )
        proc = subprocess.run(
            [sys.executable, str(script), "--codex", "--session-id", "session-1", "list"],
            capture_output=True,
            text=True,
            cwd="/tmp",
            env=self.base_env(),
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["accepted"], ["b-standalone"])


def shlex_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)


if __name__ == "__main__":
    unittest.main(verbosity=2)
