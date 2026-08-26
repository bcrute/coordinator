#!/usr/bin/env python3.14
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
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from .coordination_dashboard import draw as draw_dashboard
from .coordination_dashboard import enter as enter_dashboard
from .coordination_dashboard import leave as leave_dashboard
from .coordination_dashboard import render as render_dashboard
from .coordination_locks import acquire_lock, active_lock
from .executor_adapters import (
    EXECUTOR_ADAPTERS,
    ClaudeExecutorAdapter,
    ExecutorAdapter,
    MiniSweAgentExecutorAdapter,
    from_namespace,
)
from .executor_adapters import resolve_executable as resolve_executor_executable
from .executor_settings import ExecutorConfiguration, load_project_executor_settings
from .handoff_policy import load_handoff_configuration, validate_handoff_task
from .process_guard import guarded_command


ACTIVE_TASK_STATES = {"ready", "changes_requested"}
REVIEWABLE_CODER_STATES = {"review", "blocked"}
FINAL_VERDICTS = {"accepted", "changes_requested", "blocked"}
TASK_EXECUTORS = {"configured", *EXECUTOR_ADAPTERS}


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
    task_executor: str | None
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
        field(task, "Executor") or "configured",
        field(coder, "Task ID"),
        field(coder, "State"),
        field(coder, "Review round"),
        field(review, "Task ID"),
        field(review, "Verdict"),
        field(review, "Review round"),
    )


def next_action(state: Snapshot, repo: Path | None = None) -> tuple[str, str]:
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
    if state.task_executor not in TASK_EXECUTORS:
        return "error", f"unknown task executor: {state.task_executor!r}"

    same_coder_handoff = (
        state.coder_task_id == state.task_id and state.coder_round == state.task_round
    )
    if same_coder_handoff and state.coder_state in REVIEWABLE_CODER_STATES:
        same_review = (
            state.review_task_id == state.task_id and state.review_round == state.task_round
        )
        if same_review and state.review_verdict in FINAL_VERDICTS:
            return "error", "review verdict exists but Codex did not advance task/goal state"
        return "codex", f"executor signaled {state.coder_state} for {state.task_id}"
    if same_coder_handoff and state.coder_state == "implementing":
        if repo is not None:
            turn_locks = (
                repo / ".coordination" / ".claude-turn.lock",
                repo / ".coordination" / ".mini-swe-agent-turn.lock",
            )
            if not any(active_lock(path, reclaim_stale=True) for path in turn_locks):
                return "executor", "recovering an interrupted executor handoff"
        return "wait", "executor handoff is marked implementing"
    return "executor", f"Codex assigned subgoal {state.task_id} ({state.task_state})"


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


def agent_command(
    args: argparse.Namespace, action: str, executor: ExecutorAdapter | None = None
) -> list[str]:
    if action == "executor":
        return (executor or from_namespace(args)).command(args.repo)
    return [
        sys.executable,
        "-m",
        "coordinator.run_codex_review",
        "--repo",
        str(args.repo),
        "--codex-command",
        args.codex_command,
        "--primary-adapter",
        args.primary_adapter,
        "--claude-command",
        args.claude_command,
        "--claude-model",
        args.primary_claude_model,
        "--claude-max-turns",
        str(args.primary_claude_max_turns),
        "--mini-command",
        args.mini_swe_command,
        "--primary-local-model",
        args.primary_local_model,
        "--primary-local-step-limit",
        str(args.primary_local_step_limit),
        "--primary-local-timeout-seconds",
        str(args.primary_local_timeout_seconds),
        "--local-provider",
        args.mini_swe_provider,
        "--local-api-key-env",
        args.mini_swe_api_key_env,
        "--local-cost-limit",
        str(args.mini_swe_cost_limit),
    ] + (["--model", args.codex_model] if args.codex_model else []) + (
        ["--effort", args.codex_effort] if args.codex_effort else []
    ) + (["--claude-effort", args.primary_claude_effort] if args.primary_claude_effort else []) + (
        ["--primary-local-effort", args.primary_local_effort]
        if args.primary_local_effort else []
    ) + (["--local-api-base", args.mini_swe_api_base] if args.mini_swe_api_base else []) + (
        ["--mini-config", str(args.mini_swe_config)] if args.mini_swe_config else []
    )


