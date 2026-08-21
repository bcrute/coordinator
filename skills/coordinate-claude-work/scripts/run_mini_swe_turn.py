#!/usr/bin/env python3.14
"""Compatibility launcher for a Coordinator mini-swe-agent handoff."""

from pathlib import Path
import sys

SOURCE = Path(__file__).resolve().parents[3] / "src"
if SOURCE.is_dir():
    sys.path.insert(0, str(SOURCE))

from coordinator.run_mini_swe_turn import *  # noqa: F401,F403,E402


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
