import contextlib
import io
import json
import unittest
from unittest.mock import patch
from project_nv import banner
import autosync_core

class NVTests(unittest.TestCase):
    def test_interactive_and_machine_contracts(self):
        out=io.StringIO();err=io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):banner([])
        self.assertEqual((out.getvalue(),err.getvalue()),('',''))
        out.isatty=lambda:True;err.isatty=lambda:True
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
            banner(['--json']);self.assertEqual(err.getvalue(),'')
            banner([])
            self.assertEqual(out.getvalue(),'');self.assertEqual(err.getvalue(),'NV 5\n')
        err.seek(0);err.truncate()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err),patch('project_nv.Path.read_bytes',side_effect=OSError):banner([])
        self.assertEqual(err.getvalue(),'NV unavailable\n')

    def test_json_run_without_flag_preserves_stdout(self):
        out=io.StringIO();err=io.StringIO()
        out.isatty=lambda:True;err.isatty=lambda:True
        def run(args):
            print('{"status":"PASS"}')
            return 0
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err),patch.object(autosync_core,'command_run',side_effect=run):
            self.assertEqual(autosync_core.main(['run']),0)
        self.assertEqual(json.loads(out.getvalue()),{'status':'PASS'})
        self.assertEqual(err.getvalue(),'NV 5\n')
