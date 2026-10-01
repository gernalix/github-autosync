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
                with patch.object(gc,'sweep',return_value={'checked':0}) as sweep, patch.object(gc,'sweep_legacy_c3',return_value={'checked':0}):
                    self.assertEqual({'checked':0,'legacy_c3':{'checked':0}},gc.periodic())
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
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(writer, 'STATE_ROOT', root/'state'), patch.object(writer, 'WORKTREE_ROOT', root/'worktrees'), patch.object(gc, 'C3_DB', root/'c3.sqlite'):
                repo, remote, worktree, payload=self.fixture(root)
                payload['roadmap_prompt_id']='123456'
                with closing(sqlite3.connect(gc.C3_DB)) as db:
                    db.executescript("CREATE TABLE work_items(work_item_id,status,prompt_id); CREATE TABLE work_item_runs(work_item_id,worker_ref,metadata_json,state); CREATE TABLE work_item_checkpoints(work_item_id,next_action); CREATE TABLE work_item_execution_specs(work_item_id,worktree);")
                    db.execute("INSERT INTO work_items VALUES('prompt:123456','running','123456')")
                    db.commit()
                    self.assertFalse(gc.c3_allows(payload))
                    db.execute("UPDATE work_items SET status='completed'")
                    db.execute("INSERT INTO work_item_runs VALUES('prompt:123456','worker','{}','recovering')")
                    db.commit()
                    self.assertFalse(gc.c3_allows(payload))
                    db.execute('DELETE FROM work_item_runs')
                    db.execute("INSERT INTO work_item_checkpoints VALUES('prompt:123456','recover')")
                    db.commit()
                    self.assertFalse(gc.c3_allows(payload))
                    db.execute('DELETE FROM work_item_checkpoints')
                    db.commit()
                    self.assertTrue(gc.c3_allows(payload))
                (worktree/'new.txt').write_text('unintegrated')
                fixtures.git(['add','new.txt'],worktree)
                fixtures.git(['commit','-m','unintegrated'],worktree)
                self.assertEqual('changed-after-merge',gc.collect_record(payload)['reason'])
                payload.pop('integrated_head')
                self.assertEqual('unintegrated-tip',gc.collect_record(payload)['reason'])
                self.assertTrue(worktree.exists())

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
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            marker=root/'retired.json'
            marker.write_text('{}')
            os.utime(marker,(time.time()+1000,time.time()+1000))
            with patch.object(writer,'STATE_ROOT',root/'state'), patch.object(writer,'WORKTREE_ROOT',root/'worktrees'), patch.object(gc,'C3_DB',root/'c3.sqlite'), patch.object(gc,'RETIREMENT_MARKER',marker), patch.object(gc,'_legacy_path',return_value=True):
                repo,remote,worktree,payload=self.fixture(root)
                with closing(sqlite3.connect(gc.C3_DB)) as db:
                    db.executescript("CREATE TABLE meta(key,value); INSERT INTO meta VALUES('pre_migration_execution_retired','yes'); CREATE TABLE work_items(work_item_id,status,prompt_id); CREATE TABLE work_item_execution_specs(work_item_id,worktree); CREATE TABLE work_item_checkpoints(work_item_id,next_action,source_commit); CREATE TABLE work_item_runs(work_item_id,worker_ref,metadata_json,state);")
                    db.commit()
                    with patch.object(gc,'C3_REPO',repo):
                        (worktree/'dirty.txt').write_text('preserve')
                        self.assertEqual('dirty-or-conflicted',gc.sweep_legacy_c3(dry_run=True)['results'][0]['reason'])
                        (worktree/'dirty.txt').unlink()
                        db.execute("INSERT INTO work_items VALUES('wi:prepared','pending',NULL)")
                        db.execute('INSERT INTO work_item_execution_specs VALUES(?,?)',('wi:prepared',str(worktree)))
                        db.commit()
                        self.assertEqual('nonterminal-active-or-recovery',gc.sweep_legacy_c3(dry_run=True)['results'][0]['reason'])
                        db.execute("UPDATE work_items SET status='completed'")
                        db.commit()
                        before=db.total_changes
                        planned=gc.sweep_legacy_c3(dry_run=True)['results'][0]
                        self.assertEqual(['worktree','remote-branch','local-branch'],planned['removed'])
                        self.assertTrue(worktree.exists())
                        result=gc.sweep_legacy_c3()['results'][0]
                        self.assertEqual(planned,result)
                        self.assertEqual(before,db.total_changes)
                        self.assertFalse(worktree.exists())

    def test_app_managed_paths_are_never_legacy_gc_targets(self):
        self.assertFalse(gc._legacy_path(Path.home()/'.codex/worktrees/1234/codex-roadmap'))
        self.assertFalse(gc._legacy_path(Path.home()/'.local/share/chatgpt-rdc-supervisor/source'))
        self.assertTrue(gc._legacy_path(Path.home()/'.local/share/c2-supervisor/worktrees/123456'))
