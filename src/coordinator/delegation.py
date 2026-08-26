"""Bounded, worktree-isolated implementation delegation for MCP supervisors."""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import signal
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Callable, Sequence

from .process_guard import guarded_command
from .run_mini_swe_turn import (
    build_command,
    load_trajectory,
    signal_process_group,
    trajectory_info,
    trajectory_steps,
    trajectory_usage,
)

MAX_OBJECTIVE_CHARS = 12_000
MAX_ALLOWED_PATHS = 24
MAX_VALIDATION_COMMANDS = 20
MAX_COMMAND_ARGUMENTS = 100
MAX_ARGUMENT_CHARS = 4_000
MAX_CAPTURE_CHARS = 8_000
MAX_SUMMARY_CHARS = 4_000
MAX_VALIDATION_OUTPUT_CHARS = 1_000
VALIDATION_BUDGET_SECONDS = 60.0
PROTECTED_PATTERNS = (".git", ".git/**", ".coordination", ".coordination/**")
FORBIDDEN_VALIDATION_EXECUTABLES = {
    "bash",
    "cmd",
    "curl",
    "fish",
    "powershell",
    "pwsh",
    "scp",
    "sh",
    "ssh",
    "wget",
    "zsh",
}
FORBIDDEN_GIT_VALIDATIONS = {"clone", "fetch", "pull", "push", "remote", "send-email"}
SAFE_WORKER_ENVIRONMENT = {
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PATH",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TMPDIR",
    "TZ",
}


@dataclass(frozen=True)
class DelegationConfiguration:
    """The bounded mini-swe-agent worker configuration supplied by Coordinator."""

    repo: Path
    mini_command: str = "mini"
    model: str = ""
    effort: str = ""
    config: Path | None = None
    api_base: str = ""
    provider: str = "openai"
    api_key_env: str = ""
    step_limit: int = 12
    cost_limit: float = 0.0
    timeout_seconds: int = 900
    progress_interval: float = 1.0
    timeout_grace_seconds: int = 30


def atomic_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _safe_relative(value: str, *, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    normalized = value.strip().replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or len(normalized) > 500
        or path.is_absolute()
        or ".." in path.parts
        or "\x00" in normalized
        or normalized.startswith("~")
        or normalized in {"*", "**", "**/*"}
    ):
        raise ValueError(f"{label} must be a safe repository-relative path or glob")
    if any(fnmatch.fnmatchcase(normalized, pattern) for pattern in PROTECTED_PATTERNS):
        raise ValueError(f"{label} cannot include Coordinator or Git administration files")
    return normalized


def normalize_allowed_paths(values: Sequence[str]) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= MAX_ALLOWED_PATHS:
        raise ValueError(f"allowed_paths must contain 1 to {MAX_ALLOWED_PATHS} entries")
    return tuple(_safe_relative(value, label="allowed path") for value in values)


def normalize_validation_commands(
    values: Sequence[Sequence[str]] | None,
) -> tuple[tuple[str, ...], ...]:
    if values is None:
        return ()
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= MAX_VALIDATION_COMMANDS:
        raise ValueError(
            f"validation_commands must contain 1 to {MAX_VALIDATION_COMMANDS} commands"
        )
    commands: list[tuple[str, ...]] = []
    for command in values:
        if not isinstance(command, (list, tuple)) or not 1 <= len(command) <= MAX_COMMAND_ARGUMENTS:
            raise ValueError("each validation command must be a non-empty argument array")
        normalized: list[str] = []
        for argument in command:
            if (
                not isinstance(argument, str)
                or not argument
                or len(argument) > MAX_ARGUMENT_CHARS
                or "\x00" in argument
            ):
                raise ValueError("validation command arguments must be bounded strings")
            normalized.append(argument)
        executable = Path(normalized[0]).name.lower()
        if executable in FORBIDDEN_VALIDATION_EXECUTABLES:
            raise ValueError("validation commands cannot invoke a shell or network client")
        if (
            executable == "git"
            and len(normalized) > 1
            and normalized[1].lower() in FORBIDDEN_GIT_VALIDATIONS
        ):
            raise ValueError("validation commands cannot mutate or contact Git remotes")
        commands.append(tuple(normalized))
    return tuple(commands)


