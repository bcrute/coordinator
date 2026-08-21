#!/usr/bin/env python3.14
"""Validate the durable coordination file structure in CI."""

from __future__ import annotations

import sys
from pathlib import Path


REQUIRED_FILES = (
    "PROJECT.md",
    "README.md",
    "planner/goal.md",
    "planner/current-task.md",
    "coder/status.md",
    "coder/latest-report.md",
    "reviews/latest.md",
    "reviews/completion.md",
)
MANAGED_START = "<!-- coordinate-claude-work:start -->"
MANAGED_END = "<!-- coordinate-claude-work:end -->"


def validate(repo: Path) -> list[str]:
    errors: list[str] = []
    coordination = repo / ".coordination"
    for relative in REQUIRED_FILES:
        path = coordination / relative
        if not path.is_file():
            errors.append(f"missing .coordination/{relative}")
        elif not path.read_text(encoding="utf-8").strip():
            errors.append(f"empty .coordination/{relative}")
    for filename in ("AGENTS.md", "CLAUDE.md"):
        path = repo / filename
        if not path.is_file():
            errors.append(f"missing {filename}")
            continue
        text = path.read_text(encoding="utf-8")
        if (
            text.count(MANAGED_START) != 1
            or text.count(MANAGED_END) != 1
            or text.index(MANAGED_START) > text.index(MANAGED_END)
        ):
            errors.append(
                f"{filename} must contain one complete Coordinator-managed block"
            )
    return errors


def main() -> int:
    repo = Path(__file__).resolve().parents[2]
    errors = validate(repo)
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    print("Coordination files are structurally valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
