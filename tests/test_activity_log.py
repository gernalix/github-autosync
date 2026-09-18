from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import github_autosync as autosync


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
