"""Local stdio MCP facade for Coordinator's bounded implementation adapter."""

from __future__ import annotations

import argparse
from pathlib import Path

from mcp.server import MCPServer

from .delegation import DelegationConfiguration, DelegationService
from .executor_adapters import MiniSweAgentExecutorAdapter, validate_mini_adapter


def build_server(service: DelegationService) -> MCPServer:
    server = MCPServer(
        "coordinator-delegation",
        description="Delegate bounded mechanical implementation to Coordinator's local worker.",
        instructions=(
            "Use this server for well-scoped implementation that does not require frontier-model "
            "reasoning. Supply narrow allowed paths and shell-free validation argument arrays. "
            "Review every returned patch before applying it."
        ),
    )

    @server.tool(
        name="delegate_implementation",
        description=(
            "Run one bounded implementation task with the configured local worker in an isolated "
            "git worktree. Returns a compact summary, validation evidence, usage, changed files, "
            "and a saved patch path for supervisor review. The tool never applies the patch."
        ),
        structured_output=True,
    )
    def delegate_implementation(
        objective: str,
        allowed_paths: list[str],
        validation_commands: list[list[str]],
        routing_score: int,
        routing_rationale: str,
    ) -> dict[str, object]:
        return service.delegate(
            objective,
            allowed_paths,
            validation_commands,
            routing_score,
            routing_rationale,
        )

    return server


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--mini-command", default="mini")
    parser.add_argument("--model", default="")
    parser.add_argument(
        "--effort",
        choices=("none", "low", "medium", "high", "xhigh", "max"),
        default="",
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument("--api-base", default="")
    parser.add_argument("--provider", default="openai")
    parser.add_argument("--api-key-env", default="")
    parser.add_argument("--step-limit", type=int, default=12)
    parser.add_argument("--cost-limit", type=float, default=0.0)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.model:
        raise ValueError("--model is required")
    if args.step_limit <= 0 or args.timeout_seconds <= 0 or args.cost_limit < 0:
        raise ValueError("delegation limits are invalid")
    adapter = validate_mini_adapter(
        MiniSweAgentExecutorAdapter(
            command_name=args.mini_command,
            model=args.model,
            effort=args.effort,
            config=args.config,
            api_base=args.api_base,
            provider=args.provider,
            api_key_env=args.api_key_env,
            step_limit=args.step_limit,
            cost_limit=args.cost_limit,
            timeout_seconds=args.timeout_seconds,
        )
    )
    service = DelegationService(
        DelegationConfiguration(
            repo=args.repo,
            mini_command=adapter.command_name,
            model=adapter.model,
            effort=adapter.effort,
            config=adapter.config,
            api_base=adapter.api_base,
            provider=adapter.provider,
            api_key_env=adapter.api_key_env,
            step_limit=adapter.step_limit,
            cost_limit=adapter.cost_limit,
            timeout_seconds=adapter.timeout_seconds,
        )
    )
    build_server(service).run("stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
