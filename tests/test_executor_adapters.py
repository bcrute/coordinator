"""Focused contracts for built-in implementation-agent adapters."""

from __future__ import annotations

import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from argparse import Namespace
from contextlib import redirect_stderr
from pathlib import Path

from coordinator.executor_adapters import (
    ClaudeExecutorAdapter,
    MiniSweAgentExecutorAdapter,
    from_namespace,
)
from coordinator.configuration import parse_args as parse_web_args
from coordinator.processes import default_watcher_command
from coordinator.run_mini_swe_turn import (
    build_command,
    parse_args as parse_mini_args,
    trajectory_usage,
)


ROOT = Path(__file__).resolve().parents[1]


def prepared_repo(root: Path) -> None:
    for name in ("planner", "coder", "reviews", "runtime"):
        (root / ".coordination" / name).mkdir(parents=True, exist_ok=True)
    (root / ".coordination" / "README.md").write_text("# Coordination\n", encoding="utf-8")
    (root / ".coordination" / "planner" / "goal.md").write_text(
        "# Overall goal\n\n- Goal ID: `LOCAL-001`\n- State: `active`\n",
        encoding="utf-8",
    )
    (root / ".coordination" / "planner" / "current-task.md").write_text(
        "# Current task\n\n- Task ID: `LOCAL-TASK-001`\n- State: `ready`\n"
        "- Review round: `0`\n\n## Objective\n\nCreate one small file.\n",
        encoding="utf-8",
    )
    (root / ".coordination" / "coder" / "status.md").write_text(
        "# Coder status\n\n- Task ID: `none`\n- State: `idle`\n- Review round: `0`\n",
        encoding="utf-8",
    )
    (root / ".coordination" / "reviews" / "latest.md").write_text(
        "# Latest review\n\n- Task ID: `none`\n- Verdict: `not_reviewed`\n",
        encoding="utf-8",
    )


def executable(path: Path, source: str) -> Path:
    path.write_text("#!/usr/bin/env python3.14\n" + source, encoding="utf-8")
    path.chmod(0o755)
    return path


