from __future__ import annotations
import unittest
from unittest import mock
import repo_integrator

class RetiredIntegratorTests(unittest.TestCase):
    def test_integration_queue_is_permanently_disabled(self):
        with (mock.patch.object(repo_integrator.repo_single_writer, "process_ready_prs") as queue,
              mock.patch.object(repo_integrator.repo_gc, "periodic") as gc,
              mock.patch.object(repo_integrator.autosync_core, "queue_merged_roadmap_completions") as roadmap):
            result = repo_integrator.run_once("gernalix")
            self.assertEqual("retired", result["status"])
            self.assertEqual(0, result["queue"]["found"])
            queue.assert_not_called()
            gc.assert_not_called()
            roadmap.assert_not_called()

if __name__ == "__main__":
    unittest.main()
