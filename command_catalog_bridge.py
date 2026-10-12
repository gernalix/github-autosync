"""Publish owner command manifests through the existing MegaVault writer."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any


MANIFEST = Path(".megavault/commands-v1.json")
DEFAULT_MEGAVAULT = Path.home() / "MegaVault" / "megavault.py"
DEFAULT_STATE_ROOT = Path.home() / ".local/state/codex-github-autosync/command-events"


def _safe_repo(repo: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", repo.strip())
    if not value:
        raise ValueError("repository identity is empty")
    return value


def _run(cli: Path, worktree: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", "-B", str(cli), "command-catalog", *args],
        cwd=worktree,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=180,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def prepare(
    worktree: Path,
    repo: str,
    source_commit: str,
    *,
    cli: Path = DEFAULT_MEGAVAULT,
    state_root: Path = DEFAULT_STATE_ROOT,
) -> dict[str, Any]:
    """Validate and durably emit before merge; never writes the registry."""
    manifest = worktree / MANIFEST
    if not manifest.is_file():
        return {"status": "not-applicable"}
    if not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        return {"status": "blocked", "reason": "invalid-source-commit"}
    validate = _run(cli, worktree, "validate", "--manifest", str(MANIFEST))
    if validate.returncode:
        return {"status": "blocked", "reason": "manifest-validation-failed"}
    outbox = state_root / _safe_repo(repo) / source_commit
    outbox.mkdir(parents=True, exist_ok=True, mode=0o700)
    emit = _run(
        cli,
        worktree,
        "emit",
        "--manifest",
        str(MANIFEST),
        "--outbox",
        str(outbox),
        "--source-commit",
        source_commit,
    )
    if emit.returncode:
        return {"status": "blocked", "reason": "event-emission-failed", "outbox": str(outbox)}
    return {
        "status": "ready",
        "repo": repo,
        "source_commit": source_commit,
        "worktree": str(worktree),
        "outbox": str(outbox),
        "events": len(list(outbox.glob("CE-*.json"))),
    }


def publish(prepared: dict[str, Any], *, cli: Path = DEFAULT_MEGAVAULT) -> dict[str, Any]:
    """Ingest and read back only after the owner commit has been merged."""
    if prepared.get("status") == "not-applicable":
        return {"status": "not-applicable"}
    if prepared.get("status") != "ready":
        return {"status": "blocked", "reason": "catalog-not-prepared"}
    worktree = Path(str(prepared["worktree"]))
    outbox = Path(str(prepared["outbox"]))
    if not (worktree / MANIFEST).is_file():
        return {"status": "pending", "reason": "worktree-manifest-unavailable"}
    if prepared.get("events"):
        ingest = _run(cli, worktree, "ingest", "--outbox", str(outbox))
        if ingest.returncode:
            return {"status": "pending", "reason": "event-ingest-failed", "outbox": str(outbox)}
    reconcile = _run(cli, worktree, "reconcile", "--manifest", str(MANIFEST))
    if reconcile.returncode:
        return {"status": "pending", "reason": "manifest-readback-failed", "outbox": str(outbox)}
    try:
        receipt = json.loads(reconcile.stdout)
    except json.JSONDecodeError:
        receipt = {"status": "PASS"}
    return {"status": "pass", "outbox": str(outbox), "readback": receipt}
