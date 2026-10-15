"""Client tests for the session-owned push transport (agent_link.hosts.push).

Every test runs against a real loopback HTTP mock recipient: no model, no real
session and no bridge/inbox file is involved.  The tests pin descriptor
validation, loopback-only enforcement (including redirect refusal), token auth,
HTTP error mapping and the "no file fallback" guarantee for parent and child
dispatch targets.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from agent_link.errors import DeliveryFailure
from agent_link.hosts.omp.child import OmpChildAdapter
from agent_link.hosts.omp.parent import OmpParentAdapter
from agent_link.hosts.push import PROTOCOL, session_dir
from agent_link.storage import Store
from tests import isolate_registry
from tests.push_server import DEFAULT_TOKEN, MockPushListener, write_descriptor


class PushClientCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        isolate_registry(self)
        self.root = Path(self.temp.name) / "root"
        self.sessions = Path(self.temp.name) / "sessions"
        previous = os.environ.get("AGENT_LINK_SESSIONS")
        os.environ["AGENT_LINK_SESSIONS"] = str(self.sessions)

        def restore_env():
            os.environ.pop("AGENT_LINK_SESSIONS", None)
            if previous is not None:
                os.environ["AGENT_LINK_SESSIONS"] = previous

        self.addCleanup(restore_env)
        notifier = Path(self.temp.name) / "stub.py"
        notifier.write_text("def bridge(p):\n    return {}\n", encoding="utf-8")
        store = Store(self.root)
        try:
            store.bind(
                {
                    "binding_id": "b",
                    "run_id": "r-b",
                    "native_task_id": None,
                    "parent": {"framework": "omp", "session_id": "parent-1"},
                    "child": {"framework": "omp", "session_id": "child-1"},
                    "notifier_path": str(notifier),
                }
            )
        finally:
            store.close()
        self.listeners: list[MockPushListener] = []
        self.addCleanup(self._close_listeners)

    def _close_listeners(self):
        for listener in reversed(self.listeners):
            listener.close()
        self.listeners.clear()

    def listener(self, framework="omp", session_id="parent-1", **kwargs) -> MockPushListener:
        listener = MockPushListener(framework, session_id, **kwargs)
        self.listeners.append(listener)
        return listener

    def binding(self) -> dict:
        store = Store(self.root)
        try:
            return store.get_binding("b")
        finally:
            store.close()

    def report(self) -> dict:
        return {"kind": "report", "message_id": "m-r", "text": "progress text"}

    def ask(self) -> dict:
        return {
            "kind": "ask",
            "message_id": "m-q",
            "request_id": "q-1",
            "question": "why?",
            "timeout_seconds": 5.0,
            "deadline_epoch": time.time() + 5,
        }

    def ask_child(self) -> dict:
        return {
            "kind": "ask_child",
            "message_id": "m-c",
            "request_id": "rq-1",
            "question": "child, report",
            "timeout_seconds": 5.0,
            "deadline_epoch": time.time() + 5,
        }

    def test_sessions_dir_honours_override(self):
        self.assertEqual(session_dir(), self.sessions.resolve())

    def test_parent_dispatch_sends_authenticated_event(self):
        listener = self.listener("omp", "parent-1")
        write_descriptor(self.sessions, listener.descriptor())
        detail = OmpParentAdapter().notify(self.root, self.binding(), self.report())
        self.assertEqual(detail, "push:omp/parent-1#m-r")
        self.assertEqual(len(listener.events), 1)
        event = listener.events[0]
        self.assertEqual(event["protocol"], PROTOCOL)
        self.assertEqual(event["framework"], "omp")
        self.assertEqual(event["session_id"], "parent-1")
        self.assertEqual(event["binding_id"], "b")
        self.assertEqual(event["run_id"], "r-b")
        self.assertEqual(event["role"], "parent")
        self.assertEqual(event["message_id"], "m-r")
        self.assertIn("[agent-link:report]", event["prompt"])
        self.assertIn("progress text", event["prompt"])
        self.assertNotIn("request_id", event)
        self.assertEqual(listener.requests[0]["authorization"], "Bearer " + DEFAULT_TOKEN)

    def test_child_dispatch_sends_child_prompt(self):
        listener = self.listener("omp", "child-1")
        write_descriptor(self.sessions, listener.descriptor())
        detail = OmpChildAdapter().deliver(self.root, self.binding(), self.ask_child())
        self.assertEqual(detail, "push:omp/child-1#m-c")
        event = listener.events[0]
        self.assertEqual(event["role"], "child")
        self.assertEqual(event["framework"], "omp")
        self.assertEqual(event["session_id"], "child-1")
        self.assertIn("[agent-link:parent-ask]", event["prompt"])
        self.assertIn("request_id: rq-1", event["prompt"])
        self.assertIn("child, report", event["prompt"])

    def test_missing_descriptor_fails_without_file_fallback(self):
        with self.assertRaises(DeliveryFailure) as caught:
            OmpParentAdapter().notify(self.root, self.binding(), self.report())
        self.assertIn("no file fallback", str(caught.exception))
        self.assertFalse((self.root / "omp-bridge").exists(), "no bridge file fallback")
        self.assertFalse((self.root / "inbox").exists(), "no inbox file fallback")

    def test_identity_mismatch_descriptors_fail(self):
        listener = self.listener("omp", "parent-1")
        for overrides in (
            {"protocol": "agent-link-push/0"},
            {"framework": "pi"},
            {"session_id": "someone-else"},
        ):
            with self.subTest(overrides=overrides):
                write_descriptor(self.sessions, listener.descriptor(**overrides))
                with self.assertRaises(DeliveryFailure):
                    OmpParentAdapter().notify(self.root, self.binding(), self.report())
        self.assertEqual(listener.requests, [], "a mismatched descriptor must not be contacted")

    def test_malformed_descriptor_fails(self):
        directory = Path(self.sessions) / "omp"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "parent-1.json"
        for content in ("not json", "[]", '"text"'):
            with self.subTest(content=content):
                target.write_text(content, encoding="utf-8")
                with self.assertRaises(DeliveryFailure):
                    OmpParentAdapter().notify(self.root, self.binding(), self.report())

    def test_short_token_descriptor_fails_without_contacting(self):
        listener = self.listener("omp", "parent-1")
        write_descriptor(self.sessions, listener.descriptor(token="short"))
        with self.assertRaises(DeliveryFailure):
            OmpParentAdapter().notify(self.root, self.binding(), self.report())
        self.assertEqual(listener.requests, [])

    def test_non_loopback_endpoints_are_rejected(self):
        listener = self.listener("omp", "parent-1")
        port = listener.port
        bad_endpoints = (
            "http://192.0.2.10:1234",
            "http://localhost:1234",
            "https://127.0.0.1:%d" % port,
            "http://127.0.0.1:%d/path" % port,
            "http://127.0.0.1:%d/?x=1" % port,
            "http://127.0.0.1:%d/#fragment" % port,
            "http://user@127.0.0.1:%d" % port,
            "http://127.0.0.1",
        )
        for endpoint in bad_endpoints:
            with self.subTest(endpoint=endpoint):
                write_descriptor(self.sessions, listener.descriptor(endpoint=endpoint))
                with self.assertRaises(DeliveryFailure):
                    OmpParentAdapter().notify(self.root, self.binding(), self.report())
        self.assertEqual(listener.requests, [], "non-loopback endpoints must never be contacted")

    def test_redirect_is_not_followed(self):
        target = self.listener("omp", "target")
        redirector = self.listener("omp", "parent-1", redirect_to=target.endpoint + "/evil")
        write_descriptor(self.sessions, redirector.descriptor())
        with self.assertRaises(DeliveryFailure) as caught:
            OmpParentAdapter().notify(self.root, self.binding(), self.report())
        self.assertIn("302", str(caught.exception))
        self.assertEqual(target.requests, [], "the redirect target must not be contacted")

    def test_wrong_token_is_rejected(self):
        listener = self.listener("omp", "parent-1", token="a" * 32)
        write_descriptor(self.sessions, listener.descriptor(token="b" * 32))
        with self.assertRaises(DeliveryFailure) as caught:
            OmpParentAdapter().notify(self.root, self.binding(), self.report())
        self.assertIn("403", str(caught.exception))
        self.assertEqual(listener.events, [], "a wrong token must not deliver an event")

    def test_http_error_and_bad_acknowledgements_fail(self):
        cases = (
            {"respond_status": 500, "respond_body": {"error": "boom"}},
            {"respond_status": 200, "respond_body": {"accepted": False}},
            {"respond_status": 200, "respond_body": {"accepted": True, "session_id": "other"}},
        )
        for kwargs in cases:
            with self.subTest(kwargs=kwargs):
                listener = self.listener("omp", "parent-1", **kwargs)
                write_descriptor(self.sessions, listener.descriptor())
                with self.assertRaises(DeliveryFailure):
                    OmpParentAdapter().notify(self.root, self.binding(), self.report())

    def test_oversized_response_fails(self):
        listener = self.listener(
            "omp",
            "parent-1",
            respond_status=200,
            respond_body={"accepted": True, "session_id": "parent-1", "padding": "x" * 70000},
        )
        write_descriptor(self.sessions, listener.descriptor())
        with self.assertRaises(DeliveryFailure):
            OmpParentAdapter().notify(self.root, self.binding(), self.report())


if __name__ == "__main__":
    unittest.main(verbosity=2)
