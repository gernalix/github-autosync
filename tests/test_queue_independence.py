import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import repo_single_writer as writer

class QueueIndependenceTests(unittest.TestCase):
    def comparison(self, commit, filename='one.txt', patch='@@ -1,2 +1,2 @@'):
        return {'total_commits': 1, 'commits': [{'sha': commit}], 'files': [{'filename': filename, 'status': 'modified', 'patch': patch}]}

    def reason(self, candidate, earlier):
        replies = [{'sha': 'main'}, candidate, {'state': 'open', 'merged_at': None, 'head': {'sha': 'a'}}, earlier]
        with mock.patch.object(writer, '_github_json', side_effect=replies):
            return writer._independence_reason('gernalix/example', 'b', 'main', [1])

    def test_independent_green_candidate_bypasses_failed_predecessor(self):
        self.assertIsNone(self.reason(self.comparison('b', 'b.txt'), self.comparison('a', 'a.txt')))

    def test_shared_unmerged_commit_blocks_stacked_branch(self):
        self.assertEqual('dependency-unmerged', self.reason(self.comparison('a', 'b.txt'), self.comparison('a', 'a.txt')))

    def test_same_document_disjoint_rules_are_independent(self):
        self.assertIsNone(self.reason(self.comparison('b', patch='@@ -100,2 +100,3 @@'), self.comparison('a')))

    def test_overlapping_semantic_context_blocks_bypass(self):
        self.assertEqual('queue-overlapping-change', self.reason(self.comparison('b'), self.comparison('a')))

    def test_missing_patch_and_truncated_evidence_fail_closed(self):
        self.assertEqual('independence-unverified', self.reason(self.comparison('b', patch=''), self.comparison('a')))
        candidate = self.comparison('b'); candidate['total_commits'] = 2
        self.assertEqual('independence-unverified', self.reason(candidate, self.comparison('a')))

    def test_explicit_dependency_must_be_merged_even_when_closed(self):
        with mock.patch.object(writer, '_github_json', return_value={'state': 'closed', 'merged_at': None}):
            self.assertEqual('dependency-unmerged', writer._dependency_reason('gernalix/example', 'Depends-On: #1'))
        with mock.patch.object(writer, '_github_json', return_value={'merged_at': 'now'}) as read:
            self.assertIsNone(writer._dependency_reason('gernalix/example', 'Depends-On: gernalix/other#12'))
            read.assert_called_once_with('repos/gernalix/other/pulls/12')

    def test_dependency_read_failure_and_invalid_reference_fail_closed(self):
        self.assertEqual('dependency-invalid', writer._dependency_reason('gernalix/example', 'Depends-On: unknown'))
        with mock.patch.object(writer, '_github_json', side_effect=ValueError):
            self.assertEqual('dependency-unverified', writer._dependency_reason('gernalix/example', 'Depends-On: #1'))

    def test_concurrent_writer_cannot_enter_same_repository_lock(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(writer, 'STATE_ROOT', Path(tmp)):
            with writer.RepoLock('gernalix/Example') as held:
                contender = writer.RepoLock('gernalix/example')
                self.assertEqual(held.path, contender.path)
                probe = subprocess.run(['python3', '-c', 'import fcntl,sys; f=open(sys.argv[1],"w"); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)', str(contender.path)], capture_output=True)
                self.assertNotEqual(0, probe.returncode)
            probe = subprocess.run(['python3', '-c', 'import fcntl,sys; f=open(sys.argv[1],"w"); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)', str(contender.path)], capture_output=True)
            self.assertEqual(0, probe.returncode)
