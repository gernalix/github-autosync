#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

import repo_single_writer

DEFAULT_OWNER = "gernalix"
DEFAULT_PROJECTS_DIR = Path.home() / "projects"
DEFAULT_STATE_DIR = Path.home() / ".local/state/codex-github-autosync"
DEFAULT_MEGAVAULT = Path.home() / "MegaVault"
ACTIVITY_LOG_FILE = "activity.jsonl"
ACTIVITY_DATA_STATE_FILE = "activity-data-state.json"
ACTIVITY_DATA_CHECKOUT_DIR = "github-autosync-data"
ACTIVITY_DATA_REMOTE = "https://github.com/gernalix/github-autosync-data.git"
ACTIVITY_DATA_BRANCH = "main"
ACTIVITY_SCHEMA_VERSION = 1
REPO_STATE_FILE = "repo-state.json"
RUNTIME_DEPLOY_STATE_FILE = "runtime-deploy-state.json"
ROADMAP_REPOSITORY = "gernalix/codex-roadmap"
RUNTIME_DEPLOYERS: dict[str, tuple[str, ...]] = {
    "gernalix/workflowy-importer": ("python3", "deploy_runtime.py"),
    "gernalix/chrome-codex-switcher": ("bash", "install.sh"),
}
INDEPENDENT_CANONICAL_WRITER_REPOSITORIES = frozenset(
    {
        "gernalix/activity-watch-data",
    }
)
ROADMAP_PULL_SCRIPT = Path("tools/roadmap_pull.py")
ROADMAP_RESULT_SCRIPT = Path.home() / "projects" / "codex-roadmap" / "tools" / "roadmap_result.py"
ROADMAP_CANONICAL_FILES = frozenset(
    {
        "roadmap.sqlite",
        "roadmap.md",
        "spiegazioni.md",
        "prompt-registry.md",
    }
)
ROADMAP_CANONICAL_PREFIXES = (
    "obsidian/",
    "prompts/",
    "completed/",
    "falliti/",
    "mutations/inbox/",
    "mutations/applied/",
)
ALLOWED_REPOSITORIES = frozenset(
    {
        "gernalix/codex-roadmap",
        "gernalix/vm_oracle",
        "gernalix/MegaVault",
        "gernalix/fedora-system-monitor",
        "gernalix/codex-usage",
        "gernalix/github-autosync",
        "gernalix/workflowy-importer",
        "gernalix/chrome-codex-switcher",
        "gernalix/PersonalHub",
        "gernalix/codex-usage-monitor",
        "gernalix/fedora-t7-backup",
        "gernalix/amici_fb",
        "gernalix/salute",
    }
)


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
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
    )


def require_ok(result: subprocess.CompletedProcess[str], label: str) -> None:
    if result.returncode != 0:
        raise AutosyncError(label)


def _normalize_repo_metadata(item: dict[str, Any], *, rest: bool) -> dict[str, str]:
    if rest:
        owner = item.get("owner") or {}
        owner_login = str(owner.get("login") or "")
        return {
            "name": str(item["name"]),
            "url": str(item.get("html_url") or item.get("url") or ""),
            "default_branch": str(item.get("default_branch") or "UNKNOWN"),
            "pushed_at": str(item.get("pushed_at") or "UNKNOWN"),
            "archived": "1" if item.get("archived") else "0",
            "_owner": owner_login,
        }
    branch = item.get("defaultBranchRef") or {}
    return {
        "name": str(item["name"]),
        "url": str(item["url"]),
        "default_branch": str(branch.get("name") or "UNKNOWN"),
        "pushed_at": str(item.get("pushedAt") or "UNKNOWN"),
        "archived": "1" if item.get("isArchived") else "0",
        "_owner": "",
    }


def _github_repos_rest(owner: str) -> list[dict[str, str]] | None:
    """Prefer REST to avoid coupling the minute timer to GraphQL quota."""
    result = run(
        [
            "gh",
            "api",
            "--paginate",
            "--slurp",
            "-H",
            "Accept: application/vnd.github+json",
            "-H",
            "X-GitHub-Api-Version: 2022-11-28",
            "user/repos?affiliation=owner&per_page=100&sort=full_name",
        ],
        timeout=120,
    )
    if result.returncode != 0:
        return None
    try:
        pages = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(pages, list):
        return None
    repos: list[dict[str, str]] = []
    for page in pages:
        if not isinstance(page, list):
            return None
        for item in page:
            if not isinstance(item, dict):
                return None
            row = _normalize_repo_metadata(item, rest=True)
            if row.pop("_owner", "").lower() != owner.lower():
                continue
            if not row["url"]:
                return None
            repos.append(row)
    return sorted(repos, key=lambda row: row["name"])


def _github_repos_graphql(owner: str) -> list[dict[str, str]] | None:
    result = run(
        [
            "gh",
            "repo",
            "list",
            owner,
            "--limit",
            "1000",
            "--json",
            "name,url,defaultBranchRef,pushedAt,isArchived",
        ],
        timeout=120,
    )
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list):
        return None
    repos: list[dict[str, str]] = []
    for item in payload:
        if not isinstance(item, dict):
            return None
        row = _normalize_repo_metadata(item, rest=False)
        row.pop("_owner", None)
        repos.append(row)
    return sorted(repos, key=lambda row: row["name"])


def github_repos(owner: str) -> list[dict[str, str]]:
    """One metadata discovery path; REST first, GraphQL only as bounded fallback."""
    repos = _github_repos_rest(owner)
    if repos is not None:
        return repos
    repos = _github_repos_graphql(owner)
    if repos is not None:
        return repos
    raise AutosyncError("github_repo_list_failed")


def allowed_repo_key(owner: str, name: str) -> str:
    return f"{owner}/{name}"


def is_allowed_repo(owner: str, name: str) -> bool:
    return allowed_repo_key(owner, name) in ALLOWED_REPOSITORIES


def is_independent_canonical_writer_repo(owner: str, name: str) -> bool:
    key = allowed_repo_key(owner, name).lower()
    return any(key == repo.lower() for repo in INDEPENDENT_CANONICAL_WRITER_REPOSITORIES)


def github_remote_key(url: str) -> str | None:
    normalized = normalize_remote(url)
    prefix = "https://github.com/"
    if not normalized.startswith(prefix):
        return None
    parts = normalized.removeprefix(prefix).split("/")
    if len(parts) != 2:
        return None
    for allowed in ALLOWED_REPOSITORIES:
        owner, name = allowed.split("/", 1)
        if parts[0].lower() == owner.lower() and parts[1].lower() == name.lower():
            return allowed
    return None


def filter_allowed_repos(owner: str, repos: list[dict[str, str]]) -> list[dict[str, str]]:
    return [repo for repo in repos if is_allowed_repo(owner, repo["name"])]


def filter_allowed_inventory(inventory: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry for entry in inventory if github_remote_key(str(entry["remote_url"])) in ALLOWED_REPOSITORIES]


