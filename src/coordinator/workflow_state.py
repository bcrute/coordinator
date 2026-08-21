"""Parse portable coordination files into stable dashboard state."""

from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from pathlib import Path

from .coordination_dashboard import TOKEN_FIELDS, duration, field, load_executor_metrics, load_json
from .coordination_dashboard import one_line, read, section

WATCHER_ROLES = ("executor", "claude", "codex", "both")
RELAY_LOG_BYTES = 256 * 1024


def bullets(text: str, heading: str) -> list[str]:
    content = section(text, heading)
    if not content:
        return []
    items: list[str] = []
    wrapping = False
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            wrapping = False
            continue
        if re.match(r"^[-*](?:\s|$)", stripped):
            items.append(stripped[1:].strip())
        elif wrapping and items:
            items[-1] = f"{items[-1]} {stripped}"
        else:
            items.append(stripped)
        wrapping = True
    return [re.sub(r"\s+", " ", item).strip() for item in items]


def number(value: object) -> float | None:
    numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
    return float(value) if numeric else None


def elapsed(started: object, ended: object) -> dict[str, object]:
    start = number(started)
    end = number(ended)
    seconds = end - start if start is not None and end is not None else 0.0
    seconds = max(0.0, seconds)
    return {"seconds": round(seconds, 3), "display": duration(seconds)}


def tail(path: Path, limit: int) -> dict[str, object]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - RELAY_LOG_BYTES))
            data = handle.read()
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError, OSError):
        return {"path": str(path), "available": False, "lines": [], "truncated": False}
    text = data.decode("utf-8", errors="replace")
    if size > RELAY_LOG_BYTES:
        text = text.split("\n", 1)[-1]
    lines = text.splitlines()
    return {
        "path": str(path),
        "available": True,
        "lines": lines[-limit:] if limit > 0 else [],
        "truncated": size > RELAY_LOG_BYTES or len(lines) > max(0, limit),
    }


def goal_state(text: str) -> dict[str, object]:
    match = re.search(r"-\s+(\d+) of (\d+) (?:planned )?(?:micro-)?subgoals accepted", text)
    accepted = int(match.group(1)) if match else 0
    planned = int(match.group(2)) if match else 0
    return {
        "id": field(text, "Goal ID") or "none",
        "state": field(text, "State") or "unknown",
        "starting_ref": field(text, "Starting ref") or "not recorded",
        "target_branch": field(text, "Target branch") or "not recorded",
        "objective": one_line(section(text, "Objective")),
        "completion_criteria": bullets(text, "Completion criteria"),
        "durable_constraints": bullets(text, "Durable constraints"),
        "owner_decisions": bullets(text, "Owner decisions"),
        "progress": {
            "accepted": accepted,
            "planned": planned,
            "label": f"{accepted}/{planned}" if planned else "not recorded",
        },
    }


def roadmap_state(text: str, accepted: int, task_id: str) -> list[dict[str, object]]:
    task_match = re.search(r"(?:^|-)(\d+)(?:-|$)", task_id)
    current = int(task_match.group(1)) if task_match else None
    entries = []
    for value, title in re.findall(r"^## Turn (\d+)\s+[—-]\s+(.+)$", text, re.MULTILINE):
        turn = int(value)
        status = "accepted" if turn <= accepted else "current" if turn == current else "planned"
        entries.append({"turn": turn, "title": one_line(title), "status": status})
    return entries


def task_state(text: str) -> dict[str, object]:
    return {
        "id": field(text, "Task ID") or "none",
        "state": field(text, "State") or "unknown",
        "review_round": field(text, "Review round") or "0",
        "starting_ref": field(text, "Starting ref") or "not recorded",
        "objective": one_line(section(text, "Objective")),
        "in_scope": bullets(text, "In scope"),
        "out_of_scope": bullets(text, "Out of scope"),
        "acceptance_criteria": bullets(text, "Acceptance criteria"),
        "required_evidence": bullets(text, "Required evidence"),
        "allowed_external_actions": bullets(text, "Allowed external actions"),
        "review_corrections": bullets(text, "Review corrections"),
    }


def coder_state(text: str, task: dict[str, object]) -> dict[str, object]:
    task_id = field(text, "Task ID") or "none"
    review_round = field(text, "Review round") or "0"
    return {
        "task_id": task_id,
        "state": field(text, "State") or "unknown",
        "review_round": review_round,
        "starting_ref": field(text, "Starting ref") or "not recorded",
        "current_ref": field(text, "Current ref") or "not recorded",
        "blocker": field(text, "Blocker") or "none",
        "current_activity": one_line(section(text, "Current activity")),
        "matches_current_task": task_id == task["id"] and review_round == task["review_round"],
    }


def review_state(text: str) -> dict[str, object]:
    return {
        "task_id": field(text, "Task ID") or "none",
        "verdict": field(text, "Verdict") or "not_reviewed",
        "review_round": field(text, "Review round") or "0",
        "examined_ref": field(text, "Examined ref") or "not recorded",
        "findings": bullets(text, "Findings"),
        "next_action": bullets(text, "Next action"),
    }


def completion_state(text: str) -> dict[str, object]:
    return {
        "goal_id": field(text, "Goal ID") or "none",
        "state": field(text, "State") or "unknown",
        "accepted_ref": field(text, "Accepted ref") or "not recorded",
        "result": bullets(text, "Result"),
        "evidence": bullets(text, "Evidence"),
        "limitations": bullets(text, "Limitations and optional follow-up"),
        "present": bool(text.strip()),
    }


