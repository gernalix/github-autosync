#!/usr/bin/env python3
"""Asynchronous dependency-aware integrator for queued task pull requests."""
from __future__ import annotations
import argparse
import fcntl
import json
from pathlib import Path
from typing import Any
import autosync_core
import repo_single_writer
import repo_gc

DEFAULT_STATE_ROOT = Path.home() / ".local/state/codex-github-autosync"

class IntegratorLock:
    def __init__(self, path: Path):
        self.path = path
        self.handle: Any = None
    def __enter__(self) -> "IntegratorLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("w", encoding="utf-8")
        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return self
    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self.handle:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()

def run_once(owner: str) -> dict[str, Any]:
    """Retired PR integrator: no API reads, merges, or automatic retries."""
    return {"status": "retired", "queue": {"found": 0, "merged": 0, "deferred": 0},
            "reason": "GitHub PR integration disabled; use repo-task finish on Fedora"}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Integrate independent queued task PRs serially per repository.")
    p.add_argument("--owner", default="gernalix")
    p.add_argument("--state-root", type=Path, default=DEFAULT_STATE_ROOT)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    try:
        with IntegratorLock(args.state_root / "repo-integrator.lock"):
            result = run_once(args.owner)
    except BlockingIOError:
        result = {"status": "locked"}
    print(json.dumps(result, sort_keys=True))
    return 2 if result.get("status") == "blocked" else 0

if __name__ == "__main__":
    raise SystemExit(main())
