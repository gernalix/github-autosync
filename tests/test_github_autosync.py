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

    def test_push_failure_is_retried_only_after_fresh_fetch(self) -> None:
        calls = {"push": 0, "fetch": 0}

        def fake_run(cmd: list[str], cwd: Path | None = None, **kwargs: object) -> subprocess.CompletedProcess[str]:
            if cmd[:2] == ["git", "push"]:
                calls["push"] += 1
                if calls["push"] == 1:
                    return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="rejected")
                return ok()
            if cmd[:3] == ["git", "fetch", "--prune"]:
                calls["fetch"] += 1
                return ok()
            raise AssertionError(cmd)

        with (
            mock.patch.object(autosync, "run", side_effect=fake_run),
            mock.patch.object(autosync, "git_counts", side_effect=[(1, 0), (0, 0)]),
        ):
            action, detail = autosync._push_with_race_recovery(
                Path("/tmp/repo"),
                "origin",
                "main",
                allow_rebase=True,
            )
        self.assertEqual("pushed", action)
        self.assertEqual("", detail)
        self.assertEqual(2, calls["push"])
        self.assertEqual(2, calls["fetch"])

    def test_sync_changed_roadmap_dispatches_to_canonical_reconciler(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            projects = Path(tmp)
            worktree = projects / "codex-roadmap"
            worktree.mkdir()
            repo = {
                "name": "codex-roadmap",
                "url": "https://github.com/gernalix/codex-roadmap",
                "default_branch": "main",
                "pushed_at": "A",
                "archived": "0",
            }
            with (
                mock.patch.object(autosync, "git_repo_matches_remote", return_value=True),
                mock.patch.object(
                    autosync,
                    "sync_roadmap_repo",
                    return_value=("up_to_date", None),
                ) as reconcile,
            ):
                result = autosync.sync_changed_repo(repo, projects, dry_run=False)
            self.assertEqual(("up_to_date", None), result)
            reconcile.assert_called_once()
            self.assertEqual(worktree, reconcile.call_args.args[1])

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

    def test_missing_upstream_tracks_matching_origin_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, bare = self.make_repo_pair(Path(tmp) / "pair")
            self.assertEqual(0, git(["branch", "--unset-upstream"], repo).returncode)
            self.assertEqual(0, git(["update-ref", "-d", "refs/remotes/origin/main"], repo).returncode)
            issues, _ = autosync.audit_worktree(self.entry(repo, bare), auto_push=False, report_behind=True)
            self.assertEqual([], issues)
            upstream = git(["rev-parse", "--abbrev-ref", "@{u}"], repo)
            self.assertEqual("origin/main", upstream.stdout.strip())

    def test_missing_upstream_without_matching_origin_stays_deferred(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo, bare = self.make_repo_pair(Path(tmp) / "pair")
            self.assertEqual(0, git(["checkout", "-b", "local-only"], repo).returncode)
            entry = {**self.entry(repo, bare), "branch": "local-only"}
            issues, _ = autosync.audit_worktree(entry, auto_push=False, report_behind=True)
            self.assertEqual("no_upstream", issues[0]["kind"])

    def test_roadmap_is_reconciled_even_when_remote_fingerprint_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            projects.mkdir()
            (projects / "codex-roadmap").mkdir()
            state = root / "state"
            repo = {"owner": "gernalix", "name": "codex-roadmap", "url": "https://github.com/gernalix/codex-roadmap", "default_branch": "main", "pushed_at": "2026-09-11T10:00:00Z", "archived": "0"}
            autosync.save_repo_state(state, {"codex-roadmap": autosync.repo_fingerprint(repo)})
            args = autosync.build_parser().parse_args(["--projects-dir", str(projects), "--state-dir", str(state), "--megavault", str(root / "mv"), "--no-telegram", "--no-data-mirror", "run"])
            with (
                mock.patch.object(autosync, "megavault_inventory", return_value=[]),
                mock.patch.object(autosync, "audit_inventory", return_value=([], 0)),
                mock.patch.object(autosync, "github_repos", return_value=[{k: v for k, v in repo.items() if k != "owner"}]),
                mock.patch.object(autosync, "sync_changed_repo", return_value=("up_to_date", None)) as sync,
                mock.patch.object(autosync, "megavault_registered_remotes", return_value={autosync.normalize_remote(repo["url"])}),
                mock.patch.object(autosync, "register_in_megavault", return_value={"validation": "not_needed", "deferred": 0}),
            ):
                self.assertEqual(0, autosync.command_run(args))
            sync.assert_called_once()
            self.assertEqual("codex-roadmap", sync.call_args.args[0]["name"])

    def test_only_changed_repo_is_updated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            projects.mkdir()
            for name in ("codex-roadmap", "github-autosync"):
                (projects / name).mkdir()
            state = root / "state"
            one_old = {"name": "codex-roadmap", "url": "https://github.com/gernalix/codex-roadmap", "default_branch": "main", "pushed_at": "A", "archived": "0"}
            two = {"name": "github-autosync", "url": "https://github.com/gernalix/github-autosync", "default_branch": "main", "pushed_at": "A", "archived": "0"}
            autosync.save_repo_state(state, {"codex-roadmap": autosync.repo_fingerprint(one_old), "github-autosync": autosync.repo_fingerprint(two)})
            one_new = {**one_old, "pushed_at": "B"}
            args = autosync.build_parser().parse_args(["--projects-dir", str(projects), "--state-dir", str(state), "--megavault", str(root / "mv"), "--no-telegram", "--no-data-mirror", "run"])
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
                self.assertEqual("codex-roadmap", sync.call_args.args[0]["name"])

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
            args = autosync.build_parser().parse_args(["--projects-dir", str(root / "projects"), "--state-dir", str(root / "state"), "--megavault", str(root / "mv"), "--no-telegram", "--no-data-mirror", "run"])
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

    def test_double_discovery_syncs_once_and_preserves_project_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "canonical"
            worktree.mkdir()
            remote = "https://github.com/gernalix/codex-roadmap"
            inventory = {"project_id": 42, "slug": "codex-roadmap", "worktree": str(worktree), "remote_url": remote, "branch": "main"}
            repo = {"name": "codex-roadmap", "url": remote, "default_branch": "main", "pushed_at": "A", "archived": "0"}
            args = autosync.build_parser().parse_args(["--projects-dir", str(root / "projects"), "--state-dir", str(root / "state"), "--megavault", str(root / "mv"), "--no-telegram", "--no-data-mirror", "run"])
            with (
                mock.patch.object(autosync, "megavault_inventory", return_value=[inventory]),
                mock.patch.object(autosync, "audit_inventory", return_value=([], 0)) as audit,
                mock.patch.object(autosync, "github_repos", return_value=[repo]),
                mock.patch.object(autosync, "sync_changed_repo", return_value=("updated", None)) as sync,
                mock.patch.object(autosync, "megavault_registered_remotes", return_value={autosync.normalize_remote(remote)}),
                mock.patch.object(autosync, "register_in_megavault", return_value={"validation": "not_needed", "deferred": 0}),
            ):
                self.assertEqual(0, autosync.command_run(args))
            audit.assert_called_once_with([], auto_push=True, report_behind=False, fetch_remote=False)
            self.assertEqual(1, sync.call_count)
            self.assertEqual(42, sync.call_args.kwargs["inventory_entry"]["project_id"])

    def test_unchanged_fingerprint_uses_canonical_worktree_before_skip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            duplicate = root / "projects" / "codex-roadmap"
            duplicate.mkdir(parents=True)
            canonical = root / "canonical"
            remote = "https://github.com/gernalix/codex-roadmap"
            inventory = {"project_id": 42, "slug": "codex-roadmap", "worktree": str(canonical), "remote_url": remote, "branch": "main"}
            repo = {"name": "codex-roadmap", "url": remote, "default_branch": "main", "pushed_at": "A", "archived": "0"}
            state = root / "state"
            autosync.save_repo_state(state, {"codex-roadmap": autosync.repo_fingerprint(repo)})
            args = autosync.build_parser().parse_args(["--projects-dir", str(root / "projects"), "--state-dir", str(state), "--megavault", str(root / "mv"), "--no-telegram", "--no-data-mirror", "run"])
            with (
                mock.patch.object(autosync, "megavault_inventory", return_value=[inventory]),
                mock.patch.object(autosync, "audit_inventory", return_value=([], 0)),
                mock.patch.object(autosync, "github_repos", return_value=[repo]),
                mock.patch.object(autosync, "sync_changed_repo", return_value=("updated", None)) as sync,
                mock.patch.object(autosync, "megavault_registered_remotes", return_value={autosync.normalize_remote(remote)}),
                mock.patch.object(autosync, "register_in_megavault", return_value={"validation": "not_needed", "deferred": 0}),
            ):
                self.assertEqual(0, autosync.command_run(args))
            sync.assert_called_once()
            self.assertEqual(str(canonical), sync.call_args.kwargs["inventory_entry"]["worktree"])

    def test_github_repo_outside_allowlist_is_ignored_and_state_removed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            autosync.save_repo_state(state, {"unmanaged": "old"})
            unmanaged = {"name": "unmanaged", "url": "https://github.com/gernalix/unmanaged", "default_branch": "main", "pushed_at": "A", "archived": "0"}
            args = autosync.build_parser().parse_args(["--projects-dir", str(root / "projects"), "--state-dir", str(state), "--megavault", str(root / "mv"), "--no-telegram", "--no-data-mirror", "run"])
            with (
                mock.patch.object(autosync, "megavault_inventory", return_value=[]),
                mock.patch.object(autosync, "audit_inventory", return_value=([], 0)),
                mock.patch.object(autosync, "github_repos", return_value=[unmanaged]),
                mock.patch.object(autosync, "sync_changed_repo") as sync,
                mock.patch.object(autosync, "megavault_registered_remotes", return_value=set()),
                mock.patch.object(autosync, "register_in_megavault", return_value={"validation": "not_needed", "deferred": 0}) as register,
                mock.patch.object(autosync, "update_telegram_alert_state", return_value="disabled") as alerts,
            ):
                self.assertEqual(0, autosync.command_run(args))
            sync.assert_not_called()
            register.assert_called_once_with(root / "mv", root / "projects", [], dry_run=False)
            alerts.assert_called_once()
            self.assertEqual({}, autosync.load_repo_state(state))

    def test_megavault_worktree_outside_allowlist_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            unmanaged = {"project_id": 99, "slug": "unmanaged", "worktree": str(root / "dirty"), "remote_url": "https://github.com/gernalix/unmanaged", "branch": "main"}
            args = autosync.build_parser().parse_args(["--projects-dir", str(root / "projects"), "--state-dir", str(root / "state"), "--megavault", str(root / "mv"), "--no-telegram", "--no-data-mirror", "run"])
            with (
                mock.patch.object(autosync, "megavault_inventory", return_value=[unmanaged]),
                mock.patch.object(autosync, "audit_inventory", return_value=([], 0)) as audit,
                mock.patch.object(autosync, "github_repos", return_value=[]),
                mock.patch.object(autosync, "megavault_registered_remotes", return_value=set()),
                mock.patch.object(autosync, "register_in_megavault", return_value={"validation": "not_needed", "deferred": 0}),
                mock.patch.object(autosync, "update_telegram_alert_state", return_value="disabled") as alerts,
            ):
                self.assertEqual(0, autosync.command_run(args))
            audit.assert_called_once_with([], auto_push=True, report_behind=False, fetch_remote=False)
            alerts.assert_called_once()

    def test_allowed_repo_is_still_synced_normally(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = {"name": "salute", "url": "https://github.com/gernalix/salute", "default_branch": "main", "pushed_at": "A", "archived": "0"}
            args = autosync.build_parser().parse_args(["--projects-dir", str(root / "projects"), "--state-dir", str(root / "state"), "--megavault", str(root / "mv"), "--no-telegram", "--no-data-mirror", "run"])
            with (
                mock.patch.object(autosync, "megavault_inventory", return_value=[]),
                mock.patch.object(autosync, "audit_inventory", return_value=([], 0)),
                mock.patch.object(autosync, "github_repos", return_value=[repo]),
                mock.patch.object(autosync, "sync_changed_repo", return_value=("updated", None)) as sync,
                mock.patch.object(autosync, "megavault_registered_remotes", return_value={autosync.normalize_remote(repo["url"])}),
                mock.patch.object(autosync, "register_in_megavault", return_value={"validation": "not_needed", "deferred": 0}),
            ):
                self.assertEqual(0, autosync.command_run(args))
            sync.assert_called_once()

    def test_telegram_alert_fingerprint_suppresses_identical_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            first = [autosync.issue({"project_id": 1, "slug": "one", "worktree": "/one"}, "dirty_worktree")]
            changed = [autosync.issue({"project_id": 1, "slug": "one", "worktree": "/one"}, "diverged")]
            with mock.patch.object(autosync, "send_telegram", return_value=True) as sender:
                self.assertEqual("alert_sent", autosync.update_telegram_alert_state(state, first, enabled=True))
                self.assertEqual("unchanged", autosync.update_telegram_alert_state(state, first, enabled=True))
                self.assertEqual("alert_sent", autosync.update_telegram_alert_state(state, changed, enabled=True))
            self.assertEqual(2, sender.call_count)
            saved = json.loads((state / autosync.ALERT_STATE_FILE).read_text(encoding="utf-8"))
            self.assertIn("fingerprint", saved)

    def test_real_github_failure_is_nonzero(self) -> None:
        with mock.patch.object(autosync, "megavault_inventory", return_value=[]), mock.patch.object(autosync, "audit_inventory", return_value=([], 0)), mock.patch.object(autosync, "github_repos", side_effect=autosync.AutosyncError("github_repo_list_failed")):
            self.assertEqual(75, autosync.main(["--no-telegram", "--no-data-mirror", "run"]))


if __name__ == "__main__":
    unittest.main()
