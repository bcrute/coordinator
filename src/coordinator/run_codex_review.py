#!/usr/bin/env python3
"""Run exactly one Codex review for a completed Claude handoff."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path


REVIEWABLE_STATES = {"review", "blocked"}
VERDICTS = {"accepted", "changes_requested", "blocked"}


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
        print("error: this Claude handoff already has a Codex verdict", file=sys.stderr)
        return 2

    executable = shutil.which(args.codex_command)
    if executable is None:
        print(f"error: Codex command not found: {args.codex_command}", file=sys.stderr)
        return 127

    prompt = f"""You own the overall objective in .coordination/planner/goal.md.
Perform the pending Codex planner/reviewer handoff for subgoal {task_id}, review
round {task_round}. Read all applicable AGENTS.md files,
.coordination/README.md, .coordination/PROJECT.md, .coordination/planner/goal.md,
.coordination/planner/current-task.md, .coordination/coder/status.md,
.coordination/coder/latest-report.md, and .coordination/reviews/latest.md.

Review the actual complete product diff and independently examine or run focused
evidence that can falsify the acceptance claims. Do not edit product code. Do not
invoke Claude, start another watcher, commit, push, deploy, or mutate an external
system.

Replace .coordination/reviews/latest.md with exactly one verdict: accepted,
changes_requested, or blocked. Record task ID {task_id}, review round
{task_round}, examined ref/worktree, severity-ordered findings, independent
evidence, and next action.

If changes are required, keep task ID {task_id}, increment the planner review
round, set state changes_requested, and put concrete corrections in Review
corrections.

If this subgoal is accepted but the overall goal is not complete, replace
planner/current-task.md with the next bounded Claude-sized subgoal: use a new task
ID, state ready, review round 0, explicit scope, acceptance criteria, evidence,
and external-action limits. Base that choice on the actual remaining gap in the
overall goal, not a prewritten ceremonial checklist.

If the overall goal's completion criteria are genuinely satisfied, set this task
to accepted, set planner/goal.md State to done, and replace
.coordination/reviews/completion.md with a user-facing summary of the goal result,
accepted product ref, evidence, limitations, and any optional follow-up. This is
the durable done signal that stops the Claude watcher.

If progress needs owner authority or a missing capability, set both task and goal
to blocked and name the exact blocker. Do not mark the goal done merely because
this subgoal passed. End after writing the coordination files."""

    command = [
        executable,
        "exec",
        "--ephemeral",
        "--sandbox",
        "workspace-write",
        "--cd",
        str(repo),
    ]
    outside_git = not is_git_worktree(repo)
    if outside_git:
        command.append("--skip-git-repo-check")
    command.append(prompt)
    if args.dry_run:
        print("Would run one Codex review:")
        print(" ".join(command[:-1] + ["<coordination review prompt>"]))
        return 0

    lock_path = repo / ".coordination" / ".codex-review.lock"
    try:
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        print(f"error: another Codex review may be active; inspect {lock_path}", file=sys.stderr)
        return 2
    try:
        os.write(lock_fd, f"pid={os.getpid()} task={task_id} round={task_round}\n".encode())
        print(f"Starting Codex review: {task_id} round {task_round}", flush=True)
        completed = subprocess.run(command, cwd=repo, check=False)
    finally:
        os.close(lock_fd)
        lock_path.unlink(missing_ok=True)

    if completed.returncode != 0:
        print(f"Codex exited with status {completed.returncode}", file=sys.stderr)
        return completed.returncode
    valid, result = valid_transition(repo, task_id, task_round or "")
    if not valid:
        print(f"error: Codex review ended without a valid state transition: {result}", file=sys.stderr)
        return 3
    print(f"Codex review ended with verdict: {result}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd(), help="project root")
    parser.add_argument("--codex-command", default="codex", help="Codex executable")
    parser.add_argument("--dry-run", action="store_true", help="validate without invoking Codex")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