def repo_fingerprint(repo: dict[str, str]) -> str:
    return json.dumps(
        {
            "url": normalize_remote(repo["url"]),
            "default_branch": repo.get("default_branch") or "UNKNOWN",
            "pushed_at": repo.get("pushed_at") or "UNKNOWN",
            "archived": repo.get("archived") or "0",
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def load_repo_state(state_dir: Path) -> dict[str, str]:
    path = state_dir / REPO_STATE_FILE
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    repos = raw.get("repos") if isinstance(raw, dict) else None
    if not isinstance(repos, dict):
        return {}
    return {str(key): str(value) for key, value in repos.items()}


def save_repo_state(state_dir: Path, state: dict[str, str]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / REPO_STATE_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"repos": state}, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def load_runtime_deploy_state(state_dir: Path) -> dict[str, str]:
    path = state_dir / RUNTIME_DEPLOY_STATE_FILE
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    repos = raw.get("repos") if isinstance(raw, dict) else None
    if not isinstance(repos, dict):
        return {}
    return {str(key): str(value) for key, value in repos.items()}


def save_runtime_deploy_state(state_dir: Path, state: dict[str, str]) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / RUNTIME_DEPLOY_STATE_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"repos": state}, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def deploy_runtime_if_needed(
    repo_key: str,
    worktree: Path,
    deployed_state: dict[str, str],
    *,
    dry_run: bool,
) -> tuple[str, str]:
    command = RUNTIME_DEPLOYERS.get(repo_key)
    if command is None:
        return "not_applicable", ""

    head = run(["git", "rev-parse", "--verify", "HEAD"], worktree, timeout=30)
    if head.returncode != 0 or not head.stdout.strip():
        return "failed", "runtime_deploy_head_unavailable"
    head_sha = head.stdout.strip()
    if deployed_state.get(repo_key) == head_sha:
        return "up_to_date", head_sha

    entrypoint = worktree / command[-1]
    if not entrypoint.is_file():
        return "failed", f"runtime_deploy_entrypoint_missing:{command[-1]}"
    if dry_run:
        return "would_deploy", head_sha

    deployed = run(list(command), worktree, timeout=240)
    if deployed.returncode != 0:
        evidence = (deployed.stderr or deployed.stdout or "command_failed").strip()
        evidence = " ".join(evidence.split())[:400]
        return "failed", f"runtime_deploy_failed:{evidence}"

    deployed_state[repo_key] = head_sha
    return "deployed", head_sha


def append_activity(
    state_dir: Path,
    *,
    action: str,
    repo: str,
    branch: str | None = None,
    project_id: int | None = None,
    worktree: str | None = None,
    detail: str = "",
) -> dict[str, Any]:
    event = {
        "schema_version": ACTIVITY_SCHEMA_VERSION,
        "event_id": uuid4().hex,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "action": action,
        "repo": repo,
        "branch": branch,
        "project_id": project_id,
        "worktree": worktree,
        "detail": detail,
    }
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        with (state_dir / ACTIVITY_LOG_FILE).open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise AutosyncError("activity_log_write_failed") from exc
    return event


def _activity_data_state_path(state_dir: Path) -> Path:
    return state_dir / ACTIVITY_DATA_STATE_FILE


def _load_activity_data_cursor(state_dir: Path, total_lines: int) -> int:
    path = _activity_data_state_path(state_dir)
    if not path.is_file():
        return 0
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        next_line = int(raw.get("next_line", 0))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise AutosyncError("activity_data_state_invalid") from exc
    if next_line < 0 or next_line > total_lines:
        raise AutosyncError("activity_data_state_invalid")
    return next_line


def _save_activity_data_cursor(state_dir: Path, next_line: int) -> None:
    path = _activity_data_state_path(state_dir)
    tmp = path.with_suffix(".tmp")
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps({"next_line": next_line}, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:
        raise AutosyncError("activity_data_state_write_failed") from exc


def _normalize_activity_event(raw: dict[str, Any], *, line_number: int, raw_line: str) -> dict[str, Any]:
    event = dict(raw)
    timestamp = str(event.get("timestamp") or "")
    try:
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AutosyncError(f"activity_data_invalid_timestamp:line={line_number + 1}") from exc
    if parsed.tzinfo is None:
        raise AutosyncError(f"activity_data_invalid_timestamp:line={line_number + 1}")
    event["schema_version"] = int(event.get("schema_version") or ACTIVITY_SCHEMA_VERSION)
    if event["schema_version"] != ACTIVITY_SCHEMA_VERSION:
        raise AutosyncError(f"activity_data_unsupported_schema:line={line_number + 1}")
    event_id = str(event.get("event_id") or "").strip()
    if not event_id:
        digest = hashlib.sha256(f"{line_number}:{raw_line}".encode("utf-8")).hexdigest()
        event_id = f"legacy-{digest[:32]}"
    event["event_id"] = event_id
    event["timestamp"] = timestamp
    for key in ("action", "repo", "detail"):
        event[key] = str(event.get(key) or "")
    event["branch"] = event.get("branch")
    event["project_id"] = event.get("project_id")
    event["worktree"] = event.get("worktree")
    return event


def _ensure_activity_data_checkout(state_dir: Path) -> Path:
    checkout = state_dir / ACTIVITY_DATA_CHECKOUT_DIR
    state_dir.mkdir(parents=True, exist_ok=True)
    if checkout.exists() and not (checkout / ".git").exists():
        shutil.rmtree(checkout)
    if not checkout.exists():
        clone_tmp = state_dir / f"{ACTIVITY_DATA_CHECKOUT_DIR}.clone-tmp"
        if clone_tmp.exists():
            shutil.rmtree(clone_tmp)
        clone = run(
            [
                "git",
                "clone",
                "--origin",
                "origin",
                "--branch",
                ACTIVITY_DATA_BRANCH,
                "--single-branch",
                ACTIVITY_DATA_REMOTE,
                str(clone_tmp),
            ],
            timeout=300,
        )
        if clone.returncode != 0:
            raise AutosyncError("activity_data_clone_failed")
        os.replace(clone_tmp, checkout)
    if not (checkout / ".git").exists():
        raise AutosyncError("activity_data_not_git")
    remote = run(["git", "config", "--get", "remote.origin.url"], checkout, timeout=30)
    require_ok(remote, "activity_data_origin_missing")
    if normalize_remote(remote.stdout) != normalize_remote(ACTIVITY_DATA_REMOTE):
        raise AutosyncError("activity_data_origin_mismatch")
    branch = run(["git", "branch", "--show-current"], checkout, timeout=30)
    require_ok(branch, "activity_data_branch_check_failed")
    if branch.stdout.strip() != ACTIVITY_DATA_BRANCH:
        raise AutosyncError("activity_data_wrong_branch")
    status = run(["git", "status", "--porcelain"], checkout, timeout=30)
    require_ok(status, "activity_data_status_failed")
    if status.stdout.strip():
        dirty_paths: list[str] = []
        for line in status.stdout.splitlines():
            path = line[3:].strip()
            if " -> " in path:
                path = path.split(" -> ", 1)[1]
            dirty_paths.append(path)
        if not dirty_paths or any(not path.startswith("activity/") for path in dirty_paths):
            raise AutosyncError("activity_data_dirty")
        require_ok(run(["git", "reset", "--hard", "HEAD"], checkout, timeout=60), "activity_data_recovery_reset_failed")
        require_ok(run(["git", "clean", "-fd", "--", "activity"], checkout, timeout=60), "activity_data_recovery_clean_failed")
    fetch = run(["git", "fetch", "--prune", "origin"], checkout, timeout=120)
    require_ok(fetch, "activity_data_fetch_failed")
    relation = run(
        ["git", "rev-list", "--left-right", "--count", f"HEAD...origin/{ACTIVITY_DATA_BRANCH}"],
        checkout,
        timeout=30,
    )
    require_ok(relation, "activity_data_relation_failed")
    parts = relation.stdout.split()
    if len(parts) != 2:
        raise AutosyncError("activity_data_relation_failed")
    ahead, behind = int(parts[0]), int(parts[1])
    if ahead and behind:
        raise AutosyncError("activity_data_diverged")
    if behind:
        merge = run(["git", "merge", "--ff-only", f"origin/{ACTIVITY_DATA_BRANCH}"], checkout, timeout=120)
        require_ok(merge, "activity_data_fast_forward_failed")
    elif ahead:
        push = run(["git", "push", "origin", f"HEAD:{ACTIVITY_DATA_BRANCH}"], checkout, timeout=240)
        require_ok(push, "activity_data_recovery_push_failed")
        verify_fetch = run(["git", "fetch", "--prune", "origin"], checkout, timeout=120)
        require_ok(verify_fetch, "activity_data_recovery_verify_failed")
        verify = run(
            ["git", "rev-list", "--left-right", "--count", f"HEAD...origin/{ACTIVITY_DATA_BRANCH}"],
            checkout,
            timeout=30,
        )
        require_ok(verify, "activity_data_recovery_verify_failed")
        if verify.stdout.strip() not in {"0\t0", "0 0"}:
            raise AutosyncError("activity_data_recovery_verify_failed")
    return checkout


def mirror_pending_activity(state_dir: Path, *, enabled: bool) -> str:
    if not enabled:
        return "disabled"
    log_path = state_dir / ACTIVITY_LOG_FILE
    if not log_path.is_file():
        return "unchanged"
    try:
        raw_lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise AutosyncError("activity_data_log_read_failed") from exc
    next_line = _load_activity_data_cursor(state_dir, len(raw_lines))
    if next_line == len(raw_lines):
        return "unchanged"

    pending: list[dict[str, Any]] = []
    for index in range(next_line, len(raw_lines)):
        raw_line = raw_lines[index]
        try:
            raw = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise AutosyncError(f"activity_data_invalid_json:line={index + 1}") from exc
        if not isinstance(raw, dict):
            raise AutosyncError(f"activity_data_invalid_json:line={index + 1}")
        pending.append(_normalize_activity_event(raw, line_number=index, raw_line=raw_line))

    checkout = _ensure_activity_data_checkout(state_dir)
    added = 0
    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in pending:
        timestamp = str(event["timestamp"])
        parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        date = parsed.date().isoformat()
        grouped.setdefault(date, []).append(event)

    for date, events in sorted(grouped.items()):
        year, month, _ = date.split("-", 2)
        path = checkout / "activity" / year / month / f"{date}.jsonl"
        existing_ids: set[str] = set()
        if path.is_file():
            try:
                for existing_line in path.read_text(encoding="utf-8").splitlines():
                    if not existing_line.strip():
                        continue
                    existing = json.loads(existing_line)
                    if isinstance(existing, dict) and existing.get("event_id"):
                        existing_ids.add(str(existing["event_id"]))
            except (OSError, json.JSONDecodeError) as exc:
                raise AutosyncError(f"activity_data_existing_file_invalid:{path.name}") from exc
        new_lines: list[str] = []
        for event in events:
            event_id = str(event["event_id"])
            if event_id in existing_ids:
                continue
            new_lines.append(json.dumps(event, sort_keys=True, separators=(",", ":")))
            existing_ids.add(event_id)
            added += 1
        if new_lines:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                for line in new_lines:
                    handle.write(line + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    if not added:
        _save_activity_data_cursor(state_dir, len(raw_lines))
        return f"reconciled:{len(pending)}"

    require_ok(run(["git", "add", "activity"], checkout, timeout=30), "activity_data_git_add_failed")
    status = run(["git", "status", "--porcelain"], checkout, timeout=30)
    require_ok(status, "activity_data_status_failed")
    if not status.stdout.strip():
        _save_activity_data_cursor(state_dir, len(raw_lines))
        return f"reconciled:{len(pending)}"

    last_timestamp = str(pending[-1]["timestamp"])
    commit = run(
        [
            "git",
            "-c",
            "user.name=github-autosync",
            "-c",
            "user.email=github-autosync@localhost",
            "commit",
            "-m",
            f"Record autosync activity through {last_timestamp}",
        ],
        checkout,
        timeout=120,
    )
    require_ok(commit, "activity_data_commit_failed")
    push = run(["git", "push", "origin", f"HEAD:{ACTIVITY_DATA_BRANCH}"], checkout, timeout=240)
    require_ok(push, "activity_data_push_failed")
    verify_fetch = run(["git", "fetch", "--prune", "origin"], checkout, timeout=120)
    require_ok(verify_fetch, "activity_data_verify_failed")
    verify = run(
        ["git", "rev-list", "--left-right", "--count", f"HEAD...origin/{ACTIVITY_DATA_BRANCH}"],
        checkout,
        timeout=30,
    )
    require_ok(verify, "activity_data_verify_failed")
    if verify.stdout.strip() not in {"0\t0", "0 0"}:
        raise AutosyncError("activity_data_verify_failed")
    _save_activity_data_cursor(state_dir, len(raw_lines))
    return f"pushed:{added}"


def normalize_remote(url: str) -> str:
    text = url.strip()
    if text.startswith("git@github.com:"):
        text = "https://github.com/" + text.removeprefix("git@github.com:")
    return text.removesuffix(".git").rstrip("/").lower()


def git_repo_matches_remote(worktree: Path, expected_remote: str) -> bool:
    if not (worktree / ".git").exists():
        return False
    return _matching_remote(worktree, expected_remote) is not None


def _matching_remote(worktree: Path, expected_remote: str) -> str | None:
    names = run(["git", "remote"], worktree, timeout=30)
    if names.returncode != 0:
        return None
    matches = []
    for name in names.stdout.splitlines():
        url = run(["git", "remote", "get-url", name], worktree, timeout=30)
        if url.returncode == 0 and normalize_remote(url.stdout) == normalize_remote(expected_remote):
            matches.append(name)
    return "origin" if "origin" in matches else matches[0] if len(matches) == 1 else None


def megavault_inventory(megavault: Path) -> list[dict[str, Any]]:
    db = megavault / "megavault.sqlite"
    if not db.is_file():
        raise AutosyncError("megavault_db_missing")
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """
            select p.project_id, p.slug, p.name,
                   r.repository_id, r.worktree_path, r.remote_url, r.branch
            from projects p
            join repositories r on r.project_id = p.project_id
            where coalesce(p.archived, 0) = 0
              and coalesce(p.status, 'active') = 'active'
              and coalesce(r.status, 'active') = 'active'
              and r.worktree_path is not null and trim(r.worktree_path) <> ''
              and r.remote_url is not null and trim(r.remote_url) <> ''
              and (coalesce(r.canonical, 0) = 1 or r.repository_kind = 'local_worktree')
            order by p.project_id, r.repository_id
            """
        ).fetchall()
        con.close()
    except sqlite3.Error as exc:
        raise AutosyncError("megavault_inventory_query_failed") from exc
    inventory: dict[str, dict[str, Any]] = {}
    for row in rows:
        path = str(Path(str(row["worktree_path"])).expanduser())
        inventory.setdefault(
            path,
            {
                "project_id": int(row["project_id"]),
                "slug": str(row["slug"] or row["name"] or row["project_id"]),
                "worktree": path,
                "remote_url": str(row["remote_url"]),
                "branch": str(row["branch"] or "UNKNOWN"),
            },
        )
    return [inventory[key] for key in sorted(inventory)]


def megavault_registered_remotes(megavault: Path) -> set[str]:
    db = megavault / "megavault.sqlite"
    if not db.is_file():
        raise AutosyncError("megavault_db_missing")
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
        rows = con.execute(
            "select remote_url from repositories where remote_url is not null and trim(remote_url) <> ''"
        ).fetchall()
        con.close()
    except sqlite3.Error as exc:
        raise AutosyncError("megavault_registered_remotes_failed") from exc
    return {normalize_remote(str(row[0])) for row in rows}


def issue(entry: dict[str, Any] | None, kind: str, detail: str = "") -> dict[str, Any]:
    return {
        "project_id": entry.get("project_id") if entry else None,
        "repo": entry.get("slug") if entry else "autosync",
        "worktree": entry.get("worktree") if entry else None,
        "kind": kind,
        "detail": detail,
    }


_GIT_CORRUPTION_MARKERS = (
    "object file",
    "loose object",
    "bad object",
    "invalid object",
    "missing blob",
    "missing tree",
    "missing commit",
    "unable to read sha1 file",
)


def _git_failure_issue(
    entry: dict[str, Any],
    default_kind: str,
    result: subprocess.CompletedProcess[str],
) -> dict[str, Any]:
    """Preserve concise Git stderr so runtime failures are diagnosable in one pass."""
    raw = (result.stderr.strip() or result.stdout.strip())
    compact = " ".join(raw.split())
    detail = compact[:500]
    lowered = compact.lower()
    corrupt = any(marker in lowered for marker in _GIT_CORRUPTION_MARKERS) and (
        "empty" in lowered
        or "corrupt" in lowered
        or "bad object" in lowered
        or "invalid object" in lowered
        or "missing " in lowered
        or "unable to read" in lowered
    )
    return issue(entry, "git_object_corrupt" if corrupt else default_kind, detail)


def git_counts(worktree: Path, upstream_ref: str = "@{u}") -> tuple[int, int] | None:
    result = run(
        ["git", "rev-list", "--left-right", "--count", f"HEAD...{upstream_ref}"],
        worktree,
        timeout=30,
    )
    if result.returncode != 0:
        return None
    parts = result.stdout.split()
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


def classify_relation(ahead: int, behind: int) -> str:
    if ahead and behind:
        return "diverged"
    if ahead:
        return "ahead"
    if behind:
        return "behind"
    return "synced"


def _tracking_ref_exists(worktree: Path, remote_name: str, branch: str) -> bool:
    ref = f"refs/remotes/{remote_name}/{branch}"
    return run(["git", "show-ref", "--verify", "--quiet", ref], worktree, timeout=30).returncode == 0


def _git_operation(worktree: Path) -> str | None:
    unresolved = run(["git", "ls-files", "-u"], worktree, timeout=30)
    if unresolved.returncode != 0:
        return "index_check_failed"
    if unresolved.stdout:
        return "unresolved_conflicts"
    for name, label in (("MERGE_HEAD", "merge_in_progress"), ("rebase-merge", "rebase_in_progress"),
                        ("rebase-apply", "rebase_in_progress"), ("CHERRY_PICK_HEAD", "cherry_pick_in_progress"),
                        ("REVERT_HEAD", "revert_in_progress"), ("sequencer", "sequencer_in_progress")):
        path = run(["git", "rev-parse", "--git-path", name], worktree, timeout=30)
        if path.returncode == 0 and (worktree / path.stdout.strip()).exists():
            return label
    lock = run(["git", "rev-parse", "--git-path", "index.lock"], worktree, timeout=30)
    if lock.returncode == 0 and (worktree / lock.stdout.strip()).exists():
        return "git_lock_present"
    return None


def _remote_tracking(
    worktree: Path, *, remote_name: str, local_branch: str, default_branch: str,
    dry_run: bool,
) -> tuple[str | None, str | None]:
    """Select an unambiguous remote branch and fetch its exact ref, bypassing narrow refspecs."""
    choices = [local_branch]
    if default_branch not in {"", "UNKNOWN", local_branch}:
        choices.append(default_branch)
    upstream = run(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], worktree, timeout=30)
    if upstream.returncode == 0 and upstream.stdout.strip().startswith(remote_name + "/"):
        candidate = upstream.stdout.strip().split("/", 1)[1]
        if candidate not in choices:
            choices.append(candidate)
    chosen = None
    for branch in choices:
        probe = run(["git", "ls-remote", "--exit-code", "--heads", remote_name, f"refs/heads/{branch}"], worktree, timeout=120)
        if probe.returncode == 0 and len(probe.stdout.split()) >= 2:
            chosen = branch
            break
        if probe.returncode not in (0, 2):
            return None, "remote_probe_failed"
    if chosen is None:
        return None, "remote_branch_missing"
    ref = f"{remote_name}/{chosen}"
    if dry_run:
        return ref, None
    fetch = run(["git", "fetch", "--no-tags", remote_name,
                 f"+refs/heads/{chosen}:refs/remotes/{remote_name}/{chosen}"], worktree, timeout=120)
    if fetch.returncode != 0:
        return None, "fetch_failed"
    if not _tracking_ref_exists(worktree, remote_name, chosen):
        return None, "tracking_ref_missing_after_fetch"
    if chosen != local_branch:
        safe_fallback = run(["git", "merge-base", "--is-ancestor", "HEAD",
                             f"refs/remotes/{ref}"], worktree, timeout=30)
        if safe_fallback.returncode != 0:
            return None, "remote_branch_ambiguous"
    configured = run(["git", "config", "--get-all", f"remote.{remote_name}.fetch"], worktree, timeout=30)
    specs = configured.stdout.splitlines() if configured.returncode == 0 else []
    broad = f"+refs/heads/*:refs/remotes/{remote_name}/*"
    narrow = f"+refs/heads/{chosen}:refs/remotes/{remote_name}/{chosen}"
    if broad not in specs and narrow not in specs:
        added = run(["git", "config", "--add", f"remote.{remote_name}.fetch", narrow], worktree, timeout=30)
        if added.returncode != 0:
            return None, "refspec_repair_failed"
    if upstream.returncode != 0 or upstream.stdout.strip() != ref:
        tracking = run(["git", "branch", f"--set-upstream-to={ref}", local_branch], worktree, timeout=30)
        if tracking.returncode != 0:
            return None, "upstream_repair_failed"
    if git_counts(worktree, f"refs/remotes/{ref}") is None:
        return None, "relation_check_failed"
    return ref, None


def _recover_unborn_branch(worktree: Path, repo: dict[str, str], entry: dict[str, Any], *, dry_run: bool) -> dict[str, Any] | None:
    """Attach a clean unborn local branch to the same named remote branch."""
    status = run(["git", "status", "--porcelain=v1", "-z"], worktree, timeout=30)
    if status.returncode != 0 or status.stdout:
        return issue(entry, "unborn_branch_with_local_files")
    local = run(["git", "branch", "--show-current"], worktree, timeout=30)
    if local.returncode != 0 or not local.stdout.strip():
        return issue(entry, "unborn_branch")
    branch = local.stdout.strip()
    remote = _matching_remote(worktree, repo["url"])
    if remote is None:
        return issue(entry, "remote_ambiguous_or_missing")
    probe = run(["git", "ls-remote", "--exit-code", "--heads", remote, f"refs/heads/{branch}"], worktree, timeout=120)
    if probe.returncode != 0 or len(probe.stdout.split()) < 2:
        return issue(entry, "unborn_remote_branch_missing")
    if dry_run:
        return None
    fetched = run(["git", "fetch", "--no-tags", remote,
                   f"+refs/heads/{branch}:refs/remotes/{remote}/{branch}"], worktree, timeout=120)
    if fetched.returncode != 0:
        return issue(entry, "fetch_failed")
    switched = run(["git", "switch", "--no-overwrite-ignore", "-C", branch,
                    "--track", f"{remote}/{branch}"], worktree, timeout=120)
    if switched.returncode != 0:
        return issue(entry, "unborn_checkout_failed")
    return None


def _repair_tracking_branch(
    worktree: Path,
    *,
    local_branch: str,
    remote_name: str,
    default_branch: str,
) -> str | None:
    candidates: list[str] = []
    for candidate in (local_branch, default_branch):
        candidate = str(candidate or "").strip()
        if not candidate or candidate == "UNKNOWN" or candidate in candidates:
            continue
        candidates.append(candidate)
    for candidate in candidates:
        if not _tracking_ref_exists(worktree, remote_name, candidate):
            continue
        tracking = run(
            ["git", "branch", f"--set-upstream-to={remote_name}/{candidate}", local_branch],
            worktree,
            timeout=30,
        )
        if tracking.returncode == 0:
            return f"{remote_name}/{candidate}"
    return None


def _is_roadmap_canonical_path(path: str) -> bool:
    return path in ROADMAP_CANONICAL_FILES or path.startswith(ROADMAP_CANONICAL_PREFIXES)


def _roadmap_local_ahead_paths(worktree: Path) -> set[str] | None:
    base = run(["git", "merge-base", "HEAD", "@{u}"], worktree, timeout=30)
    if base.returncode != 0 or not base.stdout.strip():
        return None
    diff = run(
        ["git", "diff", "--name-only", "-z", f"{base.stdout.strip()}..HEAD"],
        worktree,
        timeout=30,
    )
    if diff.returncode != 0:
        return None
    return _nul_paths(diff.stdout)


def _push_with_race_recovery(
    worktree: Path,
    remote_name: str,
    remote_branch: str,
    *,
    allow_rebase: bool,
    upstream_ref: str = "@{u}",
) -> tuple[str, str]:
    """Push once, then use one fresh fetch as evidence for a bounded recovery.

    Returns (action, detail), where action is one of:
    pushed, synced, remote_ahead, blocked.
    """
    push = run(["git", "push", remote_name, f"HEAD:{remote_branch}"], worktree, timeout=240)
    if push.returncode == 0:
        return "pushed", ""

    fetch = run(["git", "fetch", "--prune", remote_name], worktree, timeout=120)
    if fetch.returncode != 0:
        return "blocked", "push_failed_then_fetch_failed"
    counts = git_counts(worktree, upstream_ref)
    if counts is None:
        return "blocked", "push_failed_relation_check_failed"
    ahead, behind = counts
    if not ahead and not behind:
        return "synced", "remote_already_contains_head"
    if not ahead and behind:
        return "remote_ahead", f"behind={behind}"
    if ahead and behind:
        if not allow_rebase:
            return "blocked", f"diverged_after_push_race:ahead={ahead},behind={behind}"
        status = run(["git", "status", "--porcelain"], worktree, timeout=30)
        if status.returncode != 0 or status.stdout.strip():
            return "blocked", f"diverged_dirty_after_push_race:ahead={ahead},behind={behind}"
        rebase = run(["git", "rebase", upstream_ref], worktree, timeout=300)
        if rebase.returncode != 0:
            run(["git", "rebase", "--abort"], worktree, timeout=120)
            return "blocked", f"rebase_conflict_after_push_race:ahead={ahead},behind={behind}"

    retry = run(["git", "push", remote_name, f"HEAD:{remote_branch}"], worktree, timeout=240)
    if retry.returncode != 0:
        return "blocked", "push_retry_failed_after_fresh_fetch"
    verify_fetch = run(["git", "fetch", "--prune", remote_name], worktree, timeout=120)
    if verify_fetch.returncode != 0:
        return "blocked", "post_push_fetch_failed"
    verify = git_counts(worktree, upstream_ref)
    if verify != (0, 0):
        return "blocked", f"post_push_verify_failed:counts={verify}"
    return "pushed", ""


def _run_roadmap_pull(worktree: Path, remote_name: str, remote_branch: str) -> tuple[bool, dict[str, Any]]:
    script = worktree / ROADMAP_PULL_SCRIPT
    if not script.is_file():
        return False, {"status": "BLOCKED", "reason": "roadmap_pull_script_missing"}
    result = run(
        [
            sys.executable,
            str(script),
            "--repo",
            str(worktree),
            "--remote",
            remote_name,
            "--branch",
            remote_branch,
            "--bootstrap-guard",
        ],
        worktree,
        timeout=300,
    )
    payload: dict[str, Any] = {}
    for line in reversed(result.stdout.splitlines()):
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            payload = parsed
            break
    if result.returncode != 0 or payload.get("status") != "PASS":
        if not payload:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit={result.returncode}"
            payload = {"status": "FAIL", "reason": detail}
        return False, payload
    return True, payload


def sync_roadmap_repo(
    repo: dict[str, str],
    worktree: Path,
    entry: dict[str, Any],
    *,
    dry_run: bool,
) -> tuple[str, dict[str, Any] | None]:
    """Use codex-roadmap's canonical guarded pull instead of generic Git pull logic."""
    if dry_run:
        return "would_update", None

    resolved, problems, upstream_name, fetched_remote = _worktree_basics(entry)
    if problems:
        return "deferred", problems[0]
    assert resolved == worktree
    assert upstream_name is not None
    remote_name, remote_branch = upstream_name.split("/", 1)

    if fetched_remote != remote_name:
        fetch = run(["git", "fetch", "--prune", remote_name], worktree, timeout=120)
        if fetch.returncode != 0:
            return "deferred", issue(entry, "fetch_failed")

    before_head = run(["git", "rev-parse", "HEAD"], worktree, timeout=30)
    if before_head.returncode != 0:
        return "deferred", issue(entry, "head_check_failed")
    before = before_head.stdout.strip()

    counts = git_counts(worktree)
    if counts is None:
        return "deferred", issue(entry, "relation_check_failed")
    ahead, behind = counts
    did_push = False

    if ahead:
        if behind:
            return "deferred", issue(entry, "roadmap_diverged", f"ahead={ahead},behind={behind}")
        ahead_paths = _roadmap_local_ahead_paths(worktree)
        if ahead_paths is None:
            return "deferred", issue(entry, "roadmap_ahead_diff_failed", f"ahead={ahead}")
        canonical = sorted(path for path in ahead_paths if _is_roadmap_canonical_path(path))
        if canonical:
            return "deferred", issue(
                entry,
                "roadmap_local_canonical_commit",
                "paths=" + ",".join(canonical[:20]),
            )
        action, detail = _push_with_race_recovery(
            worktree,
            remote_name,
            remote_branch,
            allow_rebase=False,
        )
        if action == "blocked":
            return "deferred", issue(entry, "push_failed", detail or f"ahead={ahead}")
        did_push = action == "pushed"
        # "remote_ahead" and "synced" are safe to hand to roadmap_pull, which
        # performs the guarded canonical fast-forward/no-op below.

    ok, payload = _run_roadmap_pull(worktree, remote_name, remote_branch)
    if not ok:
        return "deferred", issue(
            entry,
            "roadmap_reconcile_blocked",
            str(payload.get("reason") or payload.get("status") or "unknown"),
        )

    status = run(["git", "status", "--porcelain"], worktree, timeout=30)
    if status.returncode != 0:
        return "deferred", _git_failure_issue(entry, "status_failed", status)
    if status.stdout.strip():
        return "deferred", issue(entry, "roadmap_post_reconcile_dirty")

    final_counts = git_counts(worktree)
    if final_counts != (0, 0):
        return "deferred", issue(entry, "roadmap_post_reconcile_relation", f"counts={final_counts}")

    after_head = run(["git", "rev-parse", "HEAD"], worktree, timeout=30)
    if after_head.returncode != 0:
        return "deferred", issue(entry, "head_check_failed")
    if did_push:
        return "pushed", None
    if after_head.stdout.strip() != before:
        return "updated", None
    return "up_to_date", None


def _worktree_basics(
    entry: dict[str, Any],
) -> tuple[Path, list[dict[str, Any]], str | None, str | None]:
    worktree = Path(str(entry["worktree"])).expanduser()
    if not worktree.exists():
        return worktree, [issue(entry, "missing_worktree")], None, None
    probe = run(["git", "rev-parse", "--is-inside-work-tree"], worktree, timeout=30)
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        return worktree, [issue(entry, "not_git_worktree")], None, None
    remote = run(["git", "config", "--get", "remote.origin.url"], worktree, timeout=30)
    if remote.returncode != 0 or not remote.stdout.strip():
        return worktree, [issue(entry, "origin_missing")], None, None
    if normalize_remote(remote.stdout) != normalize_remote(str(entry["remote_url"])):
        return worktree, [issue(entry, "origin_mismatch")], None, None
    branch = run(["git", "branch", "--show-current"], worktree, timeout=30)
    if branch.returncode != 0 or not branch.stdout.strip():
        return worktree, [issue(entry, "detached_or_unknown_branch")], None, None
    branch_name = branch.stdout.strip()
    upstream = run(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], worktree, timeout=30)
    if upstream.returncode != 0 or "/" not in upstream.stdout.strip():
        fetch = run(["git", "fetch", "--prune", "origin"], worktree, timeout=120)
        if fetch.returncode != 0:
            return worktree, [issue(entry, "fetch_failed")], None, None
        remote_branch = f"refs/remotes/origin/{branch_name}"
        exists = run(["git", "show-ref", "--verify", "--quiet", remote_branch], worktree, timeout=30)
        if exists.returncode != 0:
            return worktree, [issue(entry, "no_upstream")], None, None
        tracking = run(["git", "branch", f"--set-upstream-to=origin/{branch_name}", branch_name], worktree, timeout=30)
        if tracking.returncode != 0:
            return worktree, [issue(entry, "no_upstream")], None, None
        return worktree, [], f"origin/{branch_name}", "origin"
    return worktree, [], upstream.stdout.strip(), None