def role_handles(role: str, action: str) -> bool:
    # ``claude`` remains a compatibility alias for the implementation side.
    return role == "both" or role == action or (role == "claude" and action == "executor")


def task_executor(
    repo: Path, requested: str | None, configured: ExecutorAdapter
) -> ExecutorAdapter:
    """Resolve a validated one-handoff override without changing saved settings."""

    selected = requested or "configured"
    try:
        configuration = load_project_executor_settings(repo)
    except ValueError:
        if selected == "configured":
            return configured
        raise
    if selected == "configured":
        resolved = configuration.adapter()
        if isinstance(resolved, ClaudeExecutorAdapter) and isinstance(
            configured, ClaudeExecutorAdapter
        ):
            return replace(
                resolved,
                command_name=configured.command_name,
                permission_mode=configured.permission_mode,
                delegate_command_name=configured.delegate_command_name,
                delegate_config=configured.delegate_config,
            )
        if isinstance(resolved, MiniSweAgentExecutorAdapter) and isinstance(
            configured, MiniSweAgentExecutorAdapter
        ):
            return replace(
                resolved,
                command_name=configured.command_name,
                config=configured.config,
            )
        return resolved
    if selected not in EXECUTOR_ADAPTERS:
        raise ValueError(f"unknown task executor: {selected!r}")
    return ExecutorConfiguration.from_mapping(
        {"executor_adapter": selected}, configuration
    ).adapter()


