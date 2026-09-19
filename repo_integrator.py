#!/usr/bin/env python3
"""Asynchronous FIFO integrator for queued task pull requests."""
from __future__ import annotations
import argparse
import fcntl
import json
from pathlib import Path
from typing import Any
import autosync_core
import repo_single_writer

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
    queue = repo_single_writer.process_ready_prs(owner)
    roadmap = autosync_core.queue_merged_roadmap_completions()
    transient = {"draft","checks-pending","mergeable-unknown","branch-refreshed","queue-behind-earlier","pr-read-failed"}
    hard = [
        {"repo": i.get("repo"), "number": i.get("number"), "reason": i.get("reason")}
        for i in queue.get("results", [])
        if i.get("status") == "deferred" and str(i.get("reason") or "deferred") not in transient
    ]
    return {"status": "blocked" if hard else "ok", "queue": queue, "roadmap_finalization": roadmap, "hard_blockers": hard}

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Integrate queued task PRs FIFO per repository.")
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
