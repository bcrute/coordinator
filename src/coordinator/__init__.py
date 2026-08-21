"""Coordinator: local run control for file-backed coding workflows."""

import sys

MINIMUM_PYTHON = (3, 14)

if sys.version_info < MINIMUM_PYTHON:
    raise RuntimeError("Coordinator requires Python 3.14 or newer")

__version__ = "0.3.0"
