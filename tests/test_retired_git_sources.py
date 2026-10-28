"""Disposable Git and fake units/process inventory; never contacts the live C3."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import repo_gc as gc
import repo_single_writer as writer
import retired_git_sources as policy
import test_repo_gc as fixtures


class Scope:
    project_id=51
    def __call__(self,*args):pass


class RetiredSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        for target,value in [('STATE_ROOT',self.root/'state'),('WORKTREE_ROOT',self.root/'worktrees')]:
            p=patch.object(writer,target,value);p.start();self.addCleanup(p.stop)
        p=patch.object(writer,'_repo_slug',return_value='gernalix/codex-roadmap');p.start();self.addCleanup(p.stop)
        with patch.dict(os.environ,GIT_AUTHOR_DATE='2026-10-01T00:00:00Z',GIT_COMMITTER_DATE='2026-10-01T00:00:00Z'):
            self.repo,self.remote,self.tree,self.payload=fixtures.SafeGcTests().fixture(self.root)
        self.payload.update(status='active',roadmap_prompt_id='314611')
        writer._atomic_json(writer._task_record(self.repo,'gc-example'),self.payload)
        with patch.dict(os.environ,GIT_AUTHOR_DATE='2026-10-02T00:00:00Z',GIT_COMMITTER_DATE='2026-10-02T00:00:00Z'):
            (self.tree/'retired.py').write_text('# unpublished retired implementation')
            writer._ok(self.tree,'add','retired.py');writer._ok(self.tree,'commit','-m','old retired work')
        seed=self.root/'git/seed';writer._ok(seed,'fetch','origin');writer._ok(seed,'merge','--ff-only','origin/main')
        (seed/'AGENTS.md').write_text('C2/C3 e ritirato; conservare lo storico')
        writer._ok(seed,'add','AGENTS.md');writer._ok(seed,'commit','-m','retire control plane');writer._ok(seed,'push','origin','main')
        writer._ok(self.repo,'fetch','origin')
        self.marker=self.root/'retired.json';self.marker.write_text(json.dumps({'schema':'c3.c2-retired.v1','units':['c2-runtime.service']}))
        self.proc=self.root/'proc';self.proc.mkdir()
        for obj,name,value in [(gc,'RETIREMENT_MARKER',self.marker),(policy,'PROC',self.proc)]:
            p=patch.object(obj,name,value);p.start();self.addCleanup(p.stop)
        original=writer.run
        def run(argv,*a,**kw):
            if argv[0]=='systemctl':
                return subprocess.CompletedProcess(argv,0,'inactive\n' if 'show' in argv else '','')
            return original(argv,*a,**kw)
        p=patch.object(writer,'run',side_effect=run);p.start();self.addCleanup(p.stop)
        self.scope=Scope()

    def review(self):
        p=fixtures.ReviewedArchiveTests().review(self.root,self.repo,self.tree,self.payload,'superseded')
        s=json.loads(p.read_text());s['retirement_sha256']=hashlib.sha256(self.marker.read_bytes()).hexdigest();p.write_text(json.dumps(s));return p

    def test_dirty_retired_source_preserved_without_fabricating_terminal_state(self):
        (self.tree/'integrated.txt').write_text('unpublished retired control-plane changes')
        (self.tree/'private-note').write_text('recoverable local note')
        writer._ok(self.tree,'add','integrated.txt')
        record=writer._task_record(self.repo,'gc-example');before=record.read_bytes()
        with patch.object(gc.sqlite3,'connect',side_effect=AssertionError('historical database opened')):
            result=gc.archive_reviewed_worktree(self.repo,self.review(),scope_guard=self.scope,
                                               allow_reviewed_retired_history=True,allow_reviewed_terminal_history=True)
        self.assertEqual(result['status'],'archived');self.assertFalse(self.tree.exists())
        self.assertEqual(record.read_bytes(),before)
        self.assertEqual((Path(result['original'])/'private-note').read_text(),'recoverable local note')
        self.assertTrue((Path(result['preservation'])/'commits.bundle').is_file())

    def test_current_owner_and_process_use_refuse_preservation(self):
        review=self.review();self.payload.pop('roadmap_prompt_id');writer._atomic_json(writer._task_record(self.repo,'gc-example'),self.payload)
        with self.assertRaisesRegex(ValueError,'current task owner'):
            gc.archive_reviewed_worktree(self.repo,review,scope_guard=self.scope,allow_reviewed_retired_history=True)
        self.payload['roadmap_prompt_id']='314611';writer._atomic_json(writer._task_record(self.repo,'gc-example'),self.payload)
        proc=self.proc/'123';proc.mkdir();(proc/'cmdline').write_bytes(b'python3\0--repo='+str(self.tree).encode()+b'\0')
        with self.assertRaisesRegex(ValueError,'still in use'):
            gc.archive_reviewed_worktree(self.repo,review,scope_guard=self.scope,allow_reviewed_retired_history=True)
        self.assertTrue(self.tree.exists())

    def test_wrong_project_and_new_unintegrated_commit_fail_closed(self):
        review=json.loads(self.review().read_text());self.scope.project_id=1
        with self.assertRaisesRegex(ValueError,'restricted'):policy.verify_retired_source(self.repo,review,self.scope,worktree=self.tree)
        self.scope.project_id=51
        (self.tree/'new').write_text('new active work');writer._ok(self.tree,'add','new');writer._ok(self.tree,'commit','-m','new work')
        review['expected_head']=writer._ok(self.tree,'rev-parse','HEAD')
        with self.assertRaisesRegex(ValueError,'newer than retirement cutoff'):policy.verify_retired_source(self.repo,review,self.scope,worktree=self.tree)
        self.assertTrue(self.tree.exists())

    def test_reactivated_unit_and_source_process_after_capture_refuse(self):
        review=self.review();original=writer.run
        def run(argv,*a,**kw):
            if argv[:3]==['systemctl','--user','list-units']:return subprocess.CompletedProcess(argv,0,'c3-writer.service loaded active running','')
            return original(argv,*a,**kw)
        with patch.object(writer,'run',side_effect=run):
            with self.assertRaisesRegex(ValueError,'C3 units'):gc.archive_reviewed_worktree(self.repo,review,scope_guard=self.scope,allow_reviewed_retired_history=True)
        original_snapshot=gc.worktree_snapshot;calls=0
        def snapshot(path):
            nonlocal calls
            value=original_snapshot(path);calls+=1
            if calls==2:
                proc=self.proc/'124';proc.mkdir();(proc/'cmdline').write_bytes(b'python3\0');(proc/'cwd').symlink_to(self.tree)
            return value
        with patch.object(gc,'worktree_snapshot',side_effect=snapshot):
            with self.assertRaisesRegex(ValueError,'still in use'):gc.archive_reviewed_worktree(self.repo,review,scope_guard=self.scope,allow_reviewed_retired_history=True)
        self.assertTrue(self.tree.exists())


class IndependentCloneTests(RetiredSourceTests):
    def clone_source(self):
        clone=self.root/'independent'
        writer._ok(self.repo,'clone','--no-hardlinks',str(self.repo),str(clone))
        writer._ok(clone,'checkout','--detach',writer._ok(self.tree,'rev-parse','HEAD'))
        (self.tree/'.git').unlink()
        (clone/'.git').rename(self.tree/'.git')
        writer._ok(self.tree,'remote','set-url','origin',str(self.remote))

    def independent_review(self):
        p=self.review();value=json.loads(p.read_text())
        value['independent_registered_head']=writer._ok(self.repo,'rev-parse',self.payload['branch'])
        value['independent_git_fingerprint']=hashlib.sha256(json.dumps(policy.independent_git_snapshot(self.tree),sort_keys=True).encode()).hexdigest()
        p.write_text(json.dumps(value));return p

    def test_explicit_clone_preserves_all_refs_git_and_historical_owner(self):
        self.clone_source();writer._ok(self.tree,'update-ref','refs/heads/extra-old',writer._ok(self.tree,'rev-parse','HEAD'))
        expected=policy.independent_git_snapshot(self.tree)
        record=writer._task_record(self.repo,'gc-example');before=record.read_bytes()
        result=gc.archive_reviewed_worktree(self.repo,self.independent_review(),scope_guard=self.scope,allow_reviewed_retired_history=True)
        self.assertEqual(result['status'],'archived');self.assertEqual(record.read_bytes(),before)
        self.assertTrue((Path(result['original'])/'.git/config').is_file())
        self.assertEqual(policy.independent_git_snapshot(Path(result['original'])),expected)
        bundle=Path(result['preservation'])/'commits.bundle'
        self.assertIn('refs/heads/extra-old',writer._ok(self.repo,'bundle','list-heads',str(bundle)))
        self.assertNotIn(str(self.tree),writer._ok(self.repo,'worktree','list','--porcelain'))

    def test_clone_without_exact_review_or_with_alternates_is_rejected(self):
        self.clone_source()
        with self.assertRaisesRegex(ValueError,'not reviewed'):
            gc.archive_reviewed_worktree(self.repo,self.review(),scope_guard=self.scope,allow_reviewed_retired_history=True)
        review=self.independent_review();(self.tree/'.git/objects/info/alternates').write_text('/tmp/unknown')
        with self.assertRaisesRegex(ValueError,'alternates'):
            gc.archive_reviewed_worktree(self.repo,review,scope_guard=self.scope,allow_reviewed_retired_history=True)
        self.assertTrue(self.tree.exists())

    def test_clone_new_unintegrated_ref_and_origin_mismatch_refused(self):
        self.clone_source()
        with patch.object(writer,'_repo_slug',side_effect=lambda path:'other/repo' if Path(path)==self.tree else 'gernalix/codex-roadmap'):
            with self.assertRaisesRegex(ValueError,'origin differs'):
                gc.archive_reviewed_worktree(self.repo,self.independent_review(),scope_guard=self.scope,allow_reviewed_retired_history=True)
        writer._ok(self.tree,'config','user.name','Test');writer._ok(self.tree,'config','user.email','test@example.invalid')
        (self.tree/'new-active').write_text('new');writer._ok(self.tree,'add','new-active');writer._ok(self.tree,'commit','-m','recent work')
        tip=writer._ok(self.tree,'rev-parse','HEAD');writer._ok(self.tree,'update-ref','refs/heads/hidden-new',tip)
        writer._ok(self.tree,'checkout','--detach',writer._ok(self.repo,'rev-parse',self.payload['branch']))
        with self.assertRaisesRegex(ValueError,'recent unintegrated'):
            gc.archive_reviewed_worktree(self.repo,self.independent_review(),scope_guard=self.scope,allow_reviewed_retired_history=True)
        self.assertTrue(self.tree.exists())

    def test_clone_ref_changed_after_capture_refuses_move(self):
        self.clone_source();review=self.independent_review();original=gc.worktree_snapshot;calls=0
        def snapshot(path):
            nonlocal calls
            value=original(path);calls+=1
            if calls==2:writer._ok(self.tree,'update-ref','refs/heads/late-old',writer._ok(self.tree,'rev-parse','HEAD'))
            return value
        with patch.object(gc,'worktree_snapshot',side_effect=snapshot):
            with self.assertRaisesRegex(ValueError,'Git state changed'):
                gc.archive_reviewed_worktree(self.repo,review,scope_guard=self.scope,allow_reviewed_retired_history=True)
        self.assertTrue(self.tree.exists())


    def test_divergent_registered_head_must_be_explicit_and_bundled(self):
        self.clone_source();registered=writer._ok(self.repo,'rev-parse','main')
        writer._ok(self.repo,'update-ref','refs/heads/'+self.payload['branch'],registered)
        review=self.independent_review();value=json.loads(review.read_text());value['independent_registered_head']='0'*40;review.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,'registered HEAD'):
            gc.archive_reviewed_worktree(self.repo,review,scope_guard=self.scope,allow_reviewed_retired_history=True)
        result=gc.archive_reviewed_worktree(self.repo,self.independent_review(),scope_guard=self.scope,allow_reviewed_retired_history=True)
        self.assertEqual(result['status'],'archived')
        bundled=writer._ok(self.repo,'bundle','list-heads',str(Path(result['preservation'])/'commits.bundle'))
        self.assertIn(registered,bundled)
        self.assertNotEqual(result['head'],registered)

    def test_registration_changed_during_capture_refuses_move(self):
        self.clone_source();review=self.independent_review();original=gc.worktree_snapshot;calls=0
        def snapshot(path):
            nonlocal calls
            value=original(path);calls+=1
            if calls==2:writer._ok(self.repo,'update-ref','refs/heads/'+self.payload['branch'],writer._ok(self.repo,'rev-parse','main'))
            return value
        with patch.object(gc,'worktree_snapshot',side_effect=snapshot):
            with self.assertRaisesRegex(ValueError,'registration changed'):
                gc.archive_reviewed_worktree(self.repo,review,scope_guard=self.scope,allow_reviewed_retired_history=True)
        self.assertTrue(self.tree.exists())


class HistoricalCheckoutPathTests(unittest.TestCase):
    def test_previously_unrecognized_real_retired_layouts_and_sibling_boundaries(self):
        with patch.object(policy,'HOME',Path('/home/fixture')):
            for relative in ['.local/share/c2-task-state/phase-c-source-task-checkpoint',
                             '.local/share/chatgpt-task-state/857906',
                             '.local/share/chatgpt-c2-state-alerts',
                             'Documents/ChatGPT/C2/c3-priorities-import',
                             '.local/share/codex-github-autosync/worktrees/gernalix_codex-roadmap/175908',
                             'projects/codex-roadmap-android-favorites-probe',
                             'projects/codex-roadmap-uptime-red']:
                with self.subTest(path=relative):self.assertTrue(policy.historical_path(policy.HOME/relative))
            for relative in ['projects/current-work','projects/codex-roadmap-uptime-red-current',
                             '.local/share/codex-github-autosync/worktrees/gernalix_other/175908']:
                with self.subTest(path=relative):self.assertFalse(policy.historical_path(policy.HOME/relative))