def path_is_allowed(path: str, patterns: Sequence[str]) -> bool:
    normalized = path.replace("\\", "/")
    candidate = PurePosixPath(normalized)
    for pattern in patterns:
        if pattern.endswith("/**"):
            root = pattern[:-3]
            if normalized == root or normalized.startswith(f"{root}/"):
                return True
        elif candidate.match(pattern):
            return True
    return False


def _git(repo: Path, *arguments: str, capture: bool = True) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("git")
    if executable is None:
        raise RuntimeError("git is required for isolated delegation")
    result = subprocess.run(
        [executable, "-C", str(repo), *arguments],
        check=False,
        capture_output=capture,
        text=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "git command failed").strip()
        raise RuntimeError(detail[:MAX_CAPTURE_CHARS])
    return result


def _worktree_root(repo: Path) -> Path:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", repo.name).strip("-") or "repository"
    return repo.parent / f".{slug}-coordinator-worktrees"


def _worker_prompt(
    objective: str,
    allowed_paths: Sequence[str],
    validation_commands: Sequence[Sequence[str]],
) -> str:
    paths = "\n".join(f"- {path}" for path in allowed_paths)
    validations = (
        "\n".join("- " + repr(list(command)) for command in validation_commands)
        or "- No independent validation command was supplied. Run focused checks you discover."
    )
    return f"""Implement one bounded task in this isolated git worktree.

<objective>
{objective.rstrip()}
</objective>

You may modify only paths matching these repository-relative patterns:
{paths}

The supervisor requested these validation commands:
{validations}

Inspect only the context needed for this task. Implement, test, and iterate. Do not
modify `.git/` or `.coordination/`. Do not commit, push, deploy, install system
software, access secrets, or mutate external systems. Finish by submitting a concise
summary of changes and tests. The supervisor will independently review your patch.
"""


def _bounded_output(value: str, limit: int = MAX_CAPTURE_CHARS) -> str:
    return value[-limit:] if len(value) > limit else value


def sanitized_environment(source: dict[str, str]) -> dict[str, str]:
    """Keep runtime necessities while dropping credentials and provider configuration."""

    child = {name: source[name] for name in SAFE_WORKER_ENVIRONMENT if name in source}
    child["PYTHONDONTWRITEBYTECODE"] = "1"
    return child


def worker_environment(source: dict[str, str], api_key_env: str) -> dict[str, str]:
    """Build a minimal child environment and copy only the selected endpoint key."""

    child = sanitized_environment(source)
    if api_key_env:
        if secret := source.get(api_key_env):
            child["OPENAI_API_KEY"] = secret
    else:
        child["OPENAI_API_KEY"] = "local-endpoint-no-key"
    return child


def run_validation(command: Sequence[str], cwd: Path, timeout: float) -> dict[str, object]:
    """Run one shell-free check with process-group cleanup and bounded output."""

    child = subprocess.Popen(
        guarded_command(command),
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=sanitized_environment(dict(os.environ)),
        text=True,
        start_new_session=True,
    )
    try:
        output, _ = child.communicate(timeout=max(0.01, timeout))
        return {
            "command": list(command),
            "returncode": child.returncode,
            "output": _bounded_output(output.strip(), MAX_VALIDATION_OUTPUT_CHARS),
        }
    except subprocess.TimeoutExpired:
        signal_process_group(child, signal.SIGTERM)
        try:
            output, _ = child.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            signal_process_group(child, signal.SIGKILL)
            output, _ = child.communicate()
        return {
            "command": list(command),
            "returncode": 124,
            "output": _bounded_output(
                (output or "").strip(), MAX_VALIDATION_OUTPUT_CHARS
            ),
            "timed_out": True,
        }


