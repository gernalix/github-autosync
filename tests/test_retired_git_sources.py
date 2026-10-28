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
