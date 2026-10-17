"""Owner callbacks must run with the existing repository lock held."""
import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import repo_single_writer as writer
import repo_gc as gc
import test_repo_gc as gc_fixtures


class CallbackTests(unittest.TestCase):
    def test_start_finish_and_cleanup_deny_before_any_mutation_under_lock(self):
        locked=[]
        @contextlib.contextmanager
        def lock(slug):
            locked.append(slug)
            try:yield
            finally:locked.pop()
        def deny(*args):
            self.assertEqual(locked,['test/repo'])
            raise ValueError('scope revoked')
        with patch.object(writer,'RepoLock',lock),patch.object(writer,'_repo_slug',return_value='test/repo'),patch.object(writer,'_git') as mutate,patch.object(writer,'_atomic_json') as data:
            for operation in (
                lambda:writer.start_task(Path('/fixture'),'task',scope_guard=deny),
                lambda:writer.finish_task(Path('/fixture'),'task',scope_guard=deny),
                lambda:writer.cleanup_task_after_merge('test/repo','task/example',scope_guard=deny),
                lambda:writer.cleanup_terminal_task('test/repo','task/example',expected_head='a'*40,canonical_sha='b'*40,scope_guard=deny),
            ):
                with self.assertRaisesRegex(ValueError,'scope revoked'):operation()
            mutate.assert_not_called();data.assert_not_called()

    def test_guarded_start_and_noop_finish_share_lock_without_deadlock(self):
        import fcntl
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(writer,'STATE_ROOT',root/'state'),patch.object(writer,'WORKTREE_ROOT',root/'worktrees'),patch.object(gc,'C3_DB',root/'missing'):
                repo,_=gc_fixtures.fixtures.SingleWriterTests().make_repo(root/'git')
                calls=[]
                def guard(*args):
                    lock=writer.RepoLock(writer._repo_slug(repo))
                    with lock.path.open('a') as handle:
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(handle.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                    calls.append(args)
                task=writer.start_task(repo,'guarded',scope_guard=guard)
                self.assertTrue(Path(task['worktree']).is_dir())
                result=writer.finish_task(repo,'guarded',scope_guard=guard)
                self.assertEqual(result['integration'],'no-op')
                self.assertGreater(len(calls),8)
                self.assertFalse(Path(task['worktree']).exists())

    def test_gc_refusal_preserves_all_fixture_refs_and_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(writer,'STATE_ROOT',root/'state'),patch.object(writer,'WORKTREE_ROOT',root/'worktrees'),patch.object(gc,'C3_DB',root/'missing'):
                repo,remote,tree,payload=gc_fixtures.SafeGcTests().fixture(root)
                before=gc_fixtures.fixtures.git(['show-ref'],repo).stdout
                def deny(*args):raise ValueError('scope revoked')
                with self.assertRaisesRegex(ValueError,'scope revoked'):
                    writer.cleanup_terminal_task(payload['repo'],payload['branch'],expected_head=payload['integrated_head'],canonical_sha=payload['merge_sha'],scope_guard=deny)
                self.assertTrue(tree.is_dir())
                self.assertEqual(gc_fixtures.fixtures.git(['show-ref'],repo).stdout,before)

    def test_archive_refusal_keeps_original_and_creates_no_preservation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            with patch.object(writer,'STATE_ROOT',root/'state'),patch.object(writer,'WORKTREE_ROOT',root/'worktrees'),patch.object(gc,'C3_DB',root/'missing'):
                f=gc_fixtures.ReviewedArchiveTests()
                repo,remote,tree,payload=f.fixture(root);review=f.review(root,repo,tree,payload)
                def deny(*args):raise ValueError('scope revoked')
                with self.assertRaisesRegex(ValueError,'scope revoked'):gc.archive_reviewed_worktree(repo,review,scope_guard=deny)
                self.assertTrue(tree.is_dir())
                self.assertFalse((root/'state'/'preserved-worktrees').exists())
