#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Any

DEFAULT_OWNER = "gernalix"
DEFAULT_PROJECTS_DIR = Path.home() / "projects"
DEFAULT_STATE_DIR = Path.home() / ".local/state/codex-github-autosync"
DEFAULT_MEGAVAULT = Path.home() / "MegaVault"
ALERT_STATE_FILE = "telegram-alert-state.json"
REPO_STATE_FILE = "repo-state.json"
ALLOWED_REPOSITORIES = frozenset(
    {
        "gernalix/codex-roadmap",
        "gernalix/vm_oracle",
        "gernalix/MegaVault",
        "gernalix/fedora-system-monitor",
        "gernalix/codex-usage",
        "gernalix/github-autosync",
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


def github_repos(owner: str) -> list[dict[str, str]]:
    """One metadata discovery command; no per-repo network probes."""
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
        raise AutosyncError("github_repo_list_failed")
    repos: list[dict[str, str]] = []
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AutosyncError("github_repo_list_invalid_json") from exc
    for item in payload:
        branch = item.get("defaultBranchRef") or {}
        repos.append(
            {
                "name": str(item["name"]),
                "url": str(item["url"]),
                "default_branch": str(branch.get("name") or "UNKNOWN"),
                "pushed_at": str(item.get("pushedAt") or "UNKNOWN"),
                "archived": "1" if item.get("isArchived") else "0",
            }
        )
    return sorted(repos, key=lambda row: row["name"])


def allowed_repo_key(owner: str, name: str) -> str:
    return f"{owner}/{name}"


def is_allowed_repo(owner: str, name: str) -> bool:
    return allowed_repo_key(owner, name) in ALLOWED_REPOSITORIES


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


def git_counts(worktree: Path) -> tuple[int, int] | None:
    result = run(["git", "rev-list", "--left-right", "--count", "HEAD...@{u}"], worktree, timeout=30)
    if result.returncode != 0:
        return None
    parts = result.stdout.split()
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return None


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
        return [issue(entry, "status_failed")], False
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
        push = run(["git", "push", remote_name, f"HEAD:{remote_branch}"], worktree, timeout=240)
        if push.returncode != 0:
            return [issue(entry, "push_failed", f"ahead={ahead}")], False
        if fetch_remote:
            verify_fetch = run(["git", "fetch", "--prune", remote_name], worktree, timeout=120)
            if verify_fetch.returncode != 0:
                return [issue(entry, "post_push_fetch_failed")], True
            verify = git_counts(worktree)
            if verify != (0, 0):
                return [issue(entry, "post_push_verify_failed", f"counts={verify}")], True
        return [], True
    if behind and report_behind:
        return [issue(entry, "still_behind_remote", f"behind={behind}")], False
    return [], False


def audit_inventory(
    inventory: list[dict[str, Any]], *, auto_push: bool, report_behind: bool, fetch_remote: bool = True,
) -> tuple[list[dict[str, Any]], int]:
    issues: list[dict[str, Any]] = []
    pushed = 0
    for entry in inventory:
        repo_issues, did_push = audit_worktree(
            entry,
            auto_push=auto_push,
            report_behind=report_behind,
            fetch_remote=fetch_remote,
        )
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


def sync_changed_repo(
    repo: dict[str, str], projects_dir: Path, *, dry_run: bool,
    inventory_entry: dict[str, Any] | None = None,
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
    if not worktree.exists():
        return clone_repo(repo, projects_dir, dry_run=dry_run, target_worktree=worktree), None
    if not git_repo_matches_remote(worktree, repo["url"]):
        return "deferred", issue(entry, "origin_mismatch")
    status = run(["git", "status", "--porcelain"], worktree, timeout=30)
    if status.returncode != 0:
        return "deferred", issue(entry, "status_failed")
    if status.stdout.strip():
        return "deferred", issue(entry, "dirty_worktree")
    branch = run(["git", "branch", "--show-current"], worktree, timeout=30)
    if branch.returncode != 0 or not branch.stdout.strip():
        return "deferred", issue(entry, "detached_or_unknown_branch")
    upstream = run(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], worktree, timeout=30)
    if upstream.returncode != 0 or "/" not in upstream.stdout.strip():
        branch_name = branch.stdout.strip()
        remote_branch_ref = f"refs/remotes/origin/{branch_name}"
        if dry_run:
            exists = run(["git", "show-ref", "--verify", "--quiet", remote_branch_ref], worktree, timeout=30)
            if exists.returncode != 0:
                remote_probe = run(
                    ["git", "ls-remote", "--exit-code", "--heads", "origin", f"refs/heads/{branch_name}"],
                    worktree,
                    timeout=120,
                )
                if remote_probe.returncode != 0:
                    return "deferred", issue(entry, "no_upstream")
            return "would_update", None
        fetch = run(["git", "fetch", "--prune", "origin"], worktree, timeout=120)
        if fetch.returncode != 0:
            return "deferred", issue(entry, "fetch_failed")
        fetched_remote = "origin"
        exists = run(["git", "show-ref", "--verify", "--quiet", remote_branch_ref], worktree, timeout=30)
        if exists.returncode != 0:
            return "deferred", issue(entry, "no_upstream")
        tracking = run(["git", "branch", f"--set-upstream-to=origin/{branch_name}", branch_name], worktree, timeout=30)
        if tracking.returncode != 0:
            return "deferred", issue(entry, "no_upstream")
        upstream_name = f"origin/{branch_name}"
    else:
        upstream_name = upstream.stdout.strip()
        fetched_remote = None
    if dry_run:
        return "would_update", None
    remote_name, remote_branch = upstream_name.split("/", 1)
    if fetched_remote != remote_name:
        fetch = run(["git", "fetch", "--prune", remote_name], worktree, timeout=120)
        if fetch.returncode != 0:
            raise AutosyncError(f"fetch_failed:{repo['name']}")
    counts = git_counts(worktree)
    if counts is None:
        raise AutosyncError(f"relation_check_failed:{repo['name']}")
    ahead, behind = counts
    if ahead and behind:
        return "deferred", issue(entry, "diverged", f"ahead={ahead},behind={behind}")
    if ahead:
        push = run(["git", "push", remote_name, f"HEAD:{remote_branch}"], worktree, timeout=240)
        if push.returncode != 0:
            return "deferred", issue(entry, "push_failed", f"ahead={ahead}")
        return "pushed", None
    if behind:
        merge = run(["git", "merge", "--ff-only", "@{u}"], worktree, timeout=120)
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


def format_issue_message(items: list[dict[str, Any]]) -> str:
    lines = [f"Rilevati {len(items)} intoppi nell'autosync GitHub:"]
    for item in items[:20]:
        project = f"project_id={item['project_id']}" if item.get("project_id") is not None else "sistema"
        detail = f" ({item['detail']})" if item.get("detail") else ""
        lines.append(f"- {item['repo']} [{project}]: {item['kind']}{detail}")
    if len(items) > 20:
        lines.append(f"- ... e altri {len(items) - 20}")
    return "\n".join(lines)


def send_telegram(title: str, message: str) -> bool:
    result = run([sys.executable, "-m", "telegram_notify", title, message], timeout=60)
    return result.returncode == 0


def update_telegram_alert_state(state_dir: Path, items: list[dict[str, Any]], *, enabled: bool) -> str:
    if not enabled:
        return "disabled"
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return "state_failed"
    path = state_dir / ALERT_STATE_FILE
    current = dedupe_issues(items)
    current_fingerprint = json.dumps(
        [issue_identity(item) for item in current], sort_keys=True, separators=(",", ":")
    )
    previous: list[dict[str, Any]] = []
    previous_fingerprint: str | None = None
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("issues"), list):
                previous = dedupe_issues([item for item in data["issues"] if isinstance(item, dict)])
                previous_fingerprint = str(data.get("fingerprint") or "") or None
        except (OSError, json.JSONDecodeError):
            return "state_failed"
    if previous_fingerprint is None:
        previous_fingerprint = json.dumps(
            [issue_identity(item) for item in previous], sort_keys=True, separators=(",", ":")
        )
    if current_fingerprint == previous_fingerprint:
        return "unchanged"
    if current:
        sent = send_telegram("GitHub autosync: attenzione", format_issue_message(current))
    elif previous:
        sent = send_telegram("GitHub autosync: risolto", "Tutti gli intoppi precedentemente rilevati risultano risolti.")
    else:
        sent = True
    if not sent:
        return "notify_failed"
    try:
        path.write_text(
            json.dumps({"fingerprint": current_fingerprint, "issues": current}, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        return "state_failed"
    return "alert_sent" if current else "resolved_sent"


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


def _status_for(issues: list[dict[str, Any]], work_done: int) -> str:
    if not issues:
        return "ok"
    return "partial" if work_done else "deferred"


def command_run(args: argparse.Namespace) -> int:
    projects_dir = Path(args.projects_dir).expanduser()
    state_dir = Path(args.state_dir).expanduser()
    megavault = Path(args.megavault).expanduser()
    telegram_enabled = not args.no_telegram and not args.dry_run
    issues: list[dict[str, Any]] = []
    registration: dict[str, int | str] = {"validation": "not_run"}
    counts = {
        "cloned": 0,
        "updated": 0,
        "pushed": 0,
        "audited_unchanged": 0,
        "skipped_unchanged": 0,
        "deferred": 0,
    }
    auto_pushed = 0
    try:
        with ExclusiveLock(state_dir / "autosync.lock"):
            inventory = filter_allowed_inventory(megavault_inventory(megavault))
            repos = [{**repo, "owner": args.owner} for repo in filter_allowed_repos(args.owner, github_repos(args.owner))]
            inventory_by_remote: dict[str, dict[str, Any]] = {}
            for entry in inventory:
                key = normalize_remote(str(entry["remote_url"]))
                current = inventory_by_remote.get(key)
                if current is None or (current.get("project_id") is None and entry.get("project_id") is not None):
                    inventory_by_remote[key] = entry
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
            for repo in repos:
                fingerprint = repo_fingerprint(repo)
                previous = old_state.get(repo["name"])
                inventory_entry = inventory_by_remote.get(normalize_remote(repo["url"]))
                worktree = Path(str(inventory_entry["worktree"])) if inventory_entry else projects_dir / repo["name"]
                if previous == fingerprint and worktree.exists():
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
                    if repo_issues:
                        issues.extend(repo_issues)
                        counts["deferred"] += 1
                    elif not did_push:
                        counts["skipped_unchanged"] += 1
                    continue
                result, repo_issue = sync_changed_repo(
                    repo, projects_dir, dry_run=args.dry_run, inventory_entry=inventory_entry
                )
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
                if not args.dry_run:
                    next_state[repo["name"]] = fingerprint

            if not args.dry_run:
                live_names = {repo["name"] for repo in repos}
                next_state = {name: value for name, value in next_state.items() if name in live_names}
                save_repo_state(state_dir, next_state)

            registered = megavault_registered_remotes(megavault)
            missing_reg = [repo for repo in repos if normalize_remote(repo["url"]) not in registered]
            registration = register_in_megavault(
                megavault,
                projects_dir,
                missing_reg,
                dry_run=args.dry_run,
            )
            validation = str(registration.get("validation"))
            if validation.startswith("deferred_"):
                issues.append(issue(None, f"megavault_{validation}"))
            deferred_reg = int(registration.get("deferred") or 0)
            if deferred_reg:
                issues.append(issue(None, "megavault_registration_deferred", f"count={deferred_reg}"))
    except BlockingIOError:
        raise
    except AutosyncError:
        raise
    except Exception as exc:
        raise AutosyncError(f"unexpected_{type(exc).__name__}") from exc

    issues = dedupe_issues(issues)
    notify = update_telegram_alert_state(state_dir, issues, enabled=telegram_enabled)
    if notify in {"notify_failed", "state_failed"}:
        raise AutosyncError(f"telegram_{notify}")
    work_done = counts["cloned"] + counts["updated"] + counts["pushed"] + auto_pushed
    payload = {
        "status": _status_for(issues, work_done),
        "owner": args.owner,
        "discovered": len(repos),
        "managed_repos": [repo["name"] for repo in repos],
        "auto_pushed": auto_pushed,
        "issues": len(issues),
        "telegram": notify,
        **counts,
        "megavault": registration,
    }
    print(json.dumps(payload, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Safely synchronize gernalix GitHub repositories with change fingerprints.")
    parser.add_argument("--owner", default=DEFAULT_OWNER)
    parser.add_argument("--projects-dir", default=str(DEFAULT_PROJECTS_DIR))
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    parser.add_argument("--megavault", default=str(DEFAULT_MEGAVAULT))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-telegram", action="store_true", help="Disable Telegram issue/resolution notifications")
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
