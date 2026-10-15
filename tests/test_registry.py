"""Registry tests: user-level binding index (bind writes, service refreshes, list reads)."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from agent_link.errors import ValidationError
from agent_link.registry import entry_from_row, list_bindings, registry_dir, write_entry
from tests import isolate_registry

REPO_ROOT = Path(__file__).resolve().parent.parent


def _row(**overrides):
    row = {
        "binding_id": "b-1",
        "run_id": "r-1",
        "native_task_id": None,
        "parent_framework": "omp",
        "parent_session_id": "parent-1",
        "child_framework": "pi",
        "child_session_id": "child-1",
        "config_digest": "d" * 64,
    }
    row.update(overrides)
    return row


class RegistryModuleCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / "registry"

    def test_write_entry_creates_private_entry(self):
        path = write_entry(entry_from_row("/tmp/root-x", _row()), directory=self.directory)
        self.assertTrue(path.is_file())
        entry = json.loads(path.read_text("utf-8"))
        self.assertEqual(entry["binding_id"], "b-1")
        self.assertEqual(entry["root"], str(Path("/tmp/root-x").resolve()))
        self.assertEqual(entry["parent"], {"framework": "omp", "session_id": "parent-1"})
        self.assertEqual(entry["child"], {"framework": "pi", "session_id": "child-1"})
        self.assertEqual(entry["version"], 1)
        self.assertEqual(entry["config_digest"], "d" * 64)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)

    def test_rewrite_preserves_created_at_and_moves_to_new_root(self):
        first = write_entry(entry_from_row("/tmp/root-x", _row()), directory=self.directory)
        first_created = json.loads(first.read_text("utf-8"))["created_at"]
        time.sleep(0.01)
        second = write_entry(entry_from_row("/tmp/root-y", _row()), directory=self.directory)
        entry = json.loads(second.read_text("utf-8"))
        self.assertEqual(entry["created_at"], first_created)
        self.assertGreaterEqual(entry["updated_at"], first_created)
        self.assertEqual(entry["root"], str(Path("/tmp/root-y").resolve()))

    def test_binding_id_validation_blocks_path_traversal(self):
        with self.assertRaises(ValidationError):
            write_entry(entry_from_row("/tmp/root-x", _row(binding_id="../escape")), directory=self.directory)
        self.assertFalse(self.directory.exists())

    def test_list_bindings_filters_and_skips_malformed(self):
        write_entry(entry_from_row("/tmp/root-x", _row()), directory=self.directory)
        write_entry(
            entry_from_row(
                "/tmp/root-x",
                _row(binding_id="b-2", parent_session_id="parent-2", child_session_id="child-2"),
            ),
            directory=self.directory,
        )
        (self.directory / "broken.json").write_text("{not json", encoding="utf-8")
        (self.directory / "note.txt").write_text("ignored", encoding="utf-8")
        self.assertEqual(len(list_bindings(directory=self.directory)), 2)
        by_parent = list_bindings(directory=self.directory, parent_session_id="parent-2")
        self.assertEqual([entry["binding_id"] for entry in by_parent], ["b-2"])
        by_both = list_bindings(
            directory=self.directory, child_session_id="child-1", parent_framework="omp"
        )
        self.assertEqual([entry["binding_id"] for entry in by_both], ["b-1"])
        self.assertEqual(list_bindings(directory=self.directory, parent_framework="codex-desktop"), [])

    def test_missing_directory_is_empty(self):
        self.assertEqual(list_bindings(directory=self.directory / "nope"), [])

    def test_env_override_selects_directory(self):
        with mock.patch.dict(os.environ, {"AGENT_LINK_REGISTRY": str(self.directory)}):
            self.assertEqual(registry_dir(), self.directory)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_LINK_REGISTRY", None)
            self.assertEqual(registry_dir(), Path.home() / ".agent-link" / "registry")


class RegistryCliCase(unittest.TestCase):
    """bind writes the registry; list-bindings reads it; the host service refreshes it."""

    def setUp(self):
        self.registry = Path(isolate_registry(self))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "runtime"
        self.notifier = Path(self.tmp.name) / "stub.py"
        self.notifier.write_text("def bridge(payload):\n    return {'accepted': True}\n", encoding="utf-8")

    def run_cli(self, *args):
        return subprocess.run(
            [sys.executable, "-m", "agent_link", "--root", str(self.root), *args],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            timeout=30,
        )

    def write_config(self, binding_id, parent_session="parent-1", child_session="child-1"):
        config = {
            "binding_id": binding_id,
            "run_id": "run-" + binding_id,
            "native_task_id": None,
            "parent": {"framework": "omp", "session_id": parent_session},
            "child": {"framework": "omp", "session_id": child_session},
            "notifier_path": str(self.notifier),
        }
        path = Path(self.tmp.name) / f"bind-{binding_id}.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return path

    def test_bind_writes_entry_and_list_bindings_filters(self):
        config = self.write_config("cli-b")
        proc = self.run_cli("bind", "--config", str(config))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        result = json.loads(proc.stdout)
        self.assertTrue(result["created"])
        registry_path = Path(result["registry"])
        self.assertEqual(registry_path.parent, self.registry)
        entry = json.loads(registry_path.read_text("utf-8"))
        self.assertEqual(entry["parent"], {"framework": "omp", "session_id": "parent-1"})
        self.assertEqual(entry["child"], {"framework": "omp", "session_id": "child-1"})
        self.assertEqual(entry["root"], str(self.root.resolve()))

        listing = self.run_cli("list-bindings", "--parent-session-id", "parent-1")
        self.assertEqual(listing.returncode, 0, listing.stderr)
        payload = json.loads(listing.stdout)
        self.assertEqual([item["binding_id"] for item in payload["bindings"]], ["cli-b"])
        self.assertEqual(payload["registry"], str(self.registry))
        other = self.run_cli("list-bindings", "--parent-session-id", "someone-else")
        self.assertEqual(json.loads(other.stdout)["bindings"], [])

    def test_bind_replay_refreshes_entry(self):
        config = self.write_config("cli-b")
        self.run_cli("bind", "--config", str(config))
        first = json.loads((self.registry / "cli-b.json").read_text("utf-8"))
        time.sleep(0.01)
        replay = self.run_cli("bind", "--config", str(config))
        self.assertTrue(json.loads(replay.stdout)["idempotent"])
        second = json.loads((self.registry / "cli-b.json").read_text("utf-8"))
        self.assertEqual(second["created_at"], first["created_at"])
        self.assertGreaterEqual(second["updated_at"], first["updated_at"])

    def test_host_service_regenerates_missing_entry(self):
        config = self.write_config("svc-b")
        self.run_cli("bind", "--config", str(config))
        (self.registry / "svc-b.json").unlink()
        credentials = Path(self.tmp.name) / "creds.json"
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agent_link.host",
                "--root",
                str(self.root),
                "--binding",
                "svc-b",
                "--credentials",
                str(credentials),
                "--framework",
                "omp",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=REPO_ROOT,
        )
        watchdog = threading.Timer(30, proc.kill)
        watchdog.start()
        try:
            ready = json.loads(proc.stdout.readline())
            self.assertTrue(ready["ready"])
            self.assertTrue((self.registry / "svc-b.json").is_file())
            entry = json.loads((self.registry / "svc-b.json").read_text("utf-8"))
            self.assertEqual(entry["parent"]["session_id"], "parent-1")
        finally:
            watchdog.cancel()
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=10)
            proc.stdout.close()
            proc.stderr.close()
        # The service removes its own credentials on shutdown.
        self.assertFalse(credentials.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
