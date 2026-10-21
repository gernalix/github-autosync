"""Pure repository allowlist, identity and fingerprinting policy.

Intentionally independent of subprocess, filesystem and autosync orchestration.
"""

from __future__ import annotations

import json
from typing import Any

import repo_single_writer


INDEPENDENT_CANONICAL_WRITER_REPOSITORIES = (
    repo_single_writer.INDEPENDENT_CANONICAL_WRITER_REPOSITORIES
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
        "gernalix/oracle-uptime-kuma",
        "gernalix/workflowy-import",
    }
)


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


def normalize_remote(url: str) -> str:
    text = url.strip()
    if text.startswith("git@github.com:"):
        text = "https://github.com/" + text.removeprefix("git@github.com:")
    return text.removesuffix(".git").rstrip("/").lower()