def watch(args: argparse.Namespace) -> int:
    args.repo = args.repo.resolve()
    try:
        executor = from_namespace(args)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    coordination = args.repo / ".coordination"
    if not (coordination / "README.md").is_file():
        print(f"error: coordination workflow is missing from {args.repo}", file=sys.stderr)
        return 2
    primary_command = (
        args.codex_command
        if args.primary_adapter == "codex"
        else args.claude_command
        if args.primary_adapter == "claude"
        else args.mini_swe_command
    )
    if args.role in {"codex", "both"} and shutil.which(primary_command) is None:
        command = primary_command
        print(f"error: primary command not found: {command}", file=sys.stderr)
        return 127

    runtime = coordination / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    lock_path = runtime / f"watcher-{args.role}.lock"
    try:
        lock_fd = acquire_lock(
            lock_path, f"pid={os.getpid()} role={args.role} repo={args.repo}\n"
        )
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
        if dashboard:
            enter_dashboard()
            dashboard_active = True
        else:
            print(f"Watching {args.role} coordination signals in {args.repo} (Ctrl-C to stop)")
        while True:
            state = snapshot(args.repo)
            action, detail = next_action(state, args.repo)
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
            selected_executor = executor
            if action == "executor":
                try:
                    project_configuration = load_handoff_configuration(
                        args.repo,
                        ExecutorConfiguration.from_adapter(executor),
                    )
                    validate_handoff_task(
                        read(args.repo / ".coordination" / "planner" / "current-task.md"),
                        project_configuration,
                        state.task_executor or "configured",
                    )
                    selected_executor = task_executor(
                        args.repo, state.task_executor, executor
                    )
                except ValueError as error:
                    failed_detail = f"cannot launch executor handoff: {error}"
                    write_status(args.repo, args.role, state, "error", failed_detail)
                    print(f"Coordination state error: {failed_detail}", file=sys.stderr)
                    return 3
                if resolve_executor_executable(selected_executor) is None:
                    failed_detail = (
                        f"{selected_executor.display_name} command not found: "
                        f"{selected_executor.executable()}"
                    )
                    write_status(args.repo, args.role, state, "error", failed_detail)
                    print(f"error: {failed_detail}", file=sys.stderr)
                    return 127
            command = agent_command(args, action, selected_executor)
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
                        guarded_command(command),
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
                returncode = subprocess.run(
                    guarded_command(command), cwd=args.repo, check=False
                ).returncode
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
        choices=("executor", "claude", "codex", "both"),
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
        "--primary-adapter",
        choices=("codex", "claude", "mini-swe-agent"),
        default="codex",
    )
    parser.add_argument("--primary-claude-model", default="opus")
    parser.add_argument(
        "--primary-claude-effort",
        choices=("low", "medium", "high", "xhigh", "max"),
        default="",
    )
    parser.add_argument("--primary-claude-max-turns", type=int, default=40)
    parser.add_argument("--primary-local-model", default="")
    parser.add_argument(
        "--primary-local-effort",
        choices=("low", "medium", "high", "xhigh", "max"),
        default="",
    )
    parser.add_argument("--primary-local-step-limit", type=int, default=24)
    parser.add_argument("--primary-local-timeout-seconds", type=int, default=900)
    parser.add_argument("--codex-model", default="")
    parser.add_argument(
        "--codex-effort",
        choices=("none", "low", "medium", "high", "xhigh", "max", "ultra"),
        default="",
    )
    parser.add_argument(
        "--claude-permission-mode",
        choices=("auto", "default", "acceptEdits", "plan", "dontAsk"),
        default="auto",
    )
    parser.add_argument("--claude-model", default="opus")
    parser.add_argument(
        "--claude-effort",
        choices=("low", "medium", "high", "xhigh", "max"),
        default="",
    )
    parser.add_argument("--claude-subagent-model", default="sonnet")
    parser.add_argument(
        "--claude-subagent-effort",
        choices=("low", "medium", "high", "xhigh", "max"),
        default="",
    )
    parser.add_argument("--claude-max-turns", type=int, default=40)
    parser.add_argument(
        "--claude-local-delegation",
        action=argparse.BooleanOptionalAction,
        default=False,
    )
    parser.add_argument(
        "--executor-adapter",
        choices=EXECUTOR_ADAPTERS,
        default="claude",
        help="implementation runtime (default: claude)",
    )
    parser.add_argument("--mini-swe-command", default="mini")
    parser.add_argument("--mini-swe-model", default="")
    parser.add_argument(
        "--mini-swe-profile",
        choices=("bounded", "exploratory"),
        default="bounded",
    )
    parser.add_argument(
        "--mini-swe-effort",
        choices=("low", "medium", "high", "xhigh", "max"),
        default="",
    )
    parser.add_argument("--mini-swe-config", type=Path)
    parser.add_argument("--mini-swe-api-base", default="")
    parser.add_argument("--mini-swe-provider", default="openai")
    parser.add_argument("--mini-swe-api-key-env", default="OPENAI_API_KEY")
    parser.add_argument("--mini-swe-step-limit", type=int, default=12)
    parser.add_argument("--mini-swe-cost-limit", type=float, default=0.0)
    parser.add_argument("--mini-swe-timeout-seconds", type=int, default=900)
    args = parser.parse_args()
    if args.interval <= 0:
        parser.error("--interval must be positive")
    if args.claude_max_turns <= 0:
        parser.error("--claude-max-turns must be positive")
    if args.primary_claude_max_turns <= 0:
        parser.error("--primary-claude-max-turns must be positive")
    if args.primary_local_step_limit <= 0:
        parser.error("--primary-local-step-limit must be positive")
    if args.primary_local_timeout_seconds <= 0:
        parser.error("--primary-local-timeout-seconds must be positive")
    if args.mini_swe_step_limit <= 0:
        parser.error("--mini-swe-step-limit must be positive")
    if args.mini_swe_cost_limit < 0:
        parser.error("--mini-swe-cost-limit must not be negative")
    if args.mini_swe_timeout_seconds <= 0:
        parser.error("--mini-swe-timeout-seconds must be positive")
    return args


if __name__ == "__main__":
    raise SystemExit(watch(parse_args()))
