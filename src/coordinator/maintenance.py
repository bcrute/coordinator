"""Backup, restore, rebuild, verify, and retain Coordinator operational data."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from .operational_store import OperationalStore
from .web_app import build_state


def _default_state_dir() -> Path:
    root = Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state")))
    return root / "coordinator"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=_default_state_dir())
    subcommands = parser.add_subparsers(dest="action", required=True)
    backup = subcommands.add_parser("backup", help="create and verify an online backup")
    backup.add_argument("destination", type=Path)
    restore = subcommands.add_parser("restore", help="verify and restore an online backup")
    restore.add_argument("source", type=Path)
    verify = subcommands.add_parser("verify", help="verify a backup or live index")
    verify.add_argument("path", type=Path, nargs="?")
    rebuild = subcommands.add_parser("rebuild", help="rebuild the index from repositories")
    rebuild.add_argument("repositories", type=Path, nargs="+")
    prune = subcommands.add_parser("prune", help="delete events older than a retention age")
    prune.add_argument("--days", type=float, required=True)
    subcommands.add_parser("compact", help="remove timer-only duplicate events and vacuum")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        store = OperationalStore(args.state_dir.expanduser())
        if args.action == "backup":
            path = store.backup(args.destination.expanduser())
            result = {"ok": True, "action": "backup", "path": str(path)}
        elif args.action == "restore":
            store.restore(args.source.expanduser())
            result = {"ok": True, "action": "restore", "path": str(store.path)}
        elif args.action == "verify":
            path = args.path.expanduser() if args.path else store.path
            store.verify_database(path)
            result = {"ok": True, "action": "verify", "path": str(path)}
        elif args.action == "rebuild":
            snapshots = []
            for repository in args.repositories:
                repo = repository.expanduser().resolve()
                if not repo.is_dir():
                    raise ValueError(f"repository does not exist: {repo}")
                snapshots.append((repo, build_state(repo)))
            count = store.rebuild(snapshots)
            result = {"ok": True, "action": "rebuild", "repositories": count}
        elif args.action == "prune":
            if args.days <= 0:
                raise ValueError("--days must be positive")
            count = store.prune_events(time.time() - args.days * 86400)
            result = {"ok": True, "action": "prune", "deleted": count}
        else:
            before = store.diagnostics()["database_bytes"]
            count = store.compact_events()
            store.vacuum()
            after = store.diagnostics()["database_bytes"]
            result = {
                "ok": True,
                "action": "compact",
                "deleted": count,
                "before_bytes": before,
                "after_bytes": after,
            }
    except (OSError, ValueError) as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
