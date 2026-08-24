from __future__ import annotations

import sys
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "coordinate-claude-work"
sys.path.insert(0, str(SKILL / "scripts"))

from web_app import completion_state, delegation_state, watcher_state, workflow_state


COMPLETION_TEXT = """# Overall goal completion

- Goal ID: `goal-a`
- State: `done`
- Accepted ref: `abc123`

## Result

- Everything works as intended.
- It even wraps across two
  lines of the same bullet.

## Evidence

- The focused test suite passed.

## Limitations and optional follow-up

- LAN exposure is deferred.
"""


def make_goal(goal_id: str = "goal-a", state: str = "in_progress") -> dict[str, object]:
    return {"id": goal_id, "state": state}


def make_task(task_id: str = "task-1", state: str = "implementing") -> dict[str, object]:
    return {"id": task_id, "state": state, "review_round": "1"}


def make_coder(
    task_id: str = "task-1",
    review_round: str = "1",
    state: str = "implementing",
    current_activity: str = "Doing the work.",
    blocker: str = "none",
    matches: bool = True,
) -> dict[str, object]:
    return {
        "task_id": task_id,
        "state": state,
        "review_round": review_round,
        "starting_ref": "not recorded",
        "current_ref": "not recorded",
        "blocker": blocker,
        "current_activity": current_activity,
        "matches_current_task": matches,
    }


def make_runtime(matches: bool = True) -> dict[str, object]:
    return {"matches_current_task": matches}


def make_completion(
    goal_id: str = "goal-a",
    state: str = "done",
    present: bool = True,
    result: list[str] | None = None,
) -> dict[str, object]:
    return {
        "goal_id": goal_id,
        "state": state,
        "accepted_ref": "abc123",
        "result": result if result is not None else ["Everything works as intended."],
        "evidence": [],
        "limitations": [],
        "present": present,
    }


class CompletionStateTests(unittest.TestCase):
    def test_parses_fields_and_sections(self) -> None:
        record = completion_state(COMPLETION_TEXT)
        self.assertEqual(record["goal_id"], "goal-a")
        self.assertEqual(record["state"], "done")
        self.assertEqual(record["accepted_ref"], "abc123")
        self.assertEqual(
            record["result"],
            [
                "Everything works as intended.",
                "It even wraps across two lines of the same bullet.",
            ],
        )
        self.assertEqual(record["evidence"], ["The focused test suite passed."])
        self.assertEqual(record["limitations"], ["LAN exposure is deferred."])
        self.assertTrue(record["present"])

    def test_empty_text_is_not_present(self) -> None:
        record = completion_state("")
        self.assertFalse(record["present"])
        self.assertEqual(record["goal_id"], "none")
        self.assertEqual(record["state"], "unknown")


class WorkflowStateDoneTests(unittest.TestCase):
    def test_matching_done_completion_with_stale_coder_is_current(self) -> None:
        goal = make_goal(state="done")
        task = make_task(state="ready")
        coder = make_coder(task_id="stale-task", review_round="0", state="implementing", matches=False)
        runtime = make_runtime(matches=False)
        completion = make_completion(state="done")

        result = workflow_state(goal, task, coder, runtime, completion)

        self.assertEqual(result["phase"], "done")
        self.assertFalse(result["active"])
        self.assertTrue(result["completion_current"])
        self.assertFalse(result["coder_current"])
        self.assertFalse(result["runtime_current"])
        self.assertIn("Everything works as intended.", result["detail"])

    def test_mismatched_goal_completion_is_never_current(self) -> None:
        goal = make_goal(goal_id="goal-b", state="done")
        task = make_task(state="ready")
        coder = make_coder(matches=False)
        runtime = make_runtime(matches=False)
        completion = make_completion(goal_id="goal-a", state="done")

        result = workflow_state(goal, task, coder, runtime, completion)

        self.assertFalse(result["completion_current"])
        self.assertNotEqual(result["phase"], "done")

    def test_completion_present_but_goal_not_done_is_not_current(self) -> None:
        goal = make_goal(state="in_progress")
        task = make_task(state="review")
        coder = make_coder(state="review")
        runtime = make_runtime()
        completion = make_completion(state="done")

        result = workflow_state(goal, task, coder, runtime, completion)

        self.assertFalse(result["completion_current"])


