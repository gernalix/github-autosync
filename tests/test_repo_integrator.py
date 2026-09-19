from __future__ import annotations
import unittest
from unittest import mock
import repo_integrator

class RepoIntegratorTests(unittest.TestCase):
    def test_pending_checks_are_not_hard_blockers(self) -> None:
        queue={"found":1,"merged":0,"deferred":1,"results":[{"repo":"gernalix/example","number":1,"status":"deferred","reason":"checks-pending"}]}
        with (
            mock.patch.object(repo_integrator.repo_single_writer,"process_ready_prs",return_value=queue),
            mock.patch.object(repo_integrator.autosync_core,"queue_merged_roadmap_completions",return_value={"queued":0}),
        ):
            result=repo_integrator.run_once("gernalix")
        self.assertEqual("ok",result["status"])
        self.assertEqual([],result["hard_blockers"])

    def test_semantic_conflict_is_hard_blocker(self) -> None:
        queue={"found":1,"merged":0,"deferred":1,"results":[{"repo":"gernalix/example","number":1,"status":"deferred","reason":"semantic-conflict"}]}
        with (
            mock.patch.object(repo_integrator.repo_single_writer,"process_ready_prs",return_value=queue),
            mock.patch.object(repo_integrator.autosync_core,"queue_merged_roadmap_completions",return_value={"queued":0}),
        ):
            result=repo_integrator.run_once("gernalix")
        self.assertEqual("blocked",result["status"])
        self.assertEqual("semantic-conflict",result["hard_blockers"][0]["reason"])

if __name__ == "__main__":
    unittest.main()
