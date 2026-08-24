"""Behavioral tests for bounded MCP implementation delegation."""

from __future__ import annotations

import asyncio
import json
from argparse import Namespace
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from coordinator import delegation_mcp
from coordinator.delegation import (
    DelegationConfiguration,
    DelegationService,
    normalize_allowed_paths,
    normalize_validation_commands,
    path_is_allowed,
    run_validation,
    worker_environment,
)
from coordinator.delegation_mcp import build_server
from coordinator.run_claude_turn import delegation_mcp_configuration, handoff_prompt


class DelegationValidationTests(unittest.TestCase):
    def test_paths_and_commands_reject_escape_and_shell_shaped_values(self) -> None:
        self.assertEqual(normalize_allowed_paths(["src/**"]), ("src/**",))
        self.assertTrue(path_is_allowed("src/example.py", ("src/**",)))
        self.assertTrue(path_is_allowed("src/deep/example.py", ("src/**",)))
        self.assertFalse(path_is_allowed("src/deep/example.py", ("src/*.py",)))
        with self.assertRaisesRegex(ValueError, "safe repository-relative"):
            normalize_allowed_paths(["../outside"])
        with self.assertRaisesRegex(ValueError, "administration"):
            normalize_allowed_paths([".coordination/**"])
        with self.assertRaisesRegex(ValueError, "safe repository-relative"):
            normalize_allowed_paths(["**/*"])
        with self.assertRaisesRegex(ValueError, "argument array"):
            normalize_validation_commands(["python -m unittest"])  # type: ignore[list-item]
        with self.assertRaisesRegex(ValueError, "shell or network"):
            normalize_validation_commands([["bash", "-lc", "python -m unittest"]])

    def test_server_publishes_one_explicit_structured_tool_contract(self) -> None:
        service = DelegationService(DelegationConfiguration(Path.cwd()))
        tools = asyncio.run(build_server(service).list_tools())
        self.assertEqual([tool.name for tool in tools], ["delegate_implementation"])
        schema = tools[0].input_schema
        self.assertEqual(
            set(schema["required"]),
            {
                "objective",
                "allowed_paths",
                "validation_commands",
                "routing_score",
                "routing_rationale",
            },
        )

    def test_server_forwards_the_typed_request_and_returns_structured_output(self) -> None:
        service = mock.Mock()
        service.delegate.return_value = {
            "state": "ready_for_review",
            "delegation_id": "d-example",
        }
        result = asyncio.run(
            build_server(service).call_tool(
                "delegate_implementation",
                {
                    "objective": "Update the bounded fixture.",
                    "allowed_paths": ["src/example.py"],
                    "validation_commands": [[sys.executable, "-m", "unittest"]],
                    "routing_score": 9,
                    "routing_rationale": "Local, reversible, and deterministic.",
                },
            )
        )
        service.delegate.assert_called_once_with(
            "Update the bounded fixture.",
            ["src/example.py"],
            [[sys.executable, "-m", "unittest"]],
            9,
            "Local, reversible, and deterministic.",
        )
        self.assertEqual(
            result.structured_content,
            {"state": "ready_for_review", "delegation_id": "d-example"},
        )

    def test_mcp_entrypoint_validates_and_runs_the_configured_stdio_server(self) -> None:
        server = mock.Mock()
        with (
            mock.patch.object(
                delegation_mcp, "validate_mini_adapter", side_effect=lambda adapter: adapter
            ),
            mock.patch.object(delegation_mcp, "build_server", return_value=server) as build,
        ):
            result = delegation_mcp.main(
                [
                    "--repo",
                    str(Path.cwd()),
                    "--mini-command",
                    "mini",
                    "--model",
                    "openai/local-qwen",
                    "--api-base",
                    "http://127.0.0.1:8000/v1",
                    "--step-limit",
                    "8",
                    "--timeout-seconds",
                    "600",
                ]
            )
        self.assertEqual(result, 0)
        server.run.assert_called_once_with("stdio")
        configuration = build.call_args.args[0].configuration
        self.assertEqual(configuration.model, "openai/local-qwen")
        self.assertEqual(configuration.step_limit, 8)
        self.assertEqual(configuration.timeout_seconds, 600)

    def test_mcp_entrypoint_rejects_missing_model_and_invalid_limits(self) -> None:
        with self.assertRaisesRegex(ValueError, "--model is required"):
            delegation_mcp.main(["--repo", str(Path.cwd())])
        with self.assertRaisesRegex(ValueError, "limits are invalid"):
            delegation_mcp.main(
                [
                    "--repo",
                    str(Path.cwd()),
                    "--model",
                    "openai/local-qwen",
                    "--step-limit",
                    "0",
                ]
            )

    def test_worker_environment_forwards_only_the_selected_endpoint_secret(self) -> None:
        child = worker_environment(
            {
                "PATH": "/usr/bin",
                "LOCAL_MODEL_KEY": "selected-secret",
                "ANTHROPIC_API_KEY": "must-not-leak",
                "COORDINATOR_OIDC_CLIENT_SECRET": "must-not-leak-either",
            },
            "LOCAL_MODEL_KEY",
        )
        self.assertEqual(
            child,
            {
                "PATH": "/usr/bin",
                "PYTHONDONTWRITEBYTECODE": "1",
                "OPENAI_API_KEY": "selected-secret",
            },
        )

    def test_validation_has_a_timeout_and_cannot_read_unrelated_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            "os.environ", {"COORDINATOR_TEST_SECRET": "must-not-leak"}, clear=False
        ):
            result = run_validation(
                [
                    sys.executable,
                    "-c",
                    "import os; print(os.environ.get('COORDINATOR_TEST_SECRET', 'absent'))",
                ],
                Path(directory),
                1,
            )
            timed_out = run_validation(
                [sys.executable, "-c", "import time; time.sleep(5)"],
                Path(directory),
                0.01,
            )
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["output"], "absent")
        self.assertEqual(timed_out["returncode"], 124)
        self.assertTrue(timed_out["timed_out"])

    def test_validation_output_is_bounded_for_mcp_transport(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = run_validation(
                [sys.executable, "-c", "print('x' * 5000)"],
                Path(directory),
                1,
            )
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(len(str(result["output"])), 1_000)

    def test_claude_receives_the_auditable_routing_policy_and_invocation_config(self) -> None:
        prompt = handoff_prompt("task-1", "0", "Do work.", local_delegation=True)
        self.assertIn("score 0-2 each", prompt)
        self.assertIn("Delegate at 8-10", prompt)
        self.assertIn("authentication/security-boundary", prompt)
        config = json.loads(
            delegation_mcp_configuration(
                Namespace(
                    delegate_mini_command="mini",
                    delegate_model="openai/local-qwen",
                    delegate_effort="medium",
                    delegate_provider="openai",
                    delegate_api_key_env="LOCAL_MODEL_KEY",
                    delegate_step_limit=8,
                    delegate_cost_limit=0.0,
                    delegate_timeout_seconds=600,
                    delegate_config=None,
                    delegate_api_base="http://127.0.0.1:8000/v1",
                ),
                Path("/tmp/project"),
            )
        )
        server = config["mcpServers"]["coordinator-delegation"]
        self.assertEqual(server["type"], "stdio")
        self.assertTrue(server["alwaysLoad"])
        self.assertEqual(server["timeout"], 780_000)
        self.assertIn("LOCAL_MODEL_KEY", server["args"])
        self.assertIn("medium", server["args"])
        self.assertNotIn("secret-value", json.dumps(config))


class DelegationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.email", "tests@example.invalid"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo), "config", "user.name", "Coordinator Tests"],
            check=True,
        )
        (self.repo / "src").mkdir()
        (self.repo / "src/example.py").write_text("VALUE = 1\n", encoding="utf-8")
        (self.repo / ".coordination/runtime").mkdir(parents=True)
        subprocess.run(["git", "-C", str(self.repo), "add", "src/example.py"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "commit", "-qm", "initial"], check=True)
        self.worker = self.base / "fake-mini"
        self.worker.write_text(
            f"#!{sys.executable}\n"
            "import json, pathlib, sys\n"
            "args = sys.argv[1:]\n"
            "output = pathlib.Path(args[args.index('--output') + 1])\n"
            "target = pathlib.Path('src/example.py')\n"
            "target.write_text('VALUE = 2\\n', encoding='utf-8')\n"
            "output.parent.mkdir(parents=True, exist_ok=True)\n"
            "output.write_text(json.dumps({"
            "'info': {'exit_status': 'submitted', 'submission': 'updated value', "
            "'model_stats': {'api_calls': 2}}, 'messages': []}), encoding='utf-8')\n",
            encoding="utf-8",
        )
        self.worker.chmod(0o755)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def service(self) -> DelegationService:
        return DelegationService(
            DelegationConfiguration(
                repo=self.repo,
                mini_command=str(self.worker),
                model="openai/local-qwen",
                timeout_seconds=10,
                timeout_grace_seconds=0,
                progress_interval=0.01,
            )
        )

    def test_success_returns_review_patch_without_touching_supervisor_tree(self) -> None:
        result = self.service().delegate(
            "Update the example value.",
            ["src/example.py"],
            [[sys.executable, "-c", "from src.example import VALUE; assert VALUE == 2"]],
            9,
            "Exact one-file edit with deterministic validation and trivial rollback.",
        )

        self.assertEqual(result["state"], "ready_for_review")
        self.assertEqual(result["changed_files"], ["src/example.py"])
        self.assertEqual((self.repo / "src/example.py").read_text(), "VALUE = 1\n")
        patch = Path(str(result["patch_path"]))
        self.assertIn("+VALUE = 2", patch.read_text(encoding="utf-8"))
        state = json.loads(patch.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(state["routing_score"], 9)
        self.assertEqual(state["state"], "ready_for_review")
        self.assertFalse(Path(state["worktree"]).exists())

    def test_path_violation_is_returned_for_review_and_never_applied(self) -> None:
        result = self.service().delegate(
            "Update a file.",
            ["tests/**"],
            [[sys.executable, "-c", "assert True"]],
            8,
            "Bounded and reversible, with review of the exact diff.",
        )
        self.assertEqual(result["state"], "needs_review")
        self.assertEqual(result["violations"], ["src/example.py"])
        self.assertEqual((self.repo / "src/example.py").read_text(), "VALUE = 1\n")

    def test_low_confidence_work_is_rejected_before_creating_a_job(self) -> None:
        with self.assertRaisesRegex(ValueError, "8 to 10"):
            self.service().delegate(
                "Choose and implement an architecture.",
                ["src/**"],
                [],
                6,
                "The requirements are ambiguous.",
            )
        delegation_dir = self.repo / ".coordination/runtime/delegations"
        self.assertFalse(delegation_dir.exists())


if __name__ == "__main__":
    unittest.main()
