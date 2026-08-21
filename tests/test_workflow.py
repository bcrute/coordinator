from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "coordinate-claude-work"
WEB_ASSETS = ROOT / "src" / "coordinator" / "assets" / "web"
INIT = SKILL / "scripts" / "init_project.py"
RUN = SKILL / "scripts" / "run_claude_turn.py"
REVIEW = SKILL / "scripts" / "run_codex_review.py"
WATCH = SKILL / "scripts" / "watch_coordination.py"
TEAM = SKILL / "scripts" / "start_claude_team.py"
sys.path.insert(0, str(SKILL / "scripts"))
from coordination_dashboard import render as render_dashboard
from run_claude_turn import apply_stream_event, handoff_prompt
from web_app import WatcherManager, build_state, create_server
from web_app import parse_args as parse_web_app_args


class WorkflowTests(unittest.TestCase):
    def run_init(self, target: Path, name: str = "Events") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(INIT), str(target), "--project-name", name],
            check=False,
            capture_output=True,
            text=True,
        )

    def activate(self, target: Path, task_id: str = "EVENTS-001") -> None:
        goal = target / ".coordination/planner/goal.md"
        goal_text = goal.read_text(encoding="utf-8")
        goal_text = goal_text.replace("- Goal ID: `none`", "- Goal ID: `EVENTS-GOAL-001`")
        goal_text = goal_text.replace("- State: `idle`", "- State: `active`")
        goal.write_text(goal_text, encoding="utf-8")

        task = target / ".coordination/planner/current-task.md"
        task_text = task.read_text(encoding="utf-8")
        task_text = task_text.replace("- Task ID: `none`", f"- Task ID: `{task_id}`")
        task_text = task_text.replace("- State: `idle`", "- State: `ready`")
        task.write_text(task_text, encoding="utf-8")

    def test_init_preserves_existing_instructions_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "AGENTS.md").write_text("# Existing Codex rules\n", encoding="utf-8")
            (target / "CLAUDE.md").write_text("# Existing Claude rules\n", encoding="utf-8")

            first = self.run_init(target)
            self.assertEqual(first.returncode, 0, first.stderr)
            snapshot = {
                path.relative_to(target): path.read_bytes()
                for path in target.rglob("*")
                if path.is_file()
            }
            second = self.run_init(target)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("no files changed", second.stdout)
            self.assertEqual(
                snapshot,
                {
                    path.relative_to(target): path.read_bytes()
                    for path in target.rglob("*")
                    if path.is_file()
                },
            )
            self.assertIn("# Existing Codex rules", (target / "AGENTS.md").read_text())
            self.assertIn("# Existing Claude rules", (target / "CLAUDE.md").read_text())
            self.assertIn("# Events project context", (target / ".coordination/PROJECT.md").read_text())
            self.assertTrue((target / ".coordination/planner/goal.md").is_file())
            self.assertTrue((target / ".coordination/reviews/completion.md").is_file())

    def test_init_is_idempotent_with_new_instruction_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            self.assertEqual(self.run_init(target).returncode, 0)
            agents = (target / "AGENTS.md").read_bytes()
            claude = (target / "CLAUDE.md").read_bytes()
            second = self.run_init(target)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("no files changed", second.stdout)
            self.assertEqual((target / "AGENTS.md").read_bytes(), agents)
            self.assertEqual((target / "CLAUDE.md").read_bytes(), claude)

    def test_init_refuses_malformed_managed_markers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            (target / "AGENTS.md").write_text(
                "<!-- coordinate-claude-work:start -->\nbroken\n", encoding="utf-8"
            )
            result = self.run_init(target)
            self.assertEqual(result.returncode, 2)
            self.assertIn("malformed coordination markers", result.stderr)

    def test_runner_refuses_idle_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            self.assertEqual(self.run_init(target).returncode, 0)
            result = subprocess.run(
                [sys.executable, str(RUN), "--repo", str(target), "--dry-run"],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 2)
            self.assertIn("overall goal must be active", result.stderr)

    def test_runner_validates_ready_task_without_invoking_claude(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            self.assertEqual(self.run_init(target).returncode, 0)
            self.activate(target)
            result = subprocess.run(
                [sys.executable, str(RUN), "--repo", str(target), "--dry-run"],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Would run one Claude turn", result.stdout)
            self.assertNotIn("bypassPermissions", result.stdout)
            self.assertIn("stream-json", result.stdout)
            self.assertIn("--verbose", result.stdout)
            self.assertIn("--forward-subagent-text", result.stdout)
            self.assertIn("--model opus", result.stdout)
            self.assertIn("--max-turns 40", result.stdout)
            self.assertIn("native subagent model: sonnet", result.stdout)

    def test_runner_dry_run_succeeds_without_a_resolvable_claude_executable(self) -> None:
        missing = "claude-absent-for-workflow-tests"
        self.assertIsNone(shutil.which(missing))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            self.assertEqual(self.run_init(target).returncode, 0)
            self.activate(target)
            result = subprocess.run(
                [
                    sys.executable,
                    str(RUN),
                    "--repo",
                    str(target),
                    "--claude-command",
                    missing,
                    "--dry-run",
                ],
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, "PATH": ""},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Would run one Claude turn", result.stdout)
            self.assertIn(f"{missing} -p --model opus", result.stdout)
            self.assertIn("<coordination prompt>", result.stdout)
            self.assertNotIn("command not found", result.stderr)
            self.assertFalse((target / ".coordination/.claude-turn.lock").exists())
            self.assertFalse((target / ".coordination/runtime/claude-progress.json").exists())

    def test_runner_real_handoff_requires_a_resolvable_claude_executable(self) -> None:
        missing = "claude-absent-for-workflow-tests"
        self.assertIsNone(shutil.which(missing))
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            self.assertEqual(self.run_init(target).returncode, 0)
            self.activate(target)
            result = subprocess.run(
                [
                    sys.executable,
                    str(RUN),
                    "--repo",
                    str(target),
                    "--claude-command",
                    missing,
                ],
                check=False,
                capture_output=True,
                text=True,
                env={**os.environ, "PATH": ""},
            )
            self.assertEqual(result.returncode, 127, result.stderr)
            self.assertIn(f"Claude Code command not found: {missing}", result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertFalse((target / ".coordination/.claude-turn.lock").exists())
            self.assertFalse((target / ".coordination/runtime/claude-progress.json").exists())

    def test_runner_prompt_embeds_only_the_active_coordination_packet(self) -> None:
        task = (
            "# Current task\n\n- Task ID: `EVENTS-001`\n- State: `ready`\n"
            "- Review round: `0`\n\n## Objective\n\nAdd a focused regression.\n"
        )
        prompt = handoff_prompt("EVENTS-001", "0", task)
        self.assertIn("<active-assignment>\n" + task.rstrip(), prompt)
        self.assertIn("native Sonnet subagents proactively", prompt)
        self.assertIn("Do not preload the coordination history", prompt)
        self.assertNotIn(".coordination/PROJECT.md", prompt)
        self.assertNotIn(".coordination/planner/goal.md", prompt)
        self.assertNotIn(".coordination/reviews/latest.md", prompt)

    def test_native_team_launcher_uses_interactive_opus_sonnet_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            self.assertEqual(self.run_init(target).returncode, 0)
            self.activate(target)
            result = subprocess.run(
                [
                    sys.executable,
                    str(TEAM),
                    "--repo",
                    str(target),
                    "--claude-command",
                    "/bin/true",
                    "--dry-run",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1", result.stdout)
            self.assertIn("CLAUDE_CODE_SUBAGENT_MODEL=sonnet", result.stdout)
            self.assertIn("--model opus", result.stdout)
            self.assertIn("--teammate-mode in-process", result.stdout)
            self.assertNotIn(" -p ", result.stdout)

    def test_runner_requires_review_before_a_second_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            self.assertEqual(self.run_init(target).returncode, 0)
            self.activate(target)

            fake = target / "fake-claude"
            fake.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' "
                "'{\"type\":\"assistant\",\"message\":{\"id\":\"msg-1\",\"usage\":{\"input_tokens\":10,\"cache_read_input_tokens\":30,\"cache_creation_input_tokens\":20,\"output_tokens\":4}}}'\n"
                "printf '%s\\n' "
                "'{\"type\":\"assistant\",\"message\":{\"id\":\"msg-1\",\"usage\":{\"input_tokens\":10,\"cache_read_input_tokens\":30,\"cache_creation_input_tokens\":20,\"output_tokens\":4}}}'\n"
                "printf '%s\\n' "
                "'{\"type\":\"result\",\"usage\":{\"input_tokens\":10,\"cache_read_input_tokens\":30,\"cache_creation_input_tokens\":20,\"output_tokens\":4}}'\n"
                "printf '%s' \"$CLAUDE_CODE_SUBAGENT_MODEL\" > .worker-model\n"
                "printf '%s\\n' '# Coder status' '' '- Task ID: `EVENTS-001`' "
                "'- State: `review`' '- Review round: `0`' '' "
                "'## Current activity' '' 'Finished focused tests.' '' "
                "'## Turn objectives' '' '- [x] Run focused tests.' > .coordination/coder/status.md\n"
                "printf '%s\\n' '# Latest coder report' '' '- Task ID: `EVENTS-001`' "
                "'- State: `review`' '- Review round: `0`' > .coordination/coder/latest-report.md\n",
                encoding="utf-8",
            )
            os.chmod(fake, 0o755)
            command = [
                sys.executable,
                str(RUN),
                "--repo",
                str(target),
                "--claude-command",
                str(fake),
            ]
            first = subprocess.run(command, check=False, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertIn("Starting Claude handoff: EVENTS-001", first.stdout)
            self.assertIn("Current activity:\nFinished focused tests.", first.stdout)
            self.assertIn("Legacy turn checklist:\n- [x] Run focused tests.", first.stdout)
            self.assertRegex(
                first.stdout,
                r"Activity 00:00:0\d \| Turn 00:00:0\d \| Overall 00:00:0\d \| Generated 4",
            )
            self.assertIn(
                "Generated tokens: 4 (new input 10; cache read 30; cache write 20; all categories processed 64)",
                first.stdout,
            )
            self.assertEqual((target / ".worker-model").read_text(), "sonnet")
            progress = target / ".coordination/runtime/claude-progress.json"
            self.assertTrue(progress.is_file())
            self.assertIn('"state": "completed"', progress.read_text(encoding="utf-8"))
            second = subprocess.run(command, check=False, capture_output=True, text=True)
            self.assertEqual(second.returncode, 2)
            self.assertIn("Codex must review", second.stderr)

    def test_watchers_relay_subgoal_then_stop_on_codex_done_signal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            self.assertEqual(self.run_init(target).returncode, 0)
            self.activate(target)

            fake_claude = target / "fake-claude"
            fake_claude.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' '# Coder status' '' '- Task ID: `EVENTS-001`' "
                "'- State: `review`' '- Review round: `0`' > .coordination/coder/status.md\n"
                "printf '%s\\n' '# Latest coder report' '' '- Task ID: `EVENTS-001`' "
                "'- State: `review`' '- Review round: `0`' > .coordination/coder/latest-report.md\n",
                encoding="utf-8",
            )
            os.chmod(fake_claude, 0o755)

            fake_codex = target / "fake-codex"
            fake_codex.write_text(
                "#!/bin/sh\n"
                "sed -i 's/- State: `active`/- State: `done`/' .coordination/planner/goal.md\n"
                "sed -i 's/- State: `ready`/- State: `accepted`/' .coordination/planner/current-task.md\n"
                "printf '%s\\n' '# Latest Codex review' '' '- Task ID: `EVENTS-001`' "
                "'- Verdict: `accepted`' '- Review round: `0`' > .coordination/reviews/latest.md\n"
                "printf '%s\\n' '# Overall goal completion' '' "
                "'- Goal ID: `EVENTS-GOAL-001`' '- State: `done`' > .coordination/reviews/completion.md\n",
                encoding="utf-8",
            )
            os.chmod(fake_codex, 0o755)

            command = [
                sys.executable,
                str(WATCH),
                "--repo",
                str(target),
                "--role",
                "both",
                "--interval",
                "0.01",
                "--claude-command",
                str(fake_claude),
                "--codex-command",
                str(fake_codex),
            ]
            relay = subprocess.run(command, check=False, capture_output=True, text=True)
            self.assertEqual(relay.returncode, 0, relay.stderr)
            self.assertIn("launching claude", relay.stdout)
            self.assertIn("launching codex", relay.stdout)
            self.assertIn("GOAL DONE", relay.stdout)
            self.assertIn("- State: `done`", (target / ".coordination/planner/goal.md").read_text())
            self.assertTrue((target / ".coordination/runtime/watcher-both-status.json").is_file())

    def test_side_watcher_waits_for_the_other_side(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            self.assertEqual(self.run_init(target).returncode, 0)
            self.activate(target)
            result = subprocess.run(
                [
                    sys.executable,
                    str(WATCH),
                    "--repo",
                    str(target),
                    "--role",
                    "codex",
                    "--once",
                    "--codex-command",
                    "/bin/true",
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("waiting for claude watcher", result.stdout)

    def test_watcher_stops_when_a_handoff_exits_without_review_signal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            self.assertEqual(self.run_init(target).returncode, 0)
            self.activate(target)
            fake_claude = target / "fake-incomplete-claude"
            fake_claude.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' '# Coder status' '' '- Task ID: `EVENTS-001`' "
                "'- State: `implementing`' '- Review round: `0`' > .coordination/coder/status.md\n",
                encoding="utf-8",
            )
            os.chmod(fake_claude, 0o755)

            result = subprocess.run(
                [
                    sys.executable,
                    str(WATCH),
                    "--repo",
                    str(target),
                    "--role",
                    "both",
                    "--interval",
                    "0.01",
                    "--no-dashboard",
                    "--claude-command",
                    str(fake_claude),
                    "--codex-command",
                    "/bin/true",
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )

            self.assertEqual(result.returncode, 3)
            self.assertIn("RELAY STOPPED: claude exited with status 3", result.stderr)
            watcher_status = json.loads(
                (target / ".coordination/runtime/watcher-both-status.json").read_text()
            )
            self.assertEqual(watcher_status["watcher_state"], "error")

    def test_dashboard_shows_goal_roadmap_checklist_and_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            self.assertEqual(self.run_init(target).returncode, 0)
            self.activate(target, task_id="EVENTS-002")
            goal = target / ".coordination/planner/goal.md"
            goal.write_text(
                goal.read_text(encoding="utf-8").replace(
                    "- No subgoals accepted.", "- 1 of 3 planned subgoals accepted in order."
                ),
                encoding="utf-8",
            )
            (target / ".coordination/planner/roadmap.md").write_text(
                "# Roadmap\n\n## Turn 1 — Core\n\n## Turn 2 — CLI\n\n## Turn 3 — Docs\n",
                encoding="utf-8",
            )
            (target / ".coordination/coder/status.md").write_text(
                "# Coder status\n\n- Task ID: `EVENTS-002`\n- State: `implementing`\n"
                "- Review round: `0`\n\n## Current activity\n\nWriting tests.\n\n"
                "## Turn objectives\n\n- [x] Add CLI.\n- [ ] Run tests.\n",
                encoding="utf-8",
            )
            task = target / ".coordination/planner/current-task.md"
            task.write_text(
                task.read_text(encoding="utf-8").replace(
                    "## Acceptance criteria\n\n- None.",
                    "## Acceptance criteria\n\n- Add CLI.\n- Run tests.",
                ),
                encoding="utf-8",
            )
            runtime = target / ".coordination/runtime"
            runtime.mkdir(parents=True, exist_ok=True)
            now = __import__("time").time()
            (runtime / "goal-timing.json").write_text(
                '{"goal_id":"EVENTS-GOAL-001","started_at_epoch":%s}\n' % (now - 30),
                encoding="utf-8",
            )
            (runtime / "claude-progress.json").write_text(
                '{"task_id":"EVENTS-002","state":"running",'
                '"turn_started_epoch":%s,"objective_started_epoch":%s,'
                '"usage":{"input_tokens":10,"cache_read_input_tokens":30,'
                '"cache_creation_input_tokens":20,"output_tokens":4}}\n'
                % (now - 20, now - 5),
                encoding="utf-8",
            )

            rendered = render_dashboard(target, "claude", "implementation running")
            self.assertIn("OVERALL GOAL", rendered)
            self.assertIn("[x] Turn 1: Core", rendered)
            self.assertIn("[>] Turn 2: CLI", rendered)
            self.assertIn("[ ] Turn 3: Docs", rendered)
            self.assertIn("[ ] Add CLI.", rendered)
            self.assertIn("[ ] Run tests.", rendered)
            self.assertIn("Generated 4", rendered)
            self.assertIn("Cache read 30", rendered)
            self.assertIn("CLAUDE SUBAGENTS", rendered)
            self.assertIn(
                "none active (native Claude lead; provider-selected workers available)",
                rendered,
            )
            self.assertIn("Overall 00:00:30", rendered)

    def test_stream_usage_is_deduplicated_and_subagents_are_tracked(self) -> None:
        running = {
            "input_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "output_tokens": 0,
        }
        seen: set[str] = set()
        agents: dict[str, dict[str, object]] = {}
        start = json.dumps(
            {
                "type": "assistant",
                "message": {
                    "id": "root-message",
                    "usage": {"input_tokens": 1, "output_tokens": 2},
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "agent-tool",
                            "name": "Agent",
                            "input": {
                                "description": "Check edge cases",
                                "subagent_type": "Explore",
                            },
                        }
                    ],
                },
            }
        )
        apply_stream_event(
            start,
            running,
            None,
            seen,
            agents,
            now_epoch=100.0,
            default_subagent_model="sonnet",
        )
        apply_stream_event(start, running, None, seen, agents, now_epoch=101.0)
        apply_stream_event(
            json.dumps(
                {
                    "type": "assistant",
                    "parent_tool_use_id": "agent-tool",
                    "message": {
                        "id": "agent-message",
                        "usage": {"cache_read_input_tokens": 30, "output_tokens": 4},
                        "content": [],
                    },
                }
            ),
            running,
            None,
            seen,
            agents,
            now_epoch=102.0,
        )
        apply_stream_event(
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "agent-tool",
                                "is_error": False,
                            }
                        ]
                    },
                }
            ),
            running,
            None,
            seen,
            agents,
            now_epoch=103.0,
        )

        self.assertEqual(running["output_tokens"], 6)
        self.assertEqual(running["input_tokens"], 1)
        self.assertEqual(agents["agent-tool"]["state"], "completed")
        self.assertEqual(agents["agent-tool"]["model"], "sonnet")
        self.assertEqual(agents["agent-tool"]["usage"]["output_tokens"], 4)

    def test_codex_acceptance_can_assign_the_next_subgoal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            self.assertEqual(self.run_init(target).returncode, 0)
            self.activate(target)
            (target / ".coordination/coder/status.md").write_text(
                "# Coder status\n\n- Task ID: `EVENTS-001`\n"
                "- State: `review`\n- Review round: `0`\n",
                encoding="utf-8",
            )
            (target / ".coordination/coder/latest-report.md").write_text(
                "# Latest coder report\n\n- Task ID: `EVENTS-001`\n"
                "- State: `review`\n- Review round: `0`\n",
                encoding="utf-8",
            )
            fake_codex = target / "fake-codex-next"
            fake_codex.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' \"$@\" > .codex-args\n"
                "printf '%s\\n' '# Latest Codex review' '' '- Task ID: `EVENTS-001`' "
                "'- Verdict: `accepted`' '- Review round: `0`' > .coordination/reviews/latest.md\n"
                "printf '%s\\n' '# Current task' '' '- Task ID: `EVENTS-002`' "
                "'- State: `ready`' '- Review round: `0`' '- Starting ref: `abc1234`' "
                "'' '## Objective' '' 'Add the remaining regression coverage.' "
                "'' '## In scope' '' '- Tests.' '' '## Out of scope' '' '- Deployment.' "
                "'' '## Acceptance criteria' '' '- Regression passes.' "
                "'' '## Required evidence' '' '- Run focused tests.' "
                "'' '## Allowed external actions' '' '- None.' "
                "'' '## Review corrections' '' '- None.' > .coordination/planner/current-task.md\n",
                encoding="utf-8",
            )
            os.chmod(fake_codex, 0o755)
            result = subprocess.run(
                [
                    sys.executable,
                    str(REVIEW),
                    "--repo",
                    str(target),
                    "--codex-command",
                    str(fake_codex),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("assigned next subgoal EVENTS-002", result.stdout)
            self.assertIn("Starting Codex review: EVENTS-001 round 0", result.stdout)
            self.assertIn(
                "--skip-git-repo-check",
                (target / ".codex-args").read_text(encoding="utf-8"),
            )
            self.assertIn("- State: `active`", (target / ".coordination/planner/goal.md").read_text())

    def test_user_installer_preserves_global_files_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            codex_dir = root / "codex"
            claude_dir = root / "claude"
            codex_dir.mkdir()
            claude_dir.mkdir()
            (codex_dir / "AGENTS.md").write_text(
                "# Existing global Codex rule\n", encoding="utf-8"
            )
            (claude_dir / "CLAUDE.md").write_text(
                "# Existing global Claude rule\n", encoding="utf-8"
            )
            command = [
                sys.executable,
                str(SKILL / "scripts/install_user.py"),
                "--codex-dir",
                str(codex_dir),
                "--claude-dir",
                str(claude_dir),
            ]
            first = subprocess.run(command, check=False, capture_output=True, text=True)
            self.assertEqual(first.returncode, 0, first.stderr)
            codex_after_first = (codex_dir / "AGENTS.md").read_bytes()
            claude_after_first = (claude_dir / "CLAUDE.md").read_bytes()
            second = subprocess.run(command, check=False, capture_output=True, text=True)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("Current:", second.stdout)
            self.assertEqual((codex_dir / "AGENTS.md").read_bytes(), codex_after_first)
            self.assertEqual((claude_dir / "CLAUDE.md").read_bytes(), claude_after_first)
            codex_text = (codex_dir / "AGENTS.md").read_text(encoding="utf-8")
            claude_text = (claude_dir / "CLAUDE.md").read_text(encoding="utf-8")
            self.assertIn("# Existing global Codex rule", codex_text)
            self.assertIn("# Existing global Claude rule", claude_text)
            self.assertEqual(codex_text.count("coordinate-claude-work-global:start"), 1)
            self.assertEqual(claude_text.count("coordinate-claude-work-global:start"), 1)


class WebAppTests(unittest.TestCase):
    def project(self) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        target = Path(directory.name)
        result = subprocess.run(
            [sys.executable, str(INIT), str(target), "--project-name", "Events"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        goal = target / ".coordination/planner/goal.md"
        goal.write_text(
            goal.read_text(encoding="utf-8")
            .replace("- Goal ID: `none`", "- Goal ID: `EVENTS-GOAL-001`")
            .replace("- State: `idle`", "- State: `active`")
            .replace("- No subgoals accepted.", "- 1 of 3 subgoals accepted."),
            encoding="utf-8",
        )
        task = target / ".coordination/planner/current-task.md"
        task.write_text(
            task.read_text(encoding="utf-8")
            .replace("- Task ID: `none`", "- Task ID: `events-02-api`")
            .replace("- State: `idle`", "- State: `ready`")
            .replace(
                "## Acceptance criteria\n\n- None.",
                "## Acceptance criteria\n\n- Ship the API.\n- Keep tests green.",
            ),
            encoding="utf-8",
        )
        (target / ".coordination/planner/roadmap.md").write_text(
            "# Roadmap\n\n## Turn 1 — Foundation\n\nDone.\n\n"
            "## Turn 2 — API\n\nActive.\n\n## Turn 3 — Polish\n\nLater.\n",
            encoding="utf-8",
        )
        status = target / ".coordination/coder/status.md"
        status.write_text(
            status.read_text(encoding="utf-8")
            .replace("- Task ID: `none`", "- Task ID: `events-02-api`")
            .replace("- State: `idle`", "- State: `implementing`")
            .replace("Waiting for an assignment.", "Writing the API handlers."),
            encoding="utf-8",
        )
        return target

    def write_runtime(self, target: Path) -> None:
        runtime = target / ".coordination/runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / "claude-progress.json").write_text(
            json.dumps(
                {
                    "goal_id": "EVENTS-GOAL-001",
                    "task_id": "events-02-api",
                    "review_round": "0",
                    "state": "running",
                    "turn_started_epoch": 1000.0,
                    "objective_started_epoch": 1030.0,
                    "usage": {
                        "input_tokens": 11,
                        "cache_read_input_tokens": 22,
                        "cache_creation_input_tokens": 33,
                        "output_tokens": 44,
                    },
                    "subagents": [
                        {
                            "description": "verify the API contract",
                            "model": "sonnet",
                            "state": "completed",
                            "started_at_epoch": 1010.0,
                            "completed_at_epoch": 1070.0,
                            "usage": {"output_tokens": 7},
                        },
                        "not a subagent record",
                    ],
                    "primary_model": "opus",
                    "subagent_model": "sonnet",
                    "orchestration_mode": "native-subagents",
                }
            ),
            encoding="utf-8",
        )
        (runtime / "goal-timing.json").write_text(
            json.dumps({"goal_id": "EVENTS-GOAL-001", "started_at_epoch": time.time() - 120}),
            encoding="utf-8",
        )
        (runtime / "watcher-both-status.json").write_text(
            json.dumps(
                {
                    "role": "both",
                    "watcher_state": "running",
                    "detail": "launching claude",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                    "coordination": {"task_id": "events-02-api"},
                }
            ),
            encoding="utf-8",
        )
        (runtime / "relay.log").write_text(
            "".join(f"relay line {index}\n" for index in range(500)), encoding="utf-8"
        )

    def serve(self, target: Path) -> str:
        server = create_server(target, port=0, quiet=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        host, port = server.server_address[:2]
        return f"http://{host}:{port}"

    def get(self, url: str) -> tuple[int, str, str]:
        try:
            with urllib.request.urlopen(url) as response:
                body = response.read().decode("utf-8")
                return response.status, response.headers.get("Content-Type", ""), body
        except urllib.error.HTTPError as error:
            return error.code, error.headers.get("Content-Type", ""), error.read().decode("utf-8")

    def test_web_app_defaults_to_loopback_and_one_configured_repository(self) -> None:
        defaults = parse_web_app_args([])
        self.assertEqual(defaults.host, "127.0.0.1")
        self.assertEqual(defaults.port, 8765)
        self.assertEqual(defaults.repo, Path.cwd())
        self.assertEqual(defaults.relay_log_lines, 200)

        target = self.project()
        configured = parse_web_app_args(["--repo", str(target), "--port", "0"])
        self.assertEqual(configured.repo, target)
        self.assertEqual(configured.host, "127.0.0.1")

        with self.assertRaises(SystemExit):
            parse_web_app_args(["--port", "70000"])

    def test_web_app_serves_static_shell_and_returns_clear_404(self) -> None:
        base = self.serve(self.project())

        status, content_type, body = self.get(f"{base}/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)
        self.assertIn("<title>Coordination workflow</title>", body)

        status, content_type, body = self.get(f"{base}/app.css")
        self.assertEqual(status, 200)
        self.assertIn("text/css", content_type)
        self.assertIn("body", body)

        for path in ("/missing", "/../CLAUDE.md", "/api/unknown"):
            status, _, body = self.get(f"{base}{path}")
            self.assertEqual(status, 404, path)
            self.assertIn("not found", body)

    def test_web_app_serves_dashboard_assets_over_http(self) -> None:
        base = self.serve(self.project())

        status, content_type, html_body = self.get(f"{base}/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", content_type)

        status, content_type, css_body = self.get(f"{base}/app.css")
        self.assertEqual(status, 200)
        self.assertIn("text/css", content_type)
        self.assertTrue(css_body)

        status, content_type, js_body = self.get(f"{base}/app.js")
        self.assertEqual(status, 200)
        self.assertIn("text/javascript", content_type)

        self.assertIn('href="/app.css"', html_body)
        self.assertIn('src="/app.js" defer', html_body)
        self.assertIn("<title>Coordination workflow</title>", html_body)
        self.assertIn("<main", html_body)

        self.assertIn("/api/state", js_body)
        self.assertIn("1000", js_body)

    def test_dashboard_assets_render_state_safely(self) -> None:
        html_text = (WEB_ASSETS / "index.html").read_text(encoding="utf-8")
        css_text = (WEB_ASSETS / "app.css").read_text(encoding="utf-8")
        js_text = (WEB_ASSETS / "app.js").read_text(encoding="utf-8")

        ids = re.findall(r'id="([^"]+)"', html_text)
        duplicates = [name for name in set(ids) if ids.count(name) > 1]
        self.assertEqual(duplicates, [])

        required_dashboard_ids = (
            "goal-id",
            "goal-state",
            "goal-progress",
            "goal-objective",
            "roadmap",
            "task-id",
            "task-state",
            "task-acceptance",
            "coder-state",
            "coder-activity",
            "review-verdict",
            "watchers",
            "metric-activity",
            "metric-turn",
            "metric-overall",
            "token-output",
            "token-input",
            "token-cache-read",
            "token-cache-write",
            "runtime-primary-model",
            "runtime-subagent-model",
            "subagents",
        )
        missing_required = [name for name in required_dashboard_ids if name not in ids]
        self.assertEqual(missing_required, [])

        labelled = set(re.findall(r'aria-labelledby="([^"]+)"', html_text))
        anchored = set(re.findall(r'href="#([^"]+)"', html_text))
        structural = {name for value in labelled for name in value.split()} | anchored

        routes_match = re.search(r'ROUTES\s*=\s*\[([^\]]+)\]', js_text)
        routes = re.findall(r'"([^"]+)"', routes_match.group(1)) if routes_match else []
        route_reached = {f"nav-{name}" for name in routes} | {f"view-{name}" for name in routes}

        css_addressed = set(re.findall(r'#([A-Za-z0-9_-]+)', css_text))

        unreferenced = [
            name
            for name in ids
            if name not in structural
            and f'"{name}"' not in js_text
            and name not in route_reached
            and name not in css_addressed
        ]
        self.assertEqual(unreferenced, [], f"ids declared but unused in app.js: {unreferenced}")

        targeted = set(re.findall(r'(?:el|setText|setTone|fillList)\("([^"]+)"', js_text))
        undeclared = sorted(
            name for name in targeted if name not in ids and not name.endswith("-")
        )
        self.assertEqual(undeclared, [], f"app.js targets ids missing from index.html: {undeclared}")

        unsafe_sinks = (
            "innerHTML",
            "outerHTML",
            "insertAdjacentHTML",
            "document.write",
            "eval(",
            "new Function",
        )
        for sink in unsafe_sinks:
            self.assertNotIn(sink, js_text)

        assets_by_name = (("index.html", html_text), ("app.css", css_text), ("app.js", js_text))
        external_markers = ("http://", "https://", "//cdn", "@import url(", "fonts.googleapis")
        for asset_name, text in assets_by_name:
            for external in external_markers:
                self.assertNotIn(
                    external, text, f"{asset_name} references external resource {external!r}"
                )

        self.assertIn("prefers-reduced-motion", css_text)
        self.assertIn(":focus-visible", css_text)

    def test_dashboard_asset_completes_the_watcher_control_and_log_contract(self) -> None:
        html_text = (WEB_ASSETS / "index.html").read_text(encoding="utf-8")
        js_text = (WEB_ASSETS / "app.js").read_text(encoding="utf-8")

        # The watcher control path only ever fetches a fixed literal URL: no
        # page value, API string, or user input can name a command, a path,
        # or an endpoint. Other accepted fetches (e.g. Codex output/input/
        # resize requests) may coexist elsewhere in the app.
        control_fn = re.search(r"function control\(kind\) \{(.*?)\n\}\n", js_text, re.S)
        self.assertIsNotNone(control_fn)
        control_body = control_fn.group(1)
        self.assertEqual(
            sorted(set(re.findall(r"fetch\(([^,)]+)", control_body))),
            ["CONTROL_URLS[kind]"],
        )
        self.assertIn('method: "POST"', control_body)
        self.assertNotIn("body:", control_body)
        self.assertEqual(
            re.findall(r'"(/api/watcher/[a-z]+)"', js_text),
            ["/api/watcher/start", "/api/watcher/stop"],
        )

        # Every managed_watcher and relay_log field the API reports is rendered.
        state = build_state(self.project())
        rendered = set(re.findall(r"\b(?:watcher|managed)\.(\w+)\b", js_text))
        self.assertEqual(sorted(set(state["managed_watcher"]) - rendered), [])
        logged = set(re.findall(r"\blog\.(\w+)\b", js_text))
        self.assertEqual(sorted(set(state["relay_log"]) - logged), [])

        # Both buttons are driven only by the truthful can_start/can_stop flags
        # and by whether a control request is already in flight.
        controls = re.search(r"function paintControls\(\) \{(.*?)\n\}", js_text, re.S)
        self.assertIsNotNone(controls)
        body = controls.group(1)
        self.assertEqual(body.count(".disabled ="), 2)
        self.assertIn("managed.can_start !== true", body)
        self.assertIn("managed.can_stop !== true", body)
        self.assertIn("pendingControl", body)

        # One control request at a time, with pending feedback and a refresh.
        action = re.search(r"function control\(kind\) \{(.*?)\n\}\n", js_text, re.S)
        self.assertIsNotNone(action)
        body = action.group(1)
        self.assertIn("pendingControl = kind;", body)
        self.assertIn('pendingControl = "";', body)
        self.assertIn("CONTROL_URLS[kind]", body)
        self.assertIn("restartStateFeed();", body)
        self.assertIn("applyManaged(next);", body)

        # Log lines and control feedback reach the page as text only.
        log_view = re.search(r"function renderLog\(state\) \{(.*?)\n\}", js_text, re.S)
        self.assertIsNotNone(log_view)
        self.assertIn("item(", log_view.group(1))
        self.assertIn("LOG_VIEW_LINES", log_view.group(1))

        # The control feedback and the log region stay announced and reachable.
        feedback = html_text.split('id="watcher-feedback"', 1)[1].split(">", 1)[0]
        self.assertIn('role="status"', feedback)
        self.assertIn('aria-live="polite"', feedback)
        scroll = html_text.split('id="relay-log-scroll"', 1)[1].split(">", 1)[0]
        self.assertIn('tabindex="0"', scroll)
        self.assertIn('role="region"', scroll)
        self.assertIn('aria-labelledby="relay-log-heading"', scroll)
        for name in ("watcher-start", "watcher-stop"):
            button = html_text.split(f'id="{name}"', 1)[1].split(">", 1)[0]
            self.assertIn("disabled", button)

    def test_dashboard_script_passes_node_syntax_check(self) -> None:
        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not installed")
        app_js = WEB_ASSETS / "app.js"
        result = subprocess.run(
            [node, "--check", str(app_js)], check=False, capture_output=True, text=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_web_app_state_endpoint_reports_coordination_and_runtime(self) -> None:
        target = self.project()
        self.write_runtime(target)
        base = self.serve(target)

        status, content_type, body = self.get(f"{base}/api/state?repo=/etc")
        self.assertEqual(status, 200)
        self.assertIn("application/json", content_type)
        state = json.loads(body)

        self.assertEqual(state["repo"], str(target.resolve()))
        self.assertTrue(Path(state["repo"]).is_absolute())
        self.assertTrue(state["coordination_present"])
        self.assertEqual(state["goal"]["id"], "EVENTS-GOAL-001")
        self.assertEqual(state["goal"]["state"], "active")
        self.assertEqual(state["goal"]["progress"], {"accepted": 1, "planned": 3, "label": "1/3"})
        self.assertEqual(
            [(entry["turn"], entry["status"]) for entry in state["roadmap"]],
            [(1, "accepted"), (2, "current"), (3, "planned")],
        )
        self.assertEqual(state["task"]["id"], "events-02-api")
        self.assertEqual(state["task"]["acceptance_criteria"], ["Ship the API.", "Keep tests green."])
        self.assertEqual(state["coder"]["state"], "implementing")
        self.assertEqual(state["coder"]["current_activity"], "Writing the API handlers.")
        self.assertTrue(state["coder"]["matches_current_task"])
        self.assertEqual(state["review"]["verdict"], "not_reviewed")

        runtime = state["runtime"]
        self.assertTrue(runtime["matches_current_task"])
        self.assertEqual(runtime["primary_model"], "opus")
        self.assertEqual(runtime["tokens"]["output_tokens"], 44)
        self.assertEqual(runtime["tokens"]["cache_read_input_tokens"], 22)
        timing = runtime["timing"]
        self.assertGreater(timing["turn"]["seconds"], timing["activity"]["seconds"])
        self.assertGreaterEqual(timing["overall"]["seconds"], 120)
        self.assertEqual(len(runtime["subagents"]), 1)
        self.assertEqual(runtime["subagents"][0]["model"], "sonnet")
        self.assertEqual(runtime["subagents"][0]["elapsed"]["display"], "00:01:00")

        self.assertEqual([watcher["role"] for watcher in state["watchers"]], ["both"])
        self.assertEqual(state["watchers"][0]["watcher_state"], "running")
        self.assertEqual(len(state["relay_log"]["lines"]), 200)
        self.assertEqual(state["relay_log"]["lines"][-1], "relay line 499")
        self.assertTrue(state["relay_log"]["truncated"])

    def test_web_app_state_joins_wrapped_markdown_bullets(self) -> None:
        target = self.project()
        goal = target / ".coordination/planner/goal.md"
        goal.write_text(
            goal.read_text(encoding="utf-8").replace(
                "## Completion criteria\n\n- None.",
                "## Completion criteria\n\n"
                "- Ship the events API with pagination, filtering, and a documented\n"
                "  error contract for every endpoint.\n"
                "- Keep the workflow tests green.\n"
                "* Publish the migration notes so operators can plan the rollout\n"
                "without downtime.\n",
            ),
            encoding="utf-8",
        )
        task = target / ".coordination/planner/current-task.md"
        task.write_text(
            task.read_text(encoding="utf-8").replace(
                "## Acceptance criteria\n\n- Ship the API.\n- Keep tests green.",
                "## Acceptance criteria\n\n"
                "- The API returns a stable JSON envelope for   every documented\n"
                "  endpoint,\tincluding the error cases.\n"
                "- Focused tests pass.\n",
            ),
            encoding="utf-8",
        )
        base = self.serve(target)

        status, _, body = self.get(f"{base}/api/state")
        self.assertEqual(status, 200)
        state = json.loads(body)

        self.assertEqual(
            state["goal"]["completion_criteria"],
            [
                "Ship the events API with pagination, filtering, and a documented "
                "error contract for every endpoint.",
                "Keep the workflow tests green.",
                "Publish the migration notes so operators can plan the rollout "
                "without downtime.",
            ],
        )
        self.assertEqual(
            state["task"]["acceptance_criteria"],
            [
                "The API returns a stable JSON envelope for every documented "
                "endpoint, including the error cases.",
                "Focused tests pass.",
            ],
        )
        for entry in state["goal"]["completion_criteria"] + state["task"]["acceptance_criteria"]:
            self.assertNotIn("\n", entry)
            self.assertEqual(entry, entry.strip())

    def test_web_app_state_tolerates_missing_and_malformed_runtime_files(self) -> None:
        target = self.project()
        runtime = target / ".coordination/runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        for name in ("claude-progress.json", "goal-timing.json", "watcher-claude-status.json"):
            (runtime / name).write_text("{not json", encoding="utf-8")
        (runtime / "watcher-both-status.json").write_text("[]", encoding="utf-8")

        state = build_state(target)
        self.assertEqual(state["runtime"]["tokens"], dict.fromkeys(state["runtime"]["tokens"], 0))
        self.assertEqual(state["runtime"]["subagents"], [])
        self.assertFalse(state["runtime"]["matches_current_task"])
        self.assertEqual(state["runtime"]["timing"]["overall"]["display"], "00:00:00")
        self.assertEqual(state["watchers"], [])
        self.assertEqual(state["relay_log"], {
            "path": str(runtime / "relay.log"),
            "available": False,
            "lines": [],
            "truncated": False,
        })
        self.assertEqual(state["task"]["id"], "events-02-api")

        stripped = target / ".coordination"
        for name in ("planner/goal.md", "planner/current-task.md", "coder/status.md"):
            (stripped / name).unlink()
        bare = build_state(target)
        self.assertEqual(bare["goal"]["id"], "none")
        self.assertEqual(bare["task"]["acceptance_criteria"], [])
        self.assertEqual(bare["coder"]["current_activity"], "not recorded")
        self.assertEqual(
            [entry["status"] for entry in bare["roadmap"]], ["planned", "planned", "planned"]
        )


class WatcherControlTests(unittest.TestCase):
    """Exercise WatcherManager and the /api/watcher/* control endpoints.

    Every test injects a harmless fake watcher command (a short inline Python
    script) instead of the real watch_coordination.py, so nothing here ever
    launches the real watcher, Claude, or Codex.
    """

    def project(self) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        target = Path(directory.name)
        result = subprocess.run(
            [sys.executable, str(INIT), str(target), "--project-name", "Events"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return target

    # Fake watcher commands ------------------------------------------------

    def sleep_forever_command(self) -> list[str]:
        return [sys.executable, "-c", "import time; time.sleep(300)"]

    def exit_command(self, code: int) -> list[str]:
        return [sys.executable, "-c", f"import sys; sys.exit({code})"]

    def grandchild_command(self) -> list[str]:
        return [
            sys.executable,
            "-c",
            "import subprocess, sys, time\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)'])\n"
            "print('GRANDCHILD_PID', child.pid, flush=True)\n"
            "time.sleep(300)\n",
        ]

    def relay_output_command(self, lines: int = 20) -> list[str]:
        return [
            sys.executable,
            "-c",
            "import time\n"
            f"for i in range({lines}):\n"
            "    print(f'watcher output line {i}', flush=True)\n"
            "time.sleep(300)\n",
        ]

    # Small polling helpers --------------------------------------------------

    def alive(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def wait_until(
        self, predicate, timeout: float = 10.0, message: str = "condition was not met in time"
    ) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.assertTrue(predicate(), message)

    # HTTP helpers ------------------------------------------------------------

    def serve(self, target: Path, **kwargs) -> str:
        server = create_server(target, port=0, quiet=True, **kwargs)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        host, port = server.server_address[:2]
        return f"http://{host}:{port}"

    def request(
        self, url: str, method: str = "GET", headers: dict[str, str] | None = None
    ) -> tuple[int, dict[str, str], str]:
        req = urllib.request.Request(url, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, dict(response.headers), response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read().decode("utf-8")

    def post_json(
        self, url: str, headers: dict[str, str] | None = None
    ) -> tuple[int, dict[str, object]]:
        status, _, body = self.request(url, "POST", headers)
        return status, json.loads(body) if body else {}

    def get_json(self, url: str) -> tuple[int, dict[str, object]]:
        status, _, body = self.request(url, "GET")
        return status, json.loads(body) if body else {}

    # Direct WatcherManager tests ---------------------------------------------

    def test_start_launches_one_process_and_second_start_conflicts(self) -> None:
        target = self.project()
        manager = WatcherManager(
            target, command=self.sleep_forever_command(), stop_timeout=2, start_grace=0.05
        )
        self.addCleanup(manager.shutdown)

        outcome, _ = manager.start()
        self.assertEqual(outcome, "started")
        self.wait_until(lambda: manager.snapshot()["state"] == "running")
        pid = manager.snapshot()["pid"]
        self.assertIsNotNone(pid)
        self.assertTrue(self.alive(pid))

        outcome2, message2 = manager.start()
        self.assertEqual(outcome2, "conflict")
        self.assertIn(str(pid), message2)
        snapshot = manager.snapshot()
        self.assertEqual(snapshot["pid"], pid)
        self.assertTrue(self.alive(pid))

    def test_existing_lock_file_blocks_start_without_spawning(self) -> None:
        target = self.project()
        runtime = target / ".coordination/runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        lock = runtime / "watcher-both.lock"
        lock.write_text("held by another watcher\n", encoding="utf-8")

        manager = WatcherManager(
            target, command=self.sleep_forever_command(), stop_timeout=2, start_grace=0.05
        )
        self.addCleanup(manager.shutdown)

        outcome, message = manager.start()
        self.assertEqual(outcome, "conflict")
        self.assertIn(str(lock), message)
        snapshot = manager.snapshot()
        self.assertIsNone(snapshot["pid"])
        self.assertFalse(snapshot["running"])

    def test_watcher_exit_and_failure_states_are_observed(self) -> None:
        target = self.project()

        clean = WatcherManager(target, command=self.exit_command(0), stop_timeout=1, start_grace=0.05)
        self.addCleanup(clean.shutdown)
        outcome, _ = clean.start()
        self.assertEqual(outcome, "started")
        self.wait_until(
            lambda: clean.snapshot()["state"] == "exited", message="the clean watcher never exited"
        )
        snapshot = clean.snapshot()
        self.assertEqual(snapshot["exit_code"], 0)
        self.assertFalse(snapshot["running"])

        broken = WatcherManager(
            target, command=self.exit_command(7), stop_timeout=1, start_grace=0.05
        )
        self.addCleanup(broken.shutdown)
        outcome, _ = broken.start()
        self.assertEqual(outcome, "started")
        self.wait_until(
            lambda: broken.snapshot()["state"] == "failed", message="the broken watcher never failed"
        )
        snapshot = broken.snapshot()
        self.assertEqual(snapshot["exit_code"], 7)

    def test_stop_terminates_the_whole_process_group(self) -> None:
        target = self.project()
        manager = WatcherManager(
            target, command=self.grandchild_command(), stop_timeout=3, start_grace=0.05
        )
        self.addCleanup(manager.shutdown)

        outcome, _ = manager.start()
        self.assertEqual(outcome, "started")
        self.wait_until(lambda: manager.snapshot()["state"] == "running")

        log_path = manager.log_path
        self.wait_until(
            lambda: "GRANDCHILD_PID" in log_path.read_text(encoding="utf-8"),
            message="the fake watcher never reported its grandchild pid",
        )
        match = re.search(r"GRANDCHILD_PID (\d+)", log_path.read_text(encoding="utf-8"))
        self.assertIsNotNone(match)
        grandchild_pid = int(match.group(1))
        self.assertTrue(self.alive(grandchild_pid))

        pid = manager.snapshot()["pid"]
        started = time.time()
        outcome, message = manager.stop()
        elapsed = time.time() - started
        self.assertEqual(outcome, "stopped", message)
        self.assertLess(elapsed, 6.0)

        snapshot = manager.snapshot()
        self.assertEqual(snapshot["state"], "exited")
        self.assertIsNotNone(snapshot["exit_code"])
        self.assertFalse(self.alive(pid))
        self.wait_until(
            lambda: not self.alive(grandchild_pid), message="the grandchild survived stop()"
        )

    def test_server_close_leaves_no_managed_child_running(self) -> None:
        target = self.project()
        server = create_server(
            target,
            port=0,
            quiet=True,
            watcher_command=self.sleep_forever_command(),
            stop_timeout=2,
            start_grace=0.05,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)

        watcher = server.watcher
        outcome, _ = watcher.start()
        self.assertEqual(outcome, "started")
        self.wait_until(lambda: watcher.snapshot()["state"] == "running")
        pid = watcher.snapshot()["pid"]
        self.assertTrue(self.alive(pid))

        server.shutdown()
        server.server_close()

        self.assertFalse(self.alive(pid))
        self.assertEqual(watcher.snapshot()["state"], "exited")

    # HTTP control endpoint tests ----------------------------------------------

    def test_http_start_and_stop_return_documented_json(self) -> None:
        target = self.project()
        base = self.serve(
            target, watcher_command=self.sleep_forever_command(), stop_timeout=2, start_grace=0.05
        )

        status, payload = self.post_json(f"{base}/api/watcher/start")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["action"], "start")
        self.assertEqual(payload["outcome"], "started")
        self.assertIsInstance(payload["message"], str)
        managed = payload["managed_watcher"]
        self.assertIn(managed["state"], ("starting", "running"))
        pid = managed["pid"]
        self.assertIsNotNone(pid)

        self.wait_until(
            lambda: self.get_json(f"{base}/api/state")[1]["managed_watcher"]["state"] == "running"
        )

        status, payload = self.post_json(f"{base}/api/watcher/stop")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["action"], "stop")
        self.assertEqual(payload["outcome"], "stopped")
        self.assertEqual(payload["managed_watcher"]["state"], "exited")
        self.assertFalse(self.alive(pid))

        status, payload = self.post_json(f"{base}/api/watcher/stop")
        self.assertEqual(status, 409)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["outcome"], "conflict")

    def test_cross_origin_post_is_refused_same_origin_is_accepted(self) -> None:
        target = self.project()
        base = self.serve(
            target, watcher_command=self.sleep_forever_command(), stop_timeout=2, start_grace=0.05
        )
        host = base.split("://", 1)[1]

        status, payload = self.post_json(
            f"{base}/api/watcher/start", headers={"Origin": "http://evil.example"}
        )
        self.assertEqual(status, 403)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["outcome"], "forbidden")

        status, state = self.get_json(f"{base}/api/state")
        self.assertIsNone(state["managed_watcher"]["pid"])

        status, payload = self.post_json(
            f"{base}/api/watcher/start", headers={"Sec-Fetch-Site": "cross-site"}
        )
        self.assertEqual(status, 403)
        self.assertFalse(payload["ok"])

        status, state = self.get_json(f"{base}/api/state")
        self.assertIsNone(state["managed_watcher"]["pid"])

        status, payload = self.post_json(
            f"{base}/api/watcher/start", headers={"Origin": f"http://{host}"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["outcome"], "started")

        status, payload = self.post_json(f"{base}/api/watcher/stop")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_get_on_control_endpoint_returns_405_with_allow_header(self) -> None:
        target = self.project()
        base = self.serve(target, watcher_command=self.sleep_forever_command())

        status, headers, body = self.request(f"{base}/api/watcher/start", "GET")
        self.assertEqual(status, 405)
        self.assertEqual(headers.get("Allow"), "POST")
        self.assertIn("POST", body)

        status, headers, _ = self.request(f"{base}/api/watcher/stop", "GET")
        self.assertEqual(status, 405)
        self.assertEqual(headers.get("Allow"), "POST")

    def test_state_endpoint_reports_managed_watcher_alongside_file_watchers(self) -> None:
        target = self.project()
        runtime = target / ".coordination/runtime"
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / "watcher-both-status.json").write_text(
            json.dumps(
                {
                    "role": "both",
                    "watcher_state": "running",
                    "detail": "watching from disk",
                    "updated_at": "2026-01-01T00:00:00+00:00",
                    "coordination": {},
                }
            ),
            encoding="utf-8",
        )

        command = self.sleep_forever_command()
        base = self.serve(
            target, watcher_command=command, stop_timeout=2, start_grace=0.05
        )

        status, state = self.get_json(f"{base}/api/state")
        self.assertEqual(status, 200)
        managed = state["managed_watcher"]
        self.assertEqual(managed["state"], "idle")
        self.assertIsNone(managed["pid"])
        self.assertTrue(managed["can_start"])
        self.assertFalse(managed["can_stop"])
        self.assertEqual(managed["command"], command)
        self.assertEqual(managed["log_path"], str(runtime / "relay.log"))
        self.assertFalse(managed["lock_present"])
        self.assertEqual(len(state["watchers"]), 1)
        self.assertEqual(state["watchers"][0]["role"], "both")

        status, payload = self.post_json(f"{base}/api/watcher/start")
        self.assertEqual(status, 200)
        self.wait_until(
            lambda: self.get_json(f"{base}/api/state")[1]["managed_watcher"]["state"] == "running"
        )

        status, state = self.get_json(f"{base}/api/state")
        managed = state["managed_watcher"]
        self.assertTrue(managed["can_stop"])
        self.assertFalse(managed["can_start"])
        self.assertIsNotNone(managed["pid"])
        self.assertEqual(len(state["watchers"]), 1)

        status, payload = self.post_json(f"{base}/api/watcher/stop")
        self.assertEqual(status, 200)

    def test_relay_log_output_appears_in_state_tail_and_respects_limit(self) -> None:
        target = self.project()
        base = self.serve(
            target,
            watcher_command=self.relay_output_command(20),
            relay_log_lines=3,
            stop_timeout=2,
            start_grace=0.05,
        )
        relay_log = target / ".coordination/runtime/relay.log"

        status, payload = self.post_json(f"{base}/api/watcher/start")
        self.assertEqual(status, 200)

        self.wait_until(
            lambda: "watcher output line 19" in relay_log.read_text(encoding="utf-8"),
            timeout=10,
            message="the fake watcher never finished writing its output",
        )

        status, state = self.get_json(f"{base}/api/state")
        self.assertEqual(status, 200)
        tail_lines = state["relay_log"]["lines"]
        self.assertEqual(len(tail_lines), 3)
        self.assertEqual(tail_lines[-1], "watcher output line 19")

        log_text = relay_log.read_text(encoding="utf-8")
        self.assertIn("watcher output line 0", log_text)
        self.assertIn("watcher output line 19", log_text)

        status, payload = self.post_json(f"{base}/api/watcher/stop")
        self.assertEqual(status, 200)


if __name__ == "__main__":
    unittest.main()