def audit_worktree(
    entry: dict[str, Any], *, auto_push: bool, report_behind: bool, fetch_remote: bool = True,
) -> tuple[list[dict[str, Any]], bool]:
    worktree, problems, upstream_name, fetched_remote = _worktree_basics(entry)
    if problems:
        return problems, False
    assert upstream_name is not None
    remote_name, remote_branch = upstream_name.split("/", 1)
    status = run(["git", "status", "--porcelain"], worktree, timeout=30)
    if status.returncode != 0:
        return [_git_failure_issue(entry, "status_failed", status)], False
    dirty = bool(status.stdout.strip())
    if fetch_remote and fetched_remote != remote_name:
        fetch = run(["git", "fetch", "--prune", remote_name], worktree, timeout=120)
        if fetch.returncode != 0:
            return [issue(entry, "fetch_failed")], False
    counts = git_counts(worktree)
    if counts is None:
        return [issue(entry, "relation_check_failed")], False
    ahead, behind = counts
    if dirty:
        if ahead and behind:
            kind = "dirty_diverged"
        elif ahead:
            kind = "dirty_with_unpushed_commits"
        elif behind:
            kind = "dirty_behind"
        else:
            kind = "dirty_worktree"
        return [issue(entry, kind, f"ahead={ahead},behind={behind}")], False
    if ahead and behind:
        return [issue(entry, "diverged", f"ahead={ahead},behind={behind}")], False
    if ahead:
        if not auto_push:
            return [issue(entry, "unpushed_commits", f"ahead={ahead}")], False
        action, detail = _push_with_race_recovery(
            worktree,
            remote_name,
            remote_branch,
            allow_rebase=True,
            upstream_ref=upstream_name,
        )
        if action == "pushed":
            return [], True
        if action == "synced":
            return [], False
        if action == "remote_ahead":
            if report_behind:
                return [issue(entry, "still_behind_remote", detail)], False
            return [], False
        return [issue(entry, "push_failed", detail or f"ahead={ahead}")], False
    if behind and report_behind:
        return [issue(entry, "still_behind_remote", f"behind={behind}")], False
    return [], False


