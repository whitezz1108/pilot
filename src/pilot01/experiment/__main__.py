"""``python -m pilot01.experiment`` -- see :mod:`pilot01.experiment.cli`."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
