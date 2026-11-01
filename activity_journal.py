"""Append-only local autosync activity journal, event validation and cursors.

This layer does not synchronize remote Git repositories; its caller owns
transport, retries and publication.
"""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4
from autosync_errors import AutosyncError


ACTIVITY_LOG_FILE = "activity.jsonl"


ACTIVITY_DATA_STATE_FILE = "activity-data-state.json"


ACTIVITY_SCHEMA_VERSION = 1


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
