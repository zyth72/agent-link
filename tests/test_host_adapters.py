"""Host adapter entry contract tests (docs/HOST-ADAPTERS.md).

Verifies that every framework package exposes ``get_adapters()`` with ABC
parent/child objects (``None`` where unsupported), that loading has no side
effects, that the removed old module paths are gone, and that core dispatch
actually calls the object returned by the entry point.
"""

from __future__ import annotations

import importlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_link import cli
from agent_link.errors import ValidationError
from agent_link.hosts import ChildAdapter, HostAdapters, ParentAdapter, load_adapters
from agent_link.hosts import codex, omp, opencode, pi
from agent_link.storage import Store
from tests import isolate_registry

FRAMEWORKS = {
    "codex-desktop": False,
    "pi": True,
    "omp": True,
    "opencode": True,
}


class RecordingParent(ParentAdapter):
    framework = "recording"

    def __init__(self):
        self.calls = []

    def capabilities(self):
        return {"host": "recording"}

    def notify(self, root, binding, message):
        self.calls.append(dict(message))
        return "recorded-parent"


class RecordingChild(ChildAdapter):
    framework = "recording"

    def __init__(self):
        self.calls = []

    def capabilities(self):
        return {"child": "recording"}

    def deliver(self, root, binding, message):
        self.calls.append(dict(message))
        return "recorded-child"


class AdapterEntryCase(unittest.TestCase):
    def test_every_framework_returns_abc_pairs(self):
        for framework, has_child in FRAMEWORKS.items():
            with self.subTest(framework=framework):
                adapters = load_adapters(framework)
                self.assertIsInstance(adapters, HostAdapters)
                self.assertIsInstance(adapters.parent, ParentAdapter)
                self.assertEqual(adapters.parent.framework, framework)
                self.assertIn("driver", adapters.parent.capabilities())
                if has_child:
                    self.assertIsInstance(adapters.child, ChildAdapter)
                    self.assertEqual(adapters.child.framework, framework)
                    # Every child declares its delivery transport; a driver
                    # pointer is optional because the transport helper is the
                    # stable contract.
                    child = adapters.child.capabilities()
                    self.assertTrue({"driver", "delivery_transport"} & set(child))
                else:
                    self.assertIsNone(adapters.child)

    def test_packages_expose_get_adapters_fresh_objects(self):
        for module in (codex, pi, omp, opencode):
            with self.subTest(module=module.__name__):
                first = module.get_adapters()
                second = module.get_adapters()
                self.assertIsInstance(first, HostAdapters)
                self.assertIsNot(first.parent, second.parent)
                self.assertEqual(type(first.parent), type(second.parent))

    def test_unknown_framework_is_rejected(self):
        for bad in ("", "codex", "unknown", None):
            with self.subTest(framework=bad):
                with self.assertRaises(ValidationError):
                    load_adapters(bad)

    def test_capabilities_keep_unverified_unknown(self):
        omp_child = omp.get_adapters().child
        omp_capabilities = omp_child.capabilities()
        self.assertEqual(omp_capabilities["delivery_transport"], "session_push_http")
        self.assertEqual(omp_capabilities["wake_child"], "native_injection")
        self.assertFalse(omp_capabilities["polling"])
        omp_parent = omp.get_adapters().parent
        self.assertEqual(omp_parent.capabilities()["notification_transport"], "session_push_http")
        pi_parent = pi.get_adapters().parent
        self.assertEqual(pi_parent.capabilities()["notification_transport"], "session_push_http")
        self.assertEqual(pi_parent.capabilities()["busy_behavior"], "followup_queue")
        pi_child = pi.get_adapters().child
        capabilities = pi_child.capabilities()
        self.assertEqual(capabilities["delivery_transport"], "rpc_or_session_push")
        self.assertEqual(capabilities["wake_child"], "native_injection")
        self.assertFalse(capabilities["polling"])
        codex_parent = codex.get_adapters().parent
        self.assertEqual(codex_parent.capabilities()["busy_behavior"], "unknown")
        self.assertEqual(codex_parent.capabilities()["wake_after_turn"], "unknown")
        opencode_parent = opencode.get_adapters().parent
        opencode_capabilities = opencode_parent.capabilities()
        self.assertEqual(opencode_capabilities["notification_transport"], "opencode_prompt_http")
        self.assertEqual(opencode_capabilities["delivery"], "queue")
        self.assertFalse(opencode_capabilities["polling"])
        self.assertEqual(opencode_capabilities["busy_behavior"], "queue_after_turn")
        self.assertEqual(opencode_capabilities["wake_after_turn"], "verified")
        self.assertEqual(opencode_capabilities["resume"], "framework_default_schedules")
        opencode_child = opencode.get_adapters().child
        child_capabilities = opencode_child.capabilities()
        self.assertEqual(child_capabilities["delivery_transport"], "opencode_prompt_http")
        self.assertEqual(child_capabilities["delivery"], "queue")
        self.assertFalse(child_capabilities["polling"])
        self.assertEqual(child_capabilities["wake_child"], "native_injection")
        self.assertEqual(child_capabilities["busy_behavior"], "queue_after_turn")

    def test_old_module_paths_are_gone(self):
        for name in (
            "agent_link.adapter",
            "agent_link.codex",
            "agent_link.hosts.inbox",
            "agent_link.hosts.omp_notifier",
        ):
            with self.subTest(module=name):
                importlib.invalidate_caches()
                with self.assertRaises(ModuleNotFoundError):
                    importlib.import_module(name)


