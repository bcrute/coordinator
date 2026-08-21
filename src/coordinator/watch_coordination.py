#!/usr/bin/env python3
"""Relay goal-driven handoffs between Claude and Codex through coordination files."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .coordination_dashboard import draw as draw_dashboard
from .coordination_dashboard import enter as enter_dashboard
from .coordination_dashboard import leave as leave_dashboard
from .coordination_dashboard import render as render_dashboard


ACTIVE_TASK_STATES = {"ready", "changes_requested"}
REVIEWABLE_CODER_STATES = {"review", "blocked"}
FINAL_VERDICTS = {"accepted", "changes_requested", "blocked"}


def field(text: str, name: str) -> str | None:
    match = re.search(rf"^- {re.escape(name)}:\s*`?([^`\n]+)`?\s*$", text, re.MULTILINE)
    return match.group(1).strip() if match else None


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


@dataclass(frozen=True)
class Snapshot:
    goal_id: str | None
    goal_state: str | None
    task_id: str | None
    task_state: str | None
    task_round: str | None
    coder_task_id: str | None
    coder_state: str | None
    coder_round: str | None
    review_task_id: str | None
    review_verdict: str | None
    review_round: str | None


def snapshot(repo: Path) -> Snapshot:
    goal = read(repo / ".coordination" / "planner" / "goal.md")
    task = read(repo / ".coordination" / "planner" / "current-task.md")
    coder = read(repo / ".coordination" / "coder" / "status.md")
    review = read(repo / ".coordination" / "reviews" / "latest.md")
    return Snapshot(
        field(goal, "Goal ID"),
        field(goal, "State"),
        field(task, "Task ID"),
        field(task, "State"),
        field(task, "Review round"),
        field(coder, "Task ID"),
        field(coder, "State"),
        field(coder, "Review round"),
        field(review, "Task ID"),
        field(review, "Verdict"),
        field(review, "Review round"),
    )


def next_action(state: Snapshot) -> tuple[str, str]:
    if not state.goal_id or state.goal_id == "none" or state.goal_state == "idle":
        return "wait", "no active overall goal"
    if state.goal_state == "done":
        return "done", f"overall goal {state.goal_id} is done"
    if state.goal_state == "blocked":
        return "blocked", f"overall goal {state.goal_id} is blocked"
    if state.goal_state != "active":
        return "error", f"unknown overall-goal state: {state.goal_state!r}"
    if not state.task_id or state.task_id == "none" or state.task_state == "idle":
        return "error", "overall goal is active but Codex has not assigned a subgoal"
    if state.task_state not in ACTIVE_TASK_STATES:
        return "error", f"active goal has non-runnable task state: {state.task_state!r}"

    same_coder_handoff = (
        state.coder_task_id == state.task_id and state.coder_round == state.task_round
    )
    if same_coder_handoff and state.coder_state in REVIEWABLE_CODER_STATES:
        same_review = (
            state.review_task_id == state.task_id and state.review_round == state.task_round
        )
        if same_review and state.review_verdict in FINAL_VERDICTS:
            return "error", "review verdict exists but Codex did not advance task/goal state"
        return "codex", f"Claude signaled {state.coder_state} for {state.task_id}"
    if same_coder_handoff and state.coder_state == "implementing":
        return "wait", "Claude handoff is marked implementing"
    return "claude", f"Codex assigned subgoal {state.task_id} ({state.task_state})"


def write_status(
    repo: Path, role: str, state: Snapshot, watcher_state: str, detail: str
) -> None:
    runtime = repo / ".coordination" / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    destination = runtime / f"watcher-{role}-status.json"
    temporary = runtime / f".watcher-{role}-status.{os.getpid()}.tmp"
    payload = {
        "role": role,
        "watcher_state": watcher_state,
        "detail": detail,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "coordination": asdict(state),
    }
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(destination)


def agent_command(args: argparse.Namespace, action: str) -> list[str]:
    if action == "claude":
        return [
            sys.executable,
            "-m",
            "coordinator.run_claude_turn",
            "--repo",
            str(args.repo),
            "--claude-command",
            args.claude_command,
            "--permission-mode",
            args.claude_permission_mode,
            "--model",
            args.claude_model,
            "--subagent-model",
            args.claude_subagent_model,
            "--max-turns",
            str(args.claude_max_turns),
        ]
    return [
        sys.executable,
        "-m",
        "coordinator.run_codex_review",
        "--repo",
        str(args.repo),
        "--codex-command",
        args.codex_command,
    ]


def role_handles(role: str, action: str) -> bool:
    return role == "both" or role == action


def watch(args: argparse.Namespace) -> int:
    args.repo = args.repo.resolve()
    coordination = args.repo / ".coordination"
    if not (coordination / "README.md").is_file():
        print(f"error: coordination workflow is missing from {args.repo}", file=sys.stderr)
        return 2
    if args.role in {"claude", "both"} and shutil.which(args.claude_command) is None:
        print(f"error: Claude command not found: {args.claude_command}", file=sys.stderr)
        return 127
    if args.role in {"codex", "both"} and shutil.which(args.codex_command) is None:
        print(f"error: Codex command not found: {args.codex_command}", file=sys.stderr)
        return 127

    runtime = coordination / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    lock_path = runtime / f"watcher-{args.role}.lock"
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        print(f"error: a {args.role} watcher may already be active: {lock_path}", file=sys.stderr)
        return 2

    error_state: Snapshot | None = None
    dashboard = (
        args.dashboard
        if args.dashboard is not None
        else sys.stdout.isatty() and not args.once and not args.dry_run
    )
    dashboard_active = False
    try:
        os.write(lock_fd, f"pid={os.getpid()} role={args.role} repo={args.repo}\n".encode())
        if dashboard:
            enter_dashboard()
            dashboard_active = True
        else:
            print(f"Watching {args.role} coordination signals in {args.repo} (Ctrl-C to stop)")
        while True:
            state = snapshot(args.repo)
            action, detail = next_action(state)
            if dashboard:
                draw_dashboard(render_dashboard(args.repo, action, detail))
            if action == "done":
                write_status(args.repo, args.role, state, "done", detail)
                if dashboard_active:
                    leave_dashboard()
                    dashboard_active = False
                print(f"GOAL DONE: {detail}")
                return 0
            if action == "blocked":
                write_status(args.repo, args.role, state, "blocked", detail)
                if dashboard_active:
                    leave_dashboard()
                    dashboard_active = False
                print(f"GOAL BLOCKED: {detail}")
                return 4
            if action == "error":
                write_status(args.repo, args.role, state, "error", detail)
                if not dashboard and error_state != state:
                    print(f"Coordination state error: {detail}", file=sys.stderr)
                error_state = state
                if args.once:
                    return 3
                time.sleep(args.interval)
                continue
            error_state = None
            if action == "wait" or not role_handles(args.role, action):
                waiting = detail if action == "wait" else f"waiting for {action} watcher: {detail}"
                write_status(args.repo, args.role, state, "waiting", waiting)
                if args.once:
                    print(f"No {args.role} relay action: {waiting}")
                    return 0
                time.sleep(args.interval)
                continue
            write_status(args.repo, args.role, state, "running", f"launching {action}: {detail}")
            if not dashboard:
                print(f"Signal: {detail}; launching {action}", flush=True)
            command = agent_command(args, action)
            if args.dry_run:
                print(" ".join(command))
                return 0
            if dashboard:
                log_path = runtime / "relay.log"
                with log_path.open("a", encoding="utf-8") as relay_log:
                    relay_log.write(
                        f"\n[{datetime.now(timezone.utc).isoformat()}] "
                        f"starting {action}: {detail}\n"
                    )
                    relay_log.flush()
                    child = subprocess.Popen(
                        command,
                        cwd=args.repo,
                        stdout=relay_log,
                        stderr=subprocess.STDOUT,
                        text=True,
                    )
                    while child.poll() is None:
                        live_detail = f"{action} running: {detail}"
                        write_status(
                            args.repo,
                            args.role,
                            snapshot(args.repo),
                            "running",
                            live_detail,
                        )
                        draw_dashboard(render_dashboard(args.repo, action, live_detail))
                        time.sleep(1.0)
                    returncode = child.wait()
            else:
                returncode = subprocess.run(command, cwd=args.repo, check=False).returncode
            if returncode != 0:
                failed_detail = f"{action} exited with status {returncode}"
                write_status(
                    args.repo,
                    args.role,
                    snapshot(args.repo),
                    "error",
                    failed_detail,
                )
                if dashboard_active:
                    leave_dashboard()
                    dashboard_active = False
                print(
                    f"RELAY STOPPED: {failed_detail}; inspect coordination state before restart",
                    file=sys.stderr,
                )
                return returncode
            else:
                write_status(
                    args.repo, args.role, snapshot(args.repo), "relayed", f"completed {action}"
                )
                if args.once:
                    return 0
    except KeyboardInterrupt:
        write_status(args.repo, args.role, snapshot(args.repo), "stopped", "stopped by operator")
        if dashboard_active:
            leave_dashboard()
            dashboard_active = False
        print("Watcher stopped.")
        return 130
    finally:
        if dashboard_active:
            leave_dashboard()
        os.close(lock_fd)
        lock_path.unlink(missing_ok=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="project root")
    parser.add_argument(
        "--role",
        choices=("claude", "codex", "both"),
        default="both",
        help="signals this watcher is allowed to relay",
    )
    parser.add_argument("--interval", type=float, default=2.0, help="poll interval in seconds")
    parser.add_argument("--once", action="store_true", help="process at most one pending relay")
    parser.add_argument("--dry-run", action="store_true", help="print the next relay command")
    parser.add_argument(
        "--dashboard",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="use the persistent terminal dashboard (default: auto-detect TTY)",
    )
    parser.add_argument("--claude-command", default="claude")
    parser.add_argument("--codex-command", default="codex")
    parser.add_argument(
        "--claude-permission-mode",
        choices=("auto", "default", "acceptEdits", "plan", "dontAsk"),
        default="auto",
    )
    parser.add_argument("--claude-model", default="opus")
    parser.add_argument("--claude-subagent-model", default="sonnet")
    parser.add_argument("--claude-max-turns", type=int, default=40)
    args = parser.parse_args()
    if args.interval <= 0:
        parser.error("--interval must be positive")
    if args.claude_max_turns <= 0:
        parser.error("--claude-max-turns must be positive")
    return args


if __name__ == "__main__":
    raise SystemExit(watch(parse_args()))
