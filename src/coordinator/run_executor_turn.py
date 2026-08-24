#!/usr/bin/env python3.14
"""Run one handoff through the executor selected in Coordinator settings."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from .executor_adapters import ClaudeExecutorAdapter
from .executor_settings import EXECUTOR_PREFERENCE_KEY, ExecutorSettingsService
from .operational_store import OperationalStore


def default_state_dir() -> Path:
    root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    return root / "coordinator"


def run(args: argparse.Namespace) -> int:
    repo = args.repo.expanduser().resolve()
    state_dir = args.state_dir.expanduser().resolve()
    database = state_dir / "operations.sqlite3"
    if not repo.is_dir():
        print(json.dumps({"ok": False, "error": f"repository does not exist: {repo}"}))
        return 2
    if not database.is_file():
        print(json.dumps({"ok": False, "error": f"Coordinator settings do not exist: {database}"}))
        return 2

    store = OperationalStore(state_dir)
    if EXECUTOR_PREFERENCE_KEY not in store.preferences():
        print(json.dumps({"ok": False, "error": "no executor is saved in Coordinator settings"}))
        return 2
    service = ExecutorSettingsService(store, ClaudeExecutorAdapter())
    if service.load_warning:
        print(json.dumps({"ok": False, "error": service.load_warning}))
        return 2
    adapter = service.adapter()
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
    parser.add_argument("--state-dir", type=Path, default=default_state_dir())
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    return run(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
