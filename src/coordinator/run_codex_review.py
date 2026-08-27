#!/usr/bin/env python3.14
"""Run exactly one configured primary review for an executor handoff."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .executor_adapters import EXECUTOR_ADAPTERS
from .handoff_policy import (
    load_handoff_configuration,
    policy_catalog_instruction,
    validate_handoff_task,
)
from .run_mini_swe_turn import build_command as build_mini_command


REVIEWABLE_STATES = {"review", "blocked"}
VERDICTS = {"accepted", "changes_requested", "blocked"}
TASK_EXECUTORS = {"configured", *EXECUTOR_ADAPTERS}


def field(text: str, name: str) -> str | None:
    match = re.search(rf"^- {re.escape(name)}:\s*`?([^`\n]+)`?\s*$", text, re.MULTILINE)
    return match.group(1).strip() if match else None


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def is_git_worktree(repo: Path) -> bool:
    git = shutil.which("git")
    if git is None:
        return False
    completed = subprocess.run(
        [git, "-C", str(repo), "rev-parse", "--is-inside-work-tree"],
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0 and completed.stdout.strip() == "true"


def valid_transition(repo: Path, task_id: str, reviewed_round: str) -> tuple[bool, str]:
    goal = read(repo / ".coordination" / "planner" / "goal.md")
    task = read(repo / ".coordination" / "planner" / "current-task.md")
    review = read(repo / ".coordination" / "reviews" / "latest.md")
    if field(review, "Task ID") != task_id or field(review, "Review round") != reviewed_round:
        return False, "review file does not identify the examined handoff"
    verdict = field(review, "Verdict")
    task_state = field(task, "State")
    goal_state = field(goal, "State")
    task_executor = field(task, "Executor")
    next_executor = field(review, "Next executor")
    if verdict == "accepted":
        if goal_state == "done" and field(task, "Task ID") == task_id and task_state == "accepted":
            completion = repo / ".coordination" / "reviews" / "completion.md"
            if completion.is_file():
                return True, "accepted; overall goal done"
            return False, "done goal requires reviews/completion.md"
        if (
            goal_state == "active"
            and field(task, "Task ID") not in {None, "none", task_id}
            and task_state == "ready"
            and field(task, "Review round") == "0"
        ):
            if task_executor not in TASK_EXECUTORS:
                return False, f"next subgoal executor is invalid: {task_executor!r}"
            if next_executor != task_executor:
                return False, "review next executor must match the next subgoal executor"
            try:
                validate_handoff_task(
                    task,
                    load_handoff_configuration(repo),
                    task_executor or "configured",
                )
            except ValueError as error:
                return False, str(error)
            return True, f"accepted; assigned next subgoal {field(task, 'Task ID')}"
        return False, "accepted subgoal must either complete the goal or assign a new subgoal"
    if (
        verdict == "blocked"
        and field(task, "Task ID") == task_id
        and task_state == "blocked"
        and goal_state == "blocked"
    ):
        return True, verdict
    if (
        verdict == "changes_requested"
        and field(task, "Task ID") == task_id
        and task_state == "changes_requested"
    ):
        if task_executor not in TASK_EXECUTORS:
            return False, f"correction executor is invalid: {task_executor!r}"
        if next_executor != task_executor:
            return False, "review next executor must match the correction executor"
        try:
            validate_handoff_task(
                task,
                load_handoff_configuration(repo),
                task_executor or "configured",
            )
        except ValueError as error:
            return False, str(error)
        next_round = field(task, "Review round")
        try:
            if int(next_round or "") > int(reviewed_round):
                return True, verdict
        except ValueError:
            pass
        return False, "changes_requested must increment the planner review round"
    if verdict not in VERDICTS:
        return False, f"review verdict is invalid: {verdict!r}"
    return False, f"verdict {verdict!r} and planner state {task_state!r} disagree"


def run(args: argparse.Namespace) -> int:
    repo = args.repo.resolve()
    goal_path = repo / ".coordination" / "planner" / "goal.md"
    task_path = repo / ".coordination" / "planner" / "current-task.md"
    status_path = repo / ".coordination" / "coder" / "status.md"
    report_path = repo / ".coordination" / "coder" / "latest-report.md"
    review_path = repo / ".coordination" / "reviews" / "latest.md"
    for path in (goal_path, task_path, status_path, report_path, review_path):
        if not path.is_file():
            print(f"error: coordination file is missing: {path}", file=sys.stderr)
            return 2

    task = read(task_path)
    goal = read(goal_path)
    status = read(status_path)
    task_id = field(task, "Task ID")
    task_round = field(task, "Review round")
    coder_state = field(status, "State")
    if not task_id or task_id == "none":
        print("error: no task is assigned", file=sys.stderr)
        return 2
    if field(goal, "State") != "active":
        print("error: overall goal must be active for a Codex review", file=sys.stderr)
        return 2
    if field(status, "Task ID") != task_id or field(status, "Review round") != task_round:
        print("error: coder status does not match the active task handoff", file=sys.stderr)
        return 2
    if coder_state not in REVIEWABLE_STATES:
        print(f"error: coder state is not reviewable: {coder_state!r}", file=sys.stderr)
        return 2

    current_review = read(review_path)
    if (
        field(current_review, "Task ID") == task_id
        and field(current_review, "Review round") == task_round
        and field(current_review, "Verdict") in VERDICTS
    ):
        print("error: this executor handoff already has a Codex verdict", file=sys.stderr)
        return 2

    primary_adapter = getattr(args, "primary_adapter", "codex")
    if primary_adapter == "mini-swe-agent" and not getattr(
        args, "primary_local_model", ""
    ).strip():
        print("error: local/API primary requires a model", file=sys.stderr)
        return 2
    primary_name = (
        "Codex"
        if primary_adapter == "codex"
        else "Claude"
        if primary_adapter == "claude"
        else "mini-swe-agent"
    )
    primary_command = (
        args.codex_command
        if primary_adapter == "codex"
        else getattr(args, "claude_command", "claude")
        if primary_adapter == "claude"
        else getattr(args, "mini_command", "mini")
    )
    executable = shutil.which(primary_command)
    candidate_executable = Path(primary_command).expanduser()
    if executable is None and candidate_executable.is_file():
        executable = str(candidate_executable.resolve())
    if executable is None and not args.dry_run:
        print(f"error: {primary_name} command not found: {primary_command}", file=sys.stderr)
        return 127

    try:
        handoff_policy = policy_catalog_instruction(load_handoff_configuration(repo))
    except ValueError as error:
        print(f"error: cannot load handoff policy: {error}", file=sys.stderr)
        return 2

    prompt = f"""You are the configured primary planner/reviewer and own the overall
