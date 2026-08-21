#!/usr/bin/env python3.14
"""Compatibility launcher for the installed Coordinator application."""

from pathlib import Path
import sys

SOURCE = Path(__file__).resolve().parents[3] / "src"
if SOURCE.is_dir():
    sys.path.insert(0, str(SOURCE))

from coordinator.web_app import *  # noqa: F401,F403,E402
from coordinator.web_app import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