def mini_turn(repo: Path, command: str, *arguments: str, env=None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "coordinator.run_mini_swe_turn",
            "--repo",
            str(repo),
            "--mini-command",
            command,
            "--progress-interval",
            "0.01",
            *arguments,
        ],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class AdapterContractTests(unittest.TestCase):
    def test_default_claude_adapter_preserves_existing_runner_contract(self) -> None:
        adapter = from_namespace(Namespace())
        self.assertIsInstance(adapter, ClaudeExecutorAdapter)
        self.assertEqual(
            adapter.command(Path("/tmp/project")),
            [
                sys.executable,
                "-m",
                "coordinator.run_claude_turn",
                "--repo",
                "/tmp/project",
                "--claude-command",
                "claude",
                "--permission-mode",
                "auto",
                "--model",
                "opus",
                "--subagent-model",
                "sonnet",
                "--max-turns",
                "40",
            ],
        )
        self.assertEqual(
            adapter.watcher_arguments(),
            [
                "--executor-adapter",
                "claude",
                "--claude-command",
                "claude",
                "--claude-permission-mode",
                "auto",
                "--claude-model",
                "opus",
                "--claude-subagent-model",
                "sonnet",
                "--claude-max-turns",
                "40",
            ],
        )

    def test_toml_selects_mini_and_resolves_its_config_beside_toml(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "workflow.toml"
            settings.write_text(
                'executor_adapter = "mini-swe-agent"\n'
                'mini_swe_model = "openai/local"\n'
                'mini_swe_config = "mini.yaml"\n'
                'mini_swe_api_base = "http://127.0.0.1:8000/v1"\n'
                'mini_swe_step_limit = 9\n'
                'mini_swe_cost_limit = 0.0\n'
                'mini_swe_timeout_seconds = 240\n',
                encoding="utf-8",
            )
            args = parse_web_args(["--config", str(settings)])
            self.assertEqual(args.executor_adapter, "mini-swe-agent")
            self.assertEqual(args.mini_swe_config, root / "mini.yaml")
            self.assertEqual(args.mini_swe_step_limit, 9)
            self.assertEqual(args.mini_swe_timeout_seconds, 240)

    def test_mini_adapter_builds_runner_command_without_secret_value(self) -> None:
        adapter = MiniSweAgentExecutorAdapter(
            command_name="mini-local",
            model="openai/local-coder",
            api_base="http://127.0.0.1:8000/v1",
            api_key_env="LOCAL_MODEL_KEY",
            step_limit=7,
            timeout_seconds=120,
        )
        command = adapter.command(Path("/tmp/project"))
        self.assertIn("coordinator.run_mini_swe_turn", command)
        self.assertIn("LOCAL_MODEL_KEY", command)
        self.assertNotIn("secret-value", command)
        self.assertIn("7", command)
        self.assertIn("120", command)

    def test_namespace_defaults_to_claude_and_can_select_mini(self) -> None:
        self.assertEqual(from_namespace(Namespace()).id, "claude")
        selected = from_namespace(
            Namespace(executor_adapter="mini-swe-agent", mini_swe_model="local/model")
        )
        self.assertEqual(selected.id, "mini-swe-agent")
        self.assertEqual(selected.model, "local/model")

    def test_namespace_rejects_untrusted_adapter_configuration(self) -> None:
        invalid = (
            ({"mini_swe_api_key_env": "BAD-NAME"}, "environment name"),
            ({"mini_swe_provider": "openai;command"}, "provider name"),
            ({"mini_swe_api_base": "relative/v1"}, "absolute HTTP"),
            (
                {"mini_swe_api_base": "http://user:password@127.0.0.1/v1"},
                "embedded credentials",
            ),
            ({"mini_swe_api_base": "http://127.0.0.1/v1?secret=value"}, "query"),
        )
        for values, message in invalid:
            with self.subTest(values=values):
                with self.assertRaisesRegex(ValueError, message):
                    from_namespace(Namespace(executor_adapter="mini-swe-agent", **values))

        with self.assertRaisesRegex(ValueError, "unknown executor adapter"):
            from_namespace(Namespace(executor_adapter="unknown"))

    def test_web_watcher_command_carries_trusted_adapter_configuration(self) -> None:
        adapter = MiniSweAgentExecutorAdapter(
            command_name="/opt/tools/mini",
            model="openai/local",
            api_base="http://127.0.0.1:8000/v1",
            step_limit=6,
        )
        command = default_watcher_command(Path("/tmp/project"), adapter)
        self.assertIn("--executor-adapter", command)
        self.assertIn("mini-swe-agent", command)
        self.assertIn("--mini-swe-api-base", command)
        self.assertIn("http://127.0.0.1:8000/v1", command)

    def test_watcher_selects_mini_runner_for_ready_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            prepared_repo(repo)
            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "coordinator.watch_coordination",
                    "--repo",
                    str(repo),
                    "--role",
                    "executor",
                    "--once",
                    "--dry-run",
                    "--executor-adapter",
                    "mini-swe-agent",
                    "--mini-swe-command",
                    "/bin/true",
                    "--mini-swe-model",
                    "openai/local-test",
                ],
                cwd=ROOT,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("coordinator.run_mini_swe_turn", completed.stdout)
            self.assertIn("--model openai/local-test", completed.stdout)


