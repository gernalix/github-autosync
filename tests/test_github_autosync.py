from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import github_autosync as autosync


def ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


def fail(stderr: str = "failed") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 1, stdout="", stderr=stderr)


class GithubAutosyncTests(unittest.TestCase):
    def test_ghorg_failure_skips_megavault_registration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = autosync.build_parser().parse_args(["--projects-dir", tmp, "run"])
            with (
                mock.patch.object(
                    autosync,
                    "github_repos",
                    return_value=[{"name": "repo", "url": "https://github.com/gernalix/repo", "default_branch": "main"}],
                ),
                mock.patch.object(autosync, "run_ghorg", return_value=fail("ghorg failed")),
                mock.patch.object(autosync, "register_in_megavault") as register,
            ):
                self.assertEqual(autosync.command_run(args), 75)
                register.assert_not_called()

    def test_missing_or_mismatched_worktree_is_deferred(self) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], cwd: Path | None = None, *, timeout: int = 300, env: dict[str, str] | None = None):
            calls.append(cmd)
            if cmd[:2] == ["git", "status"]:
                return ok("")
            if cmd[:2] == ["git", "fetch"]:
                return ok("")
            if cmd[:2] == ["git", "rev-list"]:
                return ok("0\t0\n")
            if cmd[:2] == ["git", "rev-parse"]:
                return ok("before\n")
            if cmd[0] == "python3" and cmd[-1] == "validate":
                return ok("VALIDATE=PASS\n")
            self.fail(f"unexpected command: {cmd}")

        repos = [{"owner": "gernalix", "name": "repo", "url": "https://github.com/gernalix/repo", "default_branch": "main"}]
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(autosync, "run", side_effect=fake_run), mock.patch.object(autosync, "git_repo_matches_remote", return_value=False):
            result = autosync.register_in_megavault(Path(tmp) / "MegaVault", Path(tmp) / "projects", repos, dry_run=False)
        self.assertEqual(1, result["deferred"])
        self.assertFalse(any(cmd and cmd[0] == "python3" and "register-github-repo" in cmd for cmd in calls))

    def test_megavault_remote_is_fetched_before_sync_check(self) -> None:
        calls: list[list[str]] = []

        def fake_run(cmd: list[str], cwd: Path | None = None, *, timeout: int = 300, env: dict[str, str] | None = None):
            calls.append(cmd)
            if cmd[:2] == ["git", "status"]:
                return ok("")
            if cmd[:2] == ["git", "fetch"]:
                return ok("")
            if cmd[:2] == ["git", "rev-list"]:
                return ok("1\t0\n")
            self.fail(f"unexpected command: {cmd}")

        repos = [{"owner": "gernalix", "name": "repo", "url": "https://github.com/gernalix/repo", "default_branch": "main"}]
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(autosync, "run", side_effect=fake_run):
            result = autosync.register_in_megavault(Path(tmp) / "MegaVault", Path(tmp) / "projects", repos, dry_run=False)
        self.assertEqual("deferred_not_synced", result["validation"])
        self.assertLess(calls.index(["git", "fetch", "origin"]), calls.index(["git", "rev-list", "--left-right", "--count", "HEAD...@{u}"]))

    def test_megavault_metadata_commit_is_fail_fast(self) -> None:
        def fake_run(cmd: list[str], cwd: Path | None = None, *, timeout: int = 300, env: dict[str, str] | None = None):
            if cmd[:2] == ["git", "status"]:
                return ok("")
            if cmd[:2] == ["git", "fetch"]:
                return ok("")
            if cmd[:2] == ["git", "rev-list"]:
                return ok("0\t0\n")
            if cmd[:2] == ["git", "rev-parse"]:
                return ok("before\n")
            if cmd[0] == "python3" and "register-github-repo" in cmd:
                return ok("status=created\n")
            if cmd[0] == "python3" and cmd[-1] == "validate":
                return ok("VALIDATE=PASS\n")
            if cmd[:2] == ["git", "add"]:
                return fail("add failed")
            self.fail(f"unexpected command: {cmd}")

        repos = [{"owner": "gernalix", "name": "repo", "url": "https://github.com/gernalix/repo", "default_branch": "main"}]
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(autosync, "run", side_effect=fake_run), mock.patch.object(autosync, "git_repo_matches_remote", return_value=True):
            with self.assertRaisesRegex(autosync.AutosyncError, "megavault_git_add_failed"):
                autosync.register_in_megavault(Path(tmp) / "MegaVault", Path(tmp) / "projects", repos, dry_run=False)


if __name__ == "__main__":
    unittest.main()
