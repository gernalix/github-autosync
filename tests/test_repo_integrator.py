from __future__ import annotations
import tempfile
import unittest
from unittest import mock
import repo_integrator

class RepoIntegratorTests(unittest.TestCase):
    def setUp(self):
        self.gc = mock.patch.object(repo_integrator.repo_gc, 'periodic', return_value={'checked': 0})
        self.gc.start()
        self.addCleanup(self.gc.stop)

    def test_pending_checks_are_not_hard_blockers(self) -> None:
        queue={"found":1,"merged":0,"deferred":1,"results":[{"repo":"gernalix/example","number":1,"status":"deferred","reason":"checks-pending"}]}
        with (
            mock.patch.object(repo_integrator.repo_single_writer,"process_ready_prs",return_value=queue),
            mock.patch.object(repo_integrator.autosync_core,"queue_merged_roadmap_completions",return_value={"queued":0}),
        ):
            result=repo_integrator.run_once("gernalix")
        self.assertEqual("ok",result["status"])
        self.assertEqual([],result["hard_blockers"])

    def test_semantic_conflict_is_isolated_hard_blocker(self) -> None:
        queue={"found":1,"merged":0,"deferred":1,"results":[{"repo":"gernalix/example","number":1,"status":"deferred","reason":"semantic-conflict"}]}
        with (
            mock.patch.object(repo_integrator.repo_single_writer,"process_ready_prs",return_value=queue),
            mock.patch.object(repo_integrator.autosync_core,"queue_merged_roadmap_completions",return_value={"queued":0}),
        ):
            result=repo_integrator.run_once("gernalix")
        self.assertEqual("partial",result["status"])
        self.assertEqual("semantic-conflict",result["hard_blockers"][0]["reason"])

    def test_checks_failure_does_not_fail_service_or_stop_roadmap_finalization(self) -> None:
        queue={"found":2,"merged":1,"deferred":1,"results":[
            {"repo":"gernalix/adb-device-keeper","number":2,"status":"deferred","reason":"checks-failed"},
            {"repo":"gernalix/MegaVault","number":10,"status":"merged"},
        ]}
        roadmap={"queued":4,"deferred":0,"prompt_ids":["613102","624831","817056","925731"],"failed":[]}
        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(repo_integrator.repo_single_writer,"process_ready_prs",return_value=queue),
            mock.patch.object(repo_integrator.autosync_core,"queue_merged_roadmap_completions",return_value=roadmap) as finalize,
        ):
            result=repo_integrator.run_once("gernalix")
            with mock.patch.object(repo_integrator,"run_once",return_value=result):
                exit_code=repo_integrator.main(["--owner","gernalix","--state-root",tmp,"--json"])
        finalize.assert_called_once_with()
        self.assertEqual("partial",result["status"])
        self.assertEqual(roadmap,result["roadmap_finalization"])
        self.assertEqual([{"repo":"gernalix/adb-device-keeper","number":2,"reason":"checks-failed"}],result["hard_blockers"])
        self.assertEqual(0,exit_code)

if __name__ == "__main__":
    unittest.main()