class DelegationService:
    """Run one mini-swe-agent task and publish compact, dashboard-readable state."""

    def __init__(
        self,
        configuration: DelegationConfiguration,
        *,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.configuration = configuration
        self.repo = configuration.repo.resolve()
        self.now = now
        self.runtime = self.repo / ".coordination" / "runtime" / "delegations"

    def _state_path(self, delegation_id: str) -> Path:
        return self.runtime / f"{delegation_id}.json"

    def _write_state(self, path: Path, state: dict[str, object]) -> None:
        state["updated_at_epoch"] = self.now()
        atomic_json(path, state)

    def _remove_worktree(self, worktree: Path) -> None:
        if worktree.exists():
            with subprocess.Popen(
                [shutil.which("git") or "git", "-C", str(self.repo), "worktree", "remove", "--force", str(worktree)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ) as child:
                child.wait(timeout=30)
        with subprocess.Popen(
            [shutil.which("git") or "git", "-C", str(self.repo), "worktree", "prune"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ) as child:
            child.wait(timeout=30)

    def delegate(
        self,
        objective: str,
        allowed_paths: Sequence[str],
        validation_commands: Sequence[Sequence[str]] | None = None,
        routing_score: int = 0,
        routing_rationale: str = "",
    ) -> dict[str, object]:
        if not isinstance(objective, str) or not objective.strip():
            raise ValueError("objective must be a non-empty string")
        if len(objective) > MAX_OBJECTIVE_CHARS or "\x00" in objective:
            raise ValueError(f"objective must not exceed {MAX_OBJECTIVE_CHARS} characters")
        if not isinstance(routing_score, int) or isinstance(routing_score, bool):
            raise ValueError("routing_score must be an integer")
        if not 8 <= routing_score <= 10:
            raise ValueError(
                "routing_score must be 8 to 10; split or retain lower-scoring work"
            )
        if (
            not isinstance(routing_rationale, str)
            or not routing_rationale.strip()
            or len(routing_rationale) > 2_000
            or "\x00" in routing_rationale
        ):
            raise ValueError("routing_rationale must be a non-empty bounded string")
        patterns = normalize_allowed_paths(allowed_paths)
        validations = normalize_validation_commands(validation_commands)
        if not (self.repo / ".git").exists():
            raise ValueError("delegation requires a Git repository")
        executable = shutil.which(self.configuration.mini_command)
        candidate = Path(self.configuration.mini_command).expanduser()
        if executable is None and candidate.is_file():
            executable = str(candidate.resolve())
        if executable is None:
            raise ValueError(
                f"mini-swe-agent command not found: {self.configuration.mini_command}"
            )

        delegation_id = f"d-{uuid.uuid4().hex[:12]}"
        started = self.now()
        worktree_root = _worktree_root(self.repo)
        worktree = worktree_root / delegation_id
        state_path = self._state_path(delegation_id)
        trajectory_path = self.runtime / f"{delegation_id}.trajectory.json"
        patch_path = self.runtime / f"{delegation_id}.patch"
        log_path = self.runtime / f"{delegation_id}.log"
        state: dict[str, object] = {
            "id": delegation_id,
            "state": "starting",
            "objective": objective.strip(),
            "routing_score": routing_score,
            "routing_rationale": routing_rationale.strip(),
            "allowed_paths": list(patterns),
            "validation_commands": [list(command) for command in validations],
            "provider_id": "mini-swe-agent",
            "model": self.configuration.model or "mini-swe-agent configured model",
            "started_at_epoch": started,
            "steps": 0,
            "usage": trajectory_usage({}),
            "changed_files": [],
            "violations": [],
            "worktree": str(worktree),
            "patch_path": str(patch_path),
            "trajectory_path": str(trajectory_path),
            "log_path": str(log_path),
        }
        self._write_state(state_path, state)
        child: subprocess.Popen[bytes] | None = None
        try:
            worktree_root.mkdir(parents=True, exist_ok=True)
            _git(self.repo, "worktree", "add", "--detach", str(worktree), "HEAD")
            prompt = _worker_prompt(objective, patterns, validations)
            command_args = SimpleNamespace(
                model=self.configuration.model,
                effort=self.configuration.effort,
                config=self.configuration.config,
                step_limit=self.configuration.step_limit,
                timeout_seconds=self.configuration.timeout_seconds,
                cost_limit=self.configuration.cost_limit,
                api_base=self.configuration.api_base,
                provider=self.configuration.provider,
                profile="bounded",
            )
            command = build_command(command_args, executable, prompt, trajectory_path)
            child_env = worker_environment(
                dict(os.environ), self.configuration.api_key_env
            )
            state["state"] = "running"
            self._write_state(state_path, state)
            with log_path.open("wb") as log:
                child = subprocess.Popen(
                    guarded_command(command),
                    cwd=worktree,
                    env=child_env,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
                deadline = time.monotonic() + self.configuration.timeout_seconds + self.configuration.timeout_grace_seconds
                timed_out = False
                while child.poll() is None:
                    trajectory = load_trajectory(trajectory_path)
                    state["steps"] = trajectory_steps(trajectory)
                    state["usage"] = trajectory_usage(trajectory)
                    state["worker_pid"] = child.pid
                    self._write_state(state_path, state)
                    if time.monotonic() >= deadline:
                        timed_out = True
                        signal_process_group(child, signal.SIGTERM)
                        try:
                            child.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            signal_process_group(child, signal.SIGKILL)
                        break
                    time.sleep(self.configuration.progress_interval)
                returncode = child.wait()

            trajectory = load_trajectory(trajectory_path)
            validation_results: list[dict[str, object]] = []
            validation_deadline = time.monotonic() + VALIDATION_BUDGET_SECONDS
            for command in validations:
                remaining = validation_deadline - time.monotonic()
                if remaining <= 0:
                    validation_results.append(
                        {
                            "command": list(command),
                            "returncode": 124,
                            "output": "shared validation time budget exhausted",
                            "timed_out": True,
                        }
                    )
                    continue
                validation_results.append(
                    run_validation(command, worktree, remaining)
                )
            _git(worktree, "add", "-A")
            names = _git(worktree, "diff", "--cached", "--name-only", "-z", "HEAD").stdout
            changed_files = [name for name in names.split("\0") if name]
            violations = [name for name in changed_files if not path_is_allowed(name, patterns)]
            patch = _git(
                worktree,
                "diff",
                "--cached",
                "--binary",
                "--full-index",
                "HEAD",
                "--",
            ).stdout
            patch_path.write_text(patch, encoding="utf-8")
            info = trajectory_info(trajectory)
            worker_status = str(info.get("exit_status") or "not recorded")
            validations_passed = all(item["returncode"] == 0 for item in validation_results)
            ready = (
                not timed_out
                and returncode == 0
                and worker_status.lower() == "submitted"
                and not violations
                and validations_passed
                and bool(changed_files)
            )
            state.update(
                {
                    "state": "ready_for_review" if ready else "needs_review",
                    "completed_at_epoch": self.now(),
                    "returncode": 124 if timed_out else returncode,
                    "worker_status": worker_status,
                    "summary": _bounded_output(
                        str(info.get("submission") or "No worker summary recorded."),
                        MAX_SUMMARY_CHARS,
                    ),
                    "steps": trajectory_steps(trajectory),
                    "usage": trajectory_usage(trajectory),
                    "changed_files": changed_files,
                    "violations": violations,
                    "validation_results": validation_results,
                    "patch_bytes": len(patch.encode("utf-8")),
                }
            )
            self._write_state(state_path, state)
            return {
                "delegation_id": delegation_id,
                "state": state["state"],
                "model": state["model"],
                "summary": state["summary"],
                "changed_files": changed_files,
                "violations": violations,
                "validation_results": validation_results,
                "usage": state["usage"],
                "patch_path": str(patch_path),
                "instruction": (
                    "Review the saved patch and validation evidence. Apply it to the supervisor "
                    "working tree only if it satisfies the assignment, then run appropriate tests."
                ),
            }
        except Exception as error:
            if child is not None and child.poll() is None:
                signal_process_group(child, signal.SIGTERM)
            state.update(
                {
                    "state": "failed",
                    "completed_at_epoch": self.now(),
                    "error": str(error)[:MAX_CAPTURE_CHARS],
                }
            )
            self._write_state(state_path, state)
            return {
                "delegation_id": delegation_id,
                "state": "failed",
                "error": state["error"],
                "instruction": "Do not apply this delegation; handle the task directly or correct the request.",
            }
        finally:
            try:
                self._remove_worktree(worktree)
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                state["cleanup_warning"] = str(error)[:MAX_CAPTURE_CHARS]
                self._write_state(state_path, state)
