#!/usr/bin/env python3.14
"""Install the coordination workflow into a project without clobbering state."""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from .github_ci import configure_github_ci


START = "<!-- coordinate-claude-work:start -->"
END = "<!-- coordinate-claude-work:end -->"


def replace_managed_block(destination: Path, block: str) -> str:
    existing = destination.read_text(encoding="utf-8") if destination.exists() else ""
    if START in existing or END in existing:
        if existing.count(START) != 1 or existing.count(END) != 1:
            raise ValueError(f"malformed coordination markers in {destination}")
        start = existing.index(START)
        end = existing.index(END, start) + len(END)
        prefix = existing[:start].rstrip()
        updated = (prefix + "\n\n" if prefix else "") + block.strip() + existing[end:]
    elif existing.strip():
        updated = existing.rstrip() + "\n\n" + block.strip() + "\n"
    else:
        updated = block.strip() + "\n"
    return updated


def install(args: argparse.Namespace) -> int:
    target = args.target.resolve()
    if not target.is_dir():
        print(f"error: target is not a directory: {target}", file=sys.stderr)
        return 2

    template = Path(__file__).resolve().parent / "assets" / "project-template"
    if not template.is_dir():
        print(f"error: bundled template is missing: {template}", file=sys.stderr)
        return 2

    changed: list[Path] = []
    for filename in ("AGENTS.md", "CLAUDE.md"):
        block = (template / f"{filename.removesuffix('.md')}.block.md").read_text(
            encoding="utf-8"
        )
        destination = target / filename
        try:
            updated = replace_managed_block(destination, block)
        except ValueError as error:
            print(f"error: {error}", file=sys.stderr)
            return 2
        current = destination.read_text(encoding="utf-8") if destination.exists() else ""
        if current != updated:
            destination.write_text(updated, encoding="utf-8")
            changed.append(destination)

    source_coordination = template / ".coordination"
    for source in sorted(source_coordination.rglob("*")):
        relative = source.relative_to(source_coordination)
        if "__pycache__" in relative.parts or source.suffix in {".pyc", ".pyo"}:
            continue
        destination = target / ".coordination" / relative
        if source.is_dir():
            destination.mkdir(parents=True, exist_ok=True)
            continue
        if destination.exists():
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        content = source.read_text(encoding="utf-8").replace(
            "{{PROJECT_NAME}}", args.project_name
        )
        destination.write_text(content, encoding="utf-8")
        shutil.copymode(source, destination)
        changed.append(destination)

    if changed:
        print("Installed or updated:")
        for path in changed:
            print(f"  {path.relative_to(target)}")
    else:
        print("Coordination workflow is already current; no files changed.")

    try:
        ci_status = configure_github_ci(target, args.github_ci)
    except (OSError, ValueError) as error:
        print(f"error: GitHub CI setup failed: {error}", file=sys.stderr)
        return 2
    print(ci_status.message)
    if ci_status.requires_confirmation:
        print(
            "Re-run with --github-ci add to add it, or --github-ci skip to keep "
            "existing CI."
        )
    if ci_status.github_repository is None:
        print(
            "GitHub remote: not detected; the workflow will activate after this "
            "repository is pushed to GitHub."
        )
    else:
        print(f"GitHub repository: {ci_status.github_repository}")
    print("Next: complete .coordination/PROJECT.md and assign planner/current-task.md")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("target", type=Path, help="project directory")
    parser.add_argument("--project-name", required=True, help="human-readable name")
    parser.add_argument(
        "--github-ci",
        choices=("auto", "add", "skip"),
        default="auto",
        help="add Coordinator CI, skip it, or prompt when other workflows exist",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    return install(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
