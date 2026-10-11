"""Interactive release display from the tracked project root."""
from pathlib import Path
import re
import sys


def banner(args):
    if not sys.stdout.isatty() or '--json' in args:
        return
    root=Path(__file__).resolve().parent
    try:
        raw=(root/'NV.txt').read_bytes()
        if not re.fullmatch(rb'[1-9][0-9]*\n',raw) or (root/'version.txt').exists():
            raise ValueError('inconsistent source')
        label='NV '+raw[:-1].decode('ascii')
    except (OSError,ValueError):
        label='NV unavailable'
    print(label)
