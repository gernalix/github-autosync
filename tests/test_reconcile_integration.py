from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import github_autosync as autosync


def git(root: Path | None, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=root, text=True, capture_output=True)


class ReconcileIntegration(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bare = self.root / "remote.git"
        self.local = self.root / "local"
        self.assertEqual(0, git(None, "init", "--bare", str(self.bare)).returncode)
        self.assertEqual(0, git(None, "clone", str(self.bare), str(self.local)).returncode)
        self.assertEqual(0, git(self.local, "config", "user.email", "test@example.invalid").returncode)
        self.assertEqual(0, git(self.local, "config", "user.name", "Test").returncode)
        (self.local / "base.txt").write_bytes(b"base\n")
        self.commit(self.local, "base")
        self.assertEqual(0, git(self.local, "branch", "-M", "main").returncode)
        self.assertEqual(0, git(self.local, "push", "-u", "origin", "main").returncode)
        self.repo = {"name": "local", "url": str(self.bare), "default_branch": "main", "pushed_at": "A", "archived": "0"}
        self.entry = {"project_id": 1, "slug": "local", "worktree": str(self.local), "remote_url": str(self.bare), "branch": "main"}

    def commit(self, path: Path, message: str) -> None:
        self.assertEqual(0, git(path, "add", "-A").returncode)
        self.assertEqual(0, git(path, "commit", "-m", message).returncode)

    def peer(self) -> Path:
        other = self.root / "peer"
        self.assertEqual(0, git(None, "clone", str(self.bare), str(other)).returncode)
        self.assertEqual(0, git(other, "checkout", "main").returncode)
        self.assertEqual(0, git(other, "config", "user.email", "test@example.invalid").returncode)
        self.assertEqual(0, git(other, "config", "user.name", "Test").returncode)
        return other

    def reconcile(self) -> tuple[str, dict | None]:
        return autosync.sync_changed_repo(self.repo, self.root, dry_run=False,
                                          inventory_entry=self.entry, auto_commit_dirty=True)

    def test_missing_tracking_ref_with_narrow_refspec_is_repaired(self) -> None:
        self.assertEqual(0, git(self.local, "branch", "--unset-upstream").returncode)
        self.assertEqual(0, git(self.local, "config", "--replace-all", "remote.origin.fetch", "+refs/heads/other:refs/remotes/origin/other").returncode)
        self.assertEqual(0, git(self.local, "update-ref", "-d", "refs/remotes/origin/main").returncode)
        self.assertEqual(("up_to_date", None), self.reconcile())
        self.assertEqual("origin/main", git(self.local, "rev-parse", "--abbrev-ref", "@{u}").stdout.strip())
        self.assertIn("refs/heads/main", git(self.local, "config", "--get-all", "remote.origin.fetch").stdout)
        self.assertEqual(("up_to_date", None), self.reconcile())

    def test_clean_unborn_branch_checks_out_existing_remote(self) -> None:
        unborn = self.root / "unborn"
        self.assertEqual(0, git(None, "init", str(unborn)).returncode)
        self.assertEqual(0, git(unborn, "symbolic-ref", "HEAD", "refs/heads/main").returncode)
        self.assertEqual(0, git(unborn, "remote", "add", "origin", str(self.bare)).returncode)
        repo = {**self.repo, "name": "unborn"}
        entry = {**self.entry, "slug": "unborn", "worktree": str(unborn)}
        result, problem = autosync.sync_changed_repo(repo, self.root, dry_run=False,
                                                     inventory_entry=entry, auto_commit_dirty=True)
        self.assertEqual(("updated", None), (result, problem))
        self.assertEqual((0, 0), autosync.git_counts(unborn))
        self.assertEqual("", git(unborn, "status", "--porcelain").stdout)

    def test_unborn_branch_with_untracked_data_is_preserved(self) -> None:
        unborn = self.root / "unborn"
        self.assertEqual(0, git(None, "init", str(unborn)).returncode)
        self.assertEqual(0, git(unborn, "symbolic-ref", "HEAD", "refs/heads/main").returncode)
        self.assertEqual(0, git(unborn, "remote", "add", "origin", str(self.bare)).returncode)
        (unborn / "local.txt").write_bytes(b"preserve me")
        repo = {**self.repo, "name": "unborn"}
        entry = {**self.entry, "slug": "unborn", "worktree": str(unborn)}
        result, problem = autosync.sync_changed_repo(repo, self.root, dry_run=False,
                                                     inventory_entry=entry, auto_commit_dirty=True)
        self.assertEqual(("deferred", "unborn_branch_with_local_files"), (result, problem["kind"]))
        self.assertEqual(b"preserve me", (unborn / "local.txt").read_bytes())

    def test_stale_upstream_and_local_branch_different_from_default(self) -> None:
        self.assertEqual(0, git(self.local, "checkout", "-b", "legacy").returncode)
        self.assertEqual(0, git(self.local, "push", "-u", "origin", "legacy").returncode)
        peer = self.peer()
        self.assertEqual(0, git(peer, "push", "origin", "--delete", "legacy").returncode)
        self.assertEqual(("up_to_date", None), self.reconcile())
        self.assertEqual("origin/main", git(self.local, "rev-parse", "--abbrev-ref", "@{u}").stdout.strip())

    def test_local_only_branch_with_commits_never_pushes_to_default(self) -> None:
        self.assertEqual(0, git(self.local, "checkout", "-b", "feature").returncode)
        (self.local / "feature.txt").write_text("private branch\n")
        self.commit(self.local, "feature")
        before = git(self.bare, "rev-parse", "refs/heads/main").stdout.strip()
        result, problem = self.reconcile()
        self.assertEqual(("deferred", "remote_branch_ambiguous"), (result, problem["kind"]))
        self.assertEqual(before, git(self.bare, "rev-parse", "refs/heads/main").stdout.strip())

    def test_dirty_mixed_states_are_checkpointed_and_ignored_files_stay_out(self) -> None:
        (self.local / ".gitignore").write_text("ignored.tmp\n")
        (self.local / "ignored.tmp").write_bytes(b"private")
        (self.local / "staged.txt").write_bytes(b"staged")
        self.assertEqual(0, git(self.local, "add", ".gitignore", "staged.txt").returncode)
        (self.local / "staged.txt").write_bytes(b"staged plus unstaged")
        (self.local / "new.txt").write_bytes(b"untracked")
        self.assertEqual(0, git(self.local, "mv", "base.txt", "renamed.txt").returncode)
        self.assertEqual(("pushed", None), self.reconcile())
        self.assertTrue((self.local / "ignored.tmp").exists())
        self.assertEqual("", git(self.local, "status", "--porcelain").stdout)
        self.assertEqual("", git(self.local, "ls-files", "ignored.tmp").stdout)
        self.assertEqual(b"staged plus unstaged", (self.local / "staged.txt").read_bytes())

    def test_multiple_commits_diverge_cleanly_and_second_run_is_noop(self) -> None:
        peer = self.peer()
        for i in range(2):
            (peer / f"remote-{i}.txt").write_text(str(i))
            self.commit(peer, f"remote {i}")
        self.assertEqual(0, git(peer, "push", "origin", "main").returncode)
        for i in range(2):
            (self.local / f"local-{i}.txt").write_text(str(i))
            self.commit(self.local, f"local {i}")
        self.assertEqual(("pushed", None), self.reconcile())
        self.assertEqual(("up_to_date", None), self.reconcile())
        self.assertEqual((0, 0), autosync.git_counts(self.local))

    def test_rebase_conflict_aborts_and_preserves_bytes(self) -> None:
        peer = self.peer()
        (peer / "base.txt").write_bytes(b"remote\n")
        self.commit(peer, "remote")
        self.assertEqual(0, git(peer, "push", "origin", "main").returncode)
        (self.local / "base.txt").write_bytes(b"local\n")
        self.commit(self.local, "local")
        head = git(self.local, "rev-parse", "HEAD").stdout.strip()
        result, problem = self.reconcile()
        self.assertEqual("deferred", result)
        self.assertEqual("rebase_conflict", problem["kind"])
        self.assertEqual(head, git(self.local, "rev-parse", "HEAD").stdout.strip())
        self.assertEqual(b"local\n", (self.local / "base.txt").read_bytes())
        self.assertEqual("", git(self.local, "status", "--porcelain").stdout)

    def test_add_add_and_delete_modify_conflicts_preserve_local_commits(self) -> None:
        for conflict in ("add_add", "delete_modify"):
            with self.subTest(conflict=conflict):
                # Start each scenario with a fresh pair so no conflict leaks.
                self.setUp()
                peer = self.peer()
                if conflict == "add_add":
                    (peer / "collision.txt").write_bytes(b"remote\n")
                    self.commit(peer, "remote add")
                    (self.local / "collision.txt").write_bytes(b"local\n")
                    self.commit(self.local, "local add")
                else:
                    (peer / "base.txt").write_bytes(b"remote change\n")
                    self.commit(peer, "remote modify")
                    self.assertEqual(0, git(self.local, "rm", "base.txt").returncode)
                    self.commit(self.local, "local delete")
                self.assertEqual(0, git(peer, "push", "origin", "main").returncode)
                original = git(self.local, "rev-parse", "HEAD").stdout.strip()
                result, problem = self.reconcile()
                self.assertEqual(("deferred", "rebase_conflict"), (result, problem["kind"]))
                self.assertEqual(original, git(self.local, "rev-parse", "HEAD").stdout.strip())

    def test_push_race_uses_new_remote_evidence(self) -> None:
        peer = self.peer()
        (self.local / "local.txt").write_text("local")
        self.commit(self.local, "local")
        original_run = autosync.run
        raced = False
        def race(cmd: list[str], cwd: Path | None = None, **kwargs: object):
            nonlocal raced
            if cmd[:2] == ["git", "push"] and not raced:
                raced = True
                (peer / "remote.txt").write_text("remote")
                self.commit(peer, "remote")
                self.assertEqual(0, git(peer, "push", "origin", "main").returncode)
            return original_run(cmd, cwd, **kwargs)
        with mock.patch.object(autosync, "run", side_effect=race):
            result, problem = self.reconcile()
        self.assertEqual(("pushed", None), (result, problem))
        self.assertEqual((0, 0), autosync.git_counts(self.local))

    def test_existing_rebase_and_index_conflict_are_left_untouched(self) -> None:
        git_dir = self.local / ".git"
        (git_dir / "rebase-merge").mkdir()
        self.assertEqual("rebase_in_progress", self.reconcile()[1]["kind"])
        (git_dir / "rebase-merge").rmdir()
        peer = self.peer()
        (peer / "base.txt").write_text("remote\n")
        self.commit(peer, "remote")
        self.assertEqual(0, git(peer, "push", "origin", "main").returncode)
        (self.local / "base.txt").write_text("local\n")
        self.commit(self.local, "local")
        self.assertEqual(0, git(self.local, "fetch", "origin", "main").returncode)
        self.assertNotEqual(0, git(self.local, "merge", "origin/main").returncode)
        self.assertEqual("unresolved_conflicts", self.reconcile()[1]["kind"])
        self.assertNotEqual("", git(self.local, "ls-files", "-u").stdout)

    def test_worktree_change_during_stability_window_preserves_index(self) -> None:
        (self.local / "base.txt").write_bytes(b"first\n")
        before_index = (self.local / ".git" / "index").read_bytes()
        def change(_: float) -> None:
            (self.local / "base.txt").write_bytes(b"second\n")
        with mock.patch.object(autosync.time, "sleep", side_effect=change):
            result, problem = self.reconcile()
        self.assertEqual("deferred", result)
        self.assertEqual("worktree_changing", problem["kind"])
        self.assertEqual(before_index, (self.local / ".git" / "index").read_bytes())
        self.assertEqual(b"second\n", (self.local / "base.txt").read_bytes())

    def test_detached_and_existing_git_operation_do_not_mutate(self) -> None:
        self.assertEqual(0, git(self.local, "checkout", "--detach").returncode)
        self.assertEqual("detached_or_unknown_branch", self.reconcile()[1]["kind"])
        self.assertEqual(0, git(self.local, "switch", "main").returncode)
        git_dir = self.local / ".git"
        for marker, expected in (("MERGE_HEAD", "merge_in_progress"), ("CHERRY_PICK_HEAD", "cherry_pick_in_progress"), ("REVERT_HEAD", "revert_in_progress")):
            (git_dir / marker).write_text("marker")
            self.assertEqual(expected, self.reconcile()[1]["kind"])
            (git_dir / marker).unlink()

    def test_second_remote_with_matching_url_is_used(self) -> None:
        self.assertEqual(0, git(self.local, "remote", "rename", "origin", "backup").returncode)
        self.assertEqual(("up_to_date", None), self.reconcile())
        self.assertEqual("backup/main", git(self.local, "rev-parse", "--abbrev-ref", "@{u}").stdout.strip())


if __name__ == "__main__":
    unittest.main()
