import contextlib
import io
import unittest
from unittest.mock import patch
from project_nv import banner

class NVTests(unittest.TestCase):
    def test_interactive_and_machine_contracts(self):
        output=io.StringIO()
        with contextlib.redirect_stdout(output):banner([])
        self.assertEqual(output.getvalue(),'')
        output.isatty=lambda:True
        with contextlib.redirect_stdout(output):
            banner(['--json']);self.assertEqual(output.getvalue(),'')
            banner([])
        self.assertEqual(output.getvalue(),'NV 1\n')
        output.seek(0);output.truncate()
        with contextlib.redirect_stdout(output),patch('project_nv.Path.read_bytes',side_effect=OSError):banner([])
        self.assertEqual(output.getvalue(),'NV unavailable\n')
