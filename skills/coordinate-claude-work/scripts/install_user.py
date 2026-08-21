#!/usr/bin/env python3.14
"""Install the skill and small global instruction blocks for one user."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

if sys.version_info < (3, 14):
    raise SystemExit("coordinate-claude-work requires Python 3.14 or newer")


START = "<!-- coordinate-claude-work-global:start -->"
END = "<!-- coordinate-claude-work-global:end -->"


def replace_managed_block(destination: Path, block: str) -> str:
    existing = destination.read_text(encoding="utf-8") if destination.exists() else ""
    if START in existing or END in existing:
        if existing.count(START) != 1 or existing.count(END) != 1:
            raise ValueError(f"malformed coordination markers in {destination}")
        start = existing.index(START)
        end = existing.index(END, start) + len(END)
        prefix = existing[:start].rstrip()
        return (prefix + "\n\n" if prefix else "") + block.strip() + existing[end:]
    if existing.strip():
        return existing.rstrip() + "\n\n" + block.strip() + "\n"
    return block.strip() + "\n"


def write_block(destination: Path, block_path: Path) -> bool:
    block = block_path.read_text(encoding="utf-8")
    updated = replace_managed_block(destination, block)
    current = destination.read_text(encoding="utf-8") if destination.exists() else ""
    if current == updated:
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(updated, encoding="utf-8")
    return True


def install(args: argparse.Namespace) -> int:
    skill = Path(__file__).resolve().parent.parent
    link = args.codex_dir.resolve() / "skills" / skill.name
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        if link.resolve() != skill:
            print(f"error: {link} points to {link.resolve()}, not {skill}", file=sys.stderr)
            return 2
    elif link.exists():
        print(f"error: refusing to replace existing path: {link}", file=sys.stderr)
        return 2
    else:
        link.symlink_to(skill, target_is_directory=True)
        print(f"Linked skill: {link} -> {skill}")

    global_assets = skill / "assets" / "global"
    destinations = (
        (args.codex_dir.resolve() / "AGENTS.md", global_assets / "AGENTS.block.md"),
        (args.claude_dir.resolve() / "CLAUDE.md", global_assets / "CLAUDE.block.md"),
    )
    for destination, block in destinations:
        try:
            changed = write_block(destination, block)
        except ValueError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        print(f"{'Updated' if changed else 'Current'}: {destination}")
    print("Restart Codex and Claude Code sessions to load the new global guidance.")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    user_root = Path(os.path.expanduser("~"))
    parser.add_argument("--codex-dir", type=Path, default=user_root / ".codex")
    parser.add_argument("--claude-dir", type=Path, default=user_root / ".claude")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(install(parse_args()))
