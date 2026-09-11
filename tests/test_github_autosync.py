from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest import mock

import github_autosync as autosync


def ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


def fail(stderr: str = "failed") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 1, stdout="", stderr=stderr)


def git(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *cmd], cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)


class GithubAutosyncTests(unittest.TestCase):
    def make_repo_pair(self, root: Path) -> tuple[Path, Path]:
        root.mkdir(parents=True, exist_ok=True)
        bare = root / "origin.git"
        repo = root / "repo"
        self.assertEqual(0, git(["init", "--bare", str(bare)]).returncode)
        self.assertEqual(0, git(["clone", str(bare), str(repo)]).returncode)
        self.assertEqual(0, git(["config", "user.email", "test@example.invalid"], repo).returncode)
        self.assertEqual(0, git(["config", "user.name", "Test"], repo).returncode)
        (repo / "base.txt").write_text("base\n", encoding="utf-8")
        self.assertEqual(0, git(["add", "base.txt"], repo).returncode)
        self.assertEqual(0, git(["commit", "-m", "base"], repo).returncode)
        self.assertEqual(0, git(["branch", "-M", "main"], repo).returncode)
        self.assertEqual(0, git(["push", "-u", "origin", "main"], repo).returncode)
        return repo, bare

    def entry(self, repo: Path, bare: Path, project_id: int = 1) -> dict[str, object]:
        return {
            "project_id": project_id,
            "slug": repo.name,
            "worktree": str(repo),
            "remote_url": str(bare),
            "branch": "main",
        }

    def test_clean_ahead_repo_is_pushed_safely(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, bare = self.make_repo_pair(root / "pair")
            (repo / "local.txt").write_text("local\n", encoding="utf-8")
            self.assertEqual(0, git(["add", "local.txt"], repo).returncode)
            self.assertEqual(0, git(["commit", "-m", "local"], repo).returncode)

            issues, pushed = autosync.audit_worktree(
                self.entry(repo, bare), auto_push=True, report_behind=True
            )

            self.assertEqual([], issues)
            self.assertTrue(pushed)
            self.assertEqual((0, 0), autosync.git_counts(repo))

    def test_dirty_ahead_repo_is_not_pushed_and_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, bare = self.make_repo_pair(root / "pair")
            (repo / "local.txt").write_text("local\n", encoding="utf-8")
            self.assertEqual(0, git(["add", "local.txt"], repo).returncode)
            self.assertEqual(0, git(["commit", "-m", "local"], repo).returncode)
            (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")

            issues, pushed = autosync.audit_worktree(
                self.entry(repo, bare), auto_push=True, report_behind=True
            )

            self.assertFalse(pushed)
            self.assertEqual("dirty_with_unpushed_commits", issues[0]["kind"])
            self.assertIn("ahead=1", issues[0]["detail"])
            remote_head = git(["--git-dir", str(bare), "rev-parse", "refs/heads/main"]).stdout.strip()
            local_parent = git(["rev-parse", "HEAD^"], repo).stdout.strip()
            self.assertEqual(local_parent, remote_head)

    def test_remote_advance_race_cannot_be_force_pushed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo, bare = self.make_repo_pair(root / "pair")
            other = root / "other"
            self.assertEqual(0, git(["clone", str(bare), str(other)]).returncode)
            self.assertEqual(0, git(["config", "user.email", "test@example.invalid"], other).returncode)
            self.assertEqual(0, git(["config", "user.name", "Test"], other).returncode)
            self.assertEqual(0, git(["checkout", "main"], other).returncode)

            (repo / "local.txt").write_text("local\n", encoding="utf-8")
            self.assertEqual(0, git(["add", "local.txt"], repo).returncode)
            self.assertEqual(0, git(["commit", "-m", "local"], repo).returncode)
            (other / "remote.txt").write_text("remote\n", encoding="utf-8")
            self.assertEqual(0, git(["add", "remote.txt"], other).returncode)
            self.assertEqual(0, git(["commit", "-m", "remote"], other).returncode)
            self.assertEqual(0, git(["push", "origin", "main"], other).returncode)

            issues, pushed = autosync.audit_worktree(
                self.entry(repo, bare), auto_push=True, report_behind=True
            )

            self.assertFalse(pushed)
            self.assertEqual("diverged", issues[0]["kind"])

    def test_megavault_inventory_uses_active_canonical_worktrees(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = root / "MegaVault"
            vault.mkdir()
            db = sqlite3.connect(vault / "megavault.sqlite")
            db.executescript(
                """
                create table projects(project_id integer primary key, slug text, name text, status text, archived integer);
                create table repositories(
                    repository_id text primary key, project_id integer, worktree_path text,
                    remote_url text, branch text, status text, canonical integer, repository_kind text
                );
                insert into projects values(1,'one','One','active',0);
                insert into projects values(2,'old','Old','active',1);
                insert into repositories values('R1',1,'/tmp/one','https://github.com/gernalix/one','main','active',1,'local_worktree');
                insert into repositories values('R2',2,'/tmp/old','https://github.com/gernalix/old','main','active',1,'local_worktree');
                """
            )
            db.commit()
            db.close()

            rows = autosync.megavault_inventory(vault)

            self.assertEqual(1, len(rows))
            self.assertEqual(1, rows[0]["project_id"])
            self.assertEqual("one", rows[0]["slug"])

    def test_telegram_alerts_are_deduplicated_and_resolution_is_sent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            issues = [
                {
                    "project_id": 7,
                    "repo": "demo",
                    "worktree": "/tmp/demo",
                    "kind": "dirty_with_unpushed_commits",
                    "detail": "ahead=2,behind=0",
                }
            ]
            with mock.patch.object(autosync, "send_telegram", return_value=True) as send:
                self.assertEqual("alert_sent", autosync.update_telegram_alert_state(state, issues, enabled=True))
                self.assertEqual("unchanged", autosync.update_telegram_alert_state(state, issues, enabled=True))
                self.assertEqual("resolved_sent", autosync.update_telegram_alert_state(state, [], enabled=True))
            self.assertEqual(2, send.call_count)
            self.assertIn("project_id=7", send.call_args_list[0].args[1])
            self.assertIn("risolti", send.call_args_list[1].args[1])

    def test_notification_failure_does_not_advance_alert_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            issues = [autosync.issue(None, "ghorg_failed")]
            with mock.patch.object(autosync, "send_telegram", return_value=False):
                self.assertEqual("notify_failed", autosync.update_telegram_alert_state(state, issues, enabled=True))
            self.assertFalse((state / autosync.ALERT_STATE_FILE).exists())

    def test_ghorg_failure_skips_megavault_registration(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            args = autosync.build_parser().parse_args(["--projects-dir", tmp, "--no-telegram", "run"])
            with (
                mock.patch.object(autosync, "megavault_inventory", return_value=[]),
                mock.patch.object(autosync, "audit_inventory", return_value=([], 0)),
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
