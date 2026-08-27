#!/usr/bin/env python3.14
"""Run one bounded mini-swe-agent implementation handoff for the active task."""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

from .coordination_locks import acquire_lock
from .executor_settings import ExecutorConfiguration
from .handoff_policy import load_handoff_configuration, validate_handoff_task
from .mini_swe_profiles import MINI_SWE_PROFILES, profile_config
from .process_guard import guarded_command


ACTIVE_STATES = {"ready", "changes_requested"}
BOUNDED_RESPONSE_TOKENS = 3072
TOKEN_FIELDS = (
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
    "output_tokens",
)
PROTECTED_COORDINATION_PATHS = (
    Path(".coordination/PROJECT.md"),
    Path(".coordination/README.md"),
    Path(".coordination/planner/goal.md"),
    Path(".coordination/planner/current-task.md"),
    Path(".coordination/reviews/latest.md"),
    Path(".coordination/reviews/completion.md"),
    Path(".coordinator-validation/report.json"),
    Path(".coordinator-validation/report.schema.json"),
    Path(".coordinator-validation/reporting.md"),
    Path(".coordinator-validation/validation-brief.md"),
)


def field(text: str, name: str) -> str | None:
    match = re.search(rf"^- {re.escape(name)}:\s*`?([^`\n]+)`?\s*$", text, re.MULTILINE)
    return match.group(1).strip() if match else None


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(value, encoding="utf-8")
    temporary.replace(path)


def atomic_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(value)
    temporary.replace(path)


