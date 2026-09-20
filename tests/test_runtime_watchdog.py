from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import runtime_watchdog as watchdog


def proc(rc: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], rc, stdout=stdout, stderr=stderr)


class RuntimeWatchdogTests(unittest.TestCase):
    def test_dirty_checkout_blocks_self_update_before_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".git").mkdir()
            with (
                mock.patch.object(watchdog, "REPO", repo),
                mock.patch.object(
                    watchdog,
                    "_git",
                    side_effect=[
                        proc(stdout="main\n"),
                        proc(stdout=" M local.txt\n"),
                    ],
                ) as git,
            ):
                result = watchdog.refresh_checkout()
        self.assertEqual({"status": "blocked", "reason": "dirty-worktree"}, result)
        self.assertEqual(2, git.call_count)

    def test_clean_behind_checkout_fast_forwards_only_to_fetched_main(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".git").mkdir()
            head = "a" * 40
            remote = "b" * 40

            def fake_git(*args: str, timeout: int = 120):
                if args == ("branch", "--show-current"):
                    return proc(stdout="main\n")
                if args == ("status", "--porcelain"):
                    return proc()
                if args[:2] == ("fetch", "--no-tags"):
                    return proc()
                if args == ("rev-parse", "HEAD"):
                    return proc(stdout=head + "\n")
                if args == ("rev-parse", "refs/remotes/origin/main"):
                    return proc(stdout=remote + "\n")
                if args == ("merge-base", "--is-ancestor", head, remote):
                    return proc()
                if args == ("merge", "--ff-only", remote):
                    return proc()
                raise AssertionError(args)

            with (
                mock.patch.object(watchdog, "REPO", repo),
                mock.patch.object(watchdog, "_git", side_effect=fake_git) as git,
            ):
                result = watchdog.refresh_checkout()

        self.assertEqual("updated", result["status"])
        self.assertEqual(remote, result["head"])
        self.assertTrue(
            any(call.args == ("merge", "--ff-only", remote) for call in git.call_args_list)
        )

    def test_repair_stops_before_install_when_checkout_is_blocked(self) -> None:
        with (
            mock.patch.object(
                watchdog,
                "refresh_checkout",
                return_value={"status": "blocked", "reason": "status-failed"},
            ),
            mock.patch.object(watchdog, "install_runtime") as install,
            mock.patch.object(watchdog, "kick_reconcile") as kick,
        ):
            result = watchdog.repair()
        self.assertEqual("blocked", result["status"])
        self.assertEqual("skipped", result["install"]["status"])
        self.assertEqual("skipped", result["kick"]["status"])
        install.assert_not_called()
        kick.assert_not_called()

    def test_repair_stops_before_kick_when_install_is_blocked(self) -> None:
        with (
            mock.patch.object(watchdog, "refresh_checkout", return_value={"status": "current"}),
            mock.patch.object(
                watchdog,
                "install_runtime",
                return_value={"status": "blocked", "reason": "install-failed"},
            ),
            mock.patch.object(watchdog, "kick_reconcile") as kick,
        ):
            result = watchdog.repair()
        self.assertEqual("blocked", result["status"])
        self.assertEqual("skipped", result["kick"]["status"])
        kick.assert_not_called()

    def test_repair_kicks_primary_service_after_runtime_install(self) -> None:
        with (
            mock.patch.object(watchdog, "refresh_checkout", return_value={"status": "current"}),
            mock.patch.object(watchdog, "install_runtime", return_value={"status": "ok"}),
            mock.patch.object(watchdog, "kick_reconcile", return_value={"status": "ok"}) as kick,
        ):
            result = watchdog.repair()
        self.assertEqual("ok", result["status"])
        kick.assert_called_once_with()

    def test_watchdog_units_are_bounded_and_persistent(self) -> None:
        root = Path(__file__).resolve().parents[1]
        service = (root / "systemd" / "github-autosync-watchdog.service").read_text()
        timer = (root / "systemd" / "github-autosync-watchdog.timer").read_text()
        self.assertIn("runtime_watchdog.py --repair", service)
        self.assertIn("TimeoutStartSec=4min", service)
        self.assertIn("OnUnitInactiveSec=5min", timer)
        self.assertIn("Persistent=true", timer)


if __name__ == "__main__":
    unittest.main()
