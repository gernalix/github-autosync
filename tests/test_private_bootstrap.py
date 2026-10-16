"""Synthetic bootstrap fixtures: no operator IDs, paths or binding metadata."""
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import private_bootstrap as bootstrap
import repo_single_writer as owner

HEAD = 'a' * 40
BASE = 'b' * 40


class PrivateBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.clone = self.root / 'clone'; self.clone.mkdir()
        self.worktree = self.root / 'task'; self.worktree.mkdir()
        self.task_path = self.root / 'task.json'
        self.task = dict(task_id='bootstrap', pr_number=1, repo='example/project',
                         branch='task/bootstrap', canonical_branch='main',
                         repo_path=str(self.clone), worktree=str(self.worktree))
        self.task_path.write_text(json.dumps(self.task))
        self.plan = dict(project_id=123, version=1,
                         before=dict(remote_url='https://github.com/example/archive', canonical=1, status='active'),
                         after=dict(remote_url='https://github.com/example/project', github_id=123,
                                    worktree_path=str(self.clone), branch='main', head=BASE,
                                    advertised_refs={'refs/heads/task/bootstrap': HEAD}),
                         clean_object_ids=[HEAD])
        self.plan_path = self.root / 'plan.json'
        self.write_plan()
        self.auth = dict(task_id='bootstrap', pr_number=1, repo='example/project',
                         branch='task/bootstrap', base=BASE, head=HEAD,
                         plan_path=str(self.plan_path),
                         plan_digest=bootstrap._private_json(self.plan_path)[1],
                         ci_run_id=456, ci_workflow='.github/workflows/ci.yml')
        self.write_auth()
        self.fenced = False
        self.writes = []
        self.owner_calls = []
        self.branch_reads = 0
        self.metadata = dict(id=123, full_name='example/project', private=True,
                             fork=False, is_template=False, default_branch='main')
        self.pr = dict(state='open', draft=False, merged=False, mergeable=True, title='[single-writer] bootstrap', body='',
                       base=dict(ref='main', repo={'id':123}),
                       head=dict(ref='task/bootstrap', sha=HEAD, repo={'id':123}))
        self.run = dict(id=456, head_sha=HEAD, repository={'id':123}, head_repository={'id':123},
                        event='workflow_dispatch', path='.github/workflows/ci.yml',
                        head_branch='task/bootstrap', status='completed', conclusion='success', workflow_id=789, check_suite_id=1234)
        self.current_base = BASE
        self.cas_current = True
        self.git_identity = 'redacted@example.invalid'
        self.origin = 'https://github.com/example/project'
        self.local_head = HEAD
        @contextmanager
        def writer(database):
            self.assertFalse(self.fenced)
            self.fenced = True
            try:
                connection = Mock()
                connection.execute.return_value.fetchall.return_value = []
                yield connection
            finally:
                self.fenced = False
        def execute(database, plan, *, apply):
            self.assertTrue(self.fenced)
            self.assertFalse(apply)
            self.owner_calls.append(deepcopy(plan))
            if not self.cas_current:
                raise ValueError('synthetic stale CAS')
            return dict(status='PREPARED', writes=0, target_private=True)
        self.cutover = Mock()
        self.cutover.store.writer = writer
        self.cutover.execute = execute
        for name, replacement in (
            ('_task_by_pr', Mock(return_value=(self.task_path, self.task))),
            ('_ok', Mock(side_effect=self.git)),
            ('_github_json', Mock(side_effect=self.api)),
            ('run', Mock(side_effect=self.command)),
        ):
            context = patch.object(owner, name, replacement)
            context.start(); self.addCleanup(context.stop)
        context = patch.object(bootstrap, '_push_exact_base', side_effect=self.push)
        context.start(); self.addCleanup(context.stop)
        context = patch.object(bootstrap, '_cutover_owner', return_value=self.cutover)
        context.start(); self.addCleanup(context.stop)

    def write_plan(self):
        self.plan_path.write_text(json.dumps(self.plan)); self.plan_path.chmod(0o600)

    def write_auth(self):
        path = bootstrap.authorization_path(self.task_path)
        path.write_text(json.dumps(self.auth)); path.chmod(0o600)

    def git(self, root, *args):
        if args[:1] == ('config',):
            return 'true' if 'megavault.redactedFastForward' in args else '123'
        if args[:2] == ('remote', 'get-url'): return self.origin
        if args[:2] == ('rev-parse', 'HEAD'): return self.local_head
        if args[:1] == ('branch',): return 'task/bootstrap'
        if args[:1] == ('rev-list',): return HEAD + ' synthetic.py'
        raise AssertionError(args)

    def api(self, endpoint):
        if endpoint == 'repos/example/project': return deepcopy(self.metadata)
        if '/pulls/' in endpoint: return deepcopy(self.pr)
        if '/branches/' in endpoint: return {'commit': {'sha': self.current_base}}
        if '/actions/runs/' in endpoint: return deepcopy(self.run)
        if '/check-runs?' in endpoint:
            return dict(total_count=1, check_runs=[dict(head_sha=HEAD, status='completed', conclusion='success',
                        check_suite={'id':1234}, app={'slug':'github-actions'})])
        if endpoint.endswith('/status'): return dict(sha=HEAD, total_count=0, state='pending')
        if '/actions/workflows/' in endpoint: return dict(path='.github/workflows/ci.yml', state='active')
        raise AssertionError(endpoint)

    def push(self, integrator, root, branch, base, head):
        self.assertTrue(self.fenced)
        self.assertEqual((base, head), (BASE, HEAD))
        self.writes.append(('nonforced-git-push', branch, head))
        self.current_base = HEAD
        self.pr['merged'] = True

    def command(self, args, **kwargs):
        endpoint = args[2]
        if 'PATCH' in args:
            self.assertTrue(self.fenced)
            self.assertIn('force=false', args)
            self.writes.append(args)
            self.current_base = HEAD
            self.pr['merged'] = True
            data = {}
        elif '/compare/' in endpoint: data = dict(status='ahead', behind_by=0)
        elif '/commits?' in endpoint:
            identity = dict(name='Redacted maintainer', email=self.git_identity)
            data = [[{'commit': dict(author=identity, committer=identity)}]]
        else: data = self.api(endpoint)
        return subprocess.CompletedProcess(args, 0, json.dumps(data), '')

    def integrate(self):
        return bootstrap.integrate(owner, 'example/project', 1, 'main', HEAD, 'task/bootstrap', 123)

    def test_private_exact_head_ci_and_fresh_owner_allow_one_fenced_nonforced_ff(self):
        result = self.integrate()
        self.assertEqual(result['status'], 'merged')
        self.assertEqual(result['sha'], HEAD)
        self.assertTrue(result['private_bootstrap'])
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(len(self.owner_calls), 2)
        self.assertFalse(self.fenced)

    def test_no_optin_preserves_canonical_gate_without_owner_or_git_write(self):
        bootstrap.authorization_path(self.task_path).unlink()
        self.assertEqual(self.integrate()['reason'], 'canonical-cutover-not-activated')
        self.assertFalse(self.owner_calls); self.assertFalse(self.writes)

    def test_live_failures_cannot_write(self):
        cases = [lambda: self.metadata.update(private=False),
                 lambda: self.metadata.update(id=999),
                 lambda: self.metadata.update(full_name='example/other'),
                 lambda: self.run.update(head_sha=BASE),
                 lambda: self.run.update(conclusion='failure'),
                 lambda: self.run.update(status='in_progress'),
                 lambda: self.run.update(event='pull_request'),
                 lambda: self.run.update(path='.github/workflows/other.yml'),
                 lambda: self.pr['head']['repo'].update(id=999),
                 lambda: self.pr['head'].update(sha=BASE),
                 lambda: setattr(self, 'current_base', 'c'*40),
                 lambda: setattr(self, 'cas_current', False),
                 lambda: setattr(self, 'origin', 'https://github.com/example/other'),
                 lambda: setattr(self, 'local_head', BASE),
                 lambda: setattr(self, 'git_identity', 'synthetic@example.com')]
        for mutate in cases:
            with self.subTest(case=cases.index(mutate)):
                saved = deepcopy((self.metadata, self.run, self.pr))
                mutate()
                self.assertEqual(self.integrate()['status'], 'deferred')
                self.assertFalse(self.writes)
                self.metadata, self.run, self.pr = saved
                self.current_base, self.cas_current, self.origin, self.local_head, self.git_identity = (
                    BASE, True, 'https://github.com/example/project', HEAD, 'redacted@example.invalid')

    def test_plan_change_and_forged_authorization_still_require_current_owner(self):
        self.plan['version'] = 2; self.write_plan()
        self.assertEqual(self.integrate()['status'], 'deferred')
        self.assertFalse(self.owner_calls)
        # A caller-supplied updated hash cannot replace canonical owner's CAS.
        self.auth['plan_digest'] = bootstrap._private_json(self.plan_path)[1]; self.write_auth()
        self.cas_current = False
        self.assertEqual(self.integrate()['status'], 'deferred')
        self.assertEqual(len(self.owner_calls), 1); self.assertFalse(self.writes)

    def test_unreviewed_objects_or_ref_and_private_custody_are_refused(self):
        for field in ('clean_object_ids', 'advertised_refs'):
            saved = deepcopy(self.plan)
            if field == 'clean_object_ids': self.plan[field] = []
            else: self.plan['after'][field] = {}
            self.write_plan(); self.auth['plan_digest'] = bootstrap._private_json(self.plan_path)[1]; self.write_auth()
            self.assertEqual(self.integrate()['status'], 'deferred'); self.assertFalse(self.writes)
            self.plan = saved
        self.plan_path.chmod(0o644)
        self.assertEqual(self.integrate()['status'], 'deferred'); self.assertFalse(self.writes)

    def test_main_race_during_history_walk_is_rechecked_before_write(self):
        original = owner.run.side_effect
        def race(args, **kwargs):
            result = original(args, **kwargs)
            if '/commits?' in args[2]: self.current_base = 'c'*40
            return result
        owner.run.side_effect = race
        self.assertEqual(self.integrate()['status'], 'deferred'); self.assertFalse(self.writes)

    def test_authorize_requires_live_owner_and_writes_only_private_task_sidecar(self):
        bootstrap.authorization_path(self.task_path).unlink()
        with patch.object(owner, 'RepoLock'), patch.object(owner, '_repo_slug', return_value='example/project'), \
             patch.object(owner, '_task_record', return_value=self.task_path):
            result = bootstrap.authorize(owner, self.clone, 'bootstrap', self.plan_path,
                                         HEAD, 456, '.github/workflows/ci.yml')
        path = bootstrap.authorization_path(self.task_path)
        self.assertEqual(result['status'], 'authorized')
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(path.read_text())['head'], HEAD)
        self.assertFalse(self.writes)
        self.assertEqual(len(self.owner_calls), 1)
        self.assertNotIn('plan_path', result)

    def test_check_provenance_pending_status_and_wrong_suite_never_write(self):
        original = owner._github_json.side_effect
        for change in ('pending', 'suite', 'status'):
            def api(endpoint):
                data = original(endpoint)
                if '/check-runs?' in endpoint:
                    if change == 'pending': data['check_runs'][0]['status'] = 'queued'
                    if change == 'suite': data['check_runs'][0]['check_suite']['id'] = 999
                if endpoint.endswith('/status') and change == 'status':
                    data.update(total_count=1, state='failure')
                return data
            owner._github_json.side_effect = api
            self.assertEqual(self.integrate()['status'], 'deferred')
            self.assertFalse(self.writes)
        owner._github_json.side_effect = original

    def test_push_ci_can_bootstrap_workflow_dispatch_before_it_exists_on_main(self):
        self.run['event'] = 'push'
        self.assertEqual(self.integrate()['status'], 'merged')
        self.assertEqual(len(self.writes), 1)

    def test_already_canonical_target_is_refused_under_owner_fence(self):
        @contextmanager
        def writer(database):
            connection = Mock()
            connection.execute.return_value.fetchall.return_value = [('https://github.com/example/project.git',)]
            yield connection
        self.cutover.store.writer = writer
        self.assertEqual(self.integrate()['status'], 'deferred')
        self.assertFalse(self.owner_calls); self.assertFalse(self.writes)

    def test_owner_cas_change_during_history_walk_prevents_push(self):
        original = owner.run.side_effect
        def race(args, **kwargs):
            result = original(args, **kwargs)
            if '/commits?' in args[2]: self.cas_current = False
            return result
        owner.run.side_effect = race
        self.assertEqual(self.integrate()['status'], 'deferred')
        self.assertFalse(self.writes)
        self.assertEqual(len(self.owner_calls), 2)

    def test_authorization_added_after_catalog_preparation_never_publishes(self):
        bootstrap.authorization_path(self.task_path).unlink()
        item = dict(repo='example/project', number=1)
        result = dict(**item, status='merged', head_branch='task/bootstrap', head_sha=HEAD, sha=HEAD, private_bootstrap=True)
        with patch.object(owner, '_retry_pending_catalog_publications', return_value=[]), \
             patch.object(owner, 'discover_ready_prs', return_value=[item]), \
             patch.object(owner, 'integrate_pr', return_value=result), \
             patch.object(owner, '_observe_task'), patch.object(owner, 'RepoLock'), \
             patch.object(owner.command_catalog_bridge, 'prepare', return_value={'status':'ready'}), \
             patch.object(owner.command_catalog_bridge, 'publish') as publish:
            self.assertEqual(owner.process_ready_prs('example')['merged'], 1)
        publish.assert_not_called()

    def test_queue_skips_catalog_and_cleanup_for_bootstrap(self):
        item = dict(repo='example/project', number=1)
        result = dict(**item, status='merged', head_branch='task/bootstrap', head_sha=HEAD, sha=HEAD, private_bootstrap=True)
        with patch.object(owner, '_retry_pending_catalog_publications', return_value=[]), \
             patch.object(owner, 'discover_ready_prs', return_value=[item]), \
             patch.object(owner, 'integrate_pr', return_value=result), \
             patch.object(owner, '_observe_task'), patch.object(owner, 'RepoLock'), \
             patch.object(owner.command_catalog_bridge, 'prepare') as prepare, \
             patch.object(owner.command_catalog_bridge, 'publish') as publish, \
             patch.object(owner, 'cleanup_task_after_merge') as cleanup:
            queue = owner.process_ready_prs('example')
        self.assertEqual(queue['merged'], 1)
        prepare.assert_not_called(); publish.assert_not_called(); cleanup.assert_not_called()
        self.assertEqual(json.loads(self.task_path.read_text())['status'], 'merged')


class ExactBaseGitTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.client = self.root / 'client'
        self.remote = self.root / 'remote.git'
        self.git(self.root, 'init', '--bare', str(self.remote))
        self.git(self.root, 'init', '-b', 'main', str(self.client))
        self.git(self.client, 'config', 'user.name', 'Redacted maintainer')
        self.git(self.client, 'config', 'user.email', 'redacted@example.invalid')
        self.git(self.client, 'config', 'commit.gpgsign', 'false')
        self.git(self.client, 'remote', 'add', 'origin', str(self.remote))
        heads = []
        for content in ('base', 'middle', 'head'):
            (self.client / 'synthetic.txt').write_text(content)
            self.git(self.client, 'add', 'synthetic.txt')
            self.git(self.client, 'commit', '-m', content)
            heads.append(self.git(self.client, 'rev-parse', 'HEAD'))
        self.base, self.middle, self.head = heads
        self.git(self.client, 'push', 'origin', self.base + ':refs/heads/main', self.head + ':refs/heads/task/bootstrap')

    def git(self, root, *args):
        result = subprocess.run(['git', '-C', str(root), *args], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, 'synthetic Git setup/read failed')
        return result.stdout.strip()

    def remote_head(self):
        return self.git(self.remote, 'rev-parse', 'refs/heads/main')

    def test_nonforced_exact_base_push_and_existing_hook_are_preserved(self):
        hook = self.client / '.git/hooks/pre-push'
        marker = self.root / 'hook-called'
        hook.write_text('#!/usr/bin/env python3\nfrom pathlib import Path\nPath(' + repr(str(marker)) + ').write_text("called")\n')
        hook.chmod(0o700)
        bootstrap._push_exact_base(owner, self.client, 'main', self.base, self.head)
        self.assertEqual(self.remote_head(), self.head)
        self.assertTrue(marker.is_file())
        self.assertEqual(self.git(self.client, 'config', '--get-regexp', '^remote.origin.url$'), 'remote.origin.url ' + str(self.remote))

    def test_concurrent_ancestor_before_advertisement_is_rejected(self):
        self.git(self.remote, 'update-ref', 'refs/heads/main', self.middle, self.base)
        with self.assertRaises(ValueError):
            bootstrap._push_exact_base(owner, self.client, 'main', self.base, self.head)
        self.assertEqual(self.remote_head(), self.middle)

    def test_concurrent_ancestor_after_advertisement_is_rejected_by_server(self):
        hook = self.client / '.git/hooks/pre-push'
        args = ['git', '-C', str(self.remote), 'update-ref', 'refs/heads/main', self.middle, self.base]
        hook.write_text('#!/usr/bin/env python3\nimport subprocess\nsubprocess.run(' + repr(args) + ', check=True)\n')
        hook.chmod(0o700)
        with self.assertRaises(ValueError):
            bootstrap._push_exact_base(owner, self.client, 'main', self.base, self.head)
        self.assertEqual(self.remote_head(), self.middle)

    def test_existing_hook_rejection_is_not_bypassed(self):
        hook = self.client / '.git/hooks/pre-push'
        hook.write_text('#!/bin/sh\nexit 1\n'); hook.chmod(0o700)
        with self.assertRaises(ValueError):
            bootstrap._push_exact_base(owner, self.client, 'main', self.base, self.head)
        self.assertEqual(self.remote_head(), self.base)


if __name__ == '__main__': unittest.main()
