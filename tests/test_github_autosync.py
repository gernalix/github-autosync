from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import github_autosync as autosync
import repo_single_writer


def git(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *cmd], cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)


def ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


class AutosyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.real_kuma_heartbeat = autosync._push_kuma_heartbeat
        self.kuma_heartbeat = mock.patch.object(autosync, "_push_kuma_heartbeat", return_value=True)
        self.kuma_heartbeat_mock = self.kuma_heartbeat.start()
        self.addCleanup(self.kuma_heartbeat.stop)

    def test_secret_service_kuma_url_precedes_legacy_sources_and_preserves_explicit_env(self) -> None:
        with (
            mock.patch.object(
                autosync,
                "_secret_service_lookup",
                return_value="https://secret.example/api/push/test",
            ) as lookup,
            mock.patch.dict(autosync.os.environ, {}, clear=False),
        ):
            autosync.os.environ.pop("GITHUB_RECONCILE_PUSH_URL", None)
            autosync.load_runtime_credentials()
            self.assertEqual(
                "https://secret.example/api/push/test",
                autosync.os.environ["GITHUB_RECONCILE_PUSH_URL"],
            )
            autosync.os.environ["GITHUB_RECONCILE_PUSH_URL"] = "https://explicit.test/push"
            autosync.load_runtime_credentials()
            self.assertEqual(
                "https://explicit.test/push",
                autosync.os.environ["GITHUB_RECONCILE_PUSH_URL"],
            )
            self.assertEqual(1, lookup.call_count)
            autosync.os.environ.pop("GITHUB_RECONCILE_PUSH_URL", None)

    def test_systemd_credential_remains_migration_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            credential = Path(tmp) / "reconcile.env"
            credential.write_text(
                "GITHUB_RECONCILE_PUSH_URL=https://example.test/api/push/test\n",
                encoding="utf-8",
            )
            with (
                mock.patch.object(autosync, "_secret_service_lookup", return_value=None),
                mock.patch.dict(
                    autosync.os.environ,
                    {"CREDENTIALS_DIRECTORY": tmp},
                    clear=False,
                ),
            ):
                autosync.os.environ.pop("GITHUB_RECONCILE_PUSH_URL", None)
                autosync.load_runtime_credentials()
                self.assertEqual(
                    "https://example.test/api/push/test",
                    autosync.os.environ["GITHUB_RECONCILE_PUSH_URL"],
                )
                autosync.os.environ.pop("GITHUB_RECONCILE_PUSH_URL", None)

    def test_systemd_unit_uses_loadcredential_not_environmentfile(self) -> None:
        unit = Path(__file__).resolve().parents[1] / "systemd" / "github-autosync.service"
        text = unit.read_text(encoding="utf-8")
        self.assertIn(
            "LoadCredential=reconcile.env:/home/daniele/.config/github-autosync/reconcile.env",
            text,
        )
        self.assertNotIn("EnvironmentFile=", text)

    def test_git_failure_issue_classifies_corrupt_object_and_keeps_evidence(self) -> None:
        result = subprocess.CompletedProcess(
            [],
            128,
            stdout="",
            stderr=(
                "error: object file .git/objects/aa/bb is empty\n"
                "fatal: loose object aabb is corrupt"
            ),
        )
        problem = autosync._git_failure_issue(
            {"project_id": 1, "slug": "demo", "worktree": "/tmp/demo"},
            "status_failed",
            result,
        )
        self.assertEqual("git_object_corrupt", problem["kind"])
        self.assertIn("object file", problem["detail"])
        self.assertIn("is empty", problem["detail"])

    def test_git_failure_issue_preserves_generic_status_error(self) -> None:
        result = subprocess.CompletedProcess(
            [], 128, stdout="", stderr="fatal: this operation must be run in a work tree"
        )
        problem = autosync._git_failure_issue(
            {"project_id": 1, "slug": "demo", "worktree": "/tmp/demo"},
            "status_failed",
            result,
        )
        self.assertEqual("status_failed", problem["kind"])
        self.assertIn("must be run in a work tree", problem["detail"])

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

    def test_roadmap_pull_bootstraps_once_from_fetched_remote_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            script = worktree / autosync.ROADMAP_PULL_SCRIPT
            script.parent.mkdir(parents=True)
            script.write_text("# old guard\n", encoding="utf-8")
            python_calls: list[Path] = []
            show_calls = 0

            def fake_run(cmd: list[str], cwd: Path | None = None, timeout: int = 120):
                nonlocal show_calls
                self.assertEqual(worktree, cwd)
                if cmd[0] == autosync.sys.executable:
                    called_script = Path(cmd[1])
                    python_calls.append(called_script)
                    if called_script == script:
                        return subprocess.CompletedProcess(
                            cmd,
                            2,
                            stdout=json.dumps(
                                {
                                    "status": "BLOCKED",
                                    "reason": "running_prompt_modified_remote:354882",
                                }
                            ),
                            stderr="",
                        )
                    self.assertEqual("# new guard\n", called_script.read_text(encoding="utf-8"))
                    return subprocess.CompletedProcess(
                        cmd,
                        0,
                        stdout=json.dumps({"status": "PASS", "head": "new"}) + "\n",
                        stderr="",
                    )
                if cmd[:2] == ["git", "show"]:
                    show_calls += 1
                    self.assertEqual(
                        "origin/main:tools/roadmap_pull.py",
                        cmd[2],
                    )
                    return subprocess.CompletedProcess(
                        cmd,
                        0,
                        stdout="# new guard\n",
                        stderr="",
                    )
                raise AssertionError(cmd)

            with mock.patch.object(autosync, "run", side_effect=fake_run):
                ok_result, payload = autosync._run_roadmap_pull(
                    worktree,
                    "origin",
                    "main",
                )

            self.assertTrue(ok_result)
            self.assertEqual("PASS", payload["status"])
            self.assertEqual(2, len(python_calls))
            self.assertEqual(script, python_calls[0])
            self.assertEqual(1, show_calls)

    def test_roadmap_pull_does_not_bootstrap_for_operational_content_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            script = worktree / autosync.ROADMAP_PULL_SCRIPT
            script.parent.mkdir(parents=True)
            script.write_text("# local guard\n", encoding="utf-8")

            def fake_run(cmd: list[str], cwd: Path | None = None, timeout: int = 120):
                if cmd[0] == autosync.sys.executable:
                    return subprocess.CompletedProcess(
                        cmd,
                        2,
                        stdout=json.dumps(
                            {
                                "status": "BLOCKED",
                                "reason": "running_prompt_content_modified_remote:354882",
                            }
                        ),
                        stderr="",
                    )
                raise AssertionError("unexpected bootstrap attempt")

            with mock.patch.object(autosync, "run", side_effect=fake_run):
                ok_result, payload = autosync._run_roadmap_pull(
                    worktree,
                    "origin",
                    "main",
                )

            self.assertFalse(ok_result)
            self.assertEqual(
                "running_prompt_content_modified_remote:354882",
                payload["reason"],
            )

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

    def test_reconcile_all_checkpoints_dirty_generic_repo_and_pushes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_path, bare = self.make_repo_pair(root / "pair")
            (repo_path / "dirty.txt").write_text("dirty\n", encoding="utf-8")
            repo = {
                "name": "repo",
                "url": str(bare),
                "default_branch": "main",
                "pushed_at": "A",
                "archived": "0",
            }
            result, problem = autosync.sync_changed_repo(
                repo,
                root,
                dry_run=False,
                inventory_entry=self.entry(repo_path, bare),
                auto_commit_dirty=True,
            )
            self.assertEqual("pushed", result)
            self.assertIsNone(problem)
            self.assertEqual("", git(["status", "--porcelain"], repo_path).stdout.strip())
            self.assertEqual((0, 0), autosync.git_counts(repo_path))
            self.assertTrue((repo_path / "dirty.txt").is_file())

    def test_reconcile_all_checkpoints_dirty_protected_canonical_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_path, bare = self.make_repo_pair(root / "pair")
            repo_single_writer.ensure_guard(repo_path, "main")
            (repo_path / "dirty.txt").write_text("dirty\n", encoding="utf-8")
            repo = {"name": "repo", "url": str(bare), "default_branch": "main", "pushed_at": "A", "archived": "0"}
            result, problem = autosync.sync_changed_repo(
                repo, root, dry_run=False, inventory_entry=self.entry(repo_path, bare), auto_commit_dirty=True
            )
            self.assertEqual("pushed", result)
            self.assertIsNone(problem)
            self.assertEqual("", git(["status", "--porcelain"], repo_path).stdout.strip())

    def test_reconcile_all_rebases_generic_divergence_then_pushes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_path, bare = self.make_repo_pair(root / "pair")
            other = root / "other"
            self.assertEqual(0, git(["clone", str(bare), str(other)]).returncode)
            self.assertEqual(0, git(["config", "user.email", "test@example.invalid"], other).returncode)
            self.assertEqual(0, git(["config", "user.name", "Test"], other).returncode)
            self.assertEqual(0, git(["checkout", "main"], other).returncode)
            (other / "remote.txt").write_text("remote\n", encoding="utf-8")
            self.assertEqual(0, git(["add", "remote.txt"], other).returncode)
            self.assertEqual(0, git(["commit", "-m", "remote"], other).returncode)
            self.assertEqual(0, git(["push", "origin", "main"], other).returncode)

            (repo_path / "local.txt").write_text("local\n", encoding="utf-8")
            repo = {
                "name": "repo",
                "url": str(bare),
                "default_branch": "main",
                "pushed_at": "B",
                "archived": "0",
            }
            result, problem = autosync.sync_changed_repo(
                repo,
                root,
                dry_run=False,
                inventory_entry=self.entry(repo_path, bare),
                auto_commit_dirty=True,
            )
            self.assertEqual("pushed", result)
            self.assertIsNone(problem)
            self.assertEqual((0, 0), autosync.git_counts(repo_path))
            self.assertTrue((repo_path / "local.txt").is_file())
            self.assertTrue((repo_path / "remote.txt").is_file())

    def test_independent_canonical_writer_repo_is_not_reconciled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = {
                "owner": "gernalix",
                "name": "activity-watch-data",
                "url": "https://github.com/gernalix/activity-watch-data.git",
                "default_branch": "main",
                "pushed_at": "A",
                "archived": "0",
            }
            result, problem = autosync.sync_changed_repo(repo, root, dry_run=False)
            self.assertEqual("external-writer", result)
            self.assertIsNone(problem)
            self.assertFalse((root / "activity-watch-data").exists())

    def test_autosync_does_not_own_repository_integration_queue(self) -> None:
        source = Path(autosync.__file__).read_text(encoding="utf-8")
        self.assertNotIn("repo_single_writer.process_ready_prs(args.owner)", source)

    def test_queue_merged_roadmap_completions_marks_only_successful_submission(self) -> None:
        pending = [{"task_id": "123456"}, {"task_id": "654321"}]
        calls: list[str] = []

        def fake_run(cmd: list[str], cwd: Path | None = None, **kwargs: object) -> subprocess.CompletedProcess[str]:
            prompt_id = cmd[cmd.index("--prompt-id") + 1]
            calls.append(prompt_id)
            return ok("{}\n") if prompt_id == "123456" else subprocess.CompletedProcess(cmd, 2, "", "failed")

        with (
            mock.patch.object(repo_single_writer, "pending_roadmap_completions", return_value=pending),
            mock.patch.object(repo_single_writer, "mark_roadmap_completion_queued") as mark,
            mock.patch.object(autosync, "ROADMAP_RESULT_SCRIPT", Path("/tmp/roadmap_result.py")),
            mock.patch.object(Path, "is_file", return_value=True),
            mock.patch.object(autosync, "run", side_effect=fake_run),
        ):
            result = autosync.queue_merged_roadmap_completions()

        self.assertEqual(["123456", "654321"], calls)
        self.assertEqual(1, result["queued"])
        self.assertEqual(1, result["deferred"])
        self.assertEqual(["123456"], result["prompt_ids"])
        self.assertEqual(["654321"], result["failed"])
        mark.assert_called_once_with("123456")

    def test_runtime_deploy_contract_covers_workflowy_and_chrome_switcher(self) -> None:
        self.assertIn("gernalix/workflowy-importer", autosync.ALLOWED_REPOSITORIES)
        self.assertIn("gernalix/chrome-codex-switcher", autosync.ALLOWED_REPOSITORIES)
        self.assertEqual(
            ("python3", "deploy_runtime.py"),
            autosync.RUNTIME_DEPLOYERS["gernalix/workflowy-importer"],
        )
        self.assertEqual(
            ("bash", "install.sh"),
            autosync.RUNTIME_DEPLOYERS["gernalix/chrome-codex-switcher"],
        )

    def test_runtime_deploy_runs_once_per_checked_out_head(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            (worktree / "install.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            state: dict[str, str] = {}
            calls: list[tuple[str, ...]] = []

            def fake_run(cmd: list[str], cwd: Path | None = None, **kwargs: object) -> subprocess.CompletedProcess[str]:
                calls.append(tuple(cmd))
                if cmd[:3] == ["git", "rev-parse", "--verify"]:
                    return ok("abc123\n")
                if cmd == ["bash", "install.sh"]:
                    return ok()
                raise AssertionError(cmd)

            with mock.patch.object(autosync, "run", side_effect=fake_run):
                first = autosync.deploy_runtime_if_needed(
                    "gernalix/chrome-codex-switcher",
                    worktree,
                    state,
                    dry_run=False,
                )
                second = autosync.deploy_runtime_if_needed(
                    "gernalix/chrome-codex-switcher",
                    worktree,
                    state,
                    dry_run=False,
                )

            self.assertEqual(("deployed", "abc123"), first)
            self.assertEqual(("up_to_date", "abc123"), second)
            self.assertEqual("abc123", state["gernalix/chrome-codex-switcher"])
            self.assertEqual(1, calls.count(("bash", "install.sh")))

    def test_runtime_deploy_failure_is_retryable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            worktree = Path(tmp)
            (worktree / "deploy_runtime.py").write_text("pass\n", encoding="utf-8")
            state: dict[str, str] = {}

            def fake_run(cmd: list[str], cwd: Path | None = None, **kwargs: object) -> subprocess.CompletedProcess[str]:
                if cmd[:3] == ["git", "rev-parse", "--verify"]:
                    return ok("def456\n")
                if cmd == ["python3", "deploy_runtime.py"]:
                    return subprocess.CompletedProcess(cmd, 1, "", "runtime failed")
                raise AssertionError(cmd)

            with mock.patch.object(autosync, "run", side_effect=fake_run):
                result = autosync.deploy_runtime_if_needed(
                    "gernalix/workflowy-importer",
                    worktree,
                    state,
                    dry_run=False,
                )

            self.assertEqual("failed", result[0])
            self.assertIn("runtime failed", result[1])
            self.assertNotIn("gernalix/workflowy-importer", state)

    def test_runtime_deploy_state_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = Path(tmp)
            expected = {
                "gernalix/workflowy-importer": "abc",
                "gernalix/chrome-codex-switcher": "def",
            }
            autosync.save_runtime_deploy_state(state_dir, expected)
            self.assertEqual(expected, autosync.load_runtime_deploy_state(state_dir))

    def test_reconcile_all_parser_enables_full_reconcile_and_dirty_checkpointing(self) -> None:
        args = autosync.build_parser().parse_args(["reconcile-all"])
        self.assertTrue(args.full_reconcile)
        self.assertTrue(args.auto_commit_dirty)

    def test_run_parser_enables_dirty_checkpointing(self) -> None:
        args = autosync.build_parser().parse_args(["run"])
        self.assertFalse(args.full_reconcile)
        self.assertTrue(args.auto_commit_dirty)

    def test_reconcile_all_repairs_stale_upstream_to_default_branch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo_path, bare = self.make_repo_pair(root / "pair")
            self.assertEqual(0, git(["checkout", "-b", "legacy"], repo_path).returncode)
            self.assertEqual(0, git(["push", "-u", "origin", "legacy"], repo_path).returncode)

            other = root / "other"
            self.assertEqual(0, git(["clone", str(bare), str(other)]).returncode)
            self.assertEqual(0, git(["config", "user.email", "test@example.invalid"], other).returncode)
            self.assertEqual(0, git(["config", "user.name", "Test"], other).returncode)
            self.assertEqual(0, git(["push", "origin", "--delete", "legacy"], other).returncode)

            repo = {
                "name": "repo",
                "url": str(bare),
                "default_branch": "main",
                "pushed_at": "B",
                "archived": "0",
            }
            result, problem = autosync.sync_changed_repo(
                repo,
                root,
                dry_run=False,
                inventory_entry=self.entry(repo_path, bare),
                auto_commit_dirty=True,
            )
            self.assertEqual("up_to_date", result)
            self.assertIsNone(problem)
            upstream = git(["rev-parse", "--abbrev-ref", "@{u}"], repo_path)
            self.assertEqual("origin/main", upstream.stdout.strip())

    def test_reconcile_all_continues_after_one_repo_operational_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            (projects / "one").mkdir(parents=True)
            (projects / "two").mkdir(parents=True)
            repos = [
                {"name": "one", "url": "https://github.com/gernalix/one", "default_branch": "main", "pushed_at": "A", "archived": "0"},
                {"name": "two", "url": "https://github.com/gernalix/two", "default_branch": "main", "pushed_at": "A", "archived": "0"},
            ]
            args = autosync.build_parser().parse_args(
                [
                    "--projects-dir",
                    str(projects),
                    "--state-dir",
                    str(root / "state"),
                    "--megavault",
                    str(root / "mv"),
                    "--no-telegram",
                    "--no-data-mirror",
                    "reconcile-all",
                ]
            )
            with (
                mock.patch.object(autosync, "megavault_inventory", return_value=[]),
                mock.patch.object(autosync, "audit_inventory", return_value=([], 0)),
                mock.patch.object(autosync, "github_repos", return_value=repos),
                mock.patch.object(
                    autosync,
                    "sync_changed_repo",
                    side_effect=[autosync.AutosyncError("boom"), ("up_to_date", None)],
                ) as sync,
                mock.patch.object(
                    autosync,
                    "megavault_registered_remotes",
                    return_value={autosync.normalize_remote(repo["url"]) for repo in repos},
                ),
                mock.patch.object(
                    autosync,
                    "register_in_megavault",
                    return_value={"validation": "not_needed", "deferred": 0},
                ),
                mock.patch("builtins.print") as printer,
            ):
                self.assertEqual(2, autosync.command_run(args))

            self.assertEqual(2, sync.call_count)
            payload = json.loads(printer.call_args.args[0])
            self.assertEqual("deferred", payload["status"])
            self.assertEqual(1, payload["issues"])
            self.assertEqual("one", payload["reconcile_issues"][0]["repo"])
            self.assertEqual("reconcile_error", payload["reconcile_issues"][0]["kind"])

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

    def test_unchanged_remote_with_dirty_local_worktree_is_reconciled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            local = projects / "codex-usage"
            local.mkdir(parents=True)
            self.assertEqual(0, git(["init"], local).returncode)
            (local / "pending.txt").write_text("publisher update\n", encoding="utf-8")
            state = root / "state"
            repo = {"name": "codex-usage", "url": "https://github.com/gernalix/codex-usage", "default_branch": "main", "pushed_at": "A", "archived": "0"}
            autosync.save_repo_state(state, {repo["name"]: autosync.repo_fingerprint(repo)})
            args = autosync.build_parser().parse_args(["--projects-dir", str(projects), "--state-dir", str(state), "--megavault", str(root / "mv"), "--no-data-mirror", "run"])
            with (
                mock.patch.object(autosync, "megavault_inventory", return_value=[]),
                mock.patch.object(autosync, "audit_inventory", return_value=([], 0)),
                mock.patch.object(autosync, "github_repos", return_value=[repo]),
                mock.patch.object(autosync, "sync_changed_repo", return_value=("pushed", None)) as sync,
                mock.patch.object(autosync, "audit_worktree") as audit,
                mock.patch.object(autosync, "megavault_registered_remotes", return_value={autosync.normalize_remote(repo["url"])}),
                mock.patch.object(autosync, "register_in_megavault", return_value={"validation": "not_needed", "deferred": 0}),
            ):
                self.assertEqual(0, autosync.command_run(args))
            sync.assert_called_once()
            audit.assert_not_called()
            self.assertTrue(sync.call_args.kwargs["auto_commit_dirty"])

    def test_reconcile_all_includes_non_allowlisted_repo_already_present_locally(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            projects = root / "projects"
            local = projects / "legacy-local"
            local.mkdir(parents=True)
            remote = "https://github.com/gernalix/legacy-local"
            repo = {
                "name": "legacy-local",
                "url": remote,
                "default_branch": "main",
                "pushed_at": "A",
                "archived": "0",
            }
            args = autosync.build_parser().parse_args(
                [
                    "--projects-dir",
                    str(projects),
                    "--state-dir",
                    str(root / "state"),
                    "--megavault",
                    str(root / "mv"),
                    "--no-telegram",
                    "--no-data-mirror",
                    "reconcile-all",
                ]
            )
            with (
                mock.patch.object(autosync, "megavault_inventory", return_value=[]),
                mock.patch.object(autosync, "audit_inventory", return_value=([], 0)),
                mock.patch.object(autosync, "github_repos", return_value=[repo]),
                mock.patch.object(autosync, "sync_changed_repo", return_value=("up_to_date", None)) as sync,
                mock.patch.object(autosync, "megavault_registered_remotes", return_value={autosync.normalize_remote(remote)}),
                mock.patch.object(autosync, "register_in_megavault", return_value={"validation": "not_needed", "deferred": 0}),
            ):
                self.assertEqual(0, autosync.command_run(args))
            sync.assert_called_once()
            self.assertTrue(sync.call_args.kwargs["auto_commit_dirty"])

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
            ):
                self.assertEqual(0, autosync.command_run(args))
            sync.assert_not_called()
            register.assert_called_once_with(root / "mv", root / "projects", [], dry_run=False)
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
            ):
                self.assertEqual(0, autosync.command_run(args))
            audit.assert_called_once_with([], auto_push=True, report_behind=False, fetch_remote=False)

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


    def test_real_github_failure_is_nonzero(self) -> None:
        with mock.patch.object(autosync, "megavault_inventory", return_value=[]), mock.patch.object(autosync, "audit_inventory", return_value=([], 0)), mock.patch.object(autosync, "github_repos", side_effect=autosync.AutosyncError("github_repo_list_failed")):
            self.assertEqual(75, autosync.main(["--no-telegram", "--no-data-mirror", "run"]))


    def test_periodic_run_sends_kuma_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = autosync.build_parser().parse_args(
                [
                    "--projects-dir",
                    str(root / "projects"),
                    "--state-dir",
                    str(root / "state"),
                    "--megavault",
                    str(root / "mv"),
                    "--no-data-mirror",
                    "run",
                ]
            )
            self.kuma_heartbeat_mock.reset_mock()
            with (
                mock.patch.object(autosync, "megavault_inventory", return_value=[]),
                mock.patch.object(autosync, "audit_inventory", return_value=([], 0)),
                mock.patch.object(autosync, "github_repos", return_value=[]),
                mock.patch.object(autosync, "megavault_registered_remotes", return_value=set()),
                mock.patch.object(
                    autosync,
                    "register_in_megavault",
                    return_value={"validation": "not_needed", "deferred": 0},
                ),
                mock.patch("builtins.print"),
            ):
                self.assertEqual(0, autosync.command_run(args))
            self.kuma_heartbeat_mock.assert_called_once_with(True, 0, [])

    def test_repo_issue_keeps_operational_kuma_monitor_up_and_exposes_cause(self) -> None:
        issues = [{"repo": "PersonalHub", "kind": "dirty_worktree", "detail": ""}]
        with mock.patch.object(autosync, "_push_kuma_status", return_value=True) as push:
            self.assertTrue(self.real_kuma_heartbeat(False, 13, issues))
        push.assert_called_once_with(
            "up",
            "reconcile attivo; 1 repository da verificare: PersonalHub/dirty_worktree",
        )

    def test_missing_kuma_url_is_a_heartbeat_failure(self) -> None:
        with mock.patch.dict(autosync.os.environ, {}, clear=True):
            self.assertFalse(autosync._push_kuma_status("up", "ok"))

    def test_fatal_error_pushes_explicit_down_heartbeat(self) -> None:
        with (
            mock.patch.object(autosync, "command_run", side_effect=autosync.AutosyncError("boom")),
            mock.patch.object(autosync, "_push_kuma_status", return_value=True) as push,
            mock.patch("builtins.print"),
        ):
            self.assertEqual(75, autosync.main(["run"]))
        push.assert_called_once_with("down", "errore fatale: boom")

    def test_systemd_periodic_service_uses_lightweight_run(self) -> None:
        service = (
            Path(__file__).resolve().parents[1] / "systemd" / "github-autosync.service"
        ).read_text(encoding="utf-8")
        self.assertIn("github_autosync.py run", service)
        self.assertNotIn(".local/bin/github-reconcile", service)


if __name__ == "__main__":
    unittest.main()
