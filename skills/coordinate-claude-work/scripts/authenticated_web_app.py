"""Compatibility imports for Coordinator's ASGI application."""

from pathlib import Path
import sys

SOURCE = Path(__file__).resolve().parents[3] / "src"
if SOURCE.is_dir():
    sys.path.insert(0, str(SOURCE))

from coordinator.authenticated_web_app import *  # noqa: F401,F403,E402
from coordinator.authenticated_web_app import (  # noqa: E402
    _local_destination,
    _validated_forwarded_allow_ips,
)