class CoreDispatchCase(unittest.TestCase):
    """Core dispatch must call the object returned by the adapter entry."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        isolate_registry(self)
        self.root = Path(self.temp.name) / "root"
        notifier = Path(self.temp.name) / "stub_notifier.py"
        notifier.write_text("def bridge(payload):\n    return {}\n", encoding="utf-8")
        self.store = Store(self.root)
        self.addCleanup(self.store.close)
        self.store.bind(
            {
                "binding_id": "b",
                "run_id": "r-b",
                "native_task_id": None,
                "parent": {"framework": "omp", "session_id": "parent"},
                "child": {"framework": "omp", "session_id": "child"},
                "notifier_path": str(notifier),
            }
        )

    def test_parent_dispatch_uses_entry_object(self):
        parent = RecordingParent()
        message = {"kind": "report", "message_id": "m-1", "text": "hello"}
        self.store.prepare_report("b", "m-1", "hello")
        with mock.patch(
            "agent_link.cli.load_adapters",
            return_value=HostAdapters(parent=parent, child=None),
        ):
            detail, error = cli._dispatch(self.store, "b", message)
        self.assertIsNone(error)
        self.assertEqual(detail, "recorded-parent")
        self.assertEqual(parent.calls, [message])
        row = self.store.get_message("b", "m-1")
        self.assertEqual(row["delivery_state"], "host_accepted")

    def test_child_dispatch_uses_entry_object(self):
        child = RecordingChild()
        message = {
            "kind": "ask_child",
            "request_id": "rq-1",
            "message_id": "m-2",
            "question": "?",
            "timeout_seconds": 5.0,
            "deadline_epoch": 0.0,
        }
        self.store.prepare_parent_ask("b", "rq-1", "m-2", "?", 5.0)
        with mock.patch(
            "agent_link.cli.load_adapters",
            return_value=HostAdapters(parent=None, child=child),
        ):
            detail, error = cli.dispatch_parent_request(self.store, "b", message)
        self.assertIsNone(error)
        self.assertEqual(detail, "recorded-child")
        self.assertEqual(child.calls, [message])
        row = self.store.get_parent_request("b", "rq-1")
        self.assertEqual(row["delivery_state"], "host_accepted")

    def test_missing_roles_fail_explicitly(self):
        self.store.prepare_report("b", "m-3", "x")
        with mock.patch(
            "agent_link.cli.load_adapters",
            return_value=HostAdapters(parent=None, child=None),
        ):
            detail, error = cli._dispatch(
                self.store, "b", {"kind": "report", "message_id": "m-3", "text": "x"}
            )
            self.assertIsNone(detail)
            self.assertIn("no parent adapter", error)
            self.assertEqual(
                self.store.get_message("b", "m-3")["delivery_state"], "failed_or_unknown"
            )
            self.store.prepare_parent_ask("b", "rq-3", "m-4", "?", 5.0)
            detail, error = cli.dispatch_parent_request(
                self.store,
                "b",
                {
                    "kind": "ask_child",
                    "request_id": "rq-3",
                    "message_id": "m-4",
                    "question": "?",
                    "timeout_seconds": 5.0,
                    "deadline_epoch": 0.0,
                },
            )
            self.assertIsNone(detail)
            self.assertIn("no child adapter", error)
            self.assertEqual(
                self.store.get_parent_request("b", "rq-3")["delivery_state"],
                "failed_or_unknown",
            )

    def test_binding_unknown_framework_fails_without_touching_transport(self):
        store = Store(self.root)
        try:
            store.bind(
                {
                    "binding_id": "b-unknown",
                    "run_id": "r-b-unknown",
                    "native_task_id": None,
                    "parent": {"framework": "made-up", "session_id": "parent"},
                    "child": {"framework": "omp", "session_id": "child"},
                    "notifier_path": str(self.root.parent / "stub_notifier.py"),
                }
            )
        finally:
            store.close()
        self.store.prepare_report("b-unknown", "m-unk", "x")
        detail, error = cli._dispatch(
            self.store, "b-unknown", {"kind": "report", "message_id": "m-unk", "text": "x"}
        )
        self.assertIsNone(detail)
        self.assertIn("unknown framework", error)
        self.assertEqual(
            self.store.get_message("b-unknown", "m-unk")["delivery_state"],
            "failed_or_unknown",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
