"""Built-in implementation-agent adapters used by the coordination watcher.

Adapters deliberately stop at the process boundary.  The provider runtime owns its
native model loop and tools; Coordinator owns task selection, process supervision,
portable coordination state, and the subsequent Codex review.
"""

from __future__ import annotations

import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit


EXECUTOR_ADAPTERS = ("claude", "mini-swe-agent")


class ExecutorAdapter(Protocol):
    """The stable process-launch seam for one bounded implementation handoff."""

    id: str
    display_name: str

    def executable(self) -> str: ...

    def command(self, repo: Path) -> list[str]: ...

    def watcher_arguments(self) -> list[str]: ...


@dataclass(frozen=True)
class ClaudeExecutorAdapter:
    command_name: str = "claude"
    permission_mode: str = "auto"
    model: str = "opus"
    subagent_model: str = "sonnet"
    max_turns: int = 40
    id: str = "claude"
    display_name: str = "Claude Code"

    def executable(self) -> str:
        return self.command_name

    def command(self, repo: Path) -> list[str]:
        return [
            sys.executable,
            "-m",
            "coordinator.run_claude_turn",
            "--repo",
            str(repo),
            "--claude-command",
            self.command_name,
            "--permission-mode",
            self.permission_mode,
            "--model",
            self.model,
            "--subagent-model",
            self.subagent_model,
            "--max-turns",
            str(self.max_turns),
        ]

    def watcher_arguments(self) -> list[str]:
        return [
            "--executor-adapter",
            self.id,
            "--claude-command",
            self.command_name,
            "--claude-permission-mode",
            self.permission_mode,
            "--claude-model",
            self.model,
            "--claude-subagent-model",
            self.subagent_model,
            "--claude-max-turns",
            str(self.max_turns),
        ]


@dataclass(frozen=True)
class MiniSweAgentExecutorAdapter:
    command_name: str = "mini"
    model: str = ""
    config: Path | None = None
    api_base: str = ""
    provider: str = "openai"
    api_key_env: str = "OPENAI_API_KEY"
    step_limit: int = 12
    cost_limit: float = 0.0
    timeout_seconds: int = 900
    id: str = "mini-swe-agent"
    display_name: str = "mini-swe-agent"

    def executable(self) -> str:
        return self.command_name

    def command(self, repo: Path) -> list[str]:
        command = [
            sys.executable,
            "-m",
            "coordinator.run_mini_swe_turn",
            "--repo",
            str(repo),
            "--mini-command",
            self.command_name,
            "--provider",
            self.provider,
            "--api-key-env",
            self.api_key_env,
            "--step-limit",
            str(self.step_limit),
            "--cost-limit",
            str(self.cost_limit),
            "--timeout-seconds",
            str(self.timeout_seconds),
        ]
        if self.model:
            command.extend(("--model", self.model))
        if self.config is not None:
            command.extend(("--config", str(self.config)))
        if self.api_base:
            command.extend(("--api-base", self.api_base))
        return command

    def watcher_arguments(self) -> list[str]:
        arguments = [
            "--executor-adapter",
            self.id,
            "--mini-swe-command",
            self.command_name,
            "--mini-swe-provider",
            self.provider,
            "--mini-swe-api-key-env",
            self.api_key_env,
            "--mini-swe-step-limit",
            str(self.step_limit),
            "--mini-swe-cost-limit",
            str(self.cost_limit),
            "--mini-swe-timeout-seconds",
            str(self.timeout_seconds),
        ]
        if self.model:
            arguments.extend(("--mini-swe-model", self.model))
        if self.config is not None:
            arguments.extend(("--mini-swe-config", str(self.config)))
        if self.api_base:
            arguments.extend(("--mini-swe-api-base", self.api_base))
        return arguments


def from_namespace(args: object) -> ExecutorAdapter:
    """Build the selected adapter from watcher/application arguments."""

    selected = str(getattr(args, "executor_adapter", "claude"))
    if selected == "claude":
        return ClaudeExecutorAdapter(
            command_name=str(getattr(args, "claude_command", "claude")),
            permission_mode=str(getattr(args, "claude_permission_mode", "auto")),
            model=str(getattr(args, "claude_model", "opus")),
            subagent_model=str(getattr(args, "claude_subagent_model", "sonnet")),
            max_turns=int(getattr(args, "claude_max_turns", 40)),
        )
    if selected == "mini-swe-agent":
        raw_config = getattr(args, "mini_swe_config", None)
        adapter = MiniSweAgentExecutorAdapter(
            command_name=str(getattr(args, "mini_swe_command", "mini")),
            model=str(getattr(args, "mini_swe_model", "")),
            config=Path(raw_config) if raw_config else None,
            api_base=str(getattr(args, "mini_swe_api_base", "")),
            provider=str(getattr(args, "mini_swe_provider", "openai")),
            api_key_env=str(getattr(args, "mini_swe_api_key_env", "OPENAI_API_KEY")),
            step_limit=int(getattr(args, "mini_swe_step_limit", 12)),
            cost_limit=float(getattr(args, "mini_swe_cost_limit", 0.0)),
            timeout_seconds=int(getattr(args, "mini_swe_timeout_seconds", 900)),
        )
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", adapter.api_key_env):
            raise ValueError("mini-swe-agent API-key environment name is invalid")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", adapter.provider):
            raise ValueError("mini-swe-agent provider name is invalid")
        if adapter.api_base:
            endpoint = urlsplit(adapter.api_base)
            if endpoint.scheme not in {"http", "https"} or not endpoint.netloc:
                raise ValueError("mini-swe-agent API base must be an absolute HTTP(S) URL")
            if endpoint.username is not None or endpoint.password is not None:
                raise ValueError("mini-swe-agent API base must not contain embedded credentials")
            if endpoint.query or endpoint.fragment:
                raise ValueError("mini-swe-agent API base must not contain a query or fragment")
        return adapter
    raise ValueError(f"unknown executor adapter: {selected!r}")


def resolve_executable(adapter: ExecutorAdapter) -> str | None:
    """Resolve an adapter executable without interpreting shell syntax."""

    command = adapter.executable()
    candidate = Path(command).expanduser()
    if candidate.is_file():
        return str(candidate.resolve())
    return shutil.which(command)
