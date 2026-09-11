from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import github_autosync as autosync


def git(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *cmd], cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)


def ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


class AutosyncTests(unittest.TestCase):
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

    def entry(self, repo: Path, bare: Path) -> dict[str, object]:
        return {"project_id": 1, "slug": repo.name, "worktree": str(repo), "remote_url": str(bare), "branch": "main"}

    def test_clean_ahead_repo_pushes_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, bare = self.make_repo_pair(Path(tmp) / "pair")
            (repo / "local.txt").write_text("local\n", encoding="utf-8")
            self.assertEqual(0, git(["add", "local.txt"], repo).returncode)
            self.assertEqual(0, git(["commit", "-m", "local"], repo).returncode)
            issues, pushed = autosync.audit_worktree(self.entry(repo, bare), auto_push=True, report_behind=True)
            self.assertEqual([], issues)
            self.assertTrue(pushed)
            self.assertEqual((0, 0), autosync.git_counts(repo))

    def test_dirty_repo_is_never_pushed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, bare = self.make_repo_pair(Path(tmp) / "pair")
            (repo / "local.txt").write_text("local\n", encoding="utf-8")
            self.assertEqual(0, git(["add", "local.txt"], repo).returncode)
            self.assertEqual(0, git(["commit", "-m", "local"], repo).returncode)
            (repo / "dirty.txt").write_text("dirty\n", encoding="utf-8")
            issues, pushed = autosync.audit_worktree(self.entry(repo, bare), auto_push=True, report_behind=True)
            self.assertFalse(pushed)
            self.assertEqual("dirty_with_unpushed_commits", issues[0]["kind"])

    def test_diverged_repo_is_deferred(self) -> None:
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
            issues, pushed = autosync.audit_worktree(self.entry(repo, bare), auto_push=True, report_behind=True)
            self.assertFalse(pushed)
            self.assertEqual("diverged", issues[0]["kind"])

    def test_second_unchanged_run_does_not_sync_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            projects.mkdir()
            (projects / "one").mkdir()
            state = root / "state"
            repo = {"owner": "gernalix", "name": "one", "url": "https://github.com/gernalix/one", "default_branch": "main", "pushed_at": "2026-09-11T10:00:00Z", "archived": "0"}
            autosync.save_repo_state(state, {"one": autosync.repo_fingerprint(repo)})
            args = autosync.build_parser().parse_args(["--projects-dir", str(projects), "--state-dir", str(state), "--megavault", str(root / "mv"), "--no-telegram", "run"])
            with (
                mock.patch.object(autosync, "megavault_inventory", return_value=[]),
                mock.patch.object(autosync, "audit_inventory", return_value=([], 0)),
                mock.patch.object(autosync, "github_repos", return_value=[{k: v for k, v in repo.items() if k != "owner"}]),
                mock.patch.object(autosync, "sync_changed_repo") as sync,
                mock.patch.object(autosync, "megavault_registered_remotes", return_value={autosync.normalize_remote(repo["url"])}),
                mock.patch.object(autosync, "register_in_megavault", return_value={"validation": "not_needed", "deferred": 0}),
            ):
                self.assertEqual(0, autosync.command_run(args))
                sync.assert_not_called()

    def test_only_changed_repo_is_updated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            projects.mkdir()
            for name in ("one", "two"):
                (projects / name).mkdir()
            state = root / "state"
            one_old = {"name": "one", "url": "https://github.com/gernalix/one", "default_branch": "main", "pushed_at": "A", "archived": "0"}
            two = {"name": "two", "url": "https://github.com/gernalix/two", "default_branch": "main", "pushed_at": "A", "archived": "0"}
            autosync.save_repo_state(state, {"one": autosync.repo_fingerprint(one_old), "two": autosync.repo_fingerprint(two)})
            one_new = {**one_old, "pushed_at": "B"}
            args = autosync.build_parser().parse_args(["--projects-dir", str(projects), "--state-dir", str(state), "--megavault", str(root / "mv"), "--no-telegram", "run"])
            with (
                mock.patch.object(autosync, "megavault_inventory", return_value=[]),
                mock.patch.object(autosync, "audit_inventory", return_value=([], 0)),
                mock.patch.object(autosync, "github_repos", return_value=[one_new, two]),
                mock.patch.object(autosync, "sync_changed_repo", return_value=("updated", None)) as sync,
                mock.patch.object(autosync, "megavault_registered_remotes", return_value={autosync.normalize_remote(one_new["url"]), autosync.normalize_remote(two["url"])}),
                mock.patch.object(autosync, "register_in_megavault", return_value={"validation": "not_needed", "deferred": 0}),
            ):
                self.assertEqual(0, autosync.command_run(args))
                self.assertEqual(1, sync.call_count)
                self.assertEqual("one", sync.call_args.args[0]["name"])

    def test_new_repo_is_cloned_directly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            repo = {"name": "new", "url": "https://github.com/gernalix/new", "default_branch": "main", "pushed_at": "A", "archived": "0"}
            with mock.patch.object(autosync, "run", return_value=ok()) as runner:
                self.assertEqual("cloned", autosync.clone_repo(repo, projects, dry_run=False))
            cmd = runner.call_args.args[0]
            self.assertEqual(["git", "clone", "--origin", "origin"], cmd[:4])
            self.assertIn(repo["url"], cmd)

    def test_megavault_deferred_is_not_false_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = autosync.build_parser().parse_args(["--projects-dir", str(root / "projects"), "--state-dir", str(root / "state"), "--megavault", str(root / "mv"), "--no-telegram", "run"])
            with (
                mock.patch.object(autosync, "megavault_inventory", return_value=[]),
                mock.patch.object(autosync, "audit_inventory", return_value=([], 0)),
                mock.patch.object(autosync, "github_repos", return_value=[]),
                mock.patch.object(autosync, "megavault_registered_remotes", return_value=set()),
                mock.patch.object(autosync, "register_in_megavault", return_value={"validation": "deferred_dirty", "deferred": 1}),
                mock.patch.object(autosync, "update_telegram_alert_state", return_value="disabled"),
                mock.patch("builtins.print") as printer,
            ):
                self.assertEqual(0, autosync.command_run(args))
            payload = json.loads(printer.call_args.args[0])
            self.assertEqual("deferred", payload["status"])

    def test_real_github_failure_is_nonzero(self) -> None:
        with mock.patch.object(autosync, "megavault_inventory", return_value=[]), mock.patch.object(autosync, "audit_inventory", return_value=([], 0)), mock.patch.object(autosync, "github_repos", side_effect=autosync.AutosyncError("github_repo_list_failed")):
            self.assertEqual(75, autosync.main(["--no-telegram", "run"]))


if __name__ == "__main__":
    unittest.main()