class MiniTrajectoryTests(unittest.TestCase):
    def test_litellm_usage_is_normalized_and_response_ids_are_deduplicated(self) -> None:
        response = {
            "id": "response-1",
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 12,
                "prompt_tokens_details": {"cached_tokens": 40},
            },
        }
        trajectory = {
            "messages": [
                {"role": "assistant", "extra": {"response": response}},
                {"role": "assistant", "extra": {"response": response}},
            ]
        }
        self.assertEqual(
            trajectory_usage(trajectory),
            {
                "input_tokens": 60,
                "cache_read_input_tokens": 40,
                "cache_creation_input_tokens": 0,
                "output_tokens": 12,
            },
        )

    def test_command_uses_default_config_then_bounded_overrides(self) -> None:
        args = Namespace(
            model="openai/local",
            config=None,
            step_limit=8,
            timeout_seconds=300,
            cost_limit=0.0,
            api_base="http://127.0.0.1:8000/v1",
            provider="openai",
        )
        command = build_command(args, "/usr/bin/mini", "task", Path("run.json"))
        first_config = command.index("--config")
        self.assertEqual(command[first_config + 1], "mini.yaml")
        self.assertIn("agent.step_limit=8", command)
        self.assertIn("agent.wall_time_limit_seconds=300", command)
        self.assertIn("model.cost_tracking=ignore_errors", command)
        self.assertIn("model.model_kwargs.api_base=http://127.0.0.1:8000/v1", command)

    def test_runner_cli_rejects_unsafe_or_unbounded_configuration(self) -> None:
        invalid = (
            (["--step-limit", "0"], "step-limit must be positive"),
            (["--cost-limit", "-1"], "cost-limit must not be negative"),
            (["--timeout-seconds", "0"], "timeout values must be positive"),
            (["--timeout-grace-seconds", "-1"], "timeout values must be positive"),
            (["--progress-interval", "0"], "progress-interval must be positive"),
            (["--provider", "openai;command"], "provider must contain only"),
            (["--api-key-env", "BAD-NAME"], "environment-variable name"),
            (["--api-base", "relative/v1"], "absolute HTTP"),
            (
                ["--api-base", "https://user:password@example.test/v1"],
                "embedded credentials",
            ),
            (["--api-base", "https://example.test/v1#fragment"], "query or fragment"),
        )
        for arguments, message in invalid:
            with self.subTest(arguments=arguments):
                stderr = io.StringIO()
                with redirect_stderr(stderr), self.assertRaises(SystemExit) as raised:
                    parse_mini_args(arguments)
                self.assertEqual(raised.exception.code, 2)
                self.assertIn(message, stderr.getvalue())


