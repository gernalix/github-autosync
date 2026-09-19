from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import github_autosync as autosync


def ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


def fail(stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 1, stdout="", stderr=stderr)


class AutosyncRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.kuma_heartbeat = mock.patch.object(autosync, "_push_kuma_heartbeat", return_value=True)
        self.kuma_heartbeat.start()
        self.addCleanup(self.kuma_heartbeat.stop)

    def test_unchanged_repo_is_locally_audited_and_ahead_can_auto_push(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "canonical"
            worktree.mkdir()
            remote = "https://github.com/gernalix/salute"
            inventory = {
                "project_id": 51,
                "slug": "salute",
                "worktree": str(worktree),
                "remote_url": remote,
                "branch": "main",
            }
            repo = {
                "name": "salute",
                "url": remote,
                "default_branch": "main",
                "pushed_at": "A",
                "archived": "0",
            }
            state = root / "state"
            autosync.save_repo_state(state, {repo["name"]: autosync.repo_fingerprint(repo)})
            args = autosync.build_parser().parse_args(
                [
                    "--projects-dir",
                    str(root / "projects"),
                    "--state-dir",
                    str(state),
                    "--megavault",
                    str(root / "mv"),
                    "--no-telegram",
                    "--no-data-mirror",
                    "run",
                ]
            )
            with (
                mock.patch.object(autosync, "megavault_inventory", return_value=[inventory]),
                mock.patch.object(autosync, "audit_inventory", return_value=([], 0)),
                mock.patch.object(autosync, "github_repos", return_value=[repo]),
                mock.patch.object(autosync, "audit_worktree", return_value=([], True)) as audit,
                mock.patch.object(autosync, "sync_changed_repo") as sync,
                mock.patch.object(autosync, "megavault_registered_remotes", return_value={autosync.normalize_remote(remote)}),
                mock.patch.object(
                    autosync,
                    "register_in_megavault",
                    return_value={"validation": "not_needed", "deferred": 0},
                ),
                mock.patch("builtins.print") as printer,
            ):
                self.assertEqual(0, autosync.command_run(args))

            audit.assert_called_once_with(
                inventory,
                auto_push=True,
                report_behind=False,
                fetch_remote=False,
            )
            sync.assert_not_called()
            payload = json.loads(printer.call_args.args[0])
            self.assertEqual(1, payload["audited_unchanged"])
            self.assertEqual(1, payload["auto_pushed"])
            self.assertEqual(0, payload["skipped_unchanged"])
            self.assertEqual("ok", payload["status"])

    def test_unchanged_dirty_repo_is_reported_instead_of_blindly_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "canonical"
            worktree.mkdir()
            remote = "https://github.com/gernalix/salute"
            inventory = {
                "project_id": 51,
                "slug": "salute",
                "worktree": str(worktree),
                "remote_url": remote,
                "branch": "main",
            }
            repo = {
                "name": "salute",
                "url": remote,
                "default_branch": "main",
                "pushed_at": "A",
                "archived": "0",
            }
            state = root / "state"
            autosync.save_repo_state(state, {repo["name"]: autosync.repo_fingerprint(repo)})
            args = autosync.build_parser().parse_args(
                [
                    "--projects-dir",
                    str(root / "projects"),
                    "--state-dir",
                    str(state),
                    "--megavault",
                    str(root / "mv"),
                    "--no-telegram",
                    "--no-data-mirror",
                    "run",
                ]
            )
            dirty = autosync.issue(inventory, "dirty_worktree", "ahead=0,behind=0")
            with (
                mock.patch.object(autosync, "megavault_inventory", return_value=[inventory]),
                mock.patch.object(autosync, "audit_inventory", return_value=([], 0)),
                mock.patch.object(autosync, "github_repos", return_value=[repo]),
                mock.patch.object(autosync, "audit_worktree", return_value=([dirty], False)),
                mock.patch.object(autosync, "sync_changed_repo") as sync,
                mock.patch.object(autosync, "megavault_registered_remotes", return_value={autosync.normalize_remote(remote)}),
                mock.patch.object(
                    autosync,
                    "register_in_megavault",
                    return_value={"validation": "not_needed", "deferred": 0},
                ),
                mock.patch("builtins.print") as printer,
            ):
                self.assertEqual(0, autosync.command_run(args))

            sync.assert_not_called()
            payload = json.loads(printer.call_args.args[0])
            self.assertEqual(1, payload["audited_unchanged"])
            self.assertEqual(1, payload["deferred"])
            self.assertEqual(1, payload["issues"])
            self.assertEqual("deferred", payload["status"])

    def test_missing_canonical_worktree_is_cloned_at_canonical_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            canonical = root / "elsewhere" / "codex-roadmap"
            repo = {
                "name": "codex-roadmap",
                "url": "https://github.com/gernalix/codex-roadmap",
                "default_branch": "main",
                "pushed_at": "A",
                "archived": "0",
            }
            inventory = {
                "project_id": 51,
                "slug": "codex-roadmap",
                "worktree": str(canonical),
                "remote_url": repo["url"],
                "branch": "main",
            }
            with mock.patch.object(autosync, "clone_repo", return_value="cloned") as clone:
                result, problem = autosync.sync_changed_repo(
                    repo,
                    root / "projects",
                    dry_run=False,
                    inventory_entry=inventory,
                )
            self.assertEqual("cloned", result)
            self.assertIsNone(problem)
            clone.assert_called_once_with(
                repo,
                root / "projects",
                dry_run=False,
                target_worktree=canonical,
            )

    def test_dry_run_missing_upstream_uses_ls_remote_without_fetch_or_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            worktree = root / "codex-roadmap"
            worktree.mkdir()
            repo = {
                "name": "codex-roadmap",
                "url": "https://github.com/gernalix/codex-roadmap",
                "default_branch": "main",
                "pushed_at": "A",
                "archived": "0",
            }

            def fake_run(cmd: list[str], cwd: Path | None = None, **_: object) -> subprocess.CompletedProcess[str]:
                if cmd[:3] == ["git", "status", "--porcelain"]:
                    return ok()
                if cmd[:3] == ["git", "branch", "--show-current"]:
                    return ok("main\n")
                if cmd[:3] == ["git", "rev-parse", "--abbrev-ref"]:
                    return fail("no upstream")
                if cmd[:3] == ["git", "show-ref", "--verify"]:
                    return fail("missing ref")
                if cmd[:3] == ["git", "ls-remote", "--exit-code"]:
                    return ok("abc\trefs/heads/main\n")
                if cmd[:2] == ["git", "fetch"]:
                    raise AssertionError("dry-run must not fetch")
                raise AssertionError(f"unexpected command: {cmd}")

            with (
                mock.patch.object(autosync, "git_repo_matches_remote", return_value=True),
                mock.patch.object(autosync, "run", side_effect=fake_run),
            ):
                result, problem = autosync.sync_changed_repo(repo, root, dry_run=True)

            self.assertEqual("would_update", result)
            self.assertIsNone(problem)

    def test_upstream_repair_fetch_is_not_immediately_repeated(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            entry = {
                "project_id": 51,
                "slug": "codex-roadmap",
                "worktree": str(worktree),
                "remote_url": "https://github.com/gernalix/codex-roadmap",
                "branch": "main",
            }
            with (
                mock.patch.object(
                    autosync,
                    "_worktree_basics",
                    return_value=(worktree, [], "origin/main", "origin"),
                ),
                mock.patch.object(autosync, "git_counts", return_value=(0, 0)),
                mock.patch.object(autosync, "run", return_value=ok()) as runner,
            ):
                problems, pushed = autosync.audit_worktree(
                    entry,
                    auto_push=False,
                    report_behind=True,
                    fetch_remote=True,
                )

            self.assertEqual([], problems)
            self.assertFalse(pushed)
            fetch_calls = [
                call
                for call in runner.call_args_list
                if call.args and call.args[0][:2] == ["git", "fetch"]
            ]
            self.assertEqual([], fetch_calls)


if __name__ == "__main__":
    unittest.main()