objective in .coordination/planner/goal.md. Perform the pending review handoff for subgoal {task_id}, review
round {task_round}. Read all applicable AGENTS.md files,
.coordination/README.md, .coordination/PROJECT.md, .coordination/planner/goal.md,
.coordination/planner/current-task.md, .coordination/coder/status.md,
.coordination/coder/latest-report.md, and .coordination/reviews/latest.md.
If .coordination/runtime/executor-settings.json exists, read it to understand
which adapter `configured` currently resolves to.

Review the actual complete product diff and independently examine or run focused
evidence that can falsify the acceptance claims. Do not edit product code. Do not
invoke another executor, start another watcher, commit, push, deploy, or mutate an external
system.

Replace .coordination/reviews/latest.md with exactly one verdict: accepted,
changes_requested, or blocked. Record task ID {task_id}, review round
{task_round}, examined ref/worktree, severity-ordered findings, independent
evidence, next action, and a machine-readable `- Next executor:` field.
Use `none` when the verdict does not launch another implementation handoff.

If changes are required, keep task ID {task_id}, increment the planner review
round, set state changes_requested, and put concrete corrections in Review
corrections. Select the next handoff adapter deliberately. Set `Executor` in
planner/current-task.md and `Next executor` in reviews/latest.md to the same one
of `configured`, `claude`, or `mini-swe-agent`. `configured` means the saved
repository default; use an explicit adapter for a one-round override. Never
describe an executor switch only in prose.

If this subgoal is accepted but the overall goal is not complete, replace
planner/current-task.md with the next bounded executor-sized subgoal: use a new task
ID, state ready, review round 0, explicit scope, acceptance criteria, evidence,
external-action limits, and an explicit `Executor`. Put the same value in the
review's `Next executor` field. Base that choice on the actual remaining gap in
the overall goal, not a prewritten ceremonial checklist.

The following handoff-sizing policy is enforced after you exit and again before
an executor launches:
{handoff_policy}

If the overall goal's completion criteria are genuinely satisfied, set this task
to accepted, set planner/goal.md State to done, and replace
.coordination/reviews/completion.md with a user-facing summary of the goal result,
accepted product ref, evidence, limitations, and any optional follow-up. This is
the durable done signal that stops the executor watcher.

