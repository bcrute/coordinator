#!/usr/bin/env python3.14
"""Run one handoff through the executor selected in Coordinator settings."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from .executor_settings import load_project_executor_settings


def run(args: argparse.Namespace) -> int:
    repo = args.repo.expanduser().resolve()
    if not repo.is_dir():
        print(json.dumps({"ok": False, "error": f"repository does not exist: {repo}"}))
        return 2
    try:
        configuration = load_project_executor_settings(repo)
    except (OSError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 2
    if args.executor != "configured":
        try:
            configuration = type(configuration).from_mapping(
                {"executor_adapter": args.executor}, configuration
            )
        except ValueError as error:
            print(json.dumps({"ok": False, "error": str(error)}))
            return 2
    adapter = configuration.adapter()
    command = adapter.command(repo)
    print(f"Configured executor: {adapter.display_name}", flush=True)
    if args.dry_run:
        print(" ".join(command), flush=True)
        return 0
    child_env = os.environ.copy()
    source_root = str(Path(__file__).resolve().parents[1])
    existing_path = child_env.get("PYTHONPATH", "")
    child_env["PYTHONPATH"] = (
        source_root if not existing_path else os.pathsep.join((source_root, existing_path))
    )
    return subprocess.run(command, cwd=repo, env=child_env, check=False).returncode


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="project root")
    parser.add_argument(
        "--executor",
        choices=("configured", "claude", "mini-swe-agent"),
        default="configured",
        help="use project settings, or an owner-requested one-turn executor override",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    return run(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
