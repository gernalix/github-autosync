#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any


DEFAULT_OWNER = "gernalix"
DEFAULT_PROJECTS_DIR = Path.home() / "projects"
# Keep the legacy state path so migration preserves the same lock and cannot
# accidentally run old and new autosync implementations concurrently.
DEFAULT_STATE_DIR = Path.home() / ".local/state/codex-github-autosync"
DEFAULT_MEGAVAULT = Path.home() / "MegaVault"
GHORG = Path.home() / ".local/bin/ghorg"


class AutosyncError(RuntimeError):
    pass


class ExclusiveLock:
    def __init__(self, path: Path):
        self.path = path
        self.handle: Any = None

    def __enter__(self) -> "ExclusiveLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("w", encoding="utf-8")
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.handle.write(f"{os.getpid()}\n")
        self.handle.flush()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.handle:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def run(
    cmd: list[str],
    cwd: Path | None = None,
    *,
    timeout: int = 300,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False, timeout=timeout)


def gh_token() -> str:
    result = run(["gh", "auth", "token"], timeout=30)
    if result.returncode != 0 or not result.stdout.strip():
        raise AutosyncError("gh_auth_token_unavailable")
    return result.stdout.strip()


def github_repos(owner: str) -> list[dict[str, str]]:
    result = run(
        [
            "gh",
            "repo",
            "list",
            owner,
            "--limit",
            "1000",
            "--json",
            "name,url,defaultBranchRef",
        ],
        timeout=120,
    )
    if result.returncode != 0:
        raise AutosyncError("github_repo_list_failed")
    repos = []
    for item in json.loads(result.stdout):
        branch = item.get("defaultBranchRef") or {}
        repos.append(
            {
                "name": str(item["name"]),
                "url": str(item["url"]),
                "default_branch": str(branch.get("name") or "UNKNOWN"),
            }
        )
    return sorted(repos, key=lambda row: row["name"])


def ghorg_args(owner: str, projects_dir: Path, *, dry_run: bool = False) -> list[str]:
    args = [
        str(GHORG),
        "clone",
        owner,
        "--scm=github",
        "--clone-type=user",
        "--github-user-option=owner",
        "--path",
        str(projects_dir),
        "--output-dir",
        ".",
        "--protocol=https",
        "--protect-local",
        "--fetch-all",
        "--fetch-prune",
        "--no-clean",
        "--no-dir-size",
    ]
    if dry_run:
        args.append("--dry-run")
    return args


def run_ghorg(owner: str, projects_dir: Path, *, dry_run: bool = False) -> subprocess.CompletedProcess[str]:
    if not GHORG.exists():
        raise AutosyncError(f"ghorg_missing:{GHORG}")
    env = {**os.environ, "GHORG_GITHUB_TOKEN": gh_token()}
    return run(ghorg_args(owner, projects_dir, dry_run=dry_run), timeout=1800, env=env)


def summarize_ghorg_output(output: str) -> dict[str, int]:
    text = output.lower()
    return {
        "cloned": len(re.findall(r"\bclon(?:e|ed|ing)\b", text)),
        "updated": len(re.findall(r"\b(fetch|pull|update|updated)\b", text)),
        "protected_skipped": len(re.findall(r"(protect-local|uncommitted|unpushed|skip|skipped)", text)),
        "errors": len(re.findall(r"\b(error|failed|fatal)\b", text)),
    }


def normalize_remote(url: str) -> str:
    text = url.strip()
    if text.startswith("git@github.com:"):
        text = "https://github.com/" + text.removeprefix("git@github.com:")
    return text.removesuffix(".git").rstrip("/").lower()


def git_repo_matches_remote(worktree: Path, expected_remote: str) -> bool:
    if not (worktree / ".git").exists():
        return False
    result = run(["git", "config", "--get", "remote.origin.url"], worktree, timeout=30)
    if result.returncode != 0:
        return False
    return normalize_remote(result.stdout) == normalize_remote(expected_remote)


def require_ok(result: subprocess.CompletedProcess[str], label: str) -> None:
    if result.returncode != 0:
        raise AutosyncError(label)


