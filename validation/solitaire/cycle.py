#!/usr/bin/env python3.14
"""Prepare and recoverably restart disposable Solitaire validation targets."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
TARGET_ASSETS = HERE / "target"
MARKER_NAME = ".coordinator-validation-target.json"
REPORT_DIRECTORY = ".coordinator-validation"
TERMINAL_OUTCOMES = {"passed", "failed", "blocked"}


class CycleError(ValueError):
    """A validation target failed a safety or report-contract check."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def require_git_repository(target: Path) -> Path:
    resolved = target.expanduser().resolve()
    if not resolved.is_dir():
        raise CycleError(f"target is not a directory: {resolved}")
    if not (resolved / ".git").is_dir():
        raise CycleError(f"target is not a Git repository: {resolved}")
    if resolved == HERE.parent.parent or resolved in HERE.parents:
        raise CycleError("the Coordinator source repository cannot be a disposable target")
    return resolved


def prepare_target(target: Path, cycle_id: int) -> dict[str, Any]:
    if cycle_id < 1:
        raise CycleError("cycle must be a positive integer")
    resolved = require_git_repository(target)
    marker_path = resolved / MARKER_NAME
    if marker_path.exists():
        marker = load_json(marker_path, "target marker")
        if marker.get("cycle_id") != cycle_id:
            raise CycleError(
                f"target is already assigned to cycle {marker.get('cycle_id')}"
            )

    report_dir = resolved / REPORT_DIRECTORY
    report_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(TARGET_ASSETS / "COORDINATOR_VALIDATION.md", resolved / "COORDINATOR_VALIDATION.md")
    shutil.copyfile(TARGET_ASSETS / "validation-brief.md", report_dir / "validation-brief.md")
    shutil.copyfile(HERE / "REPORTING.md", report_dir / "reporting.md")
    shutil.copyfile(HERE / "report.schema.json", report_dir / "report.schema.json")

    report_path = report_dir / "report.json"
    if not report_path.exists():
        report = load_json(TARGET_ASSETS / "report.template.json", "report template")
        report["cycle_id"] = cycle_id
        report["started_at"] = utc_now()
        write_json(report_path, report)

    marker = {
        "schema_version": 1,
        "cycle_id": cycle_id,
        "disposable": True,
        "target": str(resolved),
        "prepared_at": utc_now(),
    }
    write_json(marker_path, marker)
    return {
        "ok": True,
        "action": "prepare",
        "cycle_id": cycle_id,
        "target": str(resolved),
        "prompt": (TARGET_ASSETS / "START_PROMPT.md").read_text(encoding="utf-8").strip(),
    }


def load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise CycleError(f"{label} is missing: {path}") from error
    except json.JSONDecodeError as error:
        raise CycleError(f"{label} is not valid JSON: {path}: {error.msg}") from error
    if not isinstance(payload, dict):
        raise CycleError(f"{label} must contain a JSON object: {path}")
    return payload


def validated_marker(target: Path) -> dict[str, Any]:
    marker = load_json(target / MARKER_NAME, "target marker")
    if marker.get("schema_version") != 1 or marker.get("disposable") is not True:
        raise CycleError("target marker does not identify a supported disposable target")
    if marker.get("target") != str(target):
        raise CycleError("target marker path does not match the resolved target path")
    cycle_id = marker.get("cycle_id")
    if not isinstance(cycle_id, int) or isinstance(cycle_id, bool) or cycle_id < 1:
        raise CycleError("target marker has an invalid cycle_id")
    return marker


def validated_terminal_report(target: Path, cycle_id: int) -> dict[str, Any]:
    report = load_json(target / REPORT_DIRECTORY / "report.json", "validation report")
    required = {
        "schema_version",
        "cycle_id",
        "outcome",
        "stage",
        "summary",
        "started_at",
        "finished_at",
        "findings",
    }
    missing = sorted(required.difference(report))
    if missing:
        raise CycleError(f"validation report is missing fields: {', '.join(missing)}")
    if report.get("schema_version") != 1 or report.get("cycle_id") != cycle_id:
        raise CycleError("validation report does not match the target cycle")
    if report.get("outcome") not in TERMINAL_OUTCOMES:
        raise CycleError("validation report must have a terminal outcome before restart")
    if not isinstance(report.get("finished_at"), str) or not report["finished_at"].strip():
        raise CycleError("terminal validation report must include finished_at")
    if not isinstance(report.get("summary"), str) or not report["summary"].strip():
        raise CycleError("validation report summary must not be empty")
    if not isinstance(report.get("findings"), list):
        raise CycleError("validation report findings must be an array")
    return report


