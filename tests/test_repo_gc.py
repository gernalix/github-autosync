from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import os
import time
import unittest
from unittest.mock import patch

import repo_gc as gc
import repo_single_writer as writer
import test_repo_single_writer as fixtures


class SafeGcTests(unittest.TestCase):
    def test_missing_repository_is_preserved_and_periodic_runs_are_coalesced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(writer, 'STATE_ROOT', root/'state'), patch.object(writer, 'WORKTREE_ROOT', root/'worktrees'), patch.object(gc, 'C3_DB', root/'missing.sqlite'):
                payload={'status':'merged','task_id':'example','repo_path':str(root/'absent'),
                         'branch':'task/example','worktree':str(root/'worktrees'/'example')}
                self.assertEqual('repository-unavailable',gc.collect_record(payload)['reason'])
                with patch.object(gc,'sweep',return_value={'checked':0}) as sweep, patch.object(gc,'sweep_legacy_c3',return_value={'checked':0}), patch.object(gc,'sweep_orphan_c3',return_value={'checked':0}):
                    self.assertEqual({'checked':0,'legacy_c3':{'checked':0},'orphan_c3':{'checked':0}},gc.periodic())
                    self.assertEqual({'status':'not-due'},gc.periodic())
                    sweep.assert_called_once_with()

    def fixture(self, root):
        repo, remote = fixtures.SingleWriterTests().make_repo(root / 'git')
        payload = writer.start_task(repo, 'gc-example')
        worktree = Path(payload['worktree'])
        (worktree / 'integrated.txt').write_text('integrated\n')
        fixtures.git(['add', 'integrated.txt'], worktree)
        self.assertEqual(0, fixtures.git(['commit', '-m', 'feature'], worktree).returncode)
        head = fixtures.git(['rev-parse', 'HEAD'], worktree).stdout.strip()
        self.assertEqual(0, fixtures.git(['push', 'origin', 'HEAD:main', 'HEAD:' + payload['branch']], worktree).returncode)
        fixtures.git(['fetch', 'origin', 'main'], repo)
        fixtures.git(['merge', '--ff-only', 'origin/main'], repo)
        payload.update(status='merged', integrated_head=head, merge_sha=head)
        writer._atomic_json(writer._task_record(repo, 'gc-example'), payload)
        return repo, remote, worktree, payload

    def test_dirty_and_changed_work_preserve_all_refs_then_clean_collection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(writer, 'STATE_ROOT', root/'state'), patch.object(writer, 'WORKTREE_ROOT', root/'worktrees'), patch.object(gc, 'C3_DB', root/'missing.sqlite'):
                repo, remote, worktree, payload = self.fixture(root)
                (worktree/'dirty.txt').write_text('preserve')
                self.assertEqual('dirty-changed-or-conflicted', gc.collect_record(payload)['reason'])
                self.assertTrue(worktree.exists())
                self.assertEqual(0, fixtures.git(['show-ref', '--verify', 'refs/heads/'+payload['branch']], remote).returncode)
                (worktree/'dirty.txt').unlink()
                planned=gc.collect_record(payload, dry_run=True)
                self.assertEqual(['worktree','remote-branch','local-branch'], planned['removed'])
                self.assertTrue(worktree.exists())
                result=gc.collect_record(payload)
                self.assertEqual(planned, result)
                self.assertFalse(worktree.exists())
                self.assertNotEqual(0, fixtures.git(['show-ref', '--verify', 'refs/heads/'+payload['branch']], remote).returncode)
                self.assertNotEqual(0, fixtures.git(['show-ref', '--verify', 'refs/heads/'+payload['branch']], repo).returncode)

    def test_active_recovery_and_unintegrated_are_fail_closed(self):
        with patch.object(gc.sqlite3, "connect", side_effect=AssertionError("C3 database opened")):
            self.assertFalse(gc.c3_allows({"task_id":"123456", "roadmap_prompt_id":"123456"}))
            self.assertFalse(gc.c3_allows({"task_id":"new", "worktree":"/home/daniele/.local/share/c3-symphony/workspaces/GH-4"}))
            self.assertTrue(gc.c3_allows({"task_id":"new-project-issue", "worktree":"/tmp/project-task"}))

    def test_remote_only_merged_branch_is_collected_with_expected_tip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(writer, 'STATE_ROOT', root/'state'), patch.object(writer, 'WORKTREE_ROOT', root/'worktrees'), patch.object(gc, 'C3_DB', root/'missing.sqlite'):
                repo, remote, worktree, payload=self.fixture(root)
                self.assertEqual(0,fixtures.git(['worktree','remove',str(worktree)],repo).returncode)
                self.assertEqual(0,fixtures.git(['branch','-d',payload['branch']],repo).returncode)
                self.assertEqual({'removed':['remote-branch']},gc.collect_record(payload))
                self.assertNotEqual(0,fixtures.git(['show-ref','--verify','refs/heads/'+payload['branch']],remote).returncode)

    def test_legacy_c3_requires_retirement_terminal_ownership_clean_and_integrated(self):
        with patch.object(gc.sqlite3, "connect", side_effect=AssertionError("C3 archive opened")), patch.object(writer, "_git", side_effect=AssertionError("historical Git mutated")):
            self.assertIn("frozen", gc.sweep_legacy_c3()["reason"])

    def test_app_managed_paths_are_never_legacy_gc_targets(self):
        self.assertFalse(gc._legacy_path(Path.home()/'.codex/worktrees/1234/codex-roadmap'))
        self.assertFalse(gc._legacy_path(Path.home()/'.local/share/chatgpt-rdc-supervisor/source'))
        self.assertTrue(gc._legacy_path(Path.home()/'.local/share/c2-supervisor/worktrees/123456'))

    def test_orphan_gc_requires_positive_terminal_owner_and_preserves_unknown_refs(self):
        with patch.object(gc.sqlite3, "connect", side_effect=AssertionError("C3 archive opened")), patch.object(writer, "_git", side_effect=AssertionError("historical Git mutated")):
            self.assertIn("frozen", gc.sweep_orphan_c3()["reason"])
