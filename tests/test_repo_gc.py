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

    def test_ignored_artifact_is_preserved_even_on_merged_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(writer, 'STATE_ROOT', root/'state'), patch.object(writer, 'WORKTREE_ROOT', root/'worktrees'):
                repo, remote, worktree, payload=self.fixture(root)
                # Git-private exclude configuration is not application work.
                exclude=Path(fixtures.git(['rev-parse','--git-path','info/exclude'],repo).stdout.strip())
                if not exclude.is_absolute():exclude=repo/exclude
                with exclude.open('a') as stream:stream.write('only-copy.db\n')
                artifact=worktree/'only-copy.db';artifact.write_bytes(b'unique recoverable data')
                self.assertEqual('ignored-artifacts-preserved',gc.collect_record(payload)['reason'])
                self.assertEqual(artifact.read_bytes(),b'unique recoverable data')
                self.assertEqual(0,fixtures.git(['show-ref','--verify','refs/heads/'+payload['branch']],remote).returncode)

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

class ReviewedArchiveTests(unittest.TestCase):
    fixture=SafeGcTests.fixture
    def test_stale_queued_owner_requires_exact_already_merged_receipt_and_catalog(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(writer,'STATE_ROOT',root/'state'),patch.object(writer,'WORKTREE_ROOT',root/'worktrees'):
                repo,remote,tree,payload=self.fixture(root)
                payload.update(status='queued',pr_number=1)
                record=writer._task_record(repo,payload['task_id']);writer._atomic_json(record,payload)
                info={'state':'MERGED','headRefName':payload['branch'],'headRefOid':payload['integrated_head'],'baseRefName':'main','mergeCommit':{'oid':payload['merge_sha']}}
                original=writer.run
                def response(argv,*args,**kwargs):
                    if argv[:3]==['gh','pr','view']:
                        import subprocess
                        return subprocess.CompletedProcess(argv,0,json.dumps(info),'')
                    return original(argv,*args,**kwargs)
                with patch.object(writer,'run',side_effect=response),patch.object(writer.command_catalog_bridge,'prepare',return_value={'status':'not-applicable'}) as prepare:
                    info['state']='OPEN'
                    self.assertEqual(writer.recover_merged_task(payload['repo'],payload['branch'],scope_guard=lambda *a:None)['status'],'preserved');prepare.assert_not_called()
                    info['state']='MERGED';info['headRefName']='task/another'
                    self.assertEqual(writer.recover_merged_task(payload['repo'],payload['branch'],scope_guard=lambda *a:None)['status'],'preserved');prepare.assert_not_called()
                    info['headRefName']=payload['branch'];(tree/'unpublished').write_text('retain')
                    self.assertEqual(writer.recover_merged_task(payload['repo'],payload['branch'],scope_guard=lambda *a:None)['status'],'preserved');prepare.assert_not_called()
                    (tree/'unpublished').unlink()
                    with patch.object(writer.command_catalog_bridge,'publish',return_value={'status':'pending'}):
                        blocked=writer.recover_merged_task(payload['repo'],payload['branch'],scope_guard=lambda *a:None)
                        self.assertEqual(blocked['reason'],'merged-catalog-not-verified')
                        self.assertEqual(json.loads(record.read_text())['status'],'queued')
                    result=writer.recover_merged_task(payload['repo'],payload['branch'],scope_guard=lambda *a:None)
                self.assertEqual(result['status'],'merged');self.assertEqual(json.loads(record.read_text())['status'],'merged')
                self.assertEqual(writer._ok(repo,'branch','--format=%(refname:short)'),'main')

    def test_cleanup_checks_integration_against_fresh_canonical_tracking(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(writer,'STATE_ROOT',root/'state'),patch.object(writer,'WORKTREE_ROOT',root/'worktrees'):
                repo,remote,tree,payload=self.fixture(root)
                fixtures.git(['worktree','remove',str(tree)],repo)
                old=writer._ok(repo,'rev-parse','origin/main^')
                fixtures.git(['update-ref','refs/remotes/origin/main',old],repo)
                result=writer.cleanup_terminal_task(payload['repo'],payload['branch'],expected_head=payload['integrated_head'],canonical_sha=payload['merge_sha'],scope_guard=lambda *args:None,preservation_chat_id='01a15d5d-1567-7b20-915f-d9e72f0af83a')
                self.assertEqual(result['removed'],['remote-branch','local-branch'])
                self.assertEqual(writer._ok(repo,'rev-parse','origin/main'),payload['merge_sha'])

    def test_missing_historical_checkout_keeps_verified_bundle_before_ref_deletion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(writer,'STATE_ROOT',root/'state'),patch.object(writer,'WORKTREE_ROOT',root/'worktrees'):
                repo,remote,tree,payload=self.fixture(root)
                payload['roadmap_prompt_id']='123456'
                writer._atomic_json(writer._task_record(repo,payload['task_id']),payload)
                fixtures.git(['worktree','remove',str(tree)],repo)
                self.assertEqual(gc.collect_record(payload)['removed'],[])
                calls=[]
                with patch.object(gc.sqlite3,'connect',side_effect=AssertionError('historical DB must stay frozen')):
                    result=writer.cleanup_terminal_task(payload['repo'],payload['branch'],
                        expected_head=payload['integrated_head'],canonical_sha=payload['merge_sha'],
                        scope_guard=lambda *args:calls.append(args),
                        preservation_chat_id='01a15d5d-1567-7b20-915f-d9e72f0af83a')
                self.assertEqual(result['removed'],['remote-branch','local-branch'])
                self.assertTrue(calls)
                self.assertTrue(gc.preserved_integrated_history(payload,payload['integrated_head'],'origin/main'))
                bundles=list((root/'state'/'preserved-refs').glob('*/*/commits.bundle'))
                self.assertEqual(len(bundles),1)
                self.assertEqual(bundles[0].stat().st_mode&0o777,0o600)
                bundles[0].unlink()
                self.assertFalse(gc.preserved_integrated_history(payload,payload['integrated_head'],'origin/main'))

    def review(self,root,repo,tree,payload,disposition='integrated'):
        import hashlib
        head=writer._ok(tree,'rev-parse','HEAD')
        status=writer._git(tree,'status','--porcelain=v1','-z','--untracked-files=all').stdout
        value={'worktree':str(tree),'expected_head':head,'canonical_sha':writer._ok(repo,'rev-parse','origin/main'),
               'status_sha256':hashlib.sha256(status.encode()).hexdigest(),'disposition':disposition,
               'replacement_commit':writer._ok(repo,'rev-parse','origin/main'),
               'chat_id':'01a145f5-8e5c-7842-bd33-97f757614c7d',
               'index_sha256':hashlib.sha256(writer._git(tree,'diff','--cached','--binary','--full-index','HEAD').stdout.encode()).hexdigest(),
               'worktree_fingerprint':hashlib.sha256(json.dumps(gc.worktree_snapshot(tree),sort_keys=True).encode()).hexdigest(),
               'inactive_confirmation':'User reports no active chats; reviewed completed work'}
        p=root/'review.json';p.write_text(json.dumps(value));return p

    def test_complete_dirty_ignored_index_and_symlink_preservation_and_rerun(self):
        import tarfile
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(writer,'STATE_ROOT',root/'state'),patch.object(writer,'WORKTREE_ROOT',root/'worktrees'):
                repo,remote,tree,payload=self.fixture(root)
                (tree/'integrated.txt').write_text('staged contribution')
                fixtures.git(['add','integrated.txt'],tree)
                (tree/'integrated.txt').write_text('local contribution')
                exclude=Path(writer._ok(repo,'rev-parse','--git-path','info/exclude'))
                if not exclude.is_absolute():exclude=repo/exclude
                with exclude.open('a') as stream:stream.write('only-copy.db\n')
                (tree/'only-copy.db').write_bytes(b'private ignored original')
                os.link(tree/'only-copy.db',tree/'hardlinked-original')
                external=root/'external';external.write_text('outside tree')
                (tree/'link').symlink_to(external)
                review=self.review(root,repo,tree,payload)
                result=gc.archive_reviewed_worktree(repo,review)
                self.assertEqual(result['status'],'archived');self.assertFalse(tree.exists());self.assertTrue(external.exists())
                self.assertEqual((Path(result['original'])/'only-copy.db').read_bytes(),b'private ignored original')
                archive=Path(result['preservation'])
                with tarfile.open(archive/'contents.tar.gz') as tar:
                    self.assertEqual(tar.extractfile('only-copy.db').read(),b'private ignored original')
                    self.assertEqual(tar.extractfile('integrated.txt').read(),b'local contribution')
                    self.assertTrue(tar.getmember('link').issym())
                self.assertIn(b'staged contribution',(archive/'index.patch').read_bytes())
                self.assertEqual(gc.archive_reviewed_worktree(repo,review),result)
                self.assertEqual(0,fixtures.git(['show-ref','--verify','refs/heads/'+payload['branch']],remote).returncode)

    def test_reviewed_patch_equivalent_collection_preserves_originals_and_remote_tip_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(writer,'STATE_ROOT',root/'state'),patch.object(writer,'WORKTREE_ROOT',root/'worktrees'):
                repo,remote,tree,payload=self.fixture(root)
                # Simulate the original checkout surviving an integrator rebase.
                self.assertEqual(0,fixtures.git(['commit','--amend','-m','equivalent original'],tree).returncode)
                tip=writer._ok(tree,'rev-parse','HEAD')
                self.assertNotEqual(tip,payload['integrated_head'])
                review=self.review(root,repo,tree,payload,'patch-equivalent')
                archived=gc.archive_reviewed_worktree(repo,review)
                self.assertTrue(gc.preserved_patch_equivalent(payload,tip,'origin/main'))
                result=gc.collect_record(payload)
                self.assertEqual(result,{'removed':['remote-branch','local-branch']})
                self.assertTrue((Path(archived['original'])/'integrated.txt').is_file())
                self.assertTrue((Path(archived['preservation'])/'commits.bundle').is_file())
                self.assertNotEqual(0,fixtures.git(['show-ref','--verify','refs/heads/'+payload['branch']],remote).returncode)

    def test_archived_equivalent_tip_does_not_authorize_changed_branch_or_missing_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(writer,'STATE_ROOT',root/'state'),patch.object(writer,'WORKTREE_ROOT',root/'worktrees'):
                repo,remote,tree,payload=self.fixture(root)
                fixtures.git(['commit','--amend','-m','equivalent original'],tree)
                tip=writer._ok(tree,'rev-parse','HEAD')
                archived=gc.archive_reviewed_worktree(repo,self.review(root,repo,tree,payload,'patch-equivalent'))
                bundle=Path(archived['preservation'])/'commits.bundle'
                original=bundle.read_bytes();bundle.write_bytes(b'corrupt')
                self.assertFalse(gc.preserved_patch_equivalent(payload,tip,'origin/main'))
                self.assertEqual(gc.collect_record(payload)['removed'],[])
                bundle.write_bytes(original)
                self.assertTrue(gc.preserved_patch_equivalent(payload,tip,'origin/main'))
                self.assertFalse(gc.preserved_patch_equivalent(payload,payload['integrated_head'],'origin/main'))
                ancestor=writer._ok(repo,'rev-parse','origin/main^')
                fixtures.git(['update-ref','refs/heads/'+payload['branch'],ancestor],remote)
                blocked=gc.collect_record(payload)
                self.assertEqual(blocked['reason'],'remote-changed-or-active')
                self.assertEqual(blocked['removed'],[])
                self.assertEqual(writer._ok(repo,'rev-parse',payload['branch']),tip)
                fixtures.git(['update-ref','refs/heads/'+payload['branch'],payload['integrated_head']],remote)
                # A newly recreated task directory also invalidates the archival receipt.
                tree.mkdir();self.assertFalse(gc.preserved_patch_equivalent(payload,tip,'origin/main'))

    def test_explicit_terminal_history_review_preserves_dirty_originals_and_removes_refs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(writer,'STATE_ROOT',root/'state'),patch.object(writer,'WORKTREE_ROOT',root/'worktrees'):
                repo,remote,tree,payload=self.fixture(root)
                payload.update(status='superseded',roadmap_prompt_id='123456',terminal_evidence='Already superseded prototype')
                record=writer._task_record(repo,payload['task_id']);writer._atomic_json(record,payload)
                (tree/'unpublished.txt').write_text('preserve unpublished prototype')
                review=self.review(root,repo,tree,payload)
                with self.assertRaisesRegex(ValueError,'active or frozen'):
                    gc.archive_reviewed_worktree(repo,review,allow_reviewed_terminal_history=True)
                with patch.object(gc.sqlite3,'connect',side_effect=AssertionError('historical DB must stay frozen')):
                    receipt=gc.archive_reviewed_worktree(repo,review,scope_guard=lambda *args:None,allow_reviewed_terminal_history=True)
                    result=writer.cleanup_terminal_task(payload['repo'],payload['branch'],expected_head=payload['integrated_head'],canonical_sha=payload['merge_sha'],scope_guard=lambda *args:None,preservation_chat_id='01a15d5d-1567-7b20-915f-d9e72f0af83a')
                self.assertEqual(result['removed'],['remote-branch','local-branch'])
                self.assertEqual((Path(receipt['original'])/'unpublished.txt').read_text(),'preserve unpublished prototype')
                self.assertEqual(json.loads(record.read_text())['status'],'superseded')

    def test_reviewed_frozen_patch_equivalent_tip_is_collected_without_recapturing_as_ancestor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(writer,'STATE_ROOT',root/'state'),patch.object(writer,'WORKTREE_ROOT',root/'worktrees'):
                repo,remote,tree,payload=self.fixture(root)
                fixtures.git(['commit','--amend','-m','equivalent original'],tree)
                tip=writer._ok(tree,'rev-parse','HEAD');payload['roadmap_prompt_id']='123456'
                writer._atomic_json(writer._task_record(repo,payload['task_id']),payload)
                review=self.review(root,repo,tree,payload,'patch-equivalent')
                gc.archive_reviewed_worktree(repo,review,scope_guard=lambda *args:None,allow_reviewed_terminal_history=True)
                with patch.object(gc.sqlite3,'connect',side_effect=AssertionError('historical DB must stay frozen')):
                    result=writer.cleanup_terminal_task(payload['repo'],payload['branch'],expected_head=tip,canonical_sha=payload['merge_sha'],scope_guard=lambda *args:None,preservation_chat_id='01a15d5d-1567-7b20-915f-d9e72f0af83a')
                self.assertEqual(result['removed'],['remote-branch','local-branch'])

    def test_active_and_frozen_owners_remain_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(writer,'STATE_ROOT',root/'state'),patch.object(writer,'WORKTREE_ROOT',root/'worktrees'):
                repo,remote,tree,payload=self.fixture(root);review=self.review(root,repo,tree,payload)
                for status,roadmap in [('active',None),('queued',None),('merged','123456')]:
                    payload.update(status=status,roadmap_prompt_id=roadmap)
                    writer._atomic_json(writer._task_record(repo,payload['task_id']),payload)
                    with self.assertRaisesRegex(ValueError,'active or frozen'):gc.archive_reviewed_worktree(repo,review)
                    self.assertTrue(tree.exists())

    def test_explicit_integrated_history_preserves_original_then_collects_only_refs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(writer,'STATE_ROOT',root/'state'),patch.object(writer,'WORKTREE_ROOT',root/'worktrees'):
                repo,remote,tree,payload=self.fixture(root)
                payload['roadmap_prompt_id']='123456'
                writer._atomic_json(writer._task_record(repo,payload['task_id']),payload)
                review=self.review(root,repo,tree,payload)
                with patch.object(gc.sqlite3,'connect',side_effect=AssertionError('historical DB must stay frozen')):
                    receipt=gc.archive_reviewed_worktree(repo,review,allow_integrated_history=True)
                    tip=payload['integrated_head']
                    self.assertTrue(gc.preserved_integrated_history(payload,tip,'origin/main'))
                    self.assertEqual(gc.collect_record(payload)['removed'],['remote-branch','local-branch'])
                self.assertEqual(writer._ok(repo,'branch','--format=%(refname:short)'),'main')
                self.assertTrue((Path(receipt['original'])/'integrated.txt').is_file())
                self.assertFalse(gc.preserved_integrated_history(payload,'0'*40,'origin/main'))
                (Path(receipt['preservation'])/'commits.bundle').unlink()
                self.assertFalse(gc.preserved_integrated_history(payload,tip,'origin/main'))

    def test_superseded_integrated_history_requires_positive_terminal_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(writer,'STATE_ROOT',root/'state'),patch.object(writer,'WORKTREE_ROOT',root/'worktrees'):
                repo,remote,tree,payload=self.fixture(root)
                payload.update(status='superseded',roadmap_prompt_id='123456')
                record=writer._task_record(repo,payload['task_id']);writer._atomic_json(record,payload)
                review=self.review(root,repo,tree,payload)
                with self.assertRaisesRegex(ValueError,'active or frozen'):
                    gc.archive_reviewed_worktree(repo,review,allow_integrated_history=True)
                payload['terminal_evidence']='Reviewed obsolete task; original commits integrated'
                writer._atomic_json(record,payload)
                receipt=gc.archive_reviewed_worktree(repo,review,allow_integrated_history=True)
                self.assertEqual(gc.collect_record(payload)['removed'],['remote-branch','local-branch'])
                self.assertEqual(json.loads(record.read_text())['status'],'superseded')
                self.assertTrue(Path(receipt['original']).is_dir())

    def test_same_status_with_new_file_contents_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(writer,'STATE_ROOT',root/'state'),patch.object(writer,'WORKTREE_ROOT',root/'worktrees'):
                repo,remote,tree,payload=self.fixture(root)
                (tree/'integrated.txt').write_text('reviewed dirty text')
                review=self.review(root,repo,tree,payload)
                (tree/'integrated.txt').write_text('unreviewed dirty text')
                with self.assertRaisesRegex(ValueError,'contents changed'):gc.archive_reviewed_worktree(repo,review)
                self.assertEqual((tree/'integrated.txt').read_text(),'unreviewed dirty text')

    def test_only_positive_modern_numeric_receipts_allow_collection(self):
        basic={'task_id':'123456','status':'merged','worktree':'/tmp/modern'}
        self.assertFalse(gc.c3_allows(basic))
        proved={**basic,'pr_number':1,'integration_state':'merged','integrated_head':'a'*40,'merge_sha':'b'*40}
        self.assertTrue(gc.c3_allows(proved))
        self.assertFalse(gc.c3_allows({**proved,'roadmap_prompt_id':'123456'}))
        self.assertFalse(gc.c3_allows({**proved,'worktree':'/home/daniele/.local/share/c3-symphony/legacy'}))

    def test_stale_review_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(writer,'STATE_ROOT',root/'state'),patch.object(writer,'WORKTREE_ROOT',root/'worktrees'):
                repo,remote,tree,payload=self.fixture(root);review=self.review(root,repo,tree,payload)
                (tree/'changed').write_text('new work')
                with self.assertRaisesRegex(ValueError,'changed'):gc.archive_reviewed_worktree(repo,review)
                self.assertEqual((tree/'changed').read_text(),'new work')

    def test_change_during_copy_never_removes_source(self):
        import tarfile
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(writer,'STATE_ROOT',root/'state'),patch.object(writer,'WORKTREE_ROOT',root/'worktrees'):
                repo,remote,tree,payload=self.fixture(root);review=self.review(root,repo,tree,payload)
                original=tarfile.TarFile.addfile;changed=False
                def concurrent(tar,*args,**kwargs):
                    nonlocal changed
                    if not changed:(tree/'integrated.txt').write_text('concurrent update');changed=True
                    return original(tar,*args,**kwargs)
                with patch.object(tarfile.TarFile,'addfile',concurrent):
                    with self.assertRaisesRegex(ValueError,'stability|readback'):gc.archive_reviewed_worktree(repo,review)
                self.assertTrue(tree.exists());self.assertEqual((tree/'integrated.txt').read_text(),'concurrent update')