def coordination_locks(target: Path) -> list[Path]:
    coordination = target / ".coordination"
    if not coordination.is_dir():
        return []
    return sorted(
        path for path in coordination.rglob("*") if path.is_file() and path.name.endswith(".lock")
    )


def replace_with_clean_target(
    target: Path,
    archive_root: Path,
    archive_label: str,
    cycle_id: int,
) -> tuple[dict[str, Any], Path]:
    archive = archive_root.expanduser().resolve()
    if archive == target or target in archive.parents:
        raise CycleError("archive-root cannot be inside the disposable target")
    archive.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = archive / f"{target.name}-{archive_label}-{timestamp}"
    if destination.exists():
        raise CycleError(f"archive destination already exists: {destination}")

    shutil.move(str(target), str(destination))
    try:
        target.mkdir(parents=True)
        subprocess.run(
            ["git", "init", "--quiet", str(target)],
            check=True,
            capture_output=True,
            text=True,
        )
        prepared = prepare_target(target, cycle_id)
    except Exception:
        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(destination), str(target))
        raise
    return prepared, destination


def bootstrap_target(
    target: Path,
    archive_root: Path,
    confirmation: str,
) -> dict[str, Any]:
    resolved = require_git_repository(target)
    if confirmation != str(resolved):
        raise CycleError("confirmation must exactly match the resolved target path")
    if (resolved / MARKER_NAME).exists():
        raise CycleError("target is already protocol-managed; use restart")
    locks = coordination_locks(resolved)
    if locks:
        relative = ", ".join(str(path.relative_to(resolved)) for path in locks)
        raise CycleError(
            "coordination locks are still present; stop the session/watcher and preserve "
            f"or clear stale locks before bootstrap: {relative}"
        )
    prepared, destination = replace_with_clean_target(
        resolved,
        archive_root,
        "pre-protocol",
        1,
    )
    return {**prepared, "action": "bootstrap", "archive": str(destination)}


def restart_target(
    target: Path,
    archive_root: Path,
    next_cycle: int,
    confirmation: str,
) -> dict[str, Any]:
    if next_cycle < 1:
        raise CycleError("next-cycle must be a positive integer")
    resolved = require_git_repository(target)
    if confirmation != str(resolved):
        raise CycleError("confirmation must exactly match the resolved target path")
    marker = validated_marker(resolved)
    current_cycle = int(marker["cycle_id"])
    if next_cycle != current_cycle + 1:
        raise CycleError(f"next-cycle must be {current_cycle + 1}")
    report = validated_terminal_report(resolved, current_cycle)
    locks = coordination_locks(resolved)
    if locks:
        relative = ", ".join(str(path.relative_to(resolved)) for path in locks)
        raise CycleError(
            "coordination locks are still present; stop the session/watcher and preserve "
            f"or clear stale locks before restart: {relative}"
        )

    prepared, destination = replace_with_clean_target(
        resolved,
        archive_root,
        f"cycle-{current_cycle:04d}",
        next_cycle,
    )

    return {
        **prepared,
        "action": "restart",
        "previous_cycle": current_cycle,
        "previous_outcome": report["outcome"],
        "archive": str(destination),
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    prepare = commands.add_parser("prepare", help="install the contract into a disposable Git repository")
    prepare.add_argument("--target", type=Path, required=True)
    prepare.add_argument("--cycle", type=int, required=True)
    bootstrap = commands.add_parser("bootstrap", help="archive an exploratory target and create cycle 1")
    bootstrap.add_argument("--target", type=Path, required=True)
    bootstrap.add_argument("--archive-root", type=Path, required=True)
    bootstrap.add_argument("--confirm", required=True)
    restart = commands.add_parser("restart", help="archive a terminal cycle and create a fresh target")
    restart.add_argument("--target", type=Path, required=True)
    restart.add_argument("--archive-root", type=Path, required=True)
    restart.add_argument("--next-cycle", type=int, required=True)
    restart.add_argument("--confirm", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if args.action == "prepare":
            result = prepare_target(args.target, args.cycle)
        elif args.action == "bootstrap":
            result = bootstrap_target(args.target, args.archive_root, args.confirm)
        else:
            result = restart_target(
                args.target,
                args.archive_root,
                args.next_cycle,
                args.confirm,
            )
    except (CycleError, OSError, subprocess.SubprocessError) as error:
        print(json.dumps({"ok": False, "error": str(error)}))
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