def audit_inventory(
    inventory: list[dict[str, Any]], *, auto_push: bool, report_behind: bool, fetch_remote: bool = True,
) -> tuple[list[dict[str, Any]], int]:
    issues: list[dict[str, Any]] = []
    pushed = 0
    for entry in inventory:
        try:
            repo_issues, did_push = audit_worktree(
                entry, auto_push=auto_push, report_behind=report_behind,
                fetch_remote=fetch_remote,
            )
        except Exception as exc:
            repo_issues, did_push = [issue(entry, "reconcile_error", type(exc).__name__)], False
        issues.extend(repo_issues)
        pushed += int(did_push)
    return issues, pushed


def clone_repo(
    repo: dict[str, str],
    projects_dir: Path,
    *,
    dry_run: bool,
    target_worktree: Path | None = None,
) -> str:
    worktree = target_worktree or projects_dir / repo["name"]
    if worktree.exists():
        return "present"
    if dry_run:
        return "would_clone"
    worktree.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["git", "clone", "--origin", "origin"]
    if repo.get("default_branch") and repo["default_branch"] != "UNKNOWN":
        cmd += ["--branch", repo["default_branch"], "--single-branch"]
    cmd += [repo["url"], str(worktree)]
    require_ok(run(cmd, timeout=900), "clone_failed")
    return "cloned"


