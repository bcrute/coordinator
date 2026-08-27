"""Deterministic contracts for planner review and native team handoffs."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from coordinator.init_project import main as initialize_project
from coordinator.run_codex_review import run as run_review
from coordinator.run_codex_review import valid_transition
from coordinator.run_claude_turn import clean_error_detail, run as run_claude
from coordinator.start_claude_team import run as run_team
from coordinator.executor_adapters import ClaudeExecutorAdapter, MiniSweAgentExecutorAdapter
from coordinator.executor_settings import ExecutorConfiguration
from coordinator.coordination_locks import acquire_lock, active_lock, lock_pid
from coordinator.watch_coordination import Snapshot, next_action, role_handles, task_executor


class CoordinationFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        with contextlib.redirect_stdout(io.StringIO()):
            result = initialize_project(
                [
                    str(self.repo),
                    "--project-name",
                    "Runner test",
                    "--github-ci",
                    "skip",
                ]
            )
        self.assertEqual(result, 0)
        self.write_goal("active")
        self.write_task("task-1", "ready", "0")
        self.write_status("task-1", "review", "0")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_goal(self, state: str) -> None:
        (self.repo / ".coordination/planner/goal.md").write_text(
            "# Overall goal\n\n- Goal ID: `goal-1`\n"
            f"- State: `{state}`\n- Starting ref: `abc`\n",
            encoding="utf-8",
        )

    def write_task(
        self, task_id: str, state: str, review_round: str, executor: str = "configured"
    ) -> None:
        (self.repo / ".coordination/planner/current-task.md").write_text(
            "# Current task\n\n"
            f"- Task ID: `{task_id}`\n- State: `{state}`\n"
            f"- Review round: `{review_round}`\n- Executor: `{executor}`\n"
            "- Starting ref: `abc`\n\n"
            "## Objective\n\nComplete one bounded change.\n\n"
            "## In scope\n\n- One cohesive change.\n\n"
            "## Work units\n\n- [ ] Implement and verify the cohesive change.\n\n"
            "## Acceptance criteria\n\n- The focused check passes.\n",
            encoding="utf-8",
        )

    def write_status(self, task_id: str, state: str, review_round: str) -> None:
        (self.repo / ".coordination/coder/status.md").write_text(
            "# Coder status\n\n"
            f"- Task ID: `{task_id}`\n- State: `{state}`\n"
            f"- Review round: `{review_round}`\n",
            encoding="utf-8",
        )

    def write_review(
        self,
        task_id: str,
        verdict: str,
        review_round: str,
        next_executor: str = "configured",
    ) -> None:
        (self.repo / ".coordination/reviews/latest.md").write_text(
            "# Latest Codex review\n\n"
            f"- Task ID: `{task_id}`\n- Verdict: `{verdict}`\n"
            f"- Review round: `{review_round}`\n"
            f"- Next executor: `{next_executor}`\n",
            encoding="utf-8",
        )


class CodexTransitionTests(CoordinationFixture):
    def test_acceptance_requires_either_completion_evidence_or_a_new_subgoal(self) -> None:
        self.write_review("task-1", "accepted", "0")
        self.write_goal("done")
        self.write_task("task-1", "accepted", "0")
        completion = self.repo / ".coordination/reviews/completion.md"
        completion.unlink()

        valid, message = valid_transition(self.repo, "task-1", "0")
        self.assertFalse(valid)
        self.assertIn("requires reviews/completion.md", message)

        completion.write_text("# Completion\n", encoding="utf-8")
        self.assertEqual(
            valid_transition(self.repo, "task-1", "0"),
            (True, "accepted; overall goal done"),
        )

        self.write_goal("active")
        self.write_task("task-2", "ready", "0")
        self.assertEqual(
            valid_transition(self.repo, "task-1", "0"),
            (True, "accepted; assigned next subgoal task-2"),
        )

    def test_correction_and_blocked_transitions_must_match_planner_state(self) -> None:
        self.write_review("task-1", "changes_requested", "0")
        self.write_task("task-1", "changes_requested", "0")
        valid, message = valid_transition(self.repo, "task-1", "0")
        self.assertFalse(valid)
        self.assertIn("must increment", message)

        self.write_task("task-1", "changes_requested", "1")
        self.assertEqual(
            valid_transition(self.repo, "task-1", "0"),
            (True, "changes_requested"),
        )

        self.write_review("task-1", "blocked", "1")
        self.write_task("task-1", "blocked", "1")
        self.write_goal("blocked")
        self.assertEqual(
            valid_transition(self.repo, "task-1", "1"), (True, "blocked")
        )

    def test_retry_executor_must_be_explicit_and_match_the_review(self) -> None:
        self.write_review("task-1", "changes_requested", "0", "claude")
        self.write_task("task-1", "changes_requested", "1", "configured")
        valid, message = valid_transition(self.repo, "task-1", "0")
        self.assertFalse(valid)
        self.assertIn("must match", message)

        self.write_task("task-1", "changes_requested", "1", "claude")
        self.assertEqual(
            valid_transition(self.repo, "task-1", "0"),
            (True, "changes_requested"),
        )

        self.write_task("task-1", "changes_requested", "1", "invented")
        valid, message = valid_transition(self.repo, "task-1", "0")
        self.assertFalse(valid)
        self.assertIn("executor is invalid", message)

    def test_transition_rejects_stale_identity_invalid_verdict_and_state_mismatch(self) -> None:
        self.write_review("other-task", "accepted", "0")
        valid, message = valid_transition(self.repo, "task-1", "0")
        self.assertFalse(valid)
        self.assertIn("does not identify", message)

        self.write_review("task-1", "invented", "0")
        valid, message = valid_transition(self.repo, "task-1", "0")
        self.assertFalse(valid)
        self.assertIn("verdict is invalid", message)

        self.write_review("task-1", "blocked", "0")
        valid, message = valid_transition(self.repo, "task-1", "0")
        self.assertFalse(valid)
        self.assertIn("planner state", message)


class CodexReviewRunnerTests(CoordinationFixture):
    def arguments(self, **overrides) -> argparse.Namespace:
        values = {
            "repo": self.repo,
            "codex_command": "/bin/true",
            "model": "",
            "dry_run": True,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_review_dry_run_validates_handoff_and_discloses_no_prompt(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = run_review(self.arguments())
        self.assertEqual(result, 0)
        self.assertIn("<coordination review prompt>", output.getvalue())

    def test_claude_can_be_selected_as_the_primary_reviewer(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = run_review(
                self.arguments(
                    primary_adapter="claude",
                    claude_command="/bin/true",
                    claude_model="sonnet",
                    claude_effort="high",
                    claude_max_turns=24,
                )
            )
        self.assertEqual(result, 0)
        command = output.getvalue()
        self.assertIn("Would run one Claude primary review", command)
        self.assertIn("--model sonnet", command)
        self.assertIn("--effort high", command)
        self.assertIn("--max-turns 24", command)
        self.assertNotIn("You own the overall objective", output.getvalue())
        self.assertFalse((self.repo / ".coordination/.codex-review.lock").exists())

    def test_local_api_model_can_be_selected_as_the_primary_reviewer(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = run_review(
                self.arguments(
                    primary_adapter="mini-swe-agent",
                    mini_command="/bin/true",
                    primary_local_model="openai/Qwen3.8-27B",
                    primary_local_effort="high",
                    primary_local_step_limit=36,
                    primary_local_timeout_seconds=1200,
                    local_api_base="http://127.0.0.1:8000/v1",
                    local_provider="openai",
                    local_api_key_env="",
                    local_cost_limit=0.0,
                    mini_config=None,
                )
            )
        self.assertEqual(result, 0)
        command = output.getvalue()
        self.assertIn("Would run one mini-swe-agent primary review", command)
        self.assertIn("--model openai/Qwen3.8-27B", command)
        self.assertIn("agent.step_limit=36", command)
        self.assertIn("reasoning_effort=high", command)
        self.assertIn("primary-review.yaml", command)
        self.assertIn("<coordination review prompt>", command)
        self.assertNotIn("You own the overall objective", command)

    def test_local_primary_refuses_an_unbounded_model_selection(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()) as error:
            result = run_review(
                self.arguments(primary_adapter="mini-swe-agent", primary_local_model="")
            )
        self.assertEqual(result, 2)
        self.assertIn("requires a model", error.getvalue())

    def test_review_dry_run_passes_the_selected_model_to_codex(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = run_review(self.arguments(model="gpt-5.6-sol"))
        self.assertEqual(result, 0)
        self.assertIn("--model gpt-5.6-sol", output.getvalue())

    def test_review_dry_run_passes_reasoning_effort_to_codex(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = run_review(self.arguments(model="gpt-5.6-sol", effort="max"))
        self.assertEqual(result, 0)
        self.assertIn('model_reasoning_effort="max"', output.getvalue())

    def test_review_refuses_missing_stale_and_already_reviewed_handoffs(self) -> None:
        report = self.repo / ".coordination/coder/latest-report.md"
        report.unlink()
        with contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(run_review(self.arguments()), 2)
        self.assertIn("coordination file is missing", error.getvalue())

        report.write_text("# Latest coder report\n", encoding="utf-8")
        self.write_status("other-task", "review", "0")
        with contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(run_review(self.arguments()), 2)
        self.assertIn("does not match", error.getvalue())

        self.write_status("task-1", "review", "0")
        self.write_review("task-1", "accepted", "0")
        with contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(run_review(self.arguments()), 2)
        self.assertIn("already has a Codex verdict", error.getvalue())

    def test_review_refuses_inactive_unassigned_and_unreviewable_state(self) -> None:
        self.write_goal("idle")
        with contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(run_review(self.arguments()), 2)
        self.assertIn("overall goal must be active", error.getvalue())

        self.write_goal("active")
        self.write_task("none", "idle", "0")
        with contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(run_review(self.arguments()), 2)
        self.assertIn("no task is assigned", error.getvalue())

        self.write_task("task-1", "ready", "0")
        self.write_status("task-1", "implementing", "0")
        with contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(run_review(self.arguments()), 2)
        self.assertIn("coder state is not reviewable", error.getvalue())

    def test_review_reports_missing_runtime_lock_collision_and_nonzero_exit(self) -> None:
        self.write_review("none", "not_reviewed", "0")
        missing = "definitely-not-a-codex-command"
        with contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(
                run_review(self.arguments(codex_command=missing, dry_run=False)), 127
            )
        self.assertIn("Codex command not found", error.getvalue())

        lock = self.repo / ".coordination/.codex-review.lock"
        lock.write_text("busy\n", encoding="utf-8")
        with contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(run_review(self.arguments(dry_run=False)), 2)
        self.assertIn("another Codex review", error.getvalue())
        lock.unlink()

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ) as error:
            result = run_review(
                self.arguments(codex_command="/bin/false", dry_run=False)
            )
        self.assertEqual(result, 1)
        self.assertIn("Codex exited with status 1", error.getvalue())
        self.assertFalse(lock.exists())

    def test_successful_command_without_a_valid_transition_is_rejected(self) -> None:
        self.write_review("none", "not_reviewed", "0")
        lock = self.repo / ".coordination/.codex-review.lock"
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ) as error:
            result = run_review(self.arguments(dry_run=False))
        self.assertEqual(result, 3)
        self.assertIn("without a valid state transition", error.getvalue())
        self.assertFalse(lock.exists())


class NativeTeamRunnerTests(CoordinationFixture):
    def arguments(self, **overrides) -> argparse.Namespace:
        values = {
            "repo": self.repo,
            "claude_command": "/bin/true",
            "model": "opus",
            "teammate_model": "sonnet",
            "permission_mode": "auto",
            "teammate_mode": "in-process",
            "dry_run": False,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def test_team_runner_refuses_inactive_duplicate_and_noninteractive_handoffs(self) -> None:
        self.write_goal("idle")
        with contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(run_team(self.arguments()), 2)
        self.assertIn("goal must exist and be active", error.getvalue())

        self.write_goal("active")
        self.write_status("task-1", "implementing", "0")
        with contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(run_team(self.arguments()), 2)
        self.assertIn("already has an active", error.getvalue())

        self.write_status("none", "idle", "0")
        with mock.patch.object(os.sys.stdin, "isatty", return_value=False):
            with contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(run_team(self.arguments()), 2)
        self.assertIn("require an interactive terminal", error.getvalue())

    def test_team_runner_refuses_missing_task_command_and_lock_collision(self) -> None:
        task = self.repo / ".coordination/planner/current-task.md"
        task.unlink()
        with contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(run_team(self.arguments()), 2)
        self.assertIn("coordination task is missing", error.getvalue())

        self.write_task("task-1", "ready", "0")
        self.write_status("none", "idle", "0")
        with contextlib.redirect_stderr(io.StringIO()) as error:
            self.assertEqual(
                run_team(self.arguments(claude_command="missing-claude-command")), 127
            )
        self.assertIn("Claude Code command not found", error.getvalue())

        lock = self.repo / ".coordination/.claude-turn.lock"
        lock.write_text("busy\n", encoding="utf-8")
        with mock.patch.object(os.sys.stdin, "isatty", return_value=True), mock.patch.object(
            os.sys.stdout, "isatty", return_value=True
        ):
            with contextlib.redirect_stderr(io.StringIO()) as error:
                self.assertEqual(run_team(self.arguments()), 2)
        self.assertIn("another Claude handoff", error.getvalue())

    def test_team_runner_sets_native_environment_and_cleans_its_lock(self) -> None:
        (self.repo / ".coordination/coder/status.md").unlink()
        fake = self.repo / "fake-claude"
        fake.write_text(
            "#!/bin/sh\n"
            "printf '%s' \"$CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS\" > .team-enabled\n"
            "printf '%s' \"$CLAUDE_CODE_SUBAGENT_MODEL\" > .team-model\n"
            "exit 7\n",
            encoding="utf-8",
        )
        fake.chmod(0o755)
        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(
            os.sys.stdin, "isatty", return_value=True
        ), mock.patch.object(os.sys.stdout, "isatty", return_value=True):
            result = run_team(self.arguments(claude_command=str(fake)))

        self.assertEqual(result, 7)
        self.assertEqual((self.repo / ".team-enabled").read_text(), "1")
        self.assertEqual((self.repo / ".team-model").read_text(), "sonnet")
        self.assertFalse((self.repo / ".coordination/.claude-turn.lock").exists())


class ClaudeRunnerTests(CoordinationFixture):
    def arguments(self, command: Path, **overrides: object) -> argparse.Namespace:
        values: dict[str, object] = {
            "repo": self.repo,
            "claude_command": str(command),
            "model": "opus",
            "subagent_model": "sonnet",
            "permission_mode": "auto",
            "max_turns": 8,
            "progress_interval": 0.001,
            "dry_run": False,
        }
        values.update(overrides)
        return argparse.Namespace(**values)

    def executable(self, name: str, body: str) -> Path:
        path = self.repo / name
        path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        path.chmod(0o755)
        return path

    def prepare_handoff(self) -> None:
        self.write_status("none", "idle", "0")

    def test_error_detail_is_bounded_sanitized_and_redacts_environment_secrets(self) -> None:
        secret = "secret-value-for-test"
        with mock.patch.dict(os.environ, {"EXAMPLE_API_KEY": secret}):
            detail = clean_error_detail(["\033[31mfailed ", secret, "\x00\n"])
        self.assertEqual(detail, "failed [redacted]")

    def test_nonzero_execution_is_reported_and_cleans_lock(self) -> None:
        self.prepare_handoff()
        fake = self.executable("failing-claude", "printf 'provider unavailable\\n' >&2\nexit 7\n")

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ) as error:
            result = run_claude(self.arguments(fake))

        self.assertEqual(result, 7)
        self.assertIn("Claude exited with status 7", error.getvalue())
        self.assertIn("provider unavailable", error.getvalue())
        self.assertFalse((self.repo / ".coordination/.claude-turn.lock").exists())
        progress = json.loads(
            (self.repo / ".coordination/runtime/claude-progress.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(progress["state"], "failed")
        self.assertEqual(progress["exit_code"], 7)
        self.assertEqual(progress["failure_detail"], "provider unavailable")
        self.assertEqual(progress["primary_model"], "opus")
        self.assertEqual(progress["subagent_model"], "sonnet")
        status = (self.repo / ".coordination/coder/status.md").read_text(encoding="utf-8")
        report = (self.repo / ".coordination/coder/latest-report.md").read_text(encoding="utf-8")
        self.assertIn("- State: `blocked`", status)
        self.assertIn("provider unavailable", status)
        self.assertIn("executor failed with status 7", report)

    def test_interrupted_implementing_state_and_dead_lock_are_retried(self) -> None:
        self.write_status("task-1", "implementing", "0")
        lock = self.repo / ".coordination/.claude-turn.lock"
        lock.write_text("pid=999999999 task=task-1 round=0\n", encoding="utf-8")
        fake = self.executable("retry-claude", "exit 9\n")

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ):
            result = run_claude(self.arguments(fake))

        self.assertEqual(result, 9)
        self.assertFalse(lock.exists())
        status = (self.repo / ".coordination/coder/status.md").read_text(encoding="utf-8")
        self.assertIn("- State: `blocked`", status)

    def test_zero_exit_requires_a_reviewable_handoff(self) -> None:
        self.prepare_handoff()
        fake = self.executable("incomplete-claude", "exit 0\n")

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ) as error:
            result = run_claude(self.arguments(fake))

        self.assertEqual(result, 3)
        self.assertIn("without signaling review or blocked", error.getvalue())
        self.assertFalse((self.repo / ".coordination/.claude-turn.lock").exists())

    def test_planner_and_review_ownership_is_enforced_after_execution(self) -> None:
        self.prepare_handoff()
        goal = self.repo / ".coordination/planner/goal.md"
        goal_before = goal.read_bytes()
        fake = self.executable(
            "tampering-claude",
            "printf '\\nexecutor edit\\n' >> .coordination/planner/goal.md\n"
            "printf '%s\\n' '# Coder status' '' '- Task ID: `task-1`' "
            "'- State: `review`' '- Review round: `0`' "
            "> .coordination/coder/status.md\n",
        )

        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
            io.StringIO()
        ) as error:
            result = run_claude(self.arguments(fake))

        self.assertEqual(result, 3)
        self.assertIn("modified Coordinator-owned coordination files", error.getvalue())
        self.assertIn(".coordination/planner/goal.md", error.getvalue())
        self.assertIn("protected state was restored", error.getvalue())
        self.assertEqual(goal.read_bytes(), goal_before)
        self.assertFalse((self.repo / ".coordination/.claude-turn.lock").exists())


class WatcherDecisionTests(unittest.TestCase):
    def state(self, **overrides) -> Snapshot:
        values = {
            "goal_id": "goal-1",
            "goal_state": "active",
            "task_id": "task-1",
            "task_state": "ready",
            "task_round": "0",
            "task_executor": "configured",
            "coder_task_id": "none",
            "coder_state": "idle",
            "coder_round": "0",
            "review_task_id": "none",
            "review_verdict": "not_reviewed",
            "review_round": "0",
        }
        values.update(overrides)
        return Snapshot(**values)

    def test_terminal_goal_and_invalid_planner_states_are_unambiguous(self) -> None:
        cases = (
            ({"goal_id": "none", "goal_state": "idle"}, "wait"),
            ({"goal_state": "done"}, "done"),
            ({"goal_state": "blocked"}, "blocked"),
            ({"goal_state": "invented"}, "error"),
            ({"task_id": "none", "task_state": "idle"}, "error"),
            ({"task_state": "accepted"}, "error"),
            ({"task_executor": "invented"}, "error"),
        )
        for overrides, expected in cases:
            with self.subTest(overrides=overrides):
                self.assertEqual(next_action(self.state(**overrides))[0], expected)

    def test_handoff_identity_drives_exactly_one_executor_or_review_action(self) -> None:
        self.assertEqual(next_action(self.state())[0], "executor")
        self.assertEqual(
            next_action(
                self.state(
                    coder_task_id="task-1",
                    coder_state="implementing",
                    coder_round="0",
                )
            )[0],
            "wait",
        )
        reviewable = self.state(
            coder_task_id="task-1", coder_state="review", coder_round="0"
        )
        self.assertEqual(next_action(reviewable)[0], "codex")
        stale = self.state(
            coder_task_id="task-1",
            coder_state="review",
            coder_round="0",
            review_task_id="task-1",
            review_verdict="accepted",
            review_round="0",
        )
        self.assertEqual(next_action(stale)[0], "error")

    def test_interrupted_implementing_handoff_is_recovered_but_live_turn_waits(self) -> None:
        implementing = self.state(
            coder_task_id="task-1", coder_state="implementing", coder_round="0"
        )
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            coordination = repo / ".coordination"
            coordination.mkdir()
            self.assertEqual(next_action(implementing, repo)[0], "executor")

            lock = coordination / ".claude-turn.lock"
            lock.write_text(f"pid={os.getpid()} task=task-1 round=0\n", encoding="utf-8")
            self.assertEqual(next_action(implementing, repo)[0], "wait")

            lock.write_text("pid=999999999 task=task-1 round=0\n", encoding="utf-8")
            self.assertEqual(next_action(implementing, repo)[0], "executor")
            self.assertFalse(lock.exists())

    def test_role_routing_preserves_claude_compatibility_alias(self) -> None:
        self.assertTrue(role_handles("both", "executor"))
        self.assertTrue(role_handles("executor", "executor"))
        self.assertTrue(role_handles("claude", "executor"))
        self.assertTrue(role_handles("codex", "codex"))
        self.assertFalse(role_handles("codex", "executor"))

    def test_one_handoff_executor_override_uses_project_snapshot(self) -> None:
        configured = MiniSweAgentExecutorAdapter(command_name="/bin/true", model="local")
        configuration = ExecutorConfiguration(
            executor_adapter="mini-swe-agent",
            mini_swe_model="local",
            claude_model="sonnet",
        )
        with mock.patch(
            "coordinator.watch_coordination.load_project_executor_settings",
            return_value=configuration,
        ):
            selected_default = task_executor(Path("."), "configured", configured)
            selected = task_executor(Path("."), "claude", configured)
        self.assertIsInstance(selected_default, MiniSweAgentExecutorAdapter)
        self.assertEqual(selected_default.model, "local")
        self.assertEqual(selected_default.command_name, "/bin/true")
        self.assertIsInstance(selected, ClaudeExecutorAdapter)
        self.assertEqual(selected.model, "sonnet")

        with mock.patch(
            "coordinator.watch_coordination.load_project_executor_settings",
            side_effect=ValueError("missing"),
        ):
            self.assertIs(task_executor(Path("."), "configured", configured), configured)


class CoordinationLockTests(unittest.TestCase):
    def test_live_lock_is_preserved_and_refuses_second_owner(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "turn.lock"
            lock.write_text(f"pid={os.getpid()} task=t round=0\n", encoding="utf-8")
            self.assertEqual(lock_pid(lock), os.getpid())
            self.assertTrue(active_lock(lock, reclaim_stale=True))
            with self.assertRaises(FileExistsError):
                acquire_lock(lock, "pid=999\n")

    def test_dead_owner_is_reclaimed_and_new_payload_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "turn.lock"
            lock.write_text("pid=999999999 task=t round=0\n", encoding="utf-8")
            descriptor = acquire_lock(lock, f"pid={os.getpid()} task=t round=0\n")
            os.close(descriptor)
            self.assertEqual(lock_pid(lock), os.getpid())

    def test_unparseable_lock_is_never_deleted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "turn.lock"
            lock.write_text("owned by an older coordinator\n", encoding="utf-8")
            self.assertTrue(active_lock(lock, reclaim_stale=True))
            self.assertTrue(lock.exists())
