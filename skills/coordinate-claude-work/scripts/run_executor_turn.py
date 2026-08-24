#!/usr/bin/env python3.14
"""Compatibility entry point for the configured Coordinator executor."""

from pathlib import Path
import sys

SOURCE = Path(__file__).resolve().parents[3] / "src"
if SOURCE.is_dir():
    sys.path.insert(0, str(SOURCE))

from coordinator.run_executor_turn import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
