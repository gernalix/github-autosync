#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3
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
ALERT_STATE_FILE = "telegram-alert-state.json"


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
        ["gh", "repo", "list", owner, "--limit", "1000", "--json", "name,url,defaultBranchRef"],
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
        str(GHORG), "clone", owner,
        "--scm=github", "--clone-type=user", "--github-user-option=owner",
        "--path", str(projects_dir), "--output-dir", ".", "--protocol=https",
        "--protect-local", "--fetch-all", "--fetch-prune", "--no-clean", "--no-dir-size",
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


def audit_worktree(
    entry: dict[str, Any], *, auto_push: bool, report_behind: bool,
) -> tuple[list[dict[str, Any]], bool]:
    worktree = Path(str(entry["worktree"])).expanduser()
    if not worktree.exists():
        return [issue(entry, "missing_worktree")], False
    probe = run(["git", "rev-parse", "--is-inside-work-tree"], worktree, timeout=30)
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        return [issue(entry, "not_git_worktree")], False

    remote = run(["git", "config", "--get", "remote.origin.url"], worktree, timeout=30)
    if remote.returncode != 0 or not remote.stdout.strip():
        return [issue(entry, "origin_missing")], False
    if normalize_remote(remote.stdout) != normalize_remote(str(entry["remote_url"])):
        return [issue(entry, "origin_mismatch")], False

    status = run(["git", "status", "--porcelain"], worktree, timeout=30)
    if status.returncode != 0:
        return [issue(entry, "status_failed")], False
    dirty = bool(status.stdout.strip())

    branch = run(["git", "branch", "--show-current"], worktree, timeout=30)
    if branch.returncode != 0 or not branch.stdout.strip():
        return [issue(entry, "detached_or_unknown_branch")], False

    upstream = run(["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], worktree, timeout=30)
    if upstream.returncode != 0 or "/" not in upstream.stdout.strip():
        return [issue(entry, "no_upstream")], False
    upstream_name = upstream.stdout.strip()
    remote_name, remote_branch = upstream_name.split("/", 1)

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
        # No --force: a remote race after the fetch is rejected by Git.
        push = run(["git", "push", remote_name, f"HEAD:{remote_branch}"], worktree, timeout=240)
        if push.returncode != 0:
            return [issue(entry, "push_failed", f"ahead={ahead}")], False
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
    inventory: list[dict[str, Any]], *, auto_push: bool, report_behind: bool,
) -> tuple[list[dict[str, Any]], int]:
    issues: list[dict[str, Any]] = []
    pushed = 0
    for entry in inventory:
        repo_issues, did_push = audit_worktree(entry, auto_push=auto_push, report_behind=report_behind)
        issues.extend(repo_issues)
        pushed += int(did_push)
    return issues, pushed


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


def update_telegram_alert_state(
    state_dir: Path, items: list[dict[str, Any]], *, enabled: bool,
) -> str:
    if not enabled:
        return "disabled"
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return "state_failed"
    path = state_dir / ALERT_STATE_FILE
    current = dedupe_issues(items)
    previous: list[dict[str, Any]] = []
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("issues"), list):
                previous = dedupe_issues([item for item in data["issues"] if isinstance(item, dict)])
        except (OSError, json.JSONDecodeError):
            return "state_failed"

    if [issue_identity(x) for x in current] == [issue_identity(x) for x in previous]:
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
        path.write_text(json.dumps({"issues": current}, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return "state_failed"
    return "alert_sent" if current else "resolved_sent"


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
                "python3", str(megavault / "megavault.py"), "register-github-repo",
                "--owner", repo["owner"], "--name", repo["name"],
                "--remote-url", repo["url"], "--default-branch", repo["default_branch"],
                "--worktree", str(worktree),
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
    telegram_enabled = not args.no_telegram and not args.dry_run
    issues: list[dict[str, Any]] = []
    auto_pushed = 0
    ghorg_result: subprocess.CompletedProcess[str] | None = None
    registration: dict[str, int | str] = {"validation": "not_run"}
    summary = {"cloned": 0, "updated": 0, "protected_skipped": 0, "errors": 0}

    try:
        with ExclusiveLock(state_dir / "autosync.lock"):
            inventory = megavault_inventory(megavault)
            pre_issues, pre_pushed = audit_inventory(
                inventory, auto_push=not args.dry_run, report_behind=False
            )
            auto_pushed += pre_pushed

            repos = [{**repo, "owner": args.owner} for repo in github_repos(args.owner)]
            ghorg_result = run_ghorg(args.owner, projects_dir, dry_run=args.dry_run)
            summary = summarize_ghorg_output((ghorg_result.stdout or "") + "\n" + (ghorg_result.stderr or ""))
            if ghorg_result.returncode == 0:
                if summary["errors"]:
                    issues.append(issue(None, "ghorg_reported_errors", f"count={summary['errors']}"))
                if summary["protected_skipped"]:
                    issues.append(issue(None, "ghorg_protected_skips", f"count={summary['protected_skipped']}"))
                registration = register_in_megavault(megavault, projects_dir, repos, dry_run=args.dry_run)
                if str(registration.get("validation")) not in {"PASS", "dry_run"}:
                    issues.append(issue(None, f"megavault_{registration['validation']}"))
                deferred = int(registration.get("deferred") or 0)
                if deferred:
                    issues.append(issue(None, "megavault_registration_deferred", f"count={deferred}"))
            else:
                registration = {
                    "already_registered": 0,
                    "newly_registered": 0,
                    "deferred": len(repos),
                    "validation": "skipped_ghorg_failed",
                }
                issues.append(issue(None, "ghorg_failed"))

            # Re-read the authoritative inventory after registration so newly
            # discovered repositories are covered immediately.
            post_inventory = megavault_inventory(megavault)
            post_issues, post_pushed = audit_inventory(
                post_inventory, auto_push=not args.dry_run, report_behind=True
            )
            auto_pushed += post_pushed
            # Pre-run anomalies may have been resolved by ghorg; only failed
            # automatic pushes remain relevant after the post-run audit.
            residual_pre = [x for x in pre_issues if x["kind"] in {"push_failed", "post_push_fetch_failed", "post_push_verify_failed"}]
            issues.extend(residual_pre)
            issues.extend(post_issues)
    except BlockingIOError:
        raise
    except AutosyncError as exc:
        issues.append(issue(None, str(exc)))
        notify = update_telegram_alert_state(state_dir, issues, enabled=telegram_enabled)
        if notify in {"notify_failed", "state_failed"}:
            raise AutosyncError(f"telegram_{notify}") from exc
        raise
    except Exception as exc:
        issues.append(issue(None, f"unexpected_{type(exc).__name__}"))
        notify = update_telegram_alert_state(state_dir, issues, enabled=telegram_enabled)
        if notify in {"notify_failed", "state_failed"}:
            raise AutosyncError(f"telegram_{notify}") from exc
        raise AutosyncError(f"unexpected_{type(exc).__name__}") from exc

    issues = dedupe_issues(issues)
    notify = update_telegram_alert_state(state_dir, issues, enabled=telegram_enabled)
    payload = {
        "status": "ok" if ghorg_result and ghorg_result.returncode == 0 and not issues else "issues",
        "owner": args.owner,
        "discovered": len(repos) if "repos" in locals() else 0,
        "auto_pushed": auto_pushed,
        "issues": len(issues),
        "telegram": notify,
        **summary,
        "megavault": registration,
    }
    print(json.dumps(payload, sort_keys=True))
    if notify in {"notify_failed", "state_failed"}:
        return 75
    return 0 if ghorg_result and ghorg_result.returncode == 0 else 75


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Safely bidirectional-sync gernalix GitHub repos with ghorg.")
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


if __name__ == "__main__":
    raise SystemExit(main())