class MiniRunnerTests(unittest.TestCase):
    def test_fake_success_writes_adapter_owned_handoff_and_generic_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            prepared_repo(repo)
            fake = executable(
                repo / "fake-mini",
                "import json, os, sys\n"
                "from pathlib import Path\n"
                "args = sys.argv[1:]\n"
                "output = Path(args[args.index('--output') + 1])\n"
                "status = Path('.coordination/coder/status.md').read_text()\n"
                "Path('saw-implementing.txt').write_text(str('`implementing`' in status))\n"
                "Path('mapped-key.txt').write_text(os.environ.get('OPENAI_API_KEY', ''))\n"
                "Path('local-change.txt').write_text('implemented\\n')\n"
                "output.parent.mkdir(parents=True, exist_ok=True)\n"
                "output.write_text(json.dumps({"
                "'trajectory_format':'mini-swe-agent-1.1',"
                "'info':{'exit_status':'Submitted','submission':'Implemented the file.',"
                "'model_stats':{'api_calls':2,'instance_cost':0}},"
                "'messages':[{'role':'assistant','extra':{"
                "'actions':[{'command':'python -m unittest -v'}],"
                "'response':{'id':'r1','usage':{'prompt_tokens':30,"
                "'completion_tokens':5,'prompt_tokens_details':{'cached_tokens':10}}}}}]}))\n",
            )
            env = {**os.environ, "LOCAL_MODEL_KEY": "test-only-secret"}
            completed = mini_turn(
                repo,
                str(fake),
                "--model",
                "openai/local-test",
                "--api-key-env",
                "LOCAL_MODEL_KEY",
                env=env,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual((repo / "saw-implementing.txt").read_text(), "True")
            self.assertEqual((repo / "mapped-key.txt").read_text(), "test-only-secret")
            status = (repo / ".coordination/coder/status.md").read_text()
            report = (repo / ".coordination/coder/latest-report.md").read_text()
            progress = json.loads(
                (repo / ".coordination/runtime/executor-progress.json").read_text()
            )
            self.assertIn("- State: `review`", status)
            self.assertIn("- Executor: `mini-swe-agent`", status)
            self.assertIn("Implemented the file.", report)
            self.assertIn("python -m unittest -v", report)
            self.assertEqual(progress["provider_id"], "mini-swe-agent")
            self.assertEqual(progress["steps"], 2)
            self.assertEqual(progress["usage"]["input_tokens"], 20)
            self.assertEqual(progress["usage"]["cache_read_input_tokens"], 10)
            self.assertEqual(progress["usage"]["output_tokens"], 5)
            persisted = "\n".join(
                (
                    completed.stdout,
                    completed.stderr,
                    status,
                    report,
                    json.dumps(progress),
                    next(
                        (repo / ".coordination/runtime/trajectories").glob("*.json")
                    ).read_text(encoding="utf-8"),
                )
            )
            self.assertNotIn("test-only-secret", persisted)

    def test_missing_runtime_fails_cleanly_without_writing_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            prepared_repo(repo)
            completed = mini_turn(
                repo,
                "definitely-missing-mini-command",
                env={**os.environ, "PATH": ""},
            )
            self.assertEqual(completed.returncode, 127)
            self.assertIn("mini-swe-agent command not found", completed.stderr)
            self.assertFalse(
                (repo / ".coordination/runtime/executor-progress.json").exists()
            )

    def test_agent_limit_exit_is_a_reviewable_blocked_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            prepared_repo(repo)
            fake = executable(
                repo / "fake-limited-mini",
                "import json, sys\n"
                "from pathlib import Path\n"
                "args = sys.argv[1:]\n"
                "output = Path(args[args.index('--output') + 1])\n"
                "output.parent.mkdir(parents=True, exist_ok=True)\n"
                "output.write_text(json.dumps({"
                "'info':{'exit_status':'LimitsExceeded','submission':'',"
                "'model_stats':{'api_calls':12}},'messages':[]}))\n",
            )
            completed = mini_turn(repo, str(fake))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            status = (repo / ".coordination/coder/status.md").read_text()
            self.assertIn("- State: `blocked`", status)
            self.assertIn("LimitsExceeded", status)

    def test_dry_run_redacts_assignment_and_does_not_mutate_handoff_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            prepared_repo(repo)
            before = (repo / ".coordination/coder/status.md").read_text(encoding="utf-8")
            completed = mini_turn(repo, "missing-is-allowed-for-dry-run", "--dry-run")
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("<coordination prompt>", completed.stdout)
            self.assertNotIn("Create one small file", completed.stdout)
            self.assertEqual(
                (repo / ".coordination/coder/status.md").read_text(encoding="utf-8"),
                before,
            )
            self.assertFalse(
                (repo / ".coordination/runtime/executor-progress.json").exists()
            )

    def test_handoff_requires_active_goal_and_assignable_task_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            missing = mini_turn(repo, "missing-mini", "--dry-run")
            self.assertEqual(missing.returncode, 2)
            self.assertIn("goal/task files are missing", missing.stderr)

            prepared_repo(repo)
            goal = repo / ".coordination/planner/goal.md"
            goal.write_text(
                "# Overall goal\n\n- Goal ID: `LOCAL-001`\n- State: `done`\n",
                encoding="utf-8",
            )
            inactive = mini_turn(repo, "missing-mini", "--dry-run")
            self.assertEqual(inactive.returncode, 2)
            self.assertIn("active overall goal", inactive.stderr)

            goal.write_text(
                "# Overall goal\n\n- Goal ID: `LOCAL-001`\n- State: `active`\n",
                encoding="utf-8",
            )
            task = repo / ".coordination/planner/current-task.md"
            task.write_text(
                "# Current task\n\n- Task ID: `LOCAL-TASK-001`\n"
                "- State: `accepted`\n- Review round: `0`\n",
                encoding="utf-8",
            )
            accepted = mini_turn(repo, "missing-mini", "--dry-run")
            self.assertEqual(accepted.returncode, 2)
            self.assertIn("ready or changes_requested task", accepted.stderr)

    def test_changes_requested_task_is_eligible_for_one_correction_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            prepared_repo(repo)
            task = repo / ".coordination/planner/current-task.md"
            task.write_text(
                task.read_text(encoding="utf-8").replace(
                    "- State: `ready`", "- State: `changes_requested`"
                ),
                encoding="utf-8",
            )
            completed = mini_turn(repo, "missing-mini", "--dry-run")
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("Would run one mini-swe-agent turn", completed.stdout)

    def test_missing_config_is_rejected_before_a_handoff_starts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            prepared_repo(repo)
            before = (repo / ".coordination/coder/status.md").read_text(encoding="utf-8")
            completed = mini_turn(
                repo,
                "/bin/true",
                "--config",
                str(repo / "missing-mini.yaml"),
            )
            self.assertEqual(completed.returncode, 2)
            self.assertIn("config not found", completed.stderr)
            self.assertEqual(
                (repo / ".coordination/coder/status.md").read_text(encoding="utf-8"),
                before,
            )

    def test_duplicate_round_and_existing_lock_are_refused_without_execution(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            prepared_repo(repo)
            marker = repo / "should-not-run"
            fake = executable(
                repo / "fake-mini",
                "from pathlib import Path\nPath('should-not-run').write_text('ran')\n",
            )
            (repo / ".coordination/coder/status.md").write_text(
                "# Coder status\n\n- Task ID: `LOCAL-TASK-001`\n"
                "- State: `review`\n- Review round: `0`\n",
                encoding="utf-8",
            )
            duplicate = mini_turn(repo, str(fake))
            self.assertEqual(duplicate.returncode, 2)
            self.assertIn("already has an executor handoff", duplicate.stderr)
            self.assertFalse(marker.exists())

            (repo / ".coordination/coder/status.md").write_text(
                "# Coder status\n\n- Task ID: `none`\n"
                "- State: `idle`\n- Review round: `0`\n",
                encoding="utf-8",
            )
            lock = repo / ".coordination/.mini-swe-agent-turn.lock"
            lock.write_text("held by test\n", encoding="utf-8")
            locked = mini_turn(repo, str(fake))
            self.assertEqual(locked.returncode, 2)
            self.assertIn("another mini-swe-agent turn may be active", locked.stderr)
            self.assertEqual(lock.read_text(encoding="utf-8"), "held by test\n")
            self.assertFalse(marker.exists())

    def test_successful_executor_that_changes_planner_state_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            prepared_repo(repo)
            fake = executable(
                repo / "fake-tampering-mini",
                "import json, sys\n"
                "from pathlib import Path\n"
                "args = sys.argv[1:]\n"
                "output = Path(args[args.index('--output') + 1])\n"
                "goal = Path('.coordination/planner/goal.md')\n"
                "goal.write_text(goal.read_text() + '\\nexecutor edit\\n')\n"
                "output.parent.mkdir(parents=True, exist_ok=True)\n"
                "output.write_text(json.dumps({'info': {"
                "'exit_status': 'Submitted', 'submission': 'claimed success'}, "
                "'messages': []}))\n",
            )
            completed = mini_turn(repo, str(fake))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            status = (repo / ".coordination/coder/status.md").read_text(encoding="utf-8")
            report = (repo / ".coordination/coder/latest-report.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("- State: `blocked`", status)
            self.assertIn("modified Coordinator-owned coordination files", status)
            self.assertIn(".coordination/planner/goal.md", report)

    def test_nonzero_exit_without_trajectory_is_truthfully_blocked_and_cleans_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            prepared_repo(repo)
            fake = executable(repo / "fake-failing-mini", "raise SystemExit(7)\n")
            completed = mini_turn(repo, str(fake))
            self.assertEqual(completed.returncode, 7)
            self.assertIn("exited with status 7", completed.stderr)
            status = (repo / ".coordination/coder/status.md").read_text(encoding="utf-8")
            report = (repo / ".coordination/coder/latest-report.md").read_text(
                encoding="utf-8"
            )
            progress = json.loads(
                (repo / ".coordination/runtime/executor-progress.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertIn("- State: `blocked`", status)
            self.assertIn("Process exit status: `7`", report)
            self.assertIn("Agent exit status: `not recorded`", report)
            self.assertEqual(progress["state"], "completed")
            self.assertFalse((repo / ".coordination/.mini-swe-agent-turn.lock").exists())

    def test_malformed_trajectory_cannot_be_reported_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            prepared_repo(repo)
            fake = executable(
                repo / "fake-malformed-mini",
                "import sys\n"
                "from pathlib import Path\n"
                "args = sys.argv[1:]\n"
                "output = Path(args[args.index('--output') + 1])\n"
                "output.parent.mkdir(parents=True, exist_ok=True)\n"
                "output.write_text('{not-json')\n",
            )
            completed = mini_turn(repo, str(fake))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            status = (repo / ".coordination/coder/status.md").read_text(encoding="utf-8")
            report = (repo / ".coordination/coder/latest-report.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("- State: `blocked`", status)
            self.assertIn("trajectory ended as not recorded", status)
            self.assertIn("Agent exit status: `not recorded`", report)

    def test_wall_timeout_terminates_process_group_and_records_blocked_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            prepared_repo(repo)
            fake = executable(
                repo / "fake-hanging-mini",
                "import subprocess, sys, time\n"
                "from pathlib import Path\n"
                "child = subprocess.Popen([sys.executable, '-c', "
                "'import time; time.sleep(300)'])\n"
                "Path('grandchild.pid').write_text(str(child.pid))\n"
                "time.sleep(300)\n",
            )
            completed = mini_turn(
                repo,
                str(fake),
                "--timeout-seconds",
                "1",
                "--timeout-grace-seconds",
                "0",
            )
            grandchild_pid = int((repo / "grandchild.pid").read_text(encoding="utf-8"))
            try:
                self.assertEqual(completed.returncode, 124)
                self.assertTrue(
                    wait_until(lambda: not process_alive(grandchild_pid)),
                    "the mini-swe-agent grandchild survived the wall-time limit",
                )
            finally:
                if process_alive(grandchild_pid):
                    os.kill(grandchild_pid, signal.SIGKILL)
            status = (repo / ".coordination/coder/status.md").read_text(encoding="utf-8")
            report = (repo / ".coordination/coder/latest-report.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("Coordinator wall-time limit exceeded", status)
            self.assertIn("Process exit status: `124`", report)
            self.assertFalse((repo / ".coordination/.mini-swe-agent-turn.lock").exists())

    def test_sigterm_is_forwarded_and_runner_still_records_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            prepared_repo(repo)
            fake = executable(
                repo / "fake-interrupted-mini",
                "import time\n"
                "from pathlib import Path\n"
                "time.sleep(0.2)\n"
                "Path('executor-ready').write_text('ready')\n"
                "time.sleep(300)\n",
            )
            runner = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "coordinator.run_mini_swe_turn",
                    "--repo",
                    str(repo),
                    "--mini-command",
                    str(fake),
                    "--progress-interval",
                    "0.01",
                ],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertTrue(
                    wait_until(lambda: (repo / "executor-ready").exists()),
                    "the fake executor did not become ready",
                )
                runner.send_signal(signal.SIGTERM)
                stdout, stderr = runner.communicate(timeout=10)
            finally:
                if runner.poll() is None:
                    runner.kill()
                    runner.wait(timeout=5)
            self.assertEqual(runner.returncode, 128 + signal.SIGTERM, stdout + stderr)
            status = (repo / ".coordination/coder/status.md").read_text(encoding="utf-8")
            report = (repo / ".coordination/coder/latest-report.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("exited with status 143", status)
            self.assertIn("Process exit status: `143`", report)
            self.assertFalse((repo / ".coordination/.mini-swe-agent-turn.lock").exists())


if __name__ == "__main__":
    unittest.main()
