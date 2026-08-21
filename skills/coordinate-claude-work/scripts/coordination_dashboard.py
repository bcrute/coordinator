"""Compatibility imports for Coordinator's terminal dashboard renderer."""

from pathlib import Path
import sys

SOURCE = Path(__file__).resolve().parents[3] / "src"
if SOURCE.is_dir():
    sys.path.insert(0, str(SOURCE))

from coordinator.coordination_dashboard import *  # noqa: F401,F403,E402