def workflow_state(
    goal: dict[str, object],
    task: dict[str, object],
    coder: dict[str, object],
    runtime: dict[str, object],
    completion: dict[str, object],
) -> dict[str, object]:
    """Derive one owner-facing lifecycle phase from the raw coordination records.

    Raw coder/runtime/completion records are left untouched by the caller; this
    only reads them to pick a phase and a truthful current label/detail.
    """
    completion_matches = (
        completion["present"]
        and completion["goal_id"] == goal["id"]
        and completion["state"] == "done"
        and goal["state"] == "done"
    )
    coder_current = bool(coder["matches_current_task"])
    runtime_current = bool(runtime["matches_current_task"])
    coder_state = coder["state"] if coder_current else None
    task_state = task["state"]

    goal_blocked = goal["state"] == "blocked"
    task_blocked = task_state == "blocked"
    coder_blocked = coder_current and coder_state == "blocked"

    if completion_matches:
        phase = "done"
        active = False
        label = "Goal complete"
        detail = one_line(" ".join(completion["result"])) if completion["result"] else (
            "The goal is complete."
        )
    elif goal_blocked or task_blocked or coder_blocked:
        phase = "blocked"
        active = False
        label = "Blocked"
        detail = (
            coder["current_activity"]
            or (f"Blocked: {coder['blocker']}" if coder_blocked and coder["blocker"] != "none" else None)
            or "The goal or current task is blocked."
        )
    elif coder_current and coder_state == "implementing":
        phase = "implementing"
        active = True
        label = "Executor is implementing"
        detail = coder["current_activity"] or "The executor is working on the current task."
    elif coder_current and coder_state == "review":
        phase = "waiting_for_codex"
        active = False
        label = "Waiting for Codex review"
        detail = "The current task is awaiting Codex review."
    elif task_state in ("ready", "changes_requested"):
        phase = "waiting_for_claude"
        active = False
        label = "Waiting for executor"
        detail = f"The current task is {task_state} and has not been picked up yet."
    elif task_state == "review":
        phase = "waiting_for_codex"
        active = False
        label = "Waiting for Codex review"
        detail = "The current task is awaiting Codex review."
    else:
        phase = "inactive"
        active = False
        label = "Inactive"
        detail = "No active phase could be determined from the coordination records."

    return {
        "phase": phase,
        "active": active,
        "label": label,
        "detail": detail,
        "coder_current": coder_current,
        "runtime_current": runtime_current,
        "completion_current": completion_matches,
    }


def subagent_state(metrics: dict[str, object], now: float) -> list[dict[str, object]]:
    values = metrics.get("subagents")
    if not isinstance(values, list):
        return []
    agents = []
    for value in values:
        if not isinstance(value, dict):
            continue
        state = str(value.get("state") or "unknown")
        usage = value.get("usage") if isinstance(value.get("usage"), dict) else {}
        ended = now if state == "running" else value.get("completed_at_epoch")
        agents.append(
            {
                "state": state,
                "model": str(value.get("model") or metrics.get("subagent_model") or "inherited"),
                "description": one_line(str(value.get("description") or "Executor worker")),
                "started_at_epoch": number(value.get("started_at_epoch")),
                "completed_at_epoch": number(value.get("completed_at_epoch")),
                "elapsed": elapsed(value.get("started_at_epoch"), ended),
                "usage": {
                    name: usage[name] if isinstance(usage.get(name), int) else 0
                    for name in TOKEN_FIELDS
                },
            }
        )
    return agents


def runtime_state(
    repo: Path, task: dict[str, object], goal_id: str, now: float
) -> dict[str, object]:
    metrics = load_executor_metrics(repo, str(task["id"]))
    timing = load_json(repo / ".coordination" / "runtime" / "goal-timing.json")
    same_task = metrics.get("task_id") == task["id"]
    current = metrics if same_task else {}
    finished = metrics.get("completed_at_epoch") if metrics.get("state") == "completed" else None
    metric_end = number(finished) if same_task else None
    metric_end = metric_end if metric_end is not None else now
    usage = current.get("usage") if isinstance(current.get("usage"), dict) else {}
    same_goal = timing.get("goal_id") == goal_id
    goal_started = number(timing.get("started_at_epoch")) if same_goal else None
    return {
        "task_id": metrics.get("task_id") if isinstance(metrics.get("task_id"), str) else None,
        "state": str(metrics.get("state") or "unknown"),
        "matches_current_task": bool(same_task),
        "provider_id": str(metrics.get("provider_id") or "claude"),
        "primary_model": str(metrics.get("primary_model") or "Executor"),
        "subagent_model": str(metrics.get("subagent_model") or "provider-selected"),
        "orchestration_mode": str(metrics.get("orchestration_mode") or "not recorded"),
        "tokens": {
            name: usage[name] if isinstance(usage.get(name), int) else 0 for name in TOKEN_FIELDS
        },
        "timing": {
            "activity": elapsed(current.get("objective_started_epoch"), metric_end),
            "turn": elapsed(current.get("turn_started_epoch"), metric_end),
            "overall": elapsed(goal_started, now if goal_started is not None else None),
            "goal_started_at_epoch": goal_started,
        },
        "subagents": subagent_state(current, now),
    }


def watcher_state(repo: Path) -> list[dict[str, object]]:
    runtime = repo / ".coordination" / "runtime"
    watchers = []
    for role in WATCHER_ROLES:
        payload = load_json(runtime / f"watcher-{role}-status.json")
        if not payload:
            continue
        coordination = payload.get("coordination")
        watchers.append(
            {
                "role": str(payload.get("role") or role),
                "watcher_state": str(payload.get("watcher_state") or "unknown"),
                "detail": one_line(str(payload.get("detail") or "")),
                "updated_at": str(payload.get("updated_at") or "not recorded"),
                "coordination": coordination if isinstance(coordination, dict) else {},
                "lock_present": (runtime / f"watcher-{role}.lock").is_file(),
            }
        )
    return watchers
