#!/usr/bin/env python3
"""
COMPATIBILITY WRAPPER — delegates to bin/sync-config.py.

The canonical source of project configuration is `mcp-project.yaml` at the
repository root. `make.env` is regenerated from it via:

    python bin/sync-config.py

This file is kept for backward compatibility with documentation and tooling
that still reference `bin/configure-make.py`. New workflows should call
`sync-config.py` directly.
"""

import os
import sys
from pathlib import Path

if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    sync = here / "sync-config.py"
    if not sync.exists():
        print(f"error: {sync} not found — this scaffold appears incomplete",
              file=sys.stderr)
        sys.exit(2)
    os.execv(sys.executable, [sys.executable, str(sync), *sys.argv[1:]])