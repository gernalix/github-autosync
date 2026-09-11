#!/usr/bin/env python3
from __future__ import annotations

import sys
import autosync_core as _core

if __name__ == "__main__":
    raise SystemExit(_core.main())

# Preserve the historical import surface for tests and callers while keeping
# the implementation isolated in autosync_core.py.
sys.modules[__name__] = _core