def _stability_pause() -> None:
    time.sleep(0.2)


def _commit_dirty_for_reconcile(
    worktree: Path,
    entry: dict[str, Any],
) -> dict[str, Any] | None:
    """Turn a generic dirty worktree into one explicit sync checkpoint commit."""
    operation = _git_operation(worktree)
    if operation:
        return issue(entry, operation)
    def snapshot(*, include_status: bool = True) -> str | None:
        status = run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"], worktree, timeout=30)
        if status.returncode != 0:
            return None
        digest = hashlib.sha256()
        if include_status:
            digest.update(status.stdout.encode("utf-8", "surrogateescape"))
        records = status.stdout.split("\0")
        paths = sorted({record[3:] for record in records if len(record) >= 4 and record[2] == " "})
        for relative in paths:
            path = worktree / relative
            digest.update(relative.encode("utf-8", "surrogateescape"))
            try:
                if path.is_symlink():
                    digest.update(os.readlink(path).encode("utf-8", "surrogateescape"))
                elif path.is_file():
                    with path.open("rb") as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(chunk)
            except OSError:
                return None
        return digest.hexdigest()
    before = snapshot()
    if before is None:
        return issue(entry, "worktree_snapshot_failed")
    _stability_pause()
    if snapshot() != before:
        return issue(entry, "worktree_changing")
    content_before = snapshot(include_status=False)
    index_result = run(["git", "rev-parse", "--git-path", "index"], worktree, timeout=30)
    if index_result.returncode != 0:
        return issue(entry, "index_path_failed")
    index_path = worktree / index_result.stdout.strip()
    original_index = index_path.read_bytes() if index_path.exists() else None
    def restore_index() -> None:
        if original_index is None:
            index_path.unlink(missing_ok=True)
        else:
            index_path.write_bytes(original_index)
    add = run(["git", "add", "-A"], worktree, timeout=120)
    if add.returncode != 0:
        restore_index()
        return issue(entry, "git_add_failed")

    # Status changes after staging by design; compare file content through a
    # second pre-add snapshot of the same paths before allowing the commit.
    after = snapshot(include_status=False)
    if after is None:
        restore_index()
        return issue(entry, "worktree_snapshot_failed")
    # A concurrent edit can change content without changing file names.
    if after != content_before:
        restore_index()
        return issue(entry, "worktree_changing")

    staged = run(["git", "diff", "--cached", "--quiet"], worktree, timeout=30)
    if staged.returncode == 0:
        return None
    if staged.returncode != 1:
        restore_index()
        return issue(entry, "staged_diff_check_failed")

    branch = run(["git", "branch", "--show-current"], worktree, timeout=30)
    head = run(["git", "rev-parse", "--verify", "HEAD"], worktree, timeout=30)
    if branch.returncode != 0 or not branch.stdout.strip() or head.returncode != 0:
        restore_index()
        return issue(entry, "auto_commit_failed")
    tree = run(["git", "write-tree"], worktree, timeout=30)
    if tree.returncode != 0 or not tree.stdout.strip():
        restore_index()
        return issue(entry, "auto_commit_failed")

    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    slug = str(entry.get("slug") or "repository")
    commit = run(
        ["git", "commit-tree", tree.stdout.strip(), "-p", head.stdout.strip(), "-m", f"github-reconcile: sync local changes in {slug} ({stamp})"],
        worktree,
        timeout=300,
    )
    if commit.returncode != 0 or not commit.stdout.strip():
        restore_index()
        return issue(entry, "auto_commit_failed")
    auth = repo_single_writer.authorize_ref_update(
        worktree, head.stdout.strip(), commit.stdout.strip(), branch.stdout.strip()
    )
    try:
        update = run(
            ["git", "update-ref", f"refs/heads/{branch.stdout.strip()}", commit.stdout.strip(), head.stdout.strip()],
            worktree,
            timeout=30,
        )
    finally:
        auth.unlink(missing_ok=True)
    if update.returncode != 0:
        restore_index()
        return issue(entry, "auto_commit_failed")
    return None


