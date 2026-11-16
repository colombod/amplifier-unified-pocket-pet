"""``python -m pet_bridge``: a thin shim over ``cli.main`` (no logic of its own)."""

from __future__ import annotations

import os
import sys

from .cli import main

if __name__ == "__main__":
    raise SystemExit(
        main(sys.argv[1:], env=dict(os.environ), stdout=sys.stdout, stderr=sys.stderr)
    )