If progress needs owner authority or a missing capability, set both task and goal
to blocked and name the exact blocker. Do not mark the goal done merely because
this subgoal passed. End after writing the coordination files."""

    if primary_adapter == "codex":
        command = [
            executable,
            "exec",
            "--ephemeral",
            "--sandbox",
            "workspace-write",
            "--cd",
            str(repo),
        ]
        if model := getattr(args, "model", ""):
            command.extend(("--model", model))
        if effort := getattr(args, "effort", ""):
            command.extend(("-c", f'model_reasoning_effort="{effort}"'))
        if not is_git_worktree(repo):
            command.append("--skip-git-repo-check")
    elif primary_adapter == "claude":
        command = [
            executable,
            "-p",
            "--model",
            getattr(args, "claude_model", "opus"),
            "--permission-mode",
            "acceptEdits",
            "--max-turns",
            str(getattr(args, "claude_max_turns", 40)),
        ]
        if claude_effort := getattr(args, "claude_effort", ""):
            command.extend(("--effort", claude_effort))
    else:
        runtime = repo / ".coordination" / "runtime" / "trajectories"
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", task_id).strip("-") or "task"
        trajectory = runtime / f"primary-{slug}-r{task_round or '0'}.json"
        mini_args = argparse.Namespace(
            model=getattr(args, "primary_local_model", ""),
            config=getattr(args, "mini_config", None),
            step_limit=getattr(args, "primary_local_step_limit", 24),
            timeout_seconds=getattr(args, "primary_local_timeout_seconds", 900),
            cost_limit=getattr(args, "local_cost_limit", 0.0),
            api_base=getattr(args, "local_api_base", ""),
            provider=getattr(args, "local_provider", "openai"),
            effort=getattr(args, "primary_local_effort", ""),
            profile="primary-review",
        )
        command = build_mini_command(mini_args, executable or primary_command, prompt, trajectory)
    if primary_adapter != "mini-swe-agent":
        command.append(prompt)
    if args.dry_run:
        print(f"Would run one {primary_name} primary review:")
        redacted = list(command)
        if primary_adapter == "mini-swe-agent":
            redacted[redacted.index("--task") + 1] = "<coordination review prompt>"
        else:
            redacted[-1] = "<coordination review prompt>"
        print(" ".join(redacted))
        return 0

    lock_path = repo / ".coordination" / ".codex-review.lock"
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        print(f"error: another Codex review may be active; inspect {lock_path}", file=sys.stderr)
        return 2
    try:
        os.write(lock_fd, f"pid={os.getpid()} task={task_id} round={task_round}\n".encode())
        print(f"Starting {primary_name} primary review: {task_id} round {task_round}", flush=True)
        child_env = os.environ.copy()
        if primary_adapter == "mini-swe-agent":
            key_name = getattr(args, "local_api_key_env", "OPENAI_API_KEY")
            if key_name and child_env.get(key_name):
                child_env["OPENAI_API_KEY"] = child_env[key_name]
            elif not key_name:
                child_env["OPENAI_API_KEY"] = "local-endpoint-no-key"
        completed = subprocess.run(command, cwd=repo, check=False, env=child_env)
    finally:
        os.close(lock_fd)
        lock_path.unlink(missing_ok=True)

    if completed.returncode != 0:
        print(f"{primary_name} exited with status {completed.returncode}", file=sys.stderr)
        return completed.returncode
    valid, result = valid_transition(repo, task_id, task_round or "")
    if not valid:
        print(f"error: primary review ended without a valid state transition: {result}", file=sys.stderr)
        return 3
    print(f"Primary review ended with verdict: {result}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="project root")
    parser.add_argument("--codex-command", default="codex", help="Codex executable")
    parser.add_argument(
        "--primary-adapter",
        choices=("codex", "claude", "mini-swe-agent"),
        default="codex",
        help="primary planner/reviewer runtime",
    )
    parser.add_argument("--claude-command", default="claude", help="Claude executable")
    parser.add_argument("--claude-model", default="opus", help="Claude primary model")
    parser.add_argument(
        "--claude-effort",
        choices=("low", "medium", "high", "xhigh", "max"),
        default="",
        help="Claude primary effort",
    )
    parser.add_argument("--claude-max-turns", type=int, default=40)
    parser.add_argument("--mini-command", default="mini", help="mini-swe-agent executable")
    parser.add_argument("--mini-config", type=Path, help="base mini-swe-agent YAML config")
    parser.add_argument("--primary-local-model", default="", help="local/API primary model")
    parser.add_argument(
        "--primary-local-effort",
        choices=("none", "low", "medium", "high", "xhigh", "max"),
        default="",
    )
    parser.add_argument("--primary-local-step-limit", type=int, default=24)
    parser.add_argument("--primary-local-timeout-seconds", type=int, default=900)
    parser.add_argument("--local-api-base", default="")
    parser.add_argument("--local-provider", default="openai")
    parser.add_argument("--local-api-key-env", default="OPENAI_API_KEY")
    parser.add_argument("--local-cost-limit", type=float, default=0.0)
    parser.add_argument("--model", default="", help="Codex reviewer model (CLI default if blank)")
    parser.add_argument(
        "--effort",
        choices=("none", "low", "medium", "high", "xhigh", "max", "ultra"),
        default="",
        help="Codex reasoning effort (model default if blank)",
    )
    parser.add_argument("--dry-run", action="store_true", help="validate without invoking the primary")
    args = parser.parse_args()
    if args.primary_local_step_limit <= 0:
        parser.error("--primary-local-step-limit must be positive")
    if args.primary_local_timeout_seconds <= 0:
        parser.error("--primary-local-timeout-seconds must be positive")
    if args.local_cost_limit < 0:
        parser.error("--local-cost-limit must not be negative")
    return args


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
