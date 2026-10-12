import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import command_catalog_bridge as bridge


class CommandCatalogBridgeTests(unittest.TestCase):
    def manifest(self, root: Path) -> None:
        path = root / ".megavault/commands-v1.json"
        path.parent.mkdir(parents=True)
        path.write_text('{"project_id":92}\n', encoding="utf-8")

    def result(self, code: int = 0, payload: dict | None = None):
        return mock.Mock(returncode=code, stdout=json.dumps(payload or {"status": "PASS"}), stderr="")

    def test_absent_manifest_is_not_applicable(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual("not-applicable", bridge.prepare(Path(tmp), "owner/repo", "a" * 40)["status"])

    @mock.patch("command_catalog_bridge._run")
    def test_prepare_uses_relative_manifest_and_durable_outbox(self, run):
        run.return_value = self.result()
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as state:
            root = Path(tmp); self.manifest(root)
            result = bridge.prepare(root, "owner/repo", "a" * 40, state_root=Path(state))
            self.assertEqual("ready", result["status"])
            self.assertEqual(92, result["project_id"])
            self.assertTrue(result["outbox"].endswith("owner_repo/" + "a" * 40))
            self.assertIn(".megavault/commands-v1.json", run.call_args_list[0].args)
            self.assertIn(".megavault/commands-v1.json", run.call_args_list[1].args)

    @mock.patch("command_catalog_bridge._run")
    def test_validation_failure_blocks_before_emit(self, run):
        run.return_value = self.result(2)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); self.manifest(root)
            result = bridge.prepare(root, "owner/repo", "b" * 40)
            self.assertEqual({"status": "blocked", "reason": "manifest-validation-failed"}, result)
            self.assertEqual(1, run.call_count)

    @mock.patch("command_catalog_bridge._run_version")
    @mock.patch("command_catalog_bridge._run")
    def test_publish_ingests_reconciles_and_registers_version(self, run, run_version):
        run.side_effect = [self.result(), self.result(payload={"status": "PASS", "commands": []})]
        run_version.return_value = self.result(payload={"versione": 2})
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as out:
            root = Path(tmp); self.manifest(root)
            Path(out, "CE-one.json").write_text("{}", encoding="utf-8")
            result = bridge.publish({"status": "ready", "worktree": str(root), "outbox": out, "events": 1, "project_id": 92, "merge_sha": "c" * 40})
            self.assertEqual("pass", result["status"])
            self.assertEqual("ingest", run.call_args_list[0].args[2])
            self.assertEqual("reconcile", run.call_args_list[1].args[2])
            run_version.assert_called_once()

    @mock.patch("command_catalog_bridge._run")
    def test_publish_preserves_outbox_on_ingest_failure(self, run):
        run.return_value = self.result(1)
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as out:
            root = Path(tmp); self.manifest(root)
            result = bridge.publish({"status": "ready", "worktree": str(root), "outbox": out, "events": 1, "project_id": 92, "merge_sha": "d" * 40})
            self.assertEqual("pending", result["status"])
            self.assertEqual("event-ingest-failed", result["reason"])


if __name__ == "__main__":
    unittest.main()
