#!/usr/bin/env python3.14
"""Render the coordination mailbox as a compact terminal dashboard."""

from __future__ import annotations

import json
import re
import shutil
import sys
import time
from pathlib import Path


TOKEN_FIELDS = (
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


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


def one_line(value: str | None) -> str:
    if not value:
        return "not recorded"
    return re.sub(r"\s+", " ", value).strip()


def load_json(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def load_executor_metrics(repo: Path, task_id: str | None = None) -> dict[str, object]:
    """Load normalized executor telemetry, with the Claude filename as fallback."""

    runtime = repo / ".coordination" / "runtime"
    generic = load_json(runtime / "executor-progress.json")
    legacy = load_json(runtime / "claude-progress.json")
    if task_id is not None:
        if generic.get("task_id") == task_id:
            return generic
        if legacy.get("task_id") == task_id:
            return legacy
    return generic or legacy


def ensure_goal_start(repo: Path, goal_id: str) -> float:
    path = repo / ".coordination" / "runtime" / "goal-timing.json"
    timing = load_json(path)
    started = timing.get("started_at_epoch")
    if timing.get("goal_id") == goal_id and isinstance(started, (int, float)):
        return float(started)
    started = time.time()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.dashboard.tmp")
    temporary.write_text(
        json.dumps({"goal_id": goal_id, "started_at_epoch": started}, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return started


def duration(seconds: float) -> str:
    elapsed = max(0, int(seconds))
    hours, remainder = divmod(elapsed, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def panel(title: str, rows: list[str], width: int) -> list[str]:
    inner = max(1, width - 2)
    label = f" {title} "
    top = "┌" + label + "─" * max(0, inner - len(label)) + "┐"
    body = ["│" + row[:inner].ljust(inner) + "│" for row in rows]
    return [top, *body, "└" + "─" * inner + "┘"]


def roadmap_rows(roadmap: str, accepted: int, task_id: str | None) -> list[str]:
    entries = [
        (int(number), one_line(title))
        for number, title in re.findall(r"^## Turn (\d+)\s+[—-]\s+(.+)$", roadmap, re.MULTILINE)
    ]
    task_match = re.search(r"(?:^|-)(\d+)(?:-|$)", task_id or "")
    current = int(task_match.group(1)) if task_match else None
    rows: list[str] = []
    for number, title in entries:
        marker = "[x]" if number <= accepted else "[>]" if number == current else "[ ]"
        rows.append(f"{marker} Turn {number}: {title}")
    return rows or ["[ ] No roadmap recorded"]


def acceptance_rows(task: str) -> list[str]:
    content = section(task, "Acceptance criteria")
    if not content:
        return ["[ ] No acceptance criteria recorded"]
    rows = [line.strip() for line in content.splitlines() if line.strip()]
    return ["[ ] " + re.sub(r"^-\s*", "", row) for row in rows]


def subagent_rows(metrics: dict[str, object], now: float) -> list[str]:
    agents = metrics.get("subagents")
    if not isinstance(agents, list) or not agents:
        lead = str(metrics.get("primary_model") or "Claude")
        worker = str(metrics.get("subagent_model") or "provider-selected")
        return [f"none active (native {lead} lead; {worker} workers available)"]
    rows: list[str] = []
    for value in agents[:3]:
        if not isinstance(value, dict):
            continue
        state = str(value.get("state") or "unknown")
        model = str(value.get("model") or metrics.get("subagent_model") or "inherited")
        description = one_line(str(value.get("description") or "Executor worker"))
        started = value.get("started_at_epoch")
        ended = value.get("completed_at_epoch") if state != "running" else now
        elapsed = ended - started if isinstance(started, (int, float)) and isinstance(ended, (int, float)) else 0
        agent_usage = value.get("usage") if isinstance(value.get("usage"), dict) else {}
        generated = agent_usage.get("output_tokens", 0)
        if not isinstance(generated, int):
            generated = 0
        rows.append(
            f"[{state}] {model} · {description}  {duration(elapsed)}  generated {generated:,}"
        )
    return rows or ["none active"]


def render(repo: Path, phase: str, detail: str = "") -> str:
    terminal = shutil.get_terminal_size((80, 24))
    width = max(60, terminal.columns)
    height = max(18, terminal.lines)

    goal = read(repo / ".coordination" / "planner" / "goal.md")
    task = read(repo / ".coordination" / "planner" / "current-task.md")
    coder = read(repo / ".coordination" / "coder" / "status.md")
    roadmap = read(repo / ".coordination" / "planner" / "roadmap.md")

    goal_id = field(goal, "Goal ID") or "none"
    goal_state = field(goal, "State") or "unknown"
    task_id = field(task, "Task ID") or "none"
    metrics = load_executor_metrics(repo, task_id)
    task_state = field(task, "State") or "unknown"
    task_round = field(task, "Review round") or "?"
    same_coder_handoff = (
        field(coder, "Task ID") == task_id and field(coder, "Review round") == task_round
    )
    current_coder = coder if same_coder_handoff else ""
    progress_match = re.search(
        r"-\s+(\d+) of (\d+) (?:planned )?(?:micro-)?subgoals accepted", goal
    )
    accepted = int(progress_match.group(1)) if progress_match else 0
    planned = int(progress_match.group(2)) if progress_match else 0
    progress_label = f"{accepted}/{planned}" if planned else "not recorded"

    overall_started = ensure_goal_start(repo, goal_id)
    now = time.time()
    same_task = metrics.get("task_id") == task_id
    metric_end = metrics.get("completed_at_epoch") if metrics.get("state") == "completed" else now
    if not isinstance(metric_end, (int, float)):
        metric_end = now
    turn_started = metrics.get("turn_started_epoch") if same_task else None
    objective_started = metrics.get("objective_started_epoch") if same_task else None
    turn_elapsed = metric_end - turn_started if isinstance(turn_started, (int, float)) else 0
    objective_elapsed = (
        metric_end - objective_started if isinstance(objective_started, (int, float)) else 0
    )
    usage = metrics.get("usage") if same_task and isinstance(metrics.get("usage"), dict) else {}
    token_counts = {
        key: value if isinstance((value := usage.get(key)), int) else 0
        for key in TOKEN_FIELDS
    }

    goal_rows = [
        f"Goal: {goal_id}  State: {goal_state.upper()}  Accepted: {progress_label}",
        f"Objective: {one_line(section(goal, 'Objective'))}",
    ]
    roadmap_display = roadmap_rows(roadmap, accepted, task_id)[:6]
    agents_display = subagent_rows(metrics if same_task else {}, now)
    fixed_lines = 17 + len(roadmap_display) + len(agents_display)
    objective_limit = max(1, height - fixed_lines)
    objectives = acceptance_rows(task)[:objective_limit]
    activity = one_line(section(current_coder, "Current activity"))
    current_rows = [
        f"{task_id}  State: {task_state}  Review round: {task_round}  Phase: {phase}",
        f"Activity: {activity}",
        *objectives,
    ]
    metric_rows = [
        f"Activity {duration(objective_elapsed)}  |  Turn {duration(turn_elapsed)}  |  "
        f"Overall {duration(now - overall_started)}  |  Generated {token_counts['output_tokens']:,}",
        f"New input {token_counts['input_tokens']:,}  |  "
        f"Cache read {token_counts['cache_read_input_tokens']:,}  |  "
        f"Cache write {token_counts['cache_creation_input_tokens']:,}",
    ]

    lines = [
        *panel("OVERALL GOAL", goal_rows, width),
        *panel("ROADMAP", roadmap_display, width),
        *panel("CURRENT TURN", current_rows, width),
        *panel("EXECUTOR WORKERS", agents_display, width),
        *panel("LIVE METRICS", metric_rows, width),
        f" {one_line(detail)}  •  Ctrl-C stops the watcher  •  Log: .coordination/runtime/relay.log",
    ]
    return "\n".join(lines[:height])


def enter() -> None:
    sys.stdout.write("\033[?1049h\033[?25l")
    sys.stdout.flush()


def draw(content: str) -> None:
    sys.stdout.write("\033[H")
    for line in content.splitlines():
        sys.stdout.write(f"\033[2K{line}\n")
    sys.stdout.write("\033[J")
    sys.stdout.flush()


def leave() -> None:
    sys.stdout.write("\033[?25h\033[?1049l")
    sys.stdout.flush()
