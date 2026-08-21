"""Read-only installation and repository diagnostics for Coordinator."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from . import web_app


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str
    required: bool = True


def _command(name: str, *, required: bool) -> Check:
    executable = shutil.which(name)
    return Check(
        name,
        "pass" if executable else "warn",
        executable or f"{name} is not available on PATH",
        required,
    )


def evaluate(args: argparse.Namespace) -> list[Check]:
    repo = Path(args.repo).expanduser().resolve()
    root = Path(args.repositories_root).expanduser().resolve()
    marker = repo / ".coordination" / "README.md"
    return [
        Check("python", "pass", sys.version.split()[0]),
        _command("git", required=True),
        _command("codex", required=False),
        _command("claude", required=False),
        Check(
            "repository",
            "pass" if repo.is_dir() else "fail",
            str(repo) if repo.is_dir() else f"directory does not exist: {repo}",
        ),
        Check(
            "repositories_root",
            "pass" if root.is_dir() else "fail",
            str(root) if root.is_dir() else f"directory does not exist: {root}",
        ),
        Check(
            "coordination",
            "pass" if marker.is_file() else "warn",
            "coordination initialized" if marker.is_file() else "coordination is not initialized",
            False,
        ),
    ]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--repositories-root", type=Path)
    parser.add_argument("--json", action="store_true", help="emit machine-readable output")
    args = parser.parse_args(argv)

    forwarded: list[str] = []
    if args.config is not None:
        forwarded.extend(("--config", str(args.config)))
    if args.repo is not None:
        forwarded.extend(("--repo", str(args.repo)))
    if args.repositories_root is not None:
        forwarded.extend(("--repositories-root", str(args.repositories_root)))
    resolved = web_app.parse_args(forwarded)
    args.repo = resolved.repo
    args.repositories_root = resolved.repositories_root or Path(resolved.repo).resolve().parent
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    checks = evaluate(args)
    failed = any(check.required and check.status == "fail" for check in checks)
    if args.json:
        print(json.dumps({"ok": not failed, "checks": [asdict(check) for check in checks]}, indent=2))
    else:
        for check in checks:
            print(f"{check.status.upper():4}  {check.name:20} {check.detail}")
    return 1 if failed else 0
