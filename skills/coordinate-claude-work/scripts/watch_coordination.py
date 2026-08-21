#!/usr/bin/env python3
"""Compatibility launcher for the Coordinator watcher."""

from pathlib import Path
import sys

SOURCE = Path(__file__).resolve().parents[3] / "src"
if SOURCE.is_dir():
    sys.path.insert(0, str(SOURCE))

from coordinator.watch_coordination import *  # noqa: F401,F403,E402


if __name__ == "__main__":
    raise SystemExit(watch(parse_args()))
