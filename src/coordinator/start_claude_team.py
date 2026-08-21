#!/usr/bin/env python3
"""Start one native interactive Claude agent-team handoff for the active task."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from .run_claude_turn import ACTIVE_STATES, field, handoff_prompt


def run(args: argparse.Namespace) -> int:
    repo = args.repo.resolve()
    goal_path = repo / ".coordination" / "planner" / "goal.md"
    task_path = repo / ".coordination" / "planner" / "current-task.md"
    goal_is_active = goal_path.is_file() and field(
        goal_path.read_text(encoding="utf-8"), "State"
    ) == "active"
    if not goal_is_active:
        print("error: coordination goal must exist and be active", file=sys.stderr)
        return 2
    if not task_path.is_file():
        print(f"error: coordination task is missing: {task_path}", file=sys.stderr)
        return 2

    task = task_path.read_text(encoding="utf-8")
    task_id = field(task, "Task ID")
    task_state = field(task, "State")
    review_round = field(task, "Review round")
    if not task_id or task_id == "none" or task_state not in ACTIVE_STATES:
        print("error: active task must have a stable ID and runnable state", file=sys.stderr)
        return 2

    status_path = repo / ".coordination" / "coder" / "status.md"
    if status_path.is_file():
        status = status_path.read_text(encoding="utf-8")
        same_handoff = (
            field(status, "Task ID") == task_id
            and field(status, "Review round") == review_round
        )
        if same_handoff and field(status, "State") in {"implementing", "review", "blocked"}:
            print(
                f"error: {task_id} round {review_round} already has an active or "
                "reviewable coder signal",
                file=sys.stderr,
            )
            return 2

    executable = shutil.which(args.claude_command)
    if executable is None:
        print(f"error: Claude Code command not found: {args.claude_command}", file=sys.stderr)
        return 127

    prompt = handoff_prompt(task_id, review_round, task, team=True) + """

This is one interactive team handoff. Once you have written the final coder report
and `review` or `blocked` status, do not begin a newly assigned task in this same
session. Tell the owner the handoff signal is ready and wait for them to exit.
"""
    command = [
        executable,
        "--model",
        args.model,
        "--permission-mode",
        args.permission_mode,
        "--teammate-mode",
        args.teammate_mode,
        prompt,
    ]
    if args.dry_run:
        print("Would start one native interactive Claude team handoff:")
        print("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1")
        print(f"CLAUDE_CODE_SUBAGENT_MODEL={args.teammate_model}")
        print(" ".join(command[:-1] + ["<active task packet>"]))
        return 0
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(
            "error: native Claude agent teams require an interactive terminal",
            file=sys.stderr,
        )
        return 2

    lock_path = repo / ".coordination" / ".claude-turn.lock"
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        print(f"error: another Claude handoff may be active: {lock_path}", file=sys.stderr)
        return 2
    try:
        os.write(lock_fd, f"pid={os.getpid()} task={task_id} round={review_round} team=1\n".encode())
        environment = os.environ.copy()
        environment["CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS"] = "1"
        environment["CLAUDE_CODE_SUBAGENT_MODEL"] = args.teammate_model
        print(
            f"Starting native Claude team handoff: {task_id} "
            f"({args.model} lead; {args.teammate_model} teammates)",
            flush=True,
        )
        return subprocess.run(command, cwd=repo, env=environment, check=False).returncode
    finally:
        os.close(lock_fd)
        lock_path.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="project root")
    parser.add_argument("--claude-command", default="claude")
    parser.add_argument("--model", default="opus", help="team-lead model")
    parser.add_argument("--teammate-model", default="sonnet", help="native teammate model")
    parser.add_argument(
        "--teammate-mode",
        choices=("in-process", "auto", "tmux", "iterm2"),
        default="in-process",
        help="Claude Code native teammate display mode",
    )
    parser.add_argument(
        "--permission-mode",
        choices=("auto", "default", "acceptEdits", "plan", "dontAsk"),
        default="auto",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