def load_trajectory(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _integer(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def trajectory_usage(trajectory: dict[str, object]) -> dict[str, int]:
    """Normalize LiteLLM usage embedded in mini-swe-agent trajectory messages."""

    totals = {name: 0 for name in TOKEN_FIELDS}
    seen: set[str] = set()
    messages = trajectory.get("messages")
    if not isinstance(messages, list):
        return totals
    for message in messages:
        if not isinstance(message, dict):
            continue
        extra = message.get("extra")
        response = extra.get("response") if isinstance(extra, dict) else None
        if not isinstance(response, dict):
            continue
        response_id = response.get("id")
        if isinstance(response_id, str):
            if response_id in seen:
                continue
            seen.add(response_id)
        usage = response.get("usage")
        if not isinstance(usage, dict):
            continue
        prompt = _integer(usage.get("prompt_tokens")) or _integer(usage.get("input_tokens"))
        completion = _integer(usage.get("completion_tokens")) or _integer(
            usage.get("output_tokens")
        )
        prompt_details = usage.get("prompt_tokens_details")
        cached = (
            _integer(prompt_details.get("cached_tokens"))
            if isinstance(prompt_details, dict)
            else _integer(usage.get("cache_read_input_tokens"))
        )
        cache_creation = _integer(usage.get("cache_creation_input_tokens"))
        totals["cache_read_input_tokens"] += cached
        totals["cache_creation_input_tokens"] += cache_creation
        totals["input_tokens"] += max(0, prompt - cached)
        totals["output_tokens"] += completion
    return totals


def trajectory_info(trajectory: dict[str, object]) -> dict[str, object]:
    info = trajectory.get("info")
    return info if isinstance(info, dict) else {}


def trajectory_steps(trajectory: dict[str, object]) -> int:
    info = trajectory_info(trajectory)
    stats = info.get("model_stats")
    if isinstance(stats, dict):
        return _integer(stats.get("api_calls"))
    return 0


def trajectory_commands(trajectory: dict[str, object], limit: int = 20) -> list[str]:
    commands: list[str] = []
    messages = trajectory.get("messages")
    if not isinstance(messages, list):
        return commands
    for message in messages:
        if not isinstance(message, dict):
            continue
        extra = message.get("extra")
        actions = extra.get("actions") if isinstance(extra, dict) else None
        if not isinstance(actions, list):
            continue
        for action in actions:
            command = action.get("command") if isinstance(action, dict) else None
            if isinstance(command, str) and command.strip():
                commands.append(re.sub(r"\s+", " ", command).strip()[:500])
                if len(commands) >= limit:
                    return commands
    return commands


def mini_prompt(task_id: str, review_round: str | None, task: str) -> str:
    opening = (
        f"Implement exactly one bounded repository task: {task_id}, "
        f"review round {review_round}."
    )
    return f"""{opening}

The active assignment is embedded below and is authoritative. Work only in the current
repository. Inspect, edit, and test the product as needed. Coordinator-owned files under
`.coordination/` and `.coordinator-validation/` are expected to be dirty before you
start. Never edit, delete, restore, checkout, reset, clean, or otherwise change them,
even to make Git status clean. Do not commit, push, deploy, install system software, or
mutate external systems unless the assignment explicitly authorizes that action. Put a
bash tool call first in every response; do not spend a response narrating analysis before
acting. Keep any text before that call under 80 words. Stop after completing this one
assignment.

<active-assignment>
{task.rstrip()}
</active-assignment>
"""


def build_command(
    args: argparse.Namespace, executable: str, prompt: str, trajectory_path: Path
) -> list[str]:
    command = [
        executable,
        "--task",
        prompt,
        "--yolo",
        "--exit-immediately",
        "--output",
        str(trajectory_path),
    ]
    if args.model:
        command.extend(("--model", args.model))

    # mini replaces its default config list as soon as -c is used. Start from
    # mini's transport/tool defaults, layer an optional operator config, then
    # apply Coordinator's role policy last so endpoint customization cannot
    # silently restore the generic exploration workflow.
    command.extend(("--config", "mini.yaml"))
    if args.config:
        command.extend(("--config", str(args.config)))
    profile = getattr(args, "profile", "bounded")
    if policy := profile_config(profile):
        command.extend(("--config", str(policy)))
    command.extend(("--config", f"agent.step_limit={args.step_limit}"))
    command.extend(("--config", f"agent.wall_time_limit_seconds={args.timeout_seconds}"))
    command.extend(("--config", "model.cost_tracking=ignore_errors"))
    effort = getattr(args, "effort", "")
    if effort == "none":
        # OpenAI-compatible local servers commonly expose Qwen-style thinking
        # control through chat-template kwargs rather than reasoning_effort.
        command.extend(
            (
                "--config",
                "model.model_kwargs.extra_body.chat_template_kwargs.enable_thinking=false",
            )
        )
    elif effort:
        command.extend(("--config", f"model.model_kwargs.reasoning_effort={effort}"))
    command.extend(("--cost-limit", str(args.cost_limit)))
    if args.api_base:
        command.extend(
            (
                "--config",
                f"model.model_kwargs.custom_llm_provider={args.provider}",
                "--config",
                f"model.model_kwargs.api_base={args.api_base}",
            )
        )
    if profile == "bounded":
        command.extend(
            ("--config", f"model.model_kwargs.max_tokens={BOUNDED_RESPONSE_TOKENS}")
        )
    return command


def write_status(
    path: Path,
    task_id: str,
    review_round: str | None,
    state: str,
    activity: str,
    blocker: str | None = None,
) -> None:
    lines = [
        "# Coder status",
        "",
        f"- Task ID: `{task_id}`",
        f"- State: `{state}`",
        f"- Review round: `{review_round}`",
        "- Executor: `mini-swe-agent`",
    ]
    if blocker:
        lines.append(f"- Blocker: `{blocker}`")
    lines.extend(("", "## Current activity", "", activity, ""))
    atomic_text(path, "\n".join(lines))


def write_progress(
    repo: Path,
    goal_id: str,
    task_id: str,
    review_round: str | None,
    state: str,
    started: float,
    model: str,
    trajectory: dict[str, object],
    trajectory_path: Path,
    completed: float | None = None,
) -> None:
    payload: dict[str, object] = {
        "provider_id": "mini-swe-agent",
        "goal_id": goal_id,
        "task_id": task_id,
        "review_round": review_round,
        "state": state,
        "turn_started_epoch": started,
        "objective_started_epoch": started,
        "usage": trajectory_usage(trajectory),
        "subagents": [],
        "primary_model": model or "mini-swe-agent configured model",
        "subagent_model": "not supported",
        "orchestration_mode": "single-agent",
        "steps": trajectory_steps(trajectory),
        "trajectory_path": str(trajectory_path),
    }
    if completed is not None:
        payload["completed_at_epoch"] = completed
    path = repo / ".coordination" / "runtime" / "executor-progress.json"
    atomic_text(path, json.dumps(payload, indent=2) + "\n")


def _indented(value: object, limit: int = 4000) -> str:
    text = str(value or "not recorded").strip()[:limit]
    return "\n".join(f"    {line}" for line in text.splitlines()) or "    not recorded"


def write_report(
    path: Path,
    *,
    task_id: str,
    review_round: str | None,
    returncode: int,
    trajectory: dict[str, object],
    trajectory_path: Path,
    repository_status: str,
    timed_out: bool,
    coordination_changed: list[str],
) -> tuple[str, str | None]:
    info = trajectory_info(trajectory)
    exit_status = str(info.get("exit_status") or "not recorded")
    successful = returncode == 0 and exit_status.lower() == "submitted" and not coordination_changed
    state = "review" if successful else "blocked"
    if timed_out:
        blocker = "Coordinator wall-time limit exceeded"
    elif coordination_changed:
        blocker = "executor modified Coordinator-owned coordination files"
    elif returncode != 0:
        blocker = f"mini-swe-agent exited with status {returncode}"
    elif exit_status.lower() != "submitted":
        blocker = f"mini-swe-agent trajectory ended as {exit_status}"
    else:
        blocker = None
    commands = trajectory_commands(trajectory)
    command_rows = "\n\n".join(_indented(command, 500) for command in commands)
    command_rows = command_rows or "    None recorded."
    changed_rows = "\n".join(f"- `{name}`" for name in coordination_changed) or "- None detected."
    report = f"""# Coder latest report

- Task ID: `{task_id}`
- State: `{state}`
- Review round: `{review_round}`
- Executor: `mini-swe-agent`
- Process exit status: `{returncode}`
- Agent exit status: `{exit_status}`
- Trajectory: `{trajectory_path}`

## Executor submission

{_indented(info.get("submission"))}

## Commands observed

{command_rows}

## Repository status after handoff

{_indented(repository_status or "clean or unavailable")}

## Coordinator-owned files changed by executor

{changed_rows}

## Review note

This report is generated by the Coordinator adapter from the process result and
trajectory. The configured primary must review the actual diff and independently
run relevant tests.
"""
    atomic_text(path, report)
    return state, blocker


def repository_status(repo: Path) -> str:
    git = shutil.which("git")
    if git is None:
        return "git is unavailable"
    result = subprocess.run(
        [git, "-C", str(repo), "status", "--short", "--untracked-files=all"],
        check=False,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else "git status unavailable"


def signal_process_group(child: subprocess.Popen[object], number: int) -> None:
    if child.poll() is not None:
        return
    try:
        os.killpg(child.pid, number)
    except (AttributeError, ProcessLookupError, PermissionError):
        child.send_signal(number)


def run(args: argparse.Namespace) -> int:
    repo = args.repo.resolve()
    goal_path = repo / ".coordination" / "planner" / "goal.md"
    task_path = repo / ".coordination" / "planner" / "current-task.md"
    status_path = repo / ".coordination" / "coder" / "status.md"
    report_path = repo / ".coordination" / "coder" / "latest-report.md"
    if not goal_path.is_file() or not task_path.is_file():
        print("error: coordination goal/task files are missing", file=sys.stderr)
        return 2
    goal = goal_path.read_text(encoding="utf-8")
    task = task_path.read_text(encoding="utf-8")
    goal_id = field(goal, "Goal ID")
    task_id = field(task, "Task ID")
    review_round = field(task, "Review round")
    if field(goal, "State") != "active" or not goal_id or goal_id == "none":
        print("error: an active overall goal with a stable Goal ID is required", file=sys.stderr)
        return 2
    if field(task, "State") not in ACTIVE_STATES or not task_id or task_id == "none":
        print(
            "error: a ready or changes_requested task with a stable Task ID is required",
            file=sys.stderr,
        )
        return 2
    configured = load_handoff_configuration(
        repo,
        ExecutorConfiguration(
            executor_adapter="mini-swe-agent",
            mini_swe_model=args.model or "configured",
            mini_swe_step_limit=args.step_limit,
        ),
    )
    selected_executor = field(task, "Executor") or "configured"
    resolved_executor = (
        configured.executor_adapter
        if selected_executor == "configured"
        else selected_executor
    )
    if resolved_executor != "mini-swe-agent":
        print(
            f"error: task routes to {resolved_executor!r}, not mini-swe-agent",
            file=sys.stderr,
        )
        return 2
    try:
        validate_handoff_task(
            task,
            replace(configured, mini_swe_step_limit=args.step_limit),
            selected_executor,
        )
    except ValueError as error:
        print(f"error: cannot launch executor handoff: {error}", file=sys.stderr)
        return 3
    if status_path.is_file():
        prior = status_path.read_text(encoding="utf-8")
        if (
            field(prior, "Task ID") == task_id
            and field(prior, "Review round") == review_round
            and field(prior, "State") in {"review", "blocked"}
        ):
            print("error: this task round already has an executor handoff", file=sys.stderr)
            return 2

    executable = shutil.which(args.mini_command)
    candidate_executable = Path(args.mini_command).expanduser()
    if executable is None and candidate_executable.is_file():
        executable = str(candidate_executable.resolve())
    if executable is None and not args.dry_run:
        print(f"error: mini-swe-agent command not found: {args.mini_command}", file=sys.stderr)
        return 127
    if args.config is not None and not args.config.is_file():
        print(f"error: mini-swe-agent config not found: {args.config}", file=sys.stderr)
        return 2

    runtime = repo / ".coordination" / "runtime"
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", task_id).strip("-") or "task"
    trajectory_path = runtime / "trajectories" / f"{slug}-r{review_round or '0'}.json"
    prompt = mini_prompt(task_id, review_round, task)
    command = build_command(args, executable or args.mini_command, prompt, trajectory_path)
    if args.dry_run:
        redacted = list(command)
        redacted[redacted.index("--task") + 1] = "<coordination prompt>"
        print("Would run one mini-swe-agent turn:")
        print(" ".join(redacted))
        return 0

    watched_paths = [repo / relative for relative in PROTECTED_COORDINATION_PATHS]
    watched_before = {path: path.read_bytes() if path.is_file() else None for path in watched_paths}
    lock_path = repo / ".coordination" / ".mini-swe-agent-turn.lock"
    try:
        lock_fd = acquire_lock(
            lock_path, f"pid={os.getpid()} task={task_id} round={review_round}\n"
        )
    except FileExistsError:
        print(f"error: another mini-swe-agent turn may be active: {lock_path}", file=sys.stderr)
        return 2

    started = time.time()
    timed_out = False
    returncode = 1
    try:
        write_status(
            status_path,
            task_id,
            review_round,
            "implementing",
            f"mini-swe-agent is working on step 0 with {args.model or 'its configured model'}.",
        )
        child_env = os.environ.copy()
        if args.api_key_env:
            source_key = child_env.get(args.api_key_env)
            if source_key:
                child_env["OPENAI_API_KEY"] = source_key
        else:
            # LiteLLM's OpenAI-compatible client expects a value even when a
            # local endpoint deliberately performs no authentication.
            child_env["OPENAI_API_KEY"] = "local-endpoint-no-key"
        print(f"Starting mini-swe-agent handoff: {task_id}", flush=True)
        child = subprocess.Popen(
            guarded_command(command),
            cwd=repo,
            env=child_env,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        forwarded_signals: list[int] = []

        def forward_signal(number: int, _frame: object) -> None:
            forwarded_signals.append(number)
            signal_process_group(child, number)

        prior_handlers = {
            number: signal.signal(number, forward_signal)
            for number in (signal.SIGINT, signal.SIGTERM)
        }
        deadline = time.monotonic() + args.timeout_seconds + args.timeout_grace_seconds
        try:
            while child.poll() is None:
                trajectory = load_trajectory(trajectory_path)
                steps = trajectory_steps(trajectory)
                write_status(
                    status_path,
                    task_id,
                    review_round,
                    "implementing",
                    f"mini-swe-agent is working on step {steps} with "
                    f"{args.model or 'its configured model'}.",
                )
                write_progress(
                    repo,
                    goal_id,
                    task_id,
                    review_round,
                    "running",
                    started,
                    args.model,
                    trajectory,
                    trajectory_path,
                )
                if time.monotonic() >= deadline:
                    timed_out = True
                    signal_process_group(child, signal.SIGTERM)
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        signal_process_group(child, signal.SIGKILL)
                    break
                time.sleep(args.progress_interval)
            observed_returncode = child.wait()
            returncode = (
                128 + forwarded_signals[-1]
                if forwarded_signals
                else 124
                if timed_out
                else observed_returncode
            )
        finally:
            for number, handler in prior_handlers.items():
                signal.signal(number, handler)
        trajectory = load_trajectory(trajectory_path)
        changed = [
            str(path.relative_to(repo))
            for path, before in watched_before.items()
            if (path.read_bytes() if path.is_file() else None) != before
        ]
        # Preserve the primary's live administrative state even when an
        # executor ignores the ownership boundary. The violation remains a
        # blocked handoff and is reported below, but it cannot erase the goal
        # and task needed for primary review and recovery.
        for path, before in watched_before.items():
            after = path.read_bytes() if path.is_file() else None
            if after == before:
                continue
            if before is None:
                if path.is_file() or path.is_symlink():
                    path.unlink()
            else:
                atomic_bytes(path, before)
        completed = time.time()
        state, blocker = write_report(
            report_path,
            task_id=task_id,
            review_round=review_round,
            returncode=returncode,
            trajectory=trajectory,
            trajectory_path=trajectory_path,
            repository_status=repository_status(repo),
            timed_out=timed_out,
            coordination_changed=changed,
        )
        write_status(
            status_path,
            task_id,
            review_round,
            state,
            "The bounded mini-swe-agent handoff ended; primary review is required.",
            blocker,
        )
        write_progress(
            repo,
            goal_id,
            task_id,
            review_round,
            "completed" if state == "review" else "blocked",
            started,
            args.model,
            trajectory,
            trajectory_path,
            completed,
        )
    finally:
        os.close(lock_fd)
        lock_path.unlink(missing_ok=True)

    if returncode != 0:
        print(f"mini-swe-agent exited with status {returncode}", file=sys.stderr)
        return returncode
    print("mini-swe-agent handoff ended. Primary review is required before another turn.")
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="project root")
    parser.add_argument("--mini-command", default="mini", help="mini executable")
    parser.add_argument("--model", default="", help="mini-swe-agent/LiteLLM model name")
    parser.add_argument(
        "--effort",
        choices=("none", "low", "medium", "high", "xhigh", "max"),
        default="",
        help="LiteLLM reasoning effort; none disables thinking on compatible endpoints",
    )
    parser.add_argument("--config", type=Path, help="base mini-swe-agent YAML config")
    parser.add_argument(
        "--profile",
        choices=MINI_SWE_PROFILES,
        default="bounded",
        help="Coordinator execution policy (default: bounded)",
    )
    parser.add_argument("--api-base", default="", help="OpenAI-compatible API base URL")
    parser.add_argument("--provider", default="openai", help="LiteLLM custom provider name")
    parser.add_argument(
        "--api-key-env",
        default="OPENAI_API_KEY",
        help="environment variable containing the API key; its value is never an argument",
    )
    parser.add_argument("--step-limit", type=int, default=12)
    parser.add_argument("--cost-limit", type=float, default=0.0)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--timeout-grace-seconds", type=int, default=30)
    parser.add_argument("--progress-interval", type=float, default=1.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if args.step_limit <= 0:
        parser.error("--step-limit must be positive")
    if args.cost_limit < 0:
        parser.error("--cost-limit must not be negative")
    if args.timeout_seconds <= 0 or args.timeout_grace_seconds < 0:
        parser.error("timeout values must be positive (grace may be zero)")
    if args.progress_interval <= 0:
        parser.error("--progress-interval must be positive")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", args.provider):
        parser.error("--provider must contain only letters, numbers, dot, underscore, or hyphen")
    if args.api_key_env and not re.fullmatch(
        r"[A-Za-z_][A-Za-z0-9_]*", args.api_key_env
    ):
        parser.error("--api-key-env must be an environment-variable name")
    if args.api_base:
        endpoint = urlsplit(args.api_base)
        if endpoint.scheme not in {"http", "https"} or not endpoint.netloc:
            parser.error("--api-base must be an absolute HTTP(S) URL")
        if endpoint.username is not None or endpoint.password is not None:
            parser.error("--api-base must not contain embedded credentials")
        if endpoint.query or endpoint.fragment:
            parser.error("--api-base must not contain a query or fragment")
    return args


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