def sync_changed_repo(
    repo: dict[str, str], projects_dir: Path, *, dry_run: bool,
    inventory_entry: dict[str, Any] | None = None,
    auto_commit_dirty: bool = False,
) -> tuple[str, dict[str, Any] | None]:
    worktree = Path(str(inventory_entry["worktree"])) if inventory_entry else projects_dir / repo["name"]
    entry: dict[str, Any] = {
        "project_id": None,
        "slug": repo["name"],
        "worktree": str(worktree),
        "remote_url": repo["url"],
    }
    if inventory_entry:
        entry.update(inventory_entry)
    if is_independent_canonical_writer_repo(str(repo.get("owner") or DEFAULT_OWNER), repo["name"]):
        return "external-writer", None
    if not worktree.exists():
        return clone_repo(repo, projects_dir, dry_run=dry_run, target_worktree=worktree), None
    if not git_repo_matches_remote(worktree, repo["url"]):
        return "deferred", issue(entry, "origin_mismatch")
    if github_remote_key(repo["url"]) == ROADMAP_REPOSITORY:
        return sync_roadmap_repo(repo, worktree, entry, dry_run=dry_run)
    operation = _git_operation(worktree)
    if operation:
        return "deferred", issue(entry, operation)
    head = run(["git", "rev-parse", "--verify", "HEAD"], worktree, timeout=30)
    if head.returncode != 0:
        problem = _recover_unborn_branch(worktree, repo, entry, dry_run=dry_run)
        if problem:
            return "deferred", problem
        if dry_run:
            return "would_update", None
        return "updated", None
    status = run(["git", "status", "--porcelain"], worktree, timeout=30)
    if status.returncode != 0:
        return "deferred", issue(entry, "status_failed")
    if status.stdout.strip():
        if dry_run:
            if not auto_commit_dirty:
                return "deferred", issue(entry, "dirty_worktree")
            return "would_update", None
        if not auto_commit_dirty:
            return "deferred", issue(entry, "dirty_worktree")
        commit_issue = _commit_dirty_for_reconcile(worktree, entry)
        if commit_issue is not None:
            return "deferred", commit_issue
    branch = run(["git", "branch", "--show-current"], worktree, timeout=30)
    if branch.returncode != 0 or not branch.stdout.strip():
        return "deferred", issue(entry, "detached_or_unknown_branch")
    branch_name = branch.stdout.strip()
    remote_name = _matching_remote(worktree, repo["url"])
    if remote_name is None:
        return "deferred", issue(entry, "remote_ambiguous_or_missing")
    upstream_name, tracking_error = _remote_tracking(
        worktree, remote_name=remote_name, local_branch=branch_name,
        default_branch=repo.get("default_branch") or "UNKNOWN", dry_run=dry_run,
    )
    if tracking_error:
        return "deferred", issue(entry, tracking_error)
    assert upstream_name is not None
    if dry_run:
        return "would_update", None
    remote_branch = upstream_name.split("/", 1)[1]
    counts = git_counts(worktree, f"refs/remotes/{upstream_name}")
    if counts is None:
        return "deferred", issue(entry, "relation_check_failed", f"upstream={upstream_name}")
    ahead, behind = counts
    relation = classify_relation(ahead, behind)
    if relation == "diverged":
        if not auto_commit_dirty:
            return "deferred", issue(entry, "diverged", f"ahead={ahead},behind={behind}")
        rebase = run(
            ["git", "-c", "rerere.enabled=false", "rebase", upstream_name],
            worktree,
            timeout=600,
        )
        if rebase.returncode != 0:
            abort = run(["git", "rebase", "--abort"], worktree, timeout=120)
            if abort.returncode != 0 or _git_operation(worktree):
                return "deferred", issue(entry, "rebase_abort_failed")
            return "deferred", issue(
                entry,
                "rebase_conflict",
                f"ahead={ahead},behind={behind}",
            )
        action, detail = _push_with_race_recovery(
            worktree,
            remote_name,
            remote_branch,
            allow_rebase=True,
            upstream_ref=upstream_name,
        )
        if action == "pushed":
            return "pushed", None
        if action == "synced":
            return "up_to_date", None
        if action == "remote_ahead":
            merge = run(["git", "merge", "--ff-only", upstream_name], worktree, timeout=120)
            if merge.returncode == 0:
                return "updated", None
        return "deferred", issue(entry, "push_failed", detail or "after_rebase")
    if relation == "ahead":
        action, detail = _push_with_race_recovery(
            worktree,
            remote_name,
            remote_branch,
            allow_rebase=True,
            upstream_ref=upstream_name,
        )
        if action == "pushed":
            return "pushed", None
        if action == "synced":
            return "up_to_date", None
        if action == "remote_ahead":
            merge = run(["git", "merge", "--ff-only", upstream_name], worktree, timeout=120)
            if merge.returncode != 0:
                return "deferred", issue(entry, "fast_forward_failed", detail)
            return "updated", None
        return "deferred", issue(entry, "push_failed", detail or f"ahead={ahead}")
    if relation == "behind":
        merge = run(["git", "merge", "--ff-only", upstream_name], worktree, timeout=120)
        if merge.returncode != 0:
            return "deferred", issue(entry, "fast_forward_failed", f"behind={behind}")
        return "updated", None
    return "up_to_date", None


def issue_identity(item: dict[str, Any]) -> tuple[Any, ...]:
    return (item.get("project_id"), item.get("repo"), item.get("kind"), item.get("detail"))


def dedupe_issues(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[Any, ...], dict[str, Any]] = {}
    for item in items:
        unique[issue_identity(item)] = item
    return [unique[key] for key in sorted(unique, key=lambda value: tuple(str(part) for part in value))]