class WorkflowStateActivePhaseTests(unittest.TestCase):
    def test_current_implementing_signal_is_active(self) -> None:
        goal = make_goal()
        task = make_task(state="implementing")
        coder = make_coder(state="implementing", current_activity="Editing web_app.py.")
        runtime = make_runtime()
        completion = make_completion(present=False)

        result = workflow_state(goal, task, coder, runtime, completion)

        self.assertEqual(result["phase"], "implementing")
        self.assertTrue(result["active"])
        self.assertTrue(result["coder_current"])
        self.assertEqual(result["detail"], "Editing web_app.py.")

    def test_current_review_signal_is_waiting_for_codex(self) -> None:
        goal = make_goal()
        task = make_task(state="review")
        coder = make_coder(state="review")
        runtime = make_runtime()
        completion = make_completion(present=False)

        result = workflow_state(goal, task, coder, runtime, completion)

        self.assertEqual(result["phase"], "waiting_for_codex")
        self.assertFalse(result["active"])

    def test_task_review_without_current_coder_is_waiting_for_codex(self) -> None:
        goal = make_goal()
        task = make_task(state="review")
        coder = make_coder(matches=False, state="unknown")
        runtime = make_runtime(matches=False)
        completion = make_completion(present=False)

        result = workflow_state(goal, task, coder, runtime, completion)

        self.assertEqual(result["phase"], "waiting_for_codex")
        self.assertFalse(result["coder_current"])

    def test_task_ready_is_waiting_for_claude(self) -> None:
        goal = make_goal()
        task = make_task(state="ready")
        coder = make_coder(matches=False)
        runtime = make_runtime(matches=False)
        completion = make_completion(present=False)

        result = workflow_state(goal, task, coder, runtime, completion)

        self.assertEqual(result["phase"], "waiting_for_claude")
        self.assertFalse(result["active"])

    def test_task_changes_requested_is_waiting_for_claude(self) -> None:
        goal = make_goal()
        task = make_task(state="changes_requested")
        coder = make_coder(matches=False)
        runtime = make_runtime(matches=False)
        completion = make_completion(present=False)

        result = workflow_state(goal, task, coder, runtime, completion)

        self.assertEqual(result["phase"], "waiting_for_claude")
        self.assertFalse(result["active"])


class WorkflowStateBlockedTests(unittest.TestCase):
    def test_goal_blocked(self) -> None:
        goal = make_goal(state="blocked")
        task = make_task(state="implementing")
        coder = make_coder(state="implementing")
        runtime = make_runtime()
        completion = make_completion(present=False)

        result = workflow_state(goal, task, coder, runtime, completion)

        self.assertEqual(result["phase"], "blocked")
        self.assertFalse(result["active"])

    def test_task_blocked(self) -> None:
        goal = make_goal()
        task = make_task(state="blocked")
        coder = make_coder(matches=False)
        runtime = make_runtime(matches=False)
        completion = make_completion(present=False)

        result = workflow_state(goal, task, coder, runtime, completion)

        self.assertEqual(result["phase"], "blocked")

    def test_current_coder_blocked(self) -> None:
        goal = make_goal()
        task = make_task(state="implementing")
        coder = make_coder(state="blocked", blocker="Missing owner decision.", current_activity="")
        runtime = make_runtime()
        completion = make_completion(present=False)

        result = workflow_state(goal, task, coder, runtime, completion)

        self.assertEqual(result["phase"], "blocked")
        self.assertIn("Missing owner decision.", result["detail"])


class WorkflowStateFallbackTests(unittest.TestCase):
    def test_inactive_fallback(self) -> None:
        goal = make_goal(state="unknown")
        task = make_task(state="unknown")
        coder = make_coder(matches=False, state="unknown")
        runtime = make_runtime(matches=False)
        completion = make_completion(present=False)

        result = workflow_state(goal, task, coder, runtime, completion)

        self.assertEqual(result["phase"], "inactive")
        self.assertFalse(result["active"])
        self.assertFalse(result["coder_current"])
        self.assertFalse(result["runtime_current"])
        self.assertFalse(result["completion_current"])


class DelegationStateTests(unittest.TestCase):
    def test_runtime_jobs_are_bounded_and_keep_routing_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            jobs = repo / ".coordination/runtime/delegations"
            jobs.mkdir(parents=True)
            (jobs / "d-example.json").write_text(
                json.dumps(
                    {
                        "id": "d-example",
                        "state": "running",
                        "model": "openai/local-qwen",
                        "objective": "Update a focused parser.",
                        "routing_score": 9,
                        "routing_rationale": "Exact files and deterministic tests.",
                        "started_at_epoch": 90,
                        "steps": 3,
                        "usage": {"output_tokens": 25},
                        "changed_files": ["src/parser.py"],
                    }
                ),
                encoding="utf-8",
            )
            result = delegation_state(repo, 100)

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["routing_score"], 9)
        self.assertEqual(result[0]["elapsed"]["seconds"], 10)
        self.assertEqual(result[0]["usage"]["output_tokens"], 25)


class WatcherStateTests(unittest.TestCase):
    def test_unlocked_active_status_is_historical_not_running(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runtime = repo / ".coordination/runtime"
            runtime.mkdir(parents=True)
            (runtime / "watcher-claude-status.json").write_text(
                json.dumps(
                    {
                        "role": "claude",
                        "watcher_state": "running",
                        "detail": "launching an old handoff",
                        "updated_at": "2026-08-19T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            result = watcher_state(repo)

        self.assertEqual(result[0]["watcher_state"], "stale")
        self.assertEqual(result[0]["reported_state"], "running")
        self.assertFalse(result[0]["lock_present"])
        self.assertIn("no watcher lock is held", result[0]["detail"])

    def test_locked_active_status_remains_running(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            runtime = repo / ".coordination/runtime"
            runtime.mkdir(parents=True)
            (runtime / "watcher-both-status.json").write_text(
                json.dumps({"role": "both", "watcher_state": "running"}),
                encoding="utf-8",
            )
            (runtime / "watcher-both.lock").write_text("pid=123\n", encoding="utf-8")
            result = watcher_state(repo)

        self.assertEqual(result[0]["watcher_state"], "running")
        self.assertTrue(result[0]["lock_present"])


if __name__ == "__main__":
    unittest.main()
