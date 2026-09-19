#!/usr/bin/env python3
"""Generic per-repository single-writer workflow for agent work.

Workers never update the canonical branch directly. They work on task/* branches
in dedicated worktrees and submit a [single-writer] pull request. The periodic
github-reconcile process is the only automated component allowed to merge those
requests into each repository's canonical branch.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any
from urllib.parse import quote

TASK_PREFIX = "task/"
PR_PREFIX = "[single-writer]"
STATE_ROOT = Path.home() / ".local/state/codex-github-autosync/single-writer"
WORKTREE_ROOT = Path.home() / ".local/share/codex-github-autosync/worktrees"
ROADMAP_REPOSITORY = "gernalix/codex-roadmap"
HOOK_MARKER = "github-autosync-single-writer-v1"


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
    )


def _git(repo: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return run(["git", *args], repo, timeout)


def _ok(repo: Path, *args: str, timeout: int = 120) -> str:
    proc = _git(repo, *args, timeout=timeout)
    if proc.returncode:
        detail = proc.stderr.strip() or proc.stdout.strip() or f"exit={proc.returncode}"
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return proc.stdout.strip()


def _safe_task_id(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-")
    if not text:
        raise ValueError("task_id is empty")
    return text[:80]


def _remote_url(repo: Path) -> str:
    return _ok(repo, "remote", "get-url", "origin")


def _repo_slug(repo: Path) -> str:
    url = _remote_url(repo).removesuffix(".git").rstrip("/")
    if url.startswith("git@github.com:"):
        url = "https://github.com/" + url.removeprefix("git@github.com:")
    if "github.com/" in url:
        return url.split("github.com/", 1)[1]
    return repo.name


def _canonical_branch(repo: Path) -> str:
    symbolic = _git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if symbolic.returncode == 0 and "/" in symbolic.stdout.strip():
        return symbolic.stdout.strip().split("/", 1)[1]
    current = _ok(repo, "branch", "--show-current")
    if not current:
        raise RuntimeError("canonical branch is unknown")
    return current


def _git_common_dir(repo: Path) -> Path:
    raw = _ok(repo, "rev-parse", "--git-common-dir")
    path = Path(raw)
    return path.resolve() if path.is_absolute() else (repo / path).resolve()


def _effective_hooks_dir(repo: Path) -> Path:
    configured = _git(repo, "config", "--get", "core.hooksPath")
    if configured.returncode == 0 and configured.stdout.strip():
        path = Path(configured.stdout.strip())
        return path.resolve() if path.is_absolute() else (repo / path).resolve()
    return _git_common_dir(repo) / "hooks"


def _auth_path(repo: Path) -> Path:
    return _git_common_dir(repo) / "github-autosync-writer-auth.json"


def _writer_hook(canonical_ref: str, auth_path: Path, prior: Path | None) -> str:
    prior_literal = repr(str(prior) if prior else "")
    return f'''#!/usr/bin/env python3
# {HOOK_MARKER}
import json
import os
import subprocess
import sys

CANONICAL_REF = {canonical_ref!r}
AUTH_PATH = {str(auth_path)!r}
PRIOR = {prior_literal}

phase = sys.argv[1] if len(sys.argv) > 1 else ""
payload = sys.stdin.read()

if PRIOR and os.path.isfile(PRIOR) and os.access(PRIOR, os.X_OK):
    prior = subprocess.run([PRIOR, *sys.argv[1:]], input=payload, text=True)
    if prior.returncode:
        raise SystemExit(prior.returncode)

if phase != "prepared":
    raise SystemExit(0)

updates = []
for line in payload.splitlines():
    parts = line.split()
    if len(parts) == 3 and parts[2] == CANONICAL_REF:
        updates.append(tuple(parts))

if not updates:
    raise SystemExit(0)

try:
    with open(AUTH_PATH, "r", encoding="utf-8") as handle:
        auth = json.load(handle)
except Exception:
    print(f"BLOCKED: {{CANONICAL_REF}} is single-writer protected.", file=sys.stderr)
    print("Use repo-task start/finish; github-reconcile integrates completed tasks.", file=sys.stderr)
    raise SystemExit(1)

allowed = (str(auth.get("old")), str(auth.get("new")), str(auth.get("ref")))
for update in updates:
    if update != allowed:
        print(f"BLOCKED: unauthorized update of {{CANONICAL_REF}}.", file=sys.stderr)
        raise SystemExit(1)
raise SystemExit(0)
'''


def ensure_guard(repo: Path, canonical_branch: str | None = None) -> dict[str, Any]:
    repo = repo.expanduser().resolve()
    slug = _repo_slug(repo)
    if slug.lower() == ROADMAP_REPOSITORY.lower():
        return {"repo": slug, "status": "delegated-roadmap"}
    branch = canonical_branch or _canonical_branch(repo)
    hooks = _effective_hooks_dir(repo)
    hooks.mkdir(parents=True, exist_ok=True)
    target = hooks / "reference-transaction"
    prior = hooks / "reference-transaction.pre-single-writer"
    if target.exists():
        current = target.read_text(encoding="utf-8", errors="replace")
        if HOOK_MARKER not in current and not prior.exists():
            target.replace(prior)
    script = _writer_hook(f"refs/heads/{branch}", _auth_path(repo), prior if prior.exists() else None)
    if not target.exists() or target.read_text(encoding="utf-8", errors="replace") != script:
        tmp = target.with_suffix(".tmp")
        tmp.write_text(script, encoding="utf-8")
        os.chmod(tmp, 0o755)
        tmp.replace(target)
    return {"repo": slug, "status": "installed", "branch": branch}


def authorize_ref_update(repo: Path, old: str, new: str, branch: str) -> Path:
    path = _auth_path(repo)
    payload = {"old": old, "new": new, "ref": f"refs/heads/{branch}"}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return path


def _task_record(repo: Path, task_id: str) -> Path:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", _repo_slug(repo))
    return STATE_ROOT / slug / "tasks" / f"{_safe_task_id(task_id)}.json"


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def start_task(repo: Path, task_id: str, actor: str = "agent") -> dict[str, Any]:
    repo = repo.expanduser().resolve()
    task_id = _safe_task_id(task_id)
    if not (repo / ".git").exists() and _git(repo, "rev-parse", "--is-inside-work-tree").returncode != 0:
        raise RuntimeError(f"not a git repository: {repo}")
    slug = _repo_slug(repo)
    if slug.lower() == ROADMAP_REPOSITORY.lower():
        raise RuntimeError("codex-roadmap keeps its dedicated single-writer workflow")
    canonical = _canonical_branch(repo)
    ensure_guard(repo, canonical)
    status = _ok(repo, "status", "--porcelain")
    current = _ok(repo, "branch", "--show-current")
    if current == canonical and status:
        raise RuntimeError("canonical checkout is dirty; preserve/reconcile it before starting parallel work")

    record_path = _task_record(repo, task_id)
    if record_path.exists():
        existing = json.loads(record_path.read_text(encoding="utf-8"))
        if existing.get("status") in {"active", "ready"} and Path(existing["worktree"]).exists():
            return existing

    _git(repo, "fetch", "--prune", "origin", timeout=180)
    branch = TASK_PREFIX + task_id
    safe_slug = re.sub(r"[^A-Za-z0-9._-]+", "_", slug)
    worktree = WORKTREE_ROOT / safe_slug / task_id
    worktree.parent.mkdir(parents=True, exist_ok=True)
    if worktree.exists():
        raise RuntimeError(f"task worktree already exists: {worktree}")
    remote_base = f"origin/{canonical}"
    base = remote_base if _git(repo, "show-ref", "--verify", "--quiet", f"refs/remotes/{remote_base}").returncode == 0 else canonical
    if _git(repo, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0:
        add = _git(repo, "worktree", "add", str(worktree), branch, timeout=180)
    else:
        add = _git(repo, "worktree", "add", "-b", branch, str(worktree), base, timeout=180)
    if add.returncode:
        raise RuntimeError(add.stderr.strip() or add.stdout.strip() or "worktree creation failed")
    payload = {
        "schema_version": 1,
        "task_id": task_id,
        "actor": actor,
        "repo": slug,
        "repo_path": str(repo),
        "canonical_branch": canonical,
        "branch": branch,
        "worktree": str(worktree),
        "status": "active",
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _atomic_json(record_path, payload)
    return payload


def _operation_in_progress(worktree: Path) -> bool:
    if _git(worktree, "ls-files", "-u").stdout.strip():
        return True
    for marker in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer"):
        path = _git(worktree, "rev-parse", "--git-path", marker)
        if path.returncode == 0:
            candidate = Path(path.stdout.strip())
            if not candidate.is_absolute():
                candidate = worktree / candidate
            if candidate.exists():
                return True
    return False


def _checkpoint_task(worktree: Path, task_id: str) -> None:
    if _operation_in_progress(worktree):
        raise RuntimeError("task has an unfinished Git operation/conflict")
    status = _ok(worktree, "status", "--porcelain")
    if not status:
        return
    add = _git(worktree, "add", "-A", timeout=120)
    if add.returncode:
        raise RuntimeError(add.stderr.strip() or "git add failed")
    staged = _git(worktree, "diff", "--cached", "--quiet")
    if staged.returncode == 1:
        commit = _git(worktree, "commit", "-m", f"task {task_id}: checkpoint completed work", timeout=300)
        if commit.returncode:
            raise RuntimeError(commit.stderr.strip() or "task commit failed")
    elif staged.returncode != 0:
        raise RuntimeError("staged diff check failed")


def finish_task(repo: Path, task_id: str) -> dict[str, Any]:
    repo = repo.expanduser().resolve()
    task_id = _safe_task_id(task_id)
    record_path = _task_record(repo, task_id)
    if not record_path.exists():
        raise RuntimeError(f"unknown task: {task_id}")
    payload = json.loads(record_path.read_text(encoding="utf-8"))
    if payload.get("status") == "ready":
        return payload
    if payload.get("status") not in {"active", "blocked"}:
        raise RuntimeError(f"task is not finishable: {payload.get('status')}")
    worktree = Path(payload["worktree"])
    _checkpoint_task(worktree, task_id)
    branch = str(payload["branch"])
    push = _git(worktree, "push", "-u", "origin", branch, timeout=300)
    if push.returncode:
        raise RuntimeError(push.stderr.strip() or "task branch push failed")

    title = f"{PR_PREFIX} {task_id}"
    existing = run(
        ["gh", "pr", "list", "--repo", payload["repo"], "--head", branch, "--state", "open", "--json", "number,url,title"],
        timeout=120,
    )
    pr_number = None
    pr_url = None
    if existing.returncode == 0:
        try:
            rows = json.loads(existing.stdout)
            if rows:
                pr_number = int(rows[0]["number"])
                pr_url = str(rows[0].get("url") or "")
        except Exception:
            pass
    if pr_number is None:
        created = run(
            [
                "gh", "pr", "create", "--repo", payload["repo"],
                "--base", str(payload["canonical_branch"]),
                "--head", branch,
                "--title", title,
                "--body", "Ready for the per-repository single writer. Do not merge manually.",
            ],
            timeout=180,
        )
        if created.returncode:
            raise RuntimeError(created.stderr.strip() or created.stdout.strip() or "PR creation failed")
        pr_url = created.stdout.strip()
        lookup = run(
            ["gh", "pr", "list", "--repo", payload["repo"], "--head", branch, "--state", "open", "--json", "number,url"],
            timeout=120,
        )
        if lookup.returncode == 0:
            rows = json.loads(lookup.stdout)
            if rows:
                pr_number = int(rows[0]["number"])
                pr_url = str(rows[0].get("url") or pr_url)
    payload.update({
        "status": "ready",
        "ready_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pr_number": pr_number,
        "pr_url": pr_url,
    })
    _atomic_json(record_path, payload)
    return payload


def _check_rollup_allows_merge(items: Any) -> tuple[bool, str]:
    if not items:
        return True, "no-checks"
    for item in items:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "").upper()
        conclusion = str(item.get("conclusion") or item.get("state") or "").upper()
        if status and status not in {"COMPLETED", "SUCCESS"}:
            return False, "checks-pending"
        if conclusion and conclusion not in {"SUCCESS", "NEUTRAL", "SKIPPED", "EXPECTED", "COMPLETED"}:
            return False, "checks-failed"
    return True, "checks-pass"


def discover_ready_prs(owner: str) -> list[dict[str, Any]]:
    search = run(
        ["gh", "search", "prs", PR_PREFIX, "--owner", owner, "--state", "open", "--limit", "200",
         "--json", "repository,number,title,url"],
        timeout=180,
    )
    if search.returncode:
        return []
    try:
        rows = json.loads(search.stdout)
    except json.JSONDecodeError:
        return []
    result = []
    for row in rows:
        if not str(row.get("title") or "").startswith(PR_PREFIX):
            continue
        repository = row.get("repository") or {}
        full = repository.get("nameWithOwner") or repository.get("name_with_owner")
        if not full and repository.get("owner") and repository.get("name"):
            owner_value = repository["owner"]
            owner_name = owner_value.get("login") if isinstance(owner_value, dict) else owner_value
            full = f"{owner_name}/{repository['name']}"
        if not full:
            continue
        result.append({"repo": str(full), "number": int(row["number"]), "url": row.get("url")})
    return sorted(result, key=lambda item: (item["repo"].lower(), item["number"]))


class RepoLock:
    def __init__(self, repo: str):
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", repo)
        self.path = STATE_ROOT / "locks" / f"{safe}.lock"
        self.handle: Any = None

    def __enter__(self) -> "RepoLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("w", encoding="utf-8")
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.handle:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def integrate_pr(repo: str, number: int) -> dict[str, Any]:
    if repo.lower() == ROADMAP_REPOSITORY.lower():
        return {"repo": repo, "number": number, "status": "delegated-roadmap"}
    with RepoLock(repo):
        view = run(
            ["gh", "pr", "view", str(number), "--repo", repo,
             "--json", "title,isDraft,mergeable,baseRefName,headRefName,headRefOid,statusCheckRollup"],
            timeout=120,
        )
        if view.returncode:
            return {"repo": repo, "number": number, "status": "deferred", "reason": "pr-read-failed"}
        try:
            info = json.loads(view.stdout)
        except json.JSONDecodeError:
            return {"repo": repo, "number": number, "status": "deferred", "reason": "pr-json-invalid"}
        if not str(info.get("title") or "").startswith(PR_PREFIX):
            return {"repo": repo, "number": number, "status": "ignored"}
        if info.get("isDraft"):
            return {"repo": repo, "number": number, "status": "deferred", "reason": "draft"}
        if not str(info.get("headRefName") or "").startswith(TASK_PREFIX):
            return {"repo": repo, "number": number, "status": "deferred", "reason": "non-task-branch"}
        allowed, check_reason = _check_rollup_allows_merge(info.get("statusCheckRollup"))
        if not allowed:
            return {"repo": repo, "number": number, "status": "deferred", "reason": check_reason}
        mergeable = str(info.get("mergeable") or "").upper()
        if mergeable != "MERGEABLE":
            return {"repo": repo, "number": number, "status": "deferred", "reason": f"mergeable-{mergeable.lower() or 'unknown'}"}
        head = str(info.get("headRefOid") or "")
        if not head:
            return {"repo": repo, "number": number, "status": "deferred", "reason": "head-missing"}
        merged = run(
            ["gh", "api", "--method", "PUT", f"repos/{repo}/pulls/{number}/merge",
             "-f", f"sha={head}", "-f", "merge_method=merge"],
            timeout=180,
        )
        if merged.returncode:
            return {"repo": repo, "number": number, "status": "deferred", "reason": "merge-race-or-failure"}
        try:
            payload = json.loads(merged.stdout)
        except json.JSONDecodeError:
            payload = {}
        if not payload.get("merged"):
            return {"repo": repo, "number": number, "status": "deferred", "reason": str(payload.get("message") or "merge-failed")}
        return {"repo": repo, "number": number, "status": "merged", "sha": payload.get("sha")}


def process_ready_prs(owner: str) -> dict[str, Any]:
    results = [integrate_pr(item["repo"], item["number"]) for item in discover_ready_prs(owner)]
    return {
        "found": len(results),
        "merged": sum(1 for item in results if item.get("status") == "merged"),
        "deferred": sum(1 for item in results if item.get("status") == "deferred"),
        "results": results,
    }


def _print_start(payload: dict[str, Any]) -> None:
    print(payload["worktree"])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Per-repository single-writer task helper.")
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start")
    start.add_argument("--repo", type=Path, required=True)
    start.add_argument("--task-id", required=True)
    start.add_argument("--actor", default="agent")
    finish = sub.add_parser("finish")
    finish.add_argument("--repo", type=Path, required=True)
    finish.add_argument("--task-id", required=True)
    guard = sub.add_parser("guard")
    guard.add_argument("--repo", type=Path, required=True)
    guard.add_argument("--branch")
    process = sub.add_parser("process")
    process.add_argument("--owner", default="gernalix")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "start":
            payload = start_task(args.repo, args.task_id, args.actor)
            _print_start(payload)
        elif args.command == "finish":
            payload = finish_task(args.repo, args.task_id)
            print(payload.get("pr_url") or f"PR #{payload.get('pr_number')}")
        elif args.command == "guard":
            print(json.dumps(ensure_guard(args.repo, args.branch), sort_keys=True))
        elif args.command == "process":
            print(json.dumps(process_ready_prs(args.owner), sort_keys=True))
        return 0
    except Exception as exc:
        print(f"repo-task: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
