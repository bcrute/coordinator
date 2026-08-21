"""Compatibility imports for Coordinator's PTY session manager."""

from pathlib import Path
import sys

SOURCE = Path(__file__).resolve().parents[3] / "src"
if SOURCE.is_dir():
    sys.path.insert(0, str(SOURCE))

from coordinator.codex_session import *  # noqa: F401,F403,E402
