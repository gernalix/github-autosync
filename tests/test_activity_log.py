from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import github_autosync as autosync


def git(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *cmd],
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def make_data_remote(root: Path) -> Path:
    bare = root / "activity-data.git"
    seed = root / "seed"
    if git(["init", "--bare", str(bare)]).returncode != 0:
        raise AssertionError("failed to initialize bare data repo")
    if git(["clone", str(bare), str(seed)]).returncode != 0:
        raise AssertionError("failed to clone seed repo")
    git(["config", "user.email", "test@example.invalid"], seed)
    git(["config", "user.name", "Test"], seed)
    (seed / "README.md").write_text("# data\n", encoding="utf-8")
    git(["add", "README.md"], seed)
    if git(["commit", "-m", "init"], seed).returncode != 0:
        raise AssertionError("failed to commit seed")
    git(["branch", "-M", "main"], seed)
    if git(["push", "-u", "origin", "main"], seed).returncode != 0:
        raise AssertionError("failed to push seed")
    return bare


class ActivityLogTests(unittest.TestCase):
    def test_activity_log_is_append_only_and_notifies_push_pull_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            autosync.append_activity(
                state,
                action="clone",
                repo="gernalix/PersonalHub",
                branch="main",
                project_id=49,
                worktree="/tmp/PersonalHub",
                detail="managed repository cloned",
            )
            autosync.append_activity(
                state,
                action="pull",
                repo="gernalix/PersonalHub",
                branch="main",
                project_id=49,
                worktree="/tmp/PersonalHub",
                detail="fast-forward only",
            )
            autosync.append_activity(
                state,
                action="push",
                repo="gernalix/PersonalHub",
                branch="main",
                project_id=49,
                worktree="/tmp/PersonalHub",
                detail="clean ahead-only checkout",
            )

            rows = [
                json.loads(line)
                for line in (state / autosync.ACTIVITY_LOG_FILE).read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(["clone", "pull", "push"], [row["action"] for row in rows])

            with mock.patch.object(autosync, "send_telegram", return_value=True) as sender:
                self.assertEqual("sent:2", autosync.notify_pending_activity(state, enabled=True))
                self.assertEqual("unchanged", autosync.notify_pending_activity(state, enabled=True))

            self.assertEqual(
                ["GitHub autosync: pull", "GitHub autosync: push"],
                [call.args[0] for call in sender.call_args_list],
            )
            cursor = json.loads(
                (state / autosync.ACTIVITY_NOTIFY_STATE_FILE).read_text(encoding="utf-8")
            )
            self.assertEqual(3, cursor["next_line"])


    def test_private_data_mirror_shards_daily_and_retries_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            state.mkdir()
            bare = make_data_remote(root)

            events = [
                {
                    "schema_version": 1,
                    "event_id": "event-a",
                    "timestamp": "2026-09-17T23:59:00Z",
                    "action": "pull",
                    "repo": "gernalix/PersonalHub",
                    "branch": "main",
                    "project_id": 49,
                    "worktree": "/tmp/PersonalHub",
                    "detail": "fast-forward only",
                },
                {
                    "schema_version": 1,
                    "event_id": "event-b",
                    "timestamp": "2026-09-18T00:01:00Z",
                    "action": "push",
                    "repo": "gernalix/MegaVault",
                    "branch": "main",
                    "project_id": 23,
                    "worktree": "/tmp/MegaVault",
                    "detail": "clean ahead-only checkout",
                },
            ]
            (state / autosync.ACTIVITY_LOG_FILE).write_text(
                "\n".join(json.dumps(event, sort_keys=True, separators=(",", ":")) for event in events) + "\n",
                encoding="utf-8",
            )

            with mock.patch.object(autosync, "ACTIVITY_DATA_REMOTE", str(bare)):
                self.assertEqual("pushed:2", autosync.mirror_pending_activity(state, enabled=True))
                checkout = state / autosync.ACTIVITY_DATA_CHECKOUT_DIR
                day_one = checkout / "activity/2026/09/2026-09-17.jsonl"
                day_two = checkout / "activity/2026/09/2026-09-18.jsonl"
                self.assertTrue(day_one.is_file())
                self.assertTrue(day_two.is_file())
                self.assertEqual("event-a", json.loads(day_one.read_text(encoding="utf-8"))["event_id"])
                self.assertEqual("event-b", json.loads(day_two.read_text(encoding="utf-8"))["event_id"])
                self.assertEqual("2", git(["rev-list", "--count", "HEAD"], checkout).stdout.strip())

                self.assertEqual("unchanged", autosync.mirror_pending_activity(state, enabled=True))

                # Simulate a crash after remote persistence but before the cursor was durable.
                (state / autosync.ACTIVITY_DATA_STATE_FILE).unlink()
                self.assertEqual("reconciled:2", autosync.mirror_pending_activity(state, enabled=True))
                self.assertEqual("2", git(["rev-list", "--count", "HEAD"], checkout).stdout.strip())
                self.assertEqual(1, len(day_one.read_text(encoding="utf-8").splitlines()))
                self.assertEqual(1, len(day_two.read_text(encoding="utf-8").splitlines()))

    def test_legacy_activity_gets_stable_synthetic_event_id(self) -> None:
        legacy = {
            "timestamp": "2026-09-18T12:00:00Z",
            "action": "push",
            "repo": "gernalix/PersonalHub",
            "branch": "main",
            "project_id": 49,
            "worktree": "/tmp/PersonalHub",
            "detail": "legacy",
        }
        raw = json.dumps(legacy, sort_keys=True, separators=(",", ":"))
        first = autosync._normalize_activity_event(legacy, line_number=7, raw_line=raw)
        second = autosync._normalize_activity_event(legacy, line_number=7, raw_line=raw)
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertTrue(str(first["event_id"]).startswith("legacy-"))
        self.assertEqual(1, first["schema_version"])

    def test_failed_telegram_delivery_is_retried(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            autosync.append_activity(
                state,
                action="push",
                repo="gernalix/MegaVault",
                branch="main",
                detail="test",
            )

            with mock.patch.object(autosync, "send_telegram", return_value=False):
                self.assertEqual("notify_failed", autosync.notify_pending_activity(state, enabled=True))
            self.assertFalse((state / autosync.ACTIVITY_NOTIFY_STATE_FILE).exists())

            with mock.patch.object(autosync, "send_telegram", return_value=True) as sender:
                self.assertEqual("sent:1", autosync.notify_pending_activity(state, enabled=True))
            sender.assert_called_once()

    def test_disabled_telegram_does_not_advance_activity_cursor(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp)
            autosync.append_activity(
                state,
                action="pull",
                repo="gernalix/codex-roadmap",
                branch="main",
                detail="fast-forward only",
            )

            self.assertEqual("disabled", autosync.notify_pending_activity(state, enabled=False))
            self.assertFalse((state / autosync.ACTIVITY_NOTIFY_STATE_FILE).exists())

            with mock.patch.object(autosync, "send_telegram", return_value=True):
                self.assertEqual("sent:1", autosync.notify_pending_activity(state, enabled=True))


if __name__ == "__main__":
    unittest.main()