def register_in_megavault(
    megavault: Path,
    projects_dir: Path,
    repos: list[dict[str, str]],
    *,
    dry_run: bool,
) -> dict[str, int | str]:
    if not repos:
        return {"already_registered": 0, "newly_registered": 0, "deferred": 0, "validation": "not_needed"}
    if dry_run:
        return {"already_registered": 0, "newly_registered": 0, "deferred": 0, "validation": "dry_run"}

    candidates: list[dict[str, str]] = []
    deferred = 0
    for repo in repos:
        worktree = projects_dir / repo["name"]
        if git_repo_matches_remote(worktree, repo["url"]):
            candidates.append(repo)
        else:
            deferred += 1
    if not candidates:
        return {
            "already_registered": 0,
            "newly_registered": 0,
            "deferred": deferred,
            "validation": "deferred_missing_worktree",
        }

    digest = hashlib.sha256(
        "\n".join(sorted(str(repo["url"]) for repo in candidates)).encode("utf-8")
    ).hexdigest()[:12]
    task_id = f"autosync-register-{digest}"
    try:
        task = repo_single_writer.start_task(megavault, task_id, "github-autosync")
    except Exception as exc:
        return {
            "already_registered": 0,
            "newly_registered": 0,
            "deferred": len(candidates) + deferred,
            "validation": f"deferred_writer:{type(exc).__name__}",
        }

    task_repo = Path(str(task["worktree"]))
    already = created = 0
    for repo in candidates:
        worktree = projects_dir / repo["name"]
        result = run(
            [
                "python3",
                str(task_repo / "megavault.py"),
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
            cwd=task_repo,
            timeout=60,
        )
        if result.returncode != 0:
            raise AutosyncError("megavault_register_failed")
        if "status=created" in result.stdout:
            created += 1
        else:
            already += 1

    validation = run(
        ["python3", str(task_repo / "megavault.py"), "validate"],
        cwd=task_repo,
        timeout=120,
    )
    require_ok(validation, "megavault_validate_failed")
    if created:
        try:
            ready = repo_single_writer.finish_task(megavault, task_id)
        except Exception as exc:
            return {
                "already_registered": already,
                "newly_registered": created,
                "deferred": deferred,
                "validation": f"deferred_writer_finish:{type(exc).__name__}",
            }
        return {
            "already_registered": already,
            "newly_registered": created,
            "deferred": deferred,
            "validation": "queued_single_writer",
            "commit_changed": "False",
            "pr": str(ready.get("pr_number") or ""),
        }
    return {
        "already_registered": already,
        "newly_registered": 0,
        "deferred": deferred,
        "validation": "PASS",
        "commit_changed": "False",
    }

def _status_for(issues: list[dict[str, Any]], work_done: int) -> str:
    if not issues:
        return "ok"
    return "partial" if work_done else "deferred"


def queue_merged_roadmap_completions() -> dict[str, Any]:
    """Queue terminal PASS only after the repository single writer has merged the task."""
    pending = repo_single_writer.pending_roadmap_completions()
    result: dict[str, Any] = {"queued": 0, "deferred": 0, "prompt_ids": [], "failed": []}
    for task in pending:
        prompt_id = str(task.get("task_id") or "")
        if not ROADMAP_RESULT_SCRIPT.is_file():
            result["deferred"] += 1
            result["failed"].append(prompt_id)
            continue
        proc = run(
            [
                "python3",
                str(ROADMAP_RESULT_SCRIPT),
                "--repo",
                str(ROADMAP_RESULT_SCRIPT.parents[1]),
                "--prompt-id",
                prompt_id,
                "--result",
                "PASS",
                "--confirm-executed",
            ],
            timeout=120,
        )
        if proc.returncode:
            result["deferred"] += 1
            result["failed"].append(prompt_id)
            continue
        repo_single_writer.mark_roadmap_completion_queued(prompt_id)
        result["queued"] += 1
        result["prompt_ids"].append(prompt_id)
    return result


SYSTEMD_RUNTIME_UNITS = (
    "github-autosync.service",
    "github-autosync.timer",
    "repo-integrator.service",
    "repo-integrator.timer",
    "github-autosync-watchdog.service",
    "github-autosync-watchdog.timer",
)


def _systemd_runtime_needs_refresh(target_root: Path | None = None) -> bool:
    source_root = Path(__file__).resolve().with_name("systemd")
    target_root = target_root or (Path.home() / ".config" / "systemd" / "user")
    for name in SYSTEMD_RUNTIME_UNITS:
        source = source_root / name
        target = target_root / name
        if not source.is_file() or not target.is_file():
            return True
        try:
            if source.read_bytes() != target.read_bytes():
                return True
        except OSError:
            return True
    return False


def bootstrap_repo_integrator_runtime() -> None:
    """Keep the Fedora user-systemd runtime aligned with the canonical checkout."""
    if Path.home() != Path("/home/daniele"):
        return
    probe = run(["systemctl", "--user", "is-enabled", "repo-integrator.timer"], timeout=30)
    if probe.returncode == 0 and not _systemd_runtime_needs_refresh():
        return
    installer = Path(__file__).resolve().with_name("install_systemd.py")
    if not installer.is_file():
        raise AutosyncError("systemd_runtime_installer_missing")
    installed = run(["python3", str(installer)], timeout=120)
    if installed.returncode:
        raise AutosyncError("systemd_runtime_bootstrap_failed")


def command_run(args: argparse.Namespace) -> int:
    if not args.dry_run:
        bootstrap_repo_integrator_runtime()
    projects_dir = Path(args.projects_dir).expanduser()
    full_reconcile = bool(getattr(args, "full_reconcile", False))
    auto_commit_dirty = bool(getattr(args, "auto_commit_dirty", False))
    state_dir = Path(args.state_dir).expanduser()
    megavault = Path(args.megavault).expanduser()
    activity_data_enabled = not args.no_data_mirror and not args.dry_run
    issues: list[dict[str, Any]] = []
    registration: dict[str, int | str] = {"validation": "not_run"}
    counts = {
        "cloned": 0,
        "updated": 0,
        "pushed": 0,
        "deployed": 0,
        "audited_unchanged": 0,
        "skipped_unchanged": 0,
        "deferred": 0,
    }
    auto_pushed = 0
    activity_events = 0
    roadmap_finalization: dict[str, Any] = {"queued": 0, "deferred": 0, "prompt_ids": [], "failed": []}
    try:
        with ExclusiveLock(state_dir / "autosync.lock"):
            # Repository integration is owned by repo-integrator.service.
            writer = {"found": 0, "merged": 0, "deferred": 0, "results": []}
            raw_inventory = megavault_inventory(megavault)
            inventory = raw_inventory if full_reconcile else filter_allowed_inventory(raw_inventory)
            try:
                discovered_repos = github_repos(args.owner)
            except AutosyncError:
                if not full_reconcile:
                    raise
                issues.append(issue(None, "github_discovery_failed"))
                discovered_repos = [
                    {"name": str(entry["slug"]), "url": str(entry["remote_url"]),
                     "default_branch": str(entry.get("branch") or "UNKNOWN"),
                     "pushed_at": "", "archived": "0"}
                    for entry in inventory
                ]
            if full_reconcile:
                inventory_remotes = {
                    normalize_remote(str(entry["remote_url"]))
                    for entry in inventory
                }
                selected = [
                    repo
                    for repo in discovered_repos
                    if repo.get("archived") != "1"
                    and (
                        normalize_remote(repo["url"]) in inventory_remotes
                        or (projects_dir / repo["name"]).exists()
                        or is_allowed_repo(args.owner, repo["name"])
                    )
                ]
            else:
                selected = filter_allowed_repos(args.owner, discovered_repos)
            repos = [{**repo, "owner": args.owner} for repo in selected]
            inventory_by_remote: dict[str, dict[str, Any]] = {}
            for entry in inventory:
                key = normalize_remote(str(entry["remote_url"]))
                current = inventory_by_remote.get(key)
                if current is None or (current.get("project_id") is None and entry.get("project_id") is not None):
                    inventory_by_remote[key] = entry
            if full_reconcile and not args.dry_run:
                for repo in repos:
                    inventory_entry = inventory_by_remote.get(normalize_remote(repo["url"]))
                    worktree = Path(str(inventory_entry["worktree"])) if inventory_entry else projects_dir / repo["name"]
                    if (
                        not worktree.exists()
                        or allowed_repo_key(args.owner, repo["name"]).lower() == ROADMAP_REPOSITORY.lower()
                        or not git_repo_matches_remote(worktree, repo["url"])
                    ):
                        continue
                    try:
                        if is_independent_canonical_writer_repo(args.owner, repo["name"]):
                            repo_single_writer.remove_guard(worktree)
                            continue
                        repo_single_writer.ensure_guard(
                            worktree,
                            repo.get("default_branch") if repo.get("default_branch") not in {None, "", "UNKNOWN"} else None,
                        )
                    except Exception as exc:
                        issues.append(
                            issue(
                                inventory_entry or {
                                    "project_id": None,
                                    "slug": repo["name"],
                                    "worktree": str(worktree),
                                    "remote_url": repo["url"],
                                },
                                "writer_guard_failed",
                                type(exc).__name__,
                            )
                        )
            github_remotes = {normalize_remote(repo["url"]) for repo in repos}
            local_issues, auto_pushed = audit_inventory(
                [entry for key, entry in inventory_by_remote.items() if key not in github_remotes],
                auto_push=not args.dry_run,
                report_behind=False,
                fetch_remote=False,
            )
            issues.extend(x for x in local_issues if x["kind"] != "missing_worktree")

            old_state = load_repo_state(state_dir)
            next_state = dict(old_state)
            deployed_state = load_runtime_deploy_state(state_dir)
            next_deployed_state = dict(deployed_state)
            for repo in repos:
                fingerprint = repo_fingerprint(repo)
                previous = old_state.get(repo["name"])
                inventory_entry = inventory_by_remote.get(normalize_remote(repo["url"]))
                worktree = Path(str(inventory_entry["worktree"])) if inventory_entry else projects_dir / repo["name"]
                local_dirty = False
                if not full_reconcile and auto_commit_dirty and previous == fingerprint and worktree.exists():
                    local_status = run(["git", "status", "--porcelain"], worktree, timeout=30)
                    local_dirty = local_status.returncode == 0 and bool(local_status.stdout.strip())
                if is_independent_canonical_writer_repo(args.owner, repo["name"]):
                    counts["skipped_unchanged"] += 1
                    if not args.dry_run:
                        next_state[repo["name"]] = fingerprint
                    continue
                if (
                    not full_reconcile
                    and previous == fingerprint
                    and worktree.exists()
                    and not local_dirty
                    and github_remote_key(repo["url"]) != ROADMAP_REPOSITORY
                ):
                    if args.dry_run:
                        counts["skipped_unchanged"] += 1
                        continue
                    local_entry = inventory_entry or {
                        "project_id": None,
                        "slug": repo["name"],
                        "worktree": str(worktree),
                        "remote_url": repo["url"],
                        "branch": repo.get("default_branch") or "UNKNOWN",
                    }
                    repo_issues, did_push = audit_worktree(
                        local_entry,
                        auto_push=True,
                        report_behind=False,
                        fetch_remote=False,
                    )
                    counts["audited_unchanged"] += 1
                    auto_pushed += int(did_push)
                    if did_push:
                        append_activity(
                            state_dir,
                            action="push",
                            repo=allowed_repo_key(args.owner, repo["name"]),
                            branch=str(local_entry.get("branch") or repo.get("default_branch") or "UNKNOWN"),
                            project_id=local_entry.get("project_id"),
                            worktree=str(worktree),
                            detail=(
                            "full reconcile push"
                            if full_reconcile
                            else "clean ahead-only checkout"
                        ),
                        )
                        activity_events += 1
                    if repo_issues:
                        issues.extend(repo_issues)
                        counts["deferred"] += 1
                        continue
                    if not did_push:
                        counts["skipped_unchanged"] += 1

                    repo_key = allowed_repo_key(args.owner, repo["name"])
                    deploy_result, deploy_detail = deploy_runtime_if_needed(
                        repo_key,
                        worktree,
                        next_deployed_state,
                        dry_run=args.dry_run,
                    )
                    if deploy_result == "failed":
                        issues.append(
                            issue(
                                local_entry,
                                "runtime_deploy_failed",
                                deploy_detail,
                            )
                        )
                        counts["deferred"] += 1
                        continue
                    if deploy_result == "deployed":
                        counts["deployed"] += 1
                        append_activity(
                            state_dir,
                            action="deploy",
                            repo=repo_key,
                            branch=str(local_entry.get("branch") or repo.get("default_branch") or "UNKNOWN"),
                            project_id=local_entry.get("project_id"),
                            worktree=str(worktree),
                            detail=f"runtime deployed at {deploy_detail[:12]}",
                        )
                        activity_events += 1
                    continue
                try:
                    result, repo_issue = sync_changed_repo(
                        repo,
                        projects_dir,
                        dry_run=args.dry_run,
                        inventory_entry=inventory_entry,
                        auto_commit_dirty=auto_commit_dirty,
                    )
                except Exception as exc:
                    if not full_reconcile:
                        raise
                    error_entry = inventory_entry or {
                        "project_id": None,
                        "slug": repo["name"],
                        "worktree": str(worktree),
                        "remote_url": repo["url"],
                        "branch": repo.get("default_branch") or "UNKNOWN",
                    }
                    repo_issue = issue(
                        error_entry,
                        "reconcile_error",
                        f"{type(exc).__name__}:{exc}",
                    )
                    result = "deferred"
                if repo_issue is not None:
                    issues.append(repo_issue)
                    counts["deferred"] += 1
                    continue
                if result in {"cloned", "would_clone"}:
                    counts["cloned"] += 1
                elif result in {"updated", "would_update"}:
                    counts["updated"] += 1
                elif result == "pushed":
                    counts["pushed"] += 1
                if result == "cloned":
                    append_activity(
                        state_dir,
                        action="clone",
                        repo=allowed_repo_key(args.owner, repo["name"]),
                        branch=repo.get("default_branch") or None,
                        project_id=inventory_entry.get("project_id") if inventory_entry else None,
                        worktree=str(worktree),
                        detail="managed repository cloned",
                    )
                    activity_events += 1
                elif result == "updated":
                    append_activity(
                        state_dir,
                        action="pull",
                        repo=allowed_repo_key(args.owner, repo["name"]),
                        branch=repo.get("default_branch") or None,
                        project_id=inventory_entry.get("project_id") if inventory_entry else None,
                        worktree=str(worktree),
                        detail="fast-forward only",
                    )
                    activity_events += 1
                elif result == "pushed":
                    append_activity(
                        state_dir,
                        action="push",
                        repo=allowed_repo_key(args.owner, repo["name"]),
                        branch=repo.get("default_branch") or None,
                        project_id=inventory_entry.get("project_id") if inventory_entry else None,
                        worktree=str(worktree),
                        detail=(
                            "full reconcile push"
                            if full_reconcile
                            else "clean ahead-only checkout"
                        ),
                    )
                    activity_events += 1

                repo_key = allowed_repo_key(args.owner, repo["name"])
                deploy_entry = inventory_entry or {
                    "project_id": None,
                    "slug": repo["name"],
                    "worktree": str(worktree),
                    "remote_url": repo["url"],
                    "branch": repo.get("default_branch") or "UNKNOWN",
                }
                deploy_result, deploy_detail = deploy_runtime_if_needed(
                    repo_key,
                    worktree,
                    next_deployed_state,
                    dry_run=args.dry_run,
                )
                if deploy_result == "failed":
                    issues.append(
                        issue(
                            deploy_entry,
                            "runtime_deploy_failed",
                            deploy_detail,
                        )
                    )
                    counts["deferred"] += 1
                elif deploy_result == "deployed":
                    counts["deployed"] += 1
                    append_activity(
                        state_dir,
                        action="deploy",
                        repo=repo_key,
                        branch=repo.get("default_branch") or None,
                        project_id=inventory_entry.get("project_id") if inventory_entry else None,
                        worktree=str(worktree),
                        detail=f"runtime deployed at {deploy_detail[:12]}",
                    )
                    activity_events += 1

                if not args.dry_run:
                    next_state[repo["name"]] = fingerprint

            if not args.dry_run:
                live_names = {repo["name"] for repo in repos}
                next_state = {name: value for name, value in next_state.items() if name in live_names}
                save_repo_state(state_dir, next_state)
                live_repo_keys = {allowed_repo_key(args.owner, repo["name"]) for repo in repos}
                next_deployed_state = {
                    key: value
                    for key, value in next_deployed_state.items()
                    if key in live_repo_keys
                }
                save_runtime_deploy_state(state_dir, next_deployed_state)

            registered = megavault_registered_remotes(megavault)
            missing_reg = [repo for repo in repos if normalize_remote(repo["url"]) not in registered]
            registration = register_in_megavault(
                megavault,
                projects_dir,
                missing_reg,
                dry_run=args.dry_run,
            )
            if str(registration.get("commit_changed") or "").lower() == "true":
                append_activity(
                    state_dir,
                    action="push",
                    repo=allowed_repo_key(args.owner, "MegaVault"),
                    branch=None,
                    project_id=None,
                    worktree=str(megavault),
                    detail="registered autosynced repositories in MegaVault",
                )
                activity_events += 1
            validation = str(registration.get("validation"))
            if validation.startswith("deferred_"):
                issues.append(issue(None, f"megavault_{validation}"))
            deferred_reg = int(registration.get("deferred") or 0)
            if deferred_reg:
                issues.append(issue(None, "megavault_registration_deferred", f"count={deferred_reg}"))

            activity_data = mirror_pending_activity(state_dir, enabled=activity_data_enabled)
    except BlockingIOError:
        raise
    except AutosyncError:
        raise
    except Exception as exc:
        raise AutosyncError(f"unexpected_{type(exc).__name__}") from exc

    issues = dedupe_issues(issues)
    work_done = (
        counts["cloned"]
        + counts["updated"]
        + counts["pushed"]
        + counts["deployed"]
        + auto_pushed
    )
    payload = {
        "status": _status_for(issues, work_done),
        "dry_run": bool(args.dry_run),
        "owner": args.owner,
        "discovered": len(repos),
        "managed_repos": [repo["name"] for repo in repos],
        "auto_pushed": auto_pushed,
        "issues": len(issues),
        "reconcile_issues": [
            {
                "repo": item.get("repo"),
                "kind": item.get("kind"),
                "detail": item.get("detail"),
            }
            for item in issues
        ] if full_reconcile else [],
        "activity_events": activity_events,
        "activity_log": str(state_dir / ACTIVITY_LOG_FILE),
        "activity_data": activity_data,
        "single_writer": writer,
        "roadmap_finalization": roadmap_finalization,
        **counts,
        "megavault": registration,
    }
    if not args.dry_run:
        if not _push_kuma_heartbeat(not issues, len(repos), issues):
            issues.append(issue(None, "heartbeat_failed"))
            payload["issues"] = len(issues)
            payload["status"] = _status_for(issues, work_done)
            payload["reconcile_issues"].append({"repo": "autosync", "kind": "heartbeat_failed", "detail": ""})
    if getattr(args, "human_output", False):
        _print_human_summary(payload)
    else:
        print(json.dumps(payload, sort_keys=True))
    heartbeat_failed = any(item.get("kind") == "heartbeat_failed" for item in issues)
    return 2 if issues and (full_reconcile or heartbeat_failed) else 0


def _push_kuma_status(status: str, message: str) -> bool:
    url = os.environ.get("GITHUB_RECONCILE_PUSH_URL", "").strip()
    if not url:
        return False
    separator = "&" if "?" in url else "?"
    target = url + separator + urlencode({"status": status, "msg": message})
    try:
        request = Request(target, headers={"User-Agent": "github-autosync/kuma-heartbeat"}, method="GET")
        with urlopen(request, timeout=10) as response:
            return response.status == 200 and json.loads(response.read(4096)).get("ok") is True
    except Exception:
        return False


def _push_kuma_heartbeat(healthy: bool, total: int, issues: list[dict[str, Any]]) -> bool:
    message = f"{total} repository sincronizzati" if healthy else f"{len(issues)} repository richiedono attenzione"
    return _push_kuma_status("up" if healthy else "down", message)


def _print_human_summary(payload: dict[str, Any]) -> None:
    total = len(payload["managed_repos"])
    trouble = payload["issues"]
    if payload.get("dry_run"):
        print(f"GitHub reconcile: verifica di {total} repo completata, {trouble} problemi rilevati.")
        for item in payload["reconcile_issues"][:3]:
            print(f"Problema: {item['repo']} — {_human_issue(item['kind'])}.")
        return
    print(f"GitHub reconcile: {total} repo — {max(0, total - trouble)} sincronizzati, {trouble} richiedono attenzione.")
    if payload["updated"]:
        print(f"Aggiornati dal remoto: {payload['updated']}")
    if payload["pushed"] + payload["auto_pushed"]:
        print(f"Inviati a GitHub: {payload['pushed'] + payload['auto_pushed']}")
    if payload.get("deployed"):
        print(f"Runtime aggiornati: {payload['deployed']}")
    for item in payload["reconcile_issues"][:3]:
        print(f"Problema: {item['repo']} — {_human_issue(item['kind'])}.")


def _human_issue(kind: str) -> str:
    if kind in {"remote_branch_missing", "remote_probe_failed", "upstream_repair_failed", "tracking_ref_missing_after_fetch", "refspec_repair_failed", "relation_check_failed"}:
        return "collegamento al branch remoto da riparare"
    if kind in {"unresolved_conflicts", "rebase_conflict", "rebase_abort_failed"} or kind.startswith("single_writer_"):
        return "integrazione del task da verificare"
    if kind in {"worktree_changing", "git_lock_present", "merge_in_progress", "rebase_in_progress", "cherry_pick_in_progress", "revert_in_progress", "sequencer_in_progress"}:
        return "modifiche locali o operazione Git in corso"
    return "sincronizzazione da verificare"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Safely synchronize gernalix GitHub repositories with change fingerprints.")
    parser.add_argument("--owner", default=DEFAULT_OWNER)
    parser.add_argument("--projects-dir", default=str(DEFAULT_PROJECTS_DIR))
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    parser.add_argument("--megavault", default=str(DEFAULT_MEGAVAULT))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-telegram", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--no-data-mirror", action="store_true", help="Disable the private github-autosync-data mirror")
    parser.add_argument("--json", action="store_true", help="Emit full machine-readable result")
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run")
    run_p.set_defaults(func=command_run, full_reconcile=False, auto_commit_dirty=True)
    reconcile_p = sub.add_parser(
        "reconcile-all",
        help="Force a network reconcile of every managed repo and checkpoint generic dirty worktrees.",
    )
    reconcile_p.set_defaults(func=command_run, full_reconcile=True, auto_commit_dirty=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    args.human_output = args.full_reconcile and not args.json
    try:
        return int(args.func(args))
    except BlockingIOError:
        if not args.dry_run:
            _push_kuma_status("up", "reconcile gia in corso")
        print(json.dumps({"status": "locked"}, sort_keys=True) if args.json else "GitHub reconcile: un'altra esecuzione è già in corso.")
        return 0
    except AutosyncError as exc:
        if not args.dry_run:
            _push_kuma_status("down", f"errore fatale: {str(exc)[:160]}")
        message = (
            json.dumps({"status": "error", "error": str(exc)}, sort_keys=True)
            if args.json
            else f"GitHub reconcile: controllo non completato ({str(exc)})."
        )
        print(message, file=sys.stderr)
        return 75
