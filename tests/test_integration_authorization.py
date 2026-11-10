"""Provider metadata cannot grant authority over active or differently owned tasks."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import repo_single_writer as writer
import test_repo_single_writer as fixtures


class IntegrationAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo, self.bare = fixtures.SingleWriterTests('test_guard_blocks_direct_main_commit').make_repo(self.root)
        self.tree = self.root / 'draft-tree'
        self.git('worktree', 'add', '-b', 'task/draft', str(self.tree), 'main')
        (self.tree / 'draft.txt').write_text('PUBLIC_SYNTHETIC_DRAFT\n')
        self.git('add', 'draft.txt', cwd=self.tree)
        self.git('commit', '-m', 'draft', cwd=self.tree)
        self.git('push', 'origin', 'task/draft', cwd=self.tree)
        self.base = self.git('rev-parse', 'main').stdout.strip()
        self.head = self.git('rev-parse', 'HEAD', cwd=self.tree).stdout.strip()
        self.slug = 'gernalix/example'  # Explicit native tasks need not be sync-allowlisted.
        for name, value in [('STATE_ROOT', self.root / 'state'), ('WORKTREE_ROOT', self.root / 'trees')]:
            self.enterContext(mock.patch.object(writer, name, value))
        self.record = writer._task_dir_for_slug(self.slug) / 'draft.json'
        self.record.parent.mkdir(parents=True)
        self.payload = {'repo': self.slug, 'task_id': 'draft', 'branch': 'task/draft',
                        'status': 'queued', 'pr_number': 41, 'head_sha': self.head,
                        'base_sha': self.base, 'canonical_branch': 'main',
                        'worktree': str(self.tree), 'repo_path': str(self.repo)}
        self.save()
        self.info = {'number': 41, 'title': writer.PR_PREFIX + ' draft', 'body': '',
                     'state': 'OPEN', 'isDraft': False, 'mergeable': 'MERGEABLE',
                     'baseRefName': 'main', 'baseRefOid': self.base,
                     'headRefName': 'task/draft', 'headRefOid': self.head,
                     'statusCheckRollup': [], 'mergeCommit': None,
                     'isCrossRepository': False,
                     'headRepository': {'name': 'example', 'nameWithOwner': self.slug},
                     'headRepositoryOwner': {'login': 'gernalix'}}
        self.merges = []
        original = writer.run

        def provider(cmd, cwd=None, timeout=300):
            if cmd[:3] == ['gh', 'pr', 'view']:
                return fixtures.cp(stdout=json.dumps(self.info | {'number': int(cmd[3])}))
            if cmd[:3] == ['gh', 'repo', 'view']:
                return fixtures.cp(stdout=json.dumps({'defaultBranchRef': {'name': 'main'}}))
            if cmd[:2] == ['gh', 'api'] and '/git/ref/heads/' in cmd[-1]:
                return fixtures.cp(stdout=json.dumps({'object': {'sha': self.base}}))
            if cmd[:4] == ['gh', 'api', '--method', 'PUT'] and cmd[4].endswith('/merge'):
                self.merges.append(cmd)
                return fixtures.cp(stdout=json.dumps({'merged': True, 'sha': 'a' * 40}))
            if cmd and cmd[0] == 'gh':
                raise AssertionError('unexpected provider operation')
            return original(cmd, cwd, timeout)

        self.enterContext(mock.patch.object(writer, 'run', side_effect=provider))
        self.enterContext(mock.patch.object(writer, '_redacted_source_policy', return_value=None))

    def git(self, *args, cwd=None):
        result = fixtures.git(list(args), cwd or self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def save(self):
        self.record.write_text(json.dumps(self.payload))

    def assert_rejected_without_effects(self, number=41):
        before = self.record.read_bytes()
        with mock.patch.object(writer, '_refresh_task_branch_to_latest_base', side_effect=AssertionError('refresh forbidden')):
            result = writer.integrate_pr(self.slug, number)
        self.assertEqual(result['status'], 'deferred')
        self.assertEqual(self.record.read_bytes(), before)
        self.assertEqual(self.merges, [])
        self.assertEqual(self.git('rev-parse', 'main').stdout.strip(), self.base)
        self.assertEqual(fixtures.git(['rev-parse', 'main'], self.bare).stdout.strip(), self.base)

    def test_nonqueued_records_cannot_be_adopted(self):
        for status in ['active', 'blocked', 'merged', 'superseded']:
            with self.subTest(status=status):
                self.payload['status'] = status
                self.save()
                self.assert_rejected_without_effects()

    def test_wrong_pr_number_cannot_rebind_same_branch_and_commit(self):
        self.assert_rejected_without_effects(99)
        before = self.record.read_bytes()
        writer._observe_task(self.slug, 'task/draft', 'queued', pr_number=99)
        self.assertEqual(self.record.read_bytes(), before)

    def test_wrong_head_branch_or_base_cannot_refresh(self):
        for key, value in [('headRefOid', 'b' * 40), ('headRefName', 'task/other'), ('baseRefName', 'other')]:
            with self.subTest(field=key):
                old = self.info[key]
                self.info[key] = value
                self.assert_rejected_without_effects()
                self.info[key] = old

    def test_foreign_and_unknown_head_repository_fail_closed(self):
        for changes in [
            {'isCrossRepository': True, 'headRepository': {'nameWithOwner': 'outsider/example'}},
            {'isCrossRepository': None}, {'headRepository': None},
            {'headRepository': {'nameWithOwner': 'gernalix/other'}},
        ]:
            with self.subTest(changes=changes):
                previous = self.info.copy()
                self.info.update(changes)
                self.assert_rejected_without_effects()
                self.info = previous

    def test_closed_or_unknown_provider_state_is_not_merge_authority(self):
        for state in ['CLOSED', '', 'unknown']:
            with self.subTest(state=state):
                self.info['state'] = state
                self.assert_rejected_without_effects()

    def test_merged_foreign_receipt_is_not_cleanup_authority(self):
        self.info.update(state='MERGED', isCrossRepository=True, mergeCommit={'oid': 'a' * 40})
        self.assert_rejected_without_effects()

    def test_duplicate_branch_or_pr_owners_fail_closed(self):
        extra = self.record.with_name('duplicate.json')
        for changes in [{'pr_number': 42}, {'branch': 'task/other'}]:
            with self.subTest(changes=changes):
                extra.write_text(json.dumps(self.payload | changes))
                self.assert_rejected_without_effects()
                extra.unlink()

    def test_owned_queued_task_reaches_only_mock_provider_merge(self):
        result = writer.integrate_pr(self.slug, 41)
        self.assertEqual(result['status'], 'merged')
        self.assertEqual(len(self.merges), 1)
        self.assertEqual(json.loads(self.record.read_text())['pr_number'], 41)
        self.assertEqual(self.git('rev-parse', 'main').stdout.strip(), self.base)

    def test_owned_pending_checks_preserve_owner_and_defer(self):
        self.info['statusCheckRollup'] = [{'status': 'IN_PROGRESS', 'conclusion': None}]
        result = writer.integrate_pr(self.slug, 41)
        self.assertEqual(result['reason'], 'checks-pending')
        self.assertEqual(self.merges, [])
        self.assertEqual(json.loads(self.record.read_text())['pr_number'], 41)

    def test_owned_published_followup_keeps_the_same_authority(self):
        (self.tree / 'followup.txt').write_text('PUBLIC_SYNTHETIC_FOLLOWUP\n')
        self.git('add', 'followup.txt', cwd=self.tree)
        self.git('commit', '-m', 'followup', cwd=self.tree)
        self.git('push', 'origin', 'task/draft', cwd=self.tree)
        published = self.git('rev-parse', 'HEAD', cwd=self.tree).stdout.strip()
        self.payload['head_sha'] = published
        self.save()
        self.info['headRefOid'] = published
        self.assertEqual(writer.integrate_pr(self.slug, 41)['status'], 'merged')
        self.assertEqual(len(self.merges), 1)
        self.assertEqual(json.loads(self.record.read_text())['pr_number'], 41)

    def test_unowned_discovery_does_not_block_the_authorized_task(self):
        items = [{'repo': self.slug, 'number': 99}, {'repo': self.slug, 'number': 41}]
        with mock.patch.object(writer, '_retry_pending_catalog_publications', return_value=[]), \
             mock.patch.object(writer, 'discover_ready_prs', return_value=items), \
             mock.patch.object(writer.command_catalog_bridge, 'prepare', return_value={'status': 'not-applicable'}) as prepare, \
             mock.patch.object(writer.command_catalog_bridge, 'publish', return_value={'status': 'not-applicable'}), \
             mock.patch.object(writer, 'cleanup_task_after_merge', return_value={'status': 'preserved'}) as cleanup:
            result = writer.process_ready_prs('gernalix')
        self.assertEqual(result['merged'], 1)
        self.assertEqual(result['results'][0]['status'], 'ignored')
        self.assertEqual(len(self.merges), 1)
        prepare.assert_called_once()
        cleanup.assert_called_once()
        self.assertEqual(json.loads(self.record.read_text())['pr_number'], 41)

    def test_owned_merged_receipt_does_not_send_another_merge(self):
        self.info.update(state='MERGED', mergeCommit={'oid': 'a' * 40})
        result = writer.integrate_pr(self.slug, 41)
        self.assertTrue(result['already_merged'])
        self.assertEqual(self.merges, [])

    def test_discovery_rejects_active_or_foreign_before_catalog_or_state(self):
        for active in [True, False]:
            with self.subTest(active=active):
                self.payload['status'] = 'active' if active else 'queued'
                self.save()
                self.info['isCrossRepository'] = not active
                before = self.record.read_bytes()
                with mock.patch.object(writer, '_retry_pending_catalog_publications', return_value=[]), \
                     mock.patch.object(writer, 'discover_ready_prs', return_value=[{'repo': self.slug, 'number': 41}]), \
                     mock.patch.object(writer.command_catalog_bridge, 'prepare', side_effect=AssertionError('catalog forbidden')), \
                     mock.patch.object(writer, 'integrate_pr', side_effect=AssertionError('integration forbidden')), \
                     mock.patch.object(writer, 'cleanup_task_after_merge', side_effect=AssertionError('cleanup forbidden')):
                    result = writer.process_ready_prs('gernalix')
                self.assertEqual(result['merged'], 0)
                self.assertEqual(self.record.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
