from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import unittest
from unittest import mock

import github_autosync as autosync


ROOT = Path(__file__).resolve().parents[1]


class ReconcileRuntimeTests(unittest.TestCase):
    def test_relation_policy(self) -> None:
        for ahead, behind, expected in ((0, 0, "synced"), (1, 0, "ahead"), (0, 3, "behind"), (2, 4, "diverged")):
            with self.subTest(ahead=ahead, behind=behind):
                self.assertEqual(expected, autosync.classify_relation(ahead, behind))

    def test_human_output_is_short_and_json_flag_is_available(self) -> None:
        payload = {"managed_repos": ["one", "two"], "issues": 1, "updated": 1, "pushed": 0,
                   "auto_pushed": 0, "reconcile_issues": [{"repo": "two", "kind": "rebase_conflict"}]}
        output = StringIO()
        with redirect_stdout(output):
            autosync._print_human_summary(payload)
        lines = output.getvalue().splitlines()
        self.assertEqual(3, len(lines))
        self.assertIn("2 repo", lines[0])
        self.assertNotIn("rebase_conflict", output.getvalue())
        self.assertTrue(autosync.build_parser().parse_args(["--json", "reconcile-all"]).json)
        payload["dry_run"] = True
        output = StringIO()
        with redirect_stdout(output):
            autosync._print_human_summary(payload)
        self.assertIn("verifica di 2 repo", output.getvalue())
        self.assertNotIn("Aggiornati", output.getvalue())

    def test_kuma_push_uses_result_without_exposing_token(self) -> None:
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): return None
            def read(self, size): return b'{"ok":true}'
        with mock.patch.dict("os.environ", {"GITHUB_RECONCILE_PUSH_URL": "https://kuma.invalid/api/push/private-token"}):
            with mock.patch.object(autosync, "urlopen", return_value=Response()) as send:
                self.assertTrue(autosync._push_kuma_heartbeat(False, 2, [{"kind": "rebase_conflict"}]))
        self.assertIn("status=down", send.call_args.args[0].full_url)
        self.assertEqual("github-autosync/kuma-heartbeat", send.call_args.args[0].get_header("User-agent"))
        self.assertNotIn("private-token", str(autosync._human_issue("rebase_conflict")))

    def test_units_run_single_calendar_scheduler(self) -> None:
        service = (ROOT / "systemd/github-autosync.service").read_text()
        timer = (ROOT / "systemd/github-autosync.timer").read_text()
        self.assertIn("ExecStart=/home/daniele/.local/bin/github-reconcile", service)
        self.assertIn("TimeoutStartSec=10min", service)
        self.assertIn("OnCalendar=*-*-* *:*:00", timer)
        self.assertIn("Persistent=true", timer)
        self.assertIn("AccuracySec=1s", timer)

    def test_no_automatic_destructive_git_commands(self) -> None:
        code = (ROOT / "autosync_core.py").read_text()
        for forbidden in ("reset --hard", "clean -fd", "--force-with-lease", "git push --force"):
            self.assertNotIn(forbidden, code)


if __name__ == "__main__":
    unittest.main()
