from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import repo_single_writer as writer


def git(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def cp(code: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, stdout=stdout, stderr=stderr)


class SingleWriterTests(unittest.TestCase):
    def make_repo(self, root: Path) -> tuple[Path, Path]:
        bare = root / "origin.git"
        seed = root / "seed"
        repo = root / "repo"
        self.assertEqual(0, git(["init", "--bare", str(bare)]).returncode)
        self.assertEqual(0, git(["clone", str(bare), str(seed)]).returncode)
        for path in (seed,):
            self.assertEqual(0, git(["config", "user.name", "Test"], path).returncode)
            self.assertEqual(0, git(["config", "user.email", "test@example.invalid"], path).returncode)
        (seed / "base.txt").write_text("base\n", encoding="utf-8")
        self.assertEqual(0, git(["add", "base.txt"], seed).returncode)
        self.assertEqual(0, git(["commit", "-m", "base"], seed).returncode)
        self.assertEqual(0, git(["branch", "-M", "main"], seed).returncode)
        self.assertEqual(0, git(["push", "-u", "origin", "main"], seed).returncode)
        self.assertEqual(0, git(["symbolic-ref", "HEAD", "refs/heads/main"], bare).returncode)
        self.assertEqual(0, git(["clone", str(bare), str(repo)]).returncode)
        self.assertEqual(0, git(["config", "user.name", "Test"], repo).returncode)
        self.assertEqual(0, git(["config", "user.email", "test@example.invalid"], repo).returncode)
        return repo, bare

    def test_guard_blocks_direct_main_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, _ = self.make_repo(Path(tmp))
            writer.ensure_guard(repo, "main")
            (repo / "direct.txt").write_text("blocked\n", encoding="utf-8")
            self.assertEqual(0, git(["add", "direct.txt"], repo).returncode)
            commit = git(["commit", "-m", "direct main write"], repo)
            self.assertNotEqual(0, commit.returncode)
            self.assertIn("single-writer protected", commit.stderr)

    def test_authorized_writer_can_fast_forward_main(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, _ = self.make_repo(Path(tmp))
            writer.ensure_guard(repo, "main")
            self.assertEqual(0, git(["checkout", "-b", "task/demo"], repo).returncode)
            (repo / "task.txt").write_text("task\n", encoding="utf-8")
            self.assertEqual(0, git(["add", "task.txt"], repo).returncode)
            self.assertEqual(0, git(["commit", "-m", "task"], repo).returncode)
            new = git(["rev-parse", "HEAD"], repo).stdout.strip()
            self.assertEqual(0, git(["checkout", "main"], repo).returncode)
            old = git(["rev-parse", "HEAD"], repo).stdout.strip()
            auth = writer.authorize_ref_update(repo, old, new, "main")
            try:
                merged = git(["merge", "--ff-only", "task/demo"], repo)
                self.assertEqual(0, merged.returncode, merged.stderr)
            finally:
                auth.unlink(missing_ok=True)
            self.assertEqual(new, git(["rev-parse", "main"], repo).stdout.strip())

    def test_guard_allows_fast_forward_to_fetched_remote_tip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, bare = self.make_repo(root)
            writer.ensure_guard(repo, "main")
            other = root / "other"
            self.assertEqual(0, git(["clone", str(bare), str(other)]).returncode)
            self.assertEqual(0, git(["config", "user.name", "Test"], other).returncode)
            self.assertEqual(0, git(["config", "user.email", "test@example.invalid"], other).returncode)
            (other / "remote.txt").write_text("remote\n", encoding="utf-8")
            self.assertEqual(0, git(["add", "remote.txt"], other).returncode)
            self.assertEqual(0, git(["commit", "-m", "remote"], other).returncode)
            self.assertEqual(0, git(["push", "origin", "main"], other).returncode)
            self.assertEqual(0, git(["fetch", "origin"], repo).returncode)
            merged = git(["merge", "--ff-only", "origin/main"], repo)
            self.assertEqual(0, merged.returncode, merged.stderr)

    def test_start_task_creates_isolated_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _ = self.make_repo(root / "git")
            state = root / "state"
            worktrees = root / "worktrees"
            with (
                mock.patch.object(writer, "STATE_ROOT", state),
                mock.patch.object(writer, "WORKTREE_ROOT", worktrees),
            ):
                payload = writer.start_task(repo, "123456", "codex")
                task_path = Path(payload["worktree"])
                self.assertTrue(task_path.is_dir())
                self.assertEqual("task/123456", git(["branch", "--show-current"], task_path).stdout.strip())
                self.assertEqual("main", git(["branch", "--show-current"], repo).stdout.strip())
                again = writer.start_task(repo, "123456", "codex")
                self.assertEqual(payload["worktree"], again["worktree"])

    def test_task_lease_is_created_and_heartbeat_is_renewable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _ = self.make_repo(root / "git")
            state = root / "state"
            worktrees = root / "worktrees"
            with (
                mock.patch.object(writer, "STATE_ROOT", state),
                mock.patch.object(writer, "WORKTREE_ROOT", worktrees),
            ):
                payload = writer.start_task(repo, "lease-demo", "codex")
                self.assertEqual("active", payload["status"])
                self.assertTrue(payload["heartbeat_at"])
                self.assertTrue(payload["lease_expires_at"])
                renewed = writer.heartbeat_task(repo, "lease-demo")
                self.assertEqual("active", renewed["status"])
                self.assertTrue(Path(renewed["worktree"]).exists())

    def test_cleanup_after_merge_preserves_safety_and_removes_clean_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _ = self.make_repo(root / "git")
            state = root / "state"
            worktrees = root / "worktrees"
            with (
                mock.patch.object(writer, "STATE_ROOT", state),
                mock.patch.object(writer, "WORKTREE_ROOT", worktrees),
            ):
                payload = writer.start_task(repo, "cleanup-demo", "codex")
                task = Path(payload["worktree"])
                (task / "feature.txt").write_text("feature\n", encoding="utf-8")
                self.assertEqual(0, git(["add", "feature.txt"], task).returncode)
                self.assertEqual(0, git(["commit", "-m", "feature"], task).returncode)
                head = git(["rev-parse", "HEAD"], task).stdout.strip()
                self.assertEqual(0, git(["push", "-u", "origin", payload["branch"]], task).returncode)
                result = writer.cleanup_task_after_merge(
                    payload["repo"],
                    payload["branch"],
                    expected_head=head,
                    merge_sha="merged123",
                )
                self.assertEqual("merged", result["status"])
                self.assertFalse(task.exists())
                record = json.loads(writer._task_record(repo, "cleanup-demo").read_text(encoding="utf-8"))
                self.assertEqual("merged", record["status"])
                self.assertIsNone(record["lease_expires_at"])

    def test_start_roadmap_task_resolves_canonical_repo_and_creates_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _ = self.make_repo(root / "git")
            state = root / "state"
            worktrees = root / "worktrees"
            with (
                mock.patch.object(writer, "STATE_ROOT", state),
                mock.patch.object(writer, "WORKTREE_ROOT", worktrees),
                mock.patch.object(writer, "resolve_repo_path", return_value=repo),
            ):
                payload = writer.start_roadmap_task("gernalix/example", "1", "654321")
                self.assertEqual("task/654321", payload["branch"])
                self.assertTrue(Path(payload["worktree"]).exists())

    def test_wait_any_is_noop_for_non_git_roadmap_task(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(writer, "STATE_ROOT", Path(tmp) / "state"):
                payload = writer.wait_task_any("654321", timeout=0.1)
                self.assertEqual("no-task-record", payload["status"])

    def test_finish_task_pushes_branch_and_queues_pr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, _ = self.make_repo(root / "git")
            state = root / "state"
            worktrees = root / "worktrees"
            original_run = writer.run
            pr_lists = 0

            def fake_run(cmd: list[str], cwd: Path | None = None, timeout: int = 300):
                nonlocal pr_lists
                if cmd and cmd[0] == "git":
                    return original_run(cmd, cwd, timeout)
                if cmd[:3] == ["gh", "pr", "list"]:
                    pr_lists += 1
                    if pr_lists == 1:
                        return cp(stdout="[]\n")
                    return cp(stdout='[{"number":7,"url":"https://example/pr/7"}]\n')
                if cmd[:3] == ["gh", "pr", "create"]:
                    return cp(stdout="https://example/pr/7\n")
                raise AssertionError(cmd)

            with (
                mock.patch.object(writer, "STATE_ROOT", state),
                mock.patch.object(writer, "WORKTREE_ROOT", worktrees),
                mock.patch.object(writer, "run", side_effect=fake_run),
            ):
                payload = writer.start_task(repo, "alpha", "chatgpt")
                task = Path(payload["worktree"])
                (task / "feature.txt").write_text("feature\n", encoding="utf-8")
                ready = writer.finish_task(repo, "alpha")
                self.assertEqual("ready", ready["status"])
                self.assertEqual(7, ready["pr_number"])
                self.assertEqual(0, git(["show-ref", "--verify", "--quiet", "refs/remotes/origin/task/alpha"], task).returncode)

    def test_integrate_pr_merges_only_ready_task_pr(self) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], cwd: Path | None = None, timeout: int = 300):
            calls.append(cmd)
            if cmd[:3] == ["gh", "pr", "view"]:
                return cp(stdout=json.dumps({
                    "title": "[single-writer] 123",
                    "isDraft": False,
                    "mergeable": "MERGEABLE",
                    "baseRefName": "main",
                    "headRefName": "task/123",
                    "headRefOid": "abc123",
                    "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
                }))
            if cmd[:3] == ["gh", "repo", "view"]:
                return cp(stdout=json.dumps({"defaultBranchRef": {"name": "main"}}))
            if cmd[:3] == ["gh", "api", "--method"]:
                return cp(stdout=json.dumps({"merged": True, "sha": "merge123"}))
            raise AssertionError(cmd)

        with (
            mock.patch.object(writer, "run", side_effect=fake_run),
            mock.patch.object(writer, "STATE_ROOT", Path("/tmp/single-writer-test-state")),
        ):
            result = writer.integrate_pr("gernalix/example", 4)
        self.assertEqual("merged", result["status"])
        self.assertTrue(any(cmd[:2] == ["gh", "api"] for cmd in calls))

    def test_failed_or_pending_checks_are_not_merged(self) -> None:
        def fake_run(cmd: list[str], cwd: Path | None = None, timeout: int = 300):
            if cmd[:3] == ["gh", "pr", "view"]:
                return cp(stdout=json.dumps({
                    "title": "[single-writer] 123",
                    "isDraft": False,
                    "mergeable": "MERGEABLE",
                    "baseRefName": "main",
                    "headRefName": "task/123",
                    "headRefOid": "abc123",
                    "statusCheckRollup": [{"status": "IN_PROGRESS", "conclusion": ""}],
                }))
            if cmd[:3] == ["gh", "repo", "view"]:
                return cp(stdout=json.dumps({"defaultBranchRef": {"name": "main"}}))
            raise AssertionError(cmd)

        with (
            mock.patch.object(writer, "run", side_effect=fake_run),
            mock.patch.object(writer, "STATE_ROOT", Path("/tmp/single-writer-test-state")),
        ):
            result = writer.integrate_pr("gernalix/example", 4)
        self.assertEqual("deferred", result["status"])
        self.assertEqual("checks-pending", result["reason"])


if __name__ == "__main__":
    unittest.main()
