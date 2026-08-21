#!/usr/bin/env python3.14
"""Run exactly one Claude Code implementation handoff for the active task."""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path


ACTIVE_STATES = {"ready", "changes_requested"}
TOKEN_FIELDS = (
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
)


def field(text: str, name: str) -> str | None:
    match = re.search(rf"^- {re.escape(name)}:\s*`?([^`\n]+)`?\s*$", text, re.MULTILINE)
    return match.group(1).strip() if match else None


def section(text: str, heading: str) -> str | None:
    match = re.search(
        rf"^## {re.escape(heading)}\s*$\n(.*?)(?=^## |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    content = match.group(1).strip() if match else ""
    return content or None


def progress(text: str) -> tuple[str, str] | None:
    current = section(text, "Current activity")
    objectives = section(text, "Turn objectives") or section(text, "Remaining this turn")
    if current is None and objectives is None:
        return None
    return current or "Not recorded.", objectives or "- None."


def handoff_progress(
    text: str, task_id: str, review_round: str | None
) -> tuple[str, str] | None:
    if field(text, "Task ID") != task_id or field(text, "Review round") != review_round:
        return None
    if field(text, "State") not in {"implementing", "review", "blocked"}:
        return None
    return progress(text)


def normalized_usage(value: object) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    return {
        name: count if isinstance((count := value.get(name)), int) else 0
        for name in TOKEN_FIELDS
    }


def apply_stream_event(
    line: str,
    running: dict[str, int],
    final: dict[str, int] | None,
    seen_message_ids: set[str],
    subagents: dict[str, dict[str, object]],
    now_epoch: float | None = None,
    default_subagent_model: str | None = None,
) -> dict[str, int] | None:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return final
    if not isinstance(event, dict):
        return final
    if event.get("type") == "result":
        return normalized_usage(event.get("usage")) or final
    message = event.get("message")
    if not isinstance(message, dict):
        return final

    timestamp = now_epoch if now_epoch is not None else time.time()
    content = message.get("content")
    if event.get("type") == "assistant" and isinstance(content, list):
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            if block.get("name") != "Agent" or not isinstance(block.get("id"), str):
                continue
            tool_id = block["id"]
            details = block.get("input") if isinstance(block.get("input"), dict) else {}
            subagents.setdefault(
                tool_id,
                {
                    "tool_use_id": tool_id,
                    "description": details.get("description") or "Claude subagent",
                    "subagent_type": details.get("subagent_type") or "unspecified",
                    "model": details.get("model") or default_subagent_model or "inherited",
                    "state": "running",
                    "started_at_epoch": timestamp,
                    "usage": {name: 0 for name in TOKEN_FIELDS},
                },
            )

    if event.get("type") == "user" and isinstance(content, list):
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tool_id = block.get("tool_use_id")
            if not isinstance(tool_id, str) or tool_id not in subagents:
                continue
            subagents[tool_id]["state"] = "error" if block.get("is_error") else "completed"
            subagents[tool_id]["completed_at_epoch"] = timestamp
        return final

    if event.get("type") != "assistant":
        return final
    message_id = message.get("id")
    if isinstance(message_id, str):
        if message_id in seen_message_ids:
            return final
        seen_message_ids.add(message_id)
    usage = normalized_usage(message.get("usage"))
    if usage is not None:
        for name, count in usage.items():
            running[name] += count
        parent_id = event.get("parent_tool_use_id")
        if isinstance(parent_id, str) and parent_id in subagents:
            agent_usage = subagents[parent_id]["usage"]
            if isinstance(agent_usage, dict):
                for name, count in usage.items():
                    agent_usage[name] = agent_usage.get(name, 0) + count
    return final


def format_usage(usage: dict[str, int]) -> str:
    processed = sum(usage.values())
    return (
        f"Generated tokens: {usage['output_tokens']:,} "
        f"(new input {usage['input_tokens']:,}; "
        f"cache read {usage['cache_read_input_tokens']:,}; "
        f"cache write {usage['cache_creation_input_tokens']:,}; "
        f"all categories processed {processed:,})"
    )


def format_duration(seconds: float) -> str:
    elapsed = max(0, int(seconds))
    hours, remainder = divmod(elapsed, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def format_dashboard(
    now_monotonic: float,
    now_epoch: float,
    objective_started: float,
    turn_started: float,
    overall_started: float,
    usage: dict[str, int],
) -> str:
    return (
        f"Activity {format_duration(now_monotonic - objective_started)} | "
        f"Turn {format_duration(now_monotonic - turn_started)} | "
        f"Overall {format_duration(now_epoch - overall_started)} | "
        f"Generated {usage['output_tokens']:,}"
    )


def ensure_overall_start(repo: Path, goal_id: str) -> float:
    timing_path = repo / ".coordination" / "runtime" / "goal-timing.json"
    try:
        timing = json.loads(timing_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        timing = {}
    started_at = timing.get("started_at_epoch") if isinstance(timing, dict) else None
    if timing.get("goal_id") == goal_id and isinstance(started_at, (int, float)):
        return float(started_at)

    started_at = time.time()
    timing_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = timing_path.with_name(f".{timing_path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(
            {"goal_id": goal_id, "started_at_epoch": started_at},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(timing_path)
    return started_at


def write_runtime_progress(
    repo: Path,
    goal_id: str,
    task_id: str,
    review_round: str | None,
    state: str,
    turn_started_epoch: float,
    objective_started_epoch: float,
    usage: dict[str, int],
    subagents: dict[str, dict[str, object]],
    primary_model: str,
    subagent_model: str,
    orchestration_mode: str = "native-subagents",
    completed_at_epoch: float | None = None,
) -> None:
    path = repo / ".coordination" / "runtime" / "claude-progress.json"
    payload: dict[str, object] = {
        "goal_id": goal_id,
        "task_id": task_id,
        "review_round": review_round,
        "state": state,
        "turn_started_epoch": turn_started_epoch,
        "objective_started_epoch": objective_started_epoch,
        "usage": usage,
        "subagents": list(subagents.values()),
        "primary_model": primary_model,
        "subagent_model": subagent_model,
        "orchestration_mode": orchestration_mode,
    }
    if completed_at_epoch is not None:
        payload["completed_at_epoch"] = completed_at_epoch
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def print_progress(update: tuple[str, str]) -> None:
    current, contract = update
    print("Current activity:", flush=True)
    print(current, flush=True)
    if contract != "- None.":
        print("Legacy turn checklist:", flush=True)
        print(contract, flush=True)


def dashboard_lines(update: tuple[str, str], metrics: str) -> list[str]:
    current, _legacy_contract = update
    return ["Current activity:", *current.splitlines(), metrics]


def redraw_dashboard(lines: list[str], previous_line_count: int) -> int:
    if previous_line_count:
        sys.stdout.write("\r")
        if previous_line_count > 1:
            sys.stdout.write(f"\033[{previous_line_count - 1}A")
    line_count = max(previous_line_count, len(lines))
    for index in range(line_count):
        sys.stdout.write("\033[2K")
        if index < len(lines):
            sys.stdout.write(lines[index])
        if index < line_count - 1:
            sys.stdout.write("\n")
    if previous_line_count > len(lines):
        sys.stdout.write(f"\033[{previous_line_count - len(lines)}A")
    sys.stdout.flush()
    return len(lines)


def read_events(stream: object, destination: queue.Queue[str]) -> None:
    if stream is None:
        return
    for line in stream:
        destination.put(line)


def handoff_prompt(task_id: str, review_round: str | None, task: str, team: bool = False) -> str:
    orchestration = (
        "Use Claude Code's native agent-team support when parallel collaboration would "
        "materially help. The lead owns integration and may run at most two teammates "
        "concurrently."
        if team
        else "Use Claude Code's native Sonnet subagents proactively when genuinely "
        "independent investigation, implementation, or verification would benefit. "
        "The lead owns integration and may run at most two subagents concurrently."
    )
    return f"""Implement exactly one bounded handoff: task {task_id}, review round {review_round}.

Claude Code already loads the repository's normal instructions and manages its own
context, tools, tasks, and agents. Do not preload the coordination history or reread
goal, roadmap, prior reports, or workflow documentation unless this assignment
explicitly references them or you discover a concrete missing decision. Treat the
assignment embedded below as the authoritative task packet.

{orchestration} Keep trivial or sequential work in the lead. Do not create a second
coordination system or manually mirror native agent task state.

Before product edits, replace `.coordination/coder/status.md` with a concise status
for this task and state `implementing`; a one-sentence `## Current activity` is
sufficient. At the end of this handoff, replace
`.coordination/coder/latest-report.md` with a truthful report and set coder status to
`review`, or `blocked` with the exact blocker. Do not edit planner or review files.
Do not commit, push, deploy, or mutate external systems unless the assignment
explicitly authorizes it.

<active-assignment>
{task.rstrip()}
</active-assignment>
"""


def run(args: argparse.Namespace) -> int:
    repo = args.repo.resolve()
    goal_path = repo / ".coordination" / "planner" / "goal.md"
    task_path = repo / ".coordination" / "planner" / "current-task.md"
    if not goal_path.is_file():
        print(f"error: coordination goal is missing: {goal_path}", file=sys.stderr)
        return 2
    goal = goal_path.read_text(encoding="utf-8")
    if field(goal, "State") != "active":
        print("error: overall goal must be active before running Claude", file=sys.stderr)
        return 2
    goal_id = field(goal, "Goal ID")
    if not goal_id or goal_id == "none":
        print("error: assign a stable Goal ID before running Claude", file=sys.stderr)
        return 2
    if not task_path.is_file():
        print(f"error: coordination task is missing: {task_path}", file=sys.stderr)
        return 2

    task = task_path.read_text(encoding="utf-8")
    state = field(task, "State")
    task_id = field(task, "Task ID")
    if state not in ACTIVE_STATES:
        print(
            f"error: task state must be one of {sorted(ACTIVE_STATES)}, got {state!r}",
            file=sys.stderr,
        )
        return 2
    if not task_id or task_id == "none":
        print("error: assign a stable Task ID before running Claude", file=sys.stderr)
        return 2

    review_round = field(task, "Review round")
    status_path = repo / ".coordination" / "coder" / "status.md"
    if status_path.is_file():
        coder_status = status_path.read_text(encoding="utf-8")
        same_handoff = (
            field(coder_status, "Task ID") == task_id
            and field(coder_status, "Review round") == review_round
        )
        coder_state = field(coder_status, "State")
        if same_handoff and coder_state in {"implementing", "review", "blocked"}:
            print(
                f"error: {task_id} round {review_round} is {coder_state}; "
                "Codex must review or update the assignment before another turn",
                file=sys.stderr,
            )
            return 2

    executable = shutil.which(args.claude_command)
    if executable is None and not args.dry_run:
        print(f"error: Claude Code command not found: {args.claude_command}", file=sys.stderr)
        return 127

    prompt = handoff_prompt(task_id, review_round, task)

    command = [
        executable or args.claude_command,
        "-p",
        "--model",
        args.model,
        "--permission-mode",
        args.permission_mode,
        "--max-turns",
        str(args.max_turns),
        "--output-format",
        "stream-json",
        "--verbose",
        "--forward-subagent-text",
        prompt,
    ]
    if args.dry_run:
        print("Would run one Claude turn:")
        print(f"Lead model: {args.model}; native subagent model: {args.subagent_model}")
        print(" ".join(command[:-1] + ["<coordination prompt>"]))
        return 0

    lock_path = repo / ".coordination" / ".claude-turn.lock"
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        print(
            f"error: another Claude turn may be active; inspect {lock_path}",
            file=sys.stderr,
        )
        return 2
    try:
        os.write(lock_fd, f"pid={os.getpid()} task={task_id} round={review_round}\n".encode())
        print(f"Starting Claude handoff: {task_id}", flush=True)
        child_env = os.environ.copy()
        child_env["CLAUDE_CODE_SUBAGENT_MODEL"] = args.subagent_model
        child = subprocess.Popen(
            command,
            cwd=repo,
            env=child_env,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        event_lines: queue.Queue[str] = queue.Queue()
        event_reader = threading.Thread(
            target=read_events,
            args=(child.stdout, event_lines),
            daemon=True,
        )
        event_reader.start()
        running_usage = {name: 0 for name in TOKEN_FIELDS}
        final_usage: dict[str, int] | None = None
        seen_message_ids: set[str] = set()
        subagents: dict[str, dict[str, object]] = {}
        last_progress: tuple[str, str] | None = None
        turn_started = time.monotonic()
        turn_started_epoch = time.time()
        objective_started = turn_started
        objective_started_epoch = turn_started_epoch
        overall_started = ensure_overall_start(repo, goal_id)
        live_dashboard = sys.stdout.isatty()
        dashboard_line_count = 0
        while child.poll() is None:
            while True:
                try:
                    line = event_lines.get_nowait()
                except queue.Empty:
                    break
                final_usage = apply_stream_event(
                    line,
                    running_usage,
                    final_usage,
                    seen_message_ids,
                    subagents,
                    default_subagent_model=args.subagent_model,
                )
            status = status_path.read_text(encoding="utf-8") if status_path.is_file() else ""
            update = handoff_progress(status, task_id, review_round)
            if update is not None and update != last_progress:
                if last_progress is None or update[0] != last_progress[0]:
                    objective_started = time.monotonic()
                    objective_started_epoch = time.time()
                if not live_dashboard:
                    print_progress(update)
                last_progress = update
            write_runtime_progress(
                repo,
                goal_id,
                task_id,
                review_round,
                "running",
                turn_started_epoch,
                objective_started_epoch,
                final_usage or running_usage,
                subagents,
                args.model,
                args.subagent_model,
            )
            if live_dashboard and last_progress is not None:
                now_monotonic = time.monotonic()
                dashboard_line_count = redraw_dashboard(
                    dashboard_lines(
                        last_progress,
                        format_dashboard(
                            now_monotonic,
                            time.time(),
                            objective_started,
                            turn_started,
                            overall_started,
                            final_usage or running_usage,
                        ),
                    ),
                    dashboard_line_count,
                )
            time.sleep(args.progress_interval)
        returncode = child.wait()
        event_reader.join()
        while True:
            try:
                line = event_lines.get_nowait()
            except queue.Empty:
                break
            final_usage = apply_stream_event(
                line,
                running_usage,
                final_usage,
                seen_message_ids,
                subagents,
                default_subagent_model=args.subagent_model,
            )
        status = status_path.read_text(encoding="utf-8") if status_path.is_file() else ""
        update = handoff_progress(status, task_id, review_round)
        if update is not None and update != last_progress:
            if last_progress is None or update[0] != last_progress[0]:
                objective_started = time.monotonic()
                objective_started_epoch = time.time()
            if not live_dashboard:
                print_progress(update)
            last_progress = update
        final_tokens = final_usage or running_usage
        completed_at_epoch = time.time()
        for agent in subagents.values():
            if agent.get("state") == "running":
                agent["state"] = "ended"
                agent["completed_at_epoch"] = completed_at_epoch
        write_runtime_progress(
            repo,
            goal_id,
            task_id,
            review_round,
            "completed",
            turn_started_epoch,
            objective_started_epoch,
            final_tokens,
            subagents,
            args.model,
            args.subagent_model,
            completed_at_epoch=completed_at_epoch,
        )
        final_metrics = format_dashboard(
            time.monotonic(),
            completed_at_epoch,
            objective_started,
            turn_started,
            overall_started,
            final_tokens,
        )
        if live_dashboard and last_progress is not None:
            redraw_dashboard(
                dashboard_lines(last_progress, final_metrics),
                dashboard_line_count,
            )
            print(flush=True)
        else:
            print(final_metrics, flush=True)
        print(format_usage(final_tokens), flush=True)
    finally:
        os.close(lock_fd)
        lock_path.unlink(missing_ok=True)
    if returncode == 0:
        coder_status = status_path.read_text(encoding="utf-8") if status_path.is_file() else ""
        valid_handoff = (
            field(coder_status, "Task ID") == task_id
            and field(coder_status, "Review round") == review_round
            and field(coder_status, "State") in {"review", "blocked"}
        )
        if not valid_handoff:
            print(
                "error: Claude exited successfully without signaling review or blocked "
                "for this task round",
                file=sys.stderr,
            )
            return 3
        print("Claude handoff ended. Codex review is required before another turn.")
    else:
        print(
            f"Claude exited with status {returncode}; inspect repository state before retrying.",
            file=sys.stderr,
        )
    return returncode


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="project root")
    parser.add_argument("--claude-command", default="claude", help="Claude executable")
    parser.add_argument(
        "--permission-mode",
        choices=("auto", "default", "acceptEdits", "plan", "dontAsk"),
        default="auto",
        help="Claude Code permission mode; bypass modes are intentionally unsupported",
    )
    parser.add_argument("--model", default="opus", help="Claude lead model")
    parser.add_argument(
        "--subagent-model",
        default="sonnet",
        help="model selected through CLAUDE_CODE_SUBAGENT_MODEL",
    )
    parser.add_argument("--max-turns", type=int, default=40, help="runaway safety cap")
    parser.add_argument(
        "--progress-interval",
        type=float,
        default=1.0,
        help="seconds between coder status checks",
    )
    parser.add_argument("--dry-run", action="store_true", help="validate without invoking Claude")
    args = parser.parse_args()
    if args.max_turns < 1:
        parser.error("--max-turns must be positive")
    if args.progress_interval <= 0:
        parser.error("--progress-interval must be positive")
    return args


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
