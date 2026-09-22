import importlib.util
import json
from pathlib import Path
from unittest import TestCase, mock


MODULE_PATH = Path(__file__).resolve().parents[1] / "configure_kuma.py"
SPEC = importlib.util.spec_from_file_location("configure_kuma", MODULE_PATH)
assert SPEC and SPEC.loader
configure_kuma = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(configure_kuma)


class ConfigureKumaTests(TestCase):
    def test_stores_push_url_in_secret_service_via_stdin(self) -> None:
        remote = mock.Mock(returncode=0, stdout=json.dumps({"id": 7, "token": "test-token", "updated": False))
        secret_store = mock.Mock(returncode=0)
        with mock.patch.object(configure_kuma.subprocess, "run", side_effect=[remote.return_value, secret_store.return_value]) as run:
            result = configure_kuma.configure()

        self.assertEqual({"id": 7, "updated": False}, result)
        command = run.call_args_list[1].args[0]
        self.assertEqual("secret-tool", command[0])
        self.assertEqual("store", command[1])
        self.assertEqual(
            ["application", "github-autosync", "credential", "github-reconcile-push-url"],
            command[-4:],
        )
        self.assertEqual("https://kuma.danielegalati.com/api/push/test-token\n", run.call_args_list[1].kwargs["input"])
        self.assertNotIn("test-token", command)
        self.assertEqual(configure_kuma.subprocess.DEVNULL, run.call_args_list[1].kwargs["stdout"])
        self.assertEqual(configure_kuma.subprocess.DEVNULL, run.call_args_list[1].kwargs["stderr"])
