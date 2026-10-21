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


def _run_version(cli: Path, worktree: Path, project_id: int, merge_sha: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["python3", "-B", str(cli), "git-purity", "version-event", "--project-id", str(project_id), "--sha", merge_sha],
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
    try:
        project_id = int(json.loads(manifest.read_text(encoding="utf-8"))["project_id"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return {"status": "blocked", "reason": "manifest-project-id-unreadable"}
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
        "project_id": project_id,
    }


def current_canonical_readback(prepared: dict[str, Any], cli: Path) -> dict[str, Any] | None:
    """An older applied event may have been superseded by a later canonical manifest."""
    import tempfile
    import repo_single_writer as writer
    repo = str(prepared.get("repo") or "")
    merged = str(prepared.get("merge_sha") or "")
    worktree = Path(str(prepared.get("worktree") or ""))
    if not repo or not re.fullmatch(r"[0-9a-f]{40}", merged):
        return None
    try:
        if writer._repo_slug(worktree).lower() != repo.lower():
            return None
        branch = writer._canonical_branch(worktree)
        def remote_tip():
            return writer._ok(worktree,"ls-remote","origin","refs/heads/"+branch).split()[0]
        tip = remote_tip()
        if tip == merged or writer._git(worktree,"merge-base","--is-ancestor",merged,tip).returncode:
            return None
        raw = writer._ok(worktree,"show",tip+":"+str(MANIFEST))
        if json.loads(raw).get("project_id") != prepared.get("project_id"):
            return None
        with tempfile.TemporaryDirectory(prefix="catalog-readback-") as temp:
            manifest = Path(temp)/"commands-v1.json"
            manifest.write_text(raw,encoding="utf-8")
            result = _run(cli,worktree,"reconcile","--manifest",str(manifest))
        if result.returncode or remote_tip() != tip:
            return None
        readback = json.loads(result.stdout)
        if readback.get("status") != "PASS" or readback.get("project_id") != prepared.get("project_id"):
            return None
        return {"canonical_sha":tip,"readback":readback}
    except (OSError,ValueError,RuntimeError,KeyError,IndexError,subprocess.SubprocessError):
        return None


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
        current = current_canonical_readback(prepared,cli)
        if current is not None:
            # Never replay an old definition or register an older live version.
            # Its immutable events were ingested above; current authority is
            # separately verified against the newer canonical manifest.
            return {"status":"pass","outbox":str(outbox),"superseded_by":current["canonical_sha"],
                    "readback":current["readback"],"version":{"status":"superseded"}}
        return {"status": "pending", "reason": "manifest-readback-failed", "outbox": str(outbox)}
    try:
        receipt = json.loads(reconcile.stdout)
    except json.JSONDecodeError:
        receipt = {"status": "PASS"}
    merge_sha = str(prepared.get("merge_sha") or "")
    project_id = prepared.get("project_id")
    if not re.fullmatch(r"[0-9a-f]{40}", merge_sha) or not isinstance(project_id, int):
        return {"status": "pending", "reason": "version-evidence-incomplete", "outbox": str(outbox)}
    version = _run_version(cli, worktree, project_id, merge_sha)
    if version.returncode:
        return {"status": "pending", "reason": "version-registration-failed", "outbox": str(outbox)}
    try:
        version_receipt = json.loads(version.stdout)
    except json.JSONDecodeError:
        version_receipt = {"status": "PASS"}
    return {"status": "pass", "outbox": str(outbox), "readback": receipt, "version": version_receipt}
