#!/usr/bin/env python3
"""Independent user-level recovery loop for github-autosync runtime."""
from __future__ import annotations

import argparse
import fcntl
import json
from pathlib import Path
import subprocess
from typing import Any

REPO = Path.home() / "projects" / "github-autosync"
STATE_DIR = Path.home() / ".local" / "state" / "codex-github-autosync"
CANONICAL_BRANCH = "main"


def run(cmd: list[str], cwd: Path | None = None, *, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=timeout,
    )


class WatchdogLock:
    def __init__(self, path: Path):
        self.path = path
        self.handle: Any = None

    def __enter__(self) -> "WatchdogLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("w", encoding="utf-8")
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.handle:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()


def _git(*args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return run(["git", *args], REPO, timeout=timeout)


def refresh_checkout() -> dict[str, Any]:
    if not (REPO / ".git").exists():
        return {"status": "blocked", "reason": "repo-missing"}

    branch = _git("branch", "--show-current", timeout=30)
    if branch.returncode or branch.stdout.strip() != CANONICAL_BRANCH:
        return {"status": "blocked", "reason": "wrong-branch"}

    dirty = _git("status", "--porcelain", timeout=30)
    if dirty.returncode:
        return {"status": "blocked", "reason": "status-failed"}
    if dirty.stdout.strip():
        return {"status": "blocked", "reason": "dirty-worktree"}

    fetched = _git(
        "fetch",
        "--no-tags",
        "origin",
        f"+refs/heads/{CANONICAL_BRANCH}:refs/remotes/origin/{CANONICAL_BRANCH}",
        timeout=180,
    )
    if fetched.returncode:
        return {"status": "blocked", "reason": "fetch-failed"}

    head = _git("rev-parse", "HEAD", timeout=30)
    remote = _git("rev-parse", f"refs/remotes/origin/{CANONICAL_BRANCH}", timeout=30)
    if head.returncode or remote.returncode:
        return {"status": "blocked", "reason": "rev-parse-failed"}
    head_sha = head.stdout.strip()
    remote_sha = remote.stdout.strip()
    if head_sha == remote_sha:
        return {"status": "current", "head": head_sha}

    relation = _git("merge-base", "--is-ancestor", head_sha, remote_sha, timeout=30)
    if relation.returncode != 0:
        return {"status": "blocked", "reason": "non-fast-forward", "head": head_sha, "remote": remote_sha}

    merged = _git("merge", "--ff-only", remote_sha, timeout=180)
    if merged.returncode:
        return {"status": "blocked", "reason": "fast-forward-failed", "head": head_sha, "remote": remote_sha}
    return {"status": "updated", "head": remote_sha}


def install_runtime() -> dict[str, Any]:
    installer = REPO / "install_systemd.py"
    if not installer.is_file():
        return {"status": "blocked", "reason": "installer-missing"}
    proc = run(["/usr/bin/python3", str(installer)], REPO, timeout=180)
    if proc.returncode:
        return {
            "status": "blocked",
            "reason": "install-failed",
            "detail": (proc.stderr.strip() or proc.stdout.strip())[-500:],
        }
    return {"status": "ok"}


def kick_reconcile() -> dict[str, Any]:
    reset = run(["systemctl", "--user", "reset-failed", "github-autosync.service"], timeout=30)
    enabled = run(
        ["systemctl", "--user", "enable", "--now", "github-autosync.timer", "repo-integrator.timer"],
        timeout=60,
    )
    started = run(
        ["systemctl", "--user", "start", "--no-block", "github-autosync.service"],
        timeout=30,
    )
    return {
        "status": "ok" if enabled.returncode == 0 and started.returncode == 0 else "blocked",
        "reset_failed_rc": reset.returncode,
        "enable_rc": enabled.returncode,
        "start_rc": started.returncode,
    }


def repair() -> dict[str, Any]:
    checkout = refresh_checkout()
    install = install_runtime()
    kick = kick_reconcile()
    status = "ok"
    blockers = []
    for name, part in (("checkout", checkout), ("install", install), ("kick", kick)):
        if part.get("status") == "blocked":
            blockers.append(f"{name}:{part.get('reason', 'blocked')}")
    if blockers:
        status = "blocked"
    return {
        "status": status,
        "checkout": checkout,
        "install": install,
        "kick": kick,
        "blockers": blockers,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Repair github-autosync user runtime independently of the main timer.")
    parser.add_argument("--repair", action="store_true")
    args = parser.parse_args(argv)
    if not args.repair:
        parser.error("--repair is required")
    try:
        with WatchdogLock(STATE_DIR / "runtime-watchdog.lock"):
            result = repair()
    except BlockingIOError:
        result = {"status": "locked", "blockers": []}
    print(json.dumps(result, sort_keys=True))
    return 2 if result.get("status") == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