def register_in_megavault(megavault: Path, projects_dir: Path, repos: list[dict[str, str]], *, dry_run: bool) -> dict[str, int | str]:
    if dry_run:
        return {"already_registered": 0, "newly_registered": 0, "deferred": 0, "validation": "dry_run"}

    status = run(["git", "status", "--porcelain"], megavault, timeout=30)
    require_ok(status, "megavault_status_failed")
    if status.stdout.strip():
        return {"already_registered": 0, "newly_registered": 0, "deferred": len(repos), "validation": "deferred_dirty"}

    fetch = run(["git", "fetch", "origin"], megavault, timeout=120)
    require_ok(fetch, "megavault_fetch_failed")
    sync = run(["git", "rev-list", "--left-right", "--count", "HEAD...@{u}"], megavault, timeout=30)
    require_ok(sync, "megavault_sync_check_failed")
    if sync.stdout.strip() != "0\t0":
        return {"already_registered": 0, "newly_registered": 0, "deferred": len(repos), "validation": "deferred_not_synced"}

    before = run(["git", "rev-parse", "HEAD"], megavault, timeout=30)
    require_ok(before, "megavault_head_failed")
    before_sha = before.stdout.strip()
    already = created = deferred = 0

    for repo in repos:
        worktree = projects_dir / repo["name"]
        if not git_repo_matches_remote(worktree, repo["url"]):
            deferred += 1
            continue
        result = run(
            [
                "python3",
                str(megavault / "megavault.py"),
                "register-github-repo",
                "--owner",
                repo["owner"],
                "--name",
                repo["name"],
                "--remote-url",
                repo["url"],
                "--default-branch",
                repo["default_branch"],
                "--worktree",
                str(worktree),
            ],
            cwd=megavault,
            timeout=60,
        )
        if result.returncode != 0:
            raise AutosyncError("megavault_register_failed")
        if "status=created" in result.stdout:
            created += 1
        else:
            already += 1

    validation = run(["python3", str(megavault / "megavault.py"), "validate"], cwd=megavault, timeout=120)
    require_ok(validation, "megavault_validate_failed")

    if created:
        require_ok(run(["git", "add", "megavault.sqlite"], megavault, timeout=30), "megavault_git_add_failed")
        require_ok(run(["git", "commit", "-m", "Register autosynced GitHub repositories"], megavault, timeout=120), "megavault_metadata_commit_failed")
        require_ok(run(["git", "push", "origin", "HEAD"], megavault, timeout=240), "megavault_metadata_push_failed")
        verify = run(["git", "rev-list", "--left-right", "--count", "HEAD...@{u}"], megavault, timeout=30)
        require_ok(verify, "megavault_metadata_verify_failed")
        if verify.stdout.strip() != "0\t0":
            raise AutosyncError("megavault_metadata_verify_failed")

    after = run(["git", "rev-parse", "HEAD"], megavault, timeout=30)
    require_ok(after, "megavault_head_verify_failed")
    return {
        "already_registered": already,
        "newly_registered": created,
        "deferred": deferred,
        "validation": "PASS",
        "commit_changed": str(before_sha != after.stdout.strip()),
    }


def command_run(args: argparse.Namespace) -> int:
    projects_dir = Path(args.projects_dir).expanduser()
    state_dir = Path(args.state_dir).expanduser()
    megavault = Path(args.megavault).expanduser()
    with ExclusiveLock(state_dir / "autosync.lock"):
        repos = [{**repo, "owner": args.owner} for repo in github_repos(args.owner)]
        ghorg_result = run_ghorg(args.owner, projects_dir, dry_run=args.dry_run)
        summary = summarize_ghorg_output((ghorg_result.stdout or "") + "\n" + (ghorg_result.stderr or ""))
        if ghorg_result.returncode == 0:
            registration = register_in_megavault(megavault, projects_dir, repos, dry_run=args.dry_run)
        else:
            registration = {
                "already_registered": 0,
                "newly_registered": 0,
                "deferred": len(repos),
                "validation": "skipped_ghorg_failed",
            }
    payload = {
        "status": "ok" if ghorg_result.returncode == 0 else "ghorg_failed",
        "owner": args.owner,
        "discovered": len(repos),
        **summary,
        "megavault": registration,
    }
    print(json.dumps(payload, sort_keys=True))
    return 0 if ghorg_result.returncode == 0 else 75


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Safely autosync gernalix GitHub repos with ghorg.")
    parser.add_argument("--owner", default=DEFAULT_OWNER)
    parser.add_argument("--projects-dir", default=str(DEFAULT_PROJECTS_DIR))
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    parser.add_argument("--megavault", default=str(DEFAULT_MEGAVAULT))
    parser.add_argument("--dry-run", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run")
    run_p.set_defaults(func=command_run)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except BlockingIOError:
        print(json.dumps({"status": "locked"}, sort_keys=True))
        return 0
    except AutosyncError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 75


if __name__ == "__main__":
    raise SystemExit(main())
