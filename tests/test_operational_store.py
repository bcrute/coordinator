"""Contracts for the rebuildable operational run/event index."""

from __future__ import annotations

import json
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path

from coordinator.operational_store import (
    LATEST_SCHEMA_VERSION,
    GuardrailPolicy,
    OperationalStore,
    evaluate_guardrails,
    stable_id,
)


def snapshot(*, goal_state: str = "active", task_state: str = "implementing"):
    return {
        "goal": {
            "id": "professional-app",
            "state": goal_state,
            "starting_ref": "abc123",
        },
        "roadmap": [
            {"turn": 1, "title": "Foundation", "status": "accepted"},
            {"turn": 2, "title": "History", "status": "current"},
        ],
        "task": {"id": "turn-2", "state": task_state, "review_round": "1"},
        "coder": {"state": "implementing", "matches_current_task": True},
        "review": {"verdict": "not_reviewed"},
        "runtime": {
            "state": "running",
            "primary_model": "Codex",
            "subagents": [
                {
                    "model": "worker",
                    "description": "Focused check",
                    "state": "running",
                }
            ],
        },
        "completion": {"present": False},
        "workflow": {"phase": "implementing", "active": True},
        "managed_watcher": {"state": "running"},
        "codex_session": {"running": True},
    }


class OperationalStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "project"
        self.repo.mkdir()
        self.store = OperationalStore(self.root / "state")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_owner_only_database_and_current_schema(self):
        self.assertEqual(self.store.schema_version, LATEST_SCHEMA_VERSION)
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.store.state_dir.stat().st_mode), 0o700)

    def test_stable_ids_are_deterministic_and_namespaced(self):
        self.assertEqual(stable_id("run", "a", 1), stable_id("run", "a", 1))
        self.assertNotEqual(stable_id("run", "a", 1), stable_id("turn", "a", 1))
        self.assertTrue(stable_id("run", "a", 1).startswith("run_"))

    def test_snapshot_sync_is_idempotent_and_records_real_transitions(self):
        first = self.store.sync_snapshot(self.repo, snapshot(), now=10)
        second = self.store.sync_snapshot(self.repo, snapshot(), now=20)
        changed = snapshot(task_state="review")
        third = self.store.sync_snapshot(self.repo, changed, now=30)

        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        self.assertTrue(third["changed"])
        self.assertEqual(first["run_id"], third["run_id"])
        events = self.store.list_events(first["run_id"])
        self.assertEqual([event["type"] for event in events], ["run_discovered", "state_changed"])
        self.assertEqual(self.store.get_run(first["run_id"])["snapshot"]["task"]["state"], "review")

    def test_advancing_display_timers_do_not_create_state_transitions(self):
        value = snapshot()
        value["runtime"]["timing"] = {"turn": {"seconds": 1}}
        first = self.store.sync_snapshot(self.repo, value, now=10)
        value["runtime"]["timing"] = {"turn": {"seconds": 9}}
        second = self.store.sync_snapshot(self.repo, value, now=20)
        self.assertFalse(second["changed"])
        self.assertEqual(len(self.store.list_events(first["run_id"])), 1)

    def test_objectives_and_agent_hierarchy_are_indexed(self):
        details = self.store.sync_snapshot(self.repo, snapshot())
        with sqlite3.connect(self.store.path) as connection:
            objective_count = connection.execute(
                "SELECT COUNT(*) FROM objectives WHERE run_id = ?", (details["run_id"],)
            ).fetchone()[0]
            agents = connection.execute(
                "SELECT parent_agent_id FROM agents WHERE run_id = ? ORDER BY ordinal",
                (details["run_id"],),
            ).fetchall()
        self.assertEqual(objective_count, 2)
        self.assertEqual(len(agents), 2)
        self.assertIsNone(agents[0][0])
        self.assertIsNotNone(agents[1][0])

    def test_policy_pause_requires_explicit_resume(self):
        details = self.store.sync_snapshot(self.repo, snapshot())
        policy = GuardrailPolicy(turn_seconds=60, generated_tokens=1000)
        self.assertTrue(self.store.set_policy(details["run_id"], policy))
        self.assertTrue(self.store.pause(details["run_id"], "turn time limit"))
        paused = self.store.get_run(details["run_id"])
        self.assertEqual(paused["status"], "paused")
        self.assertTrue(paused["resume_required"])
        self.assertEqual(paused["policy"]["generated_tokens"], 1000)

        self.store.sync_snapshot(self.repo, snapshot(goal_state="active"))
        self.assertEqual(self.store.get_run(details["run_id"])["status"], "paused")
        self.assertTrue(self.store.resume(details["run_id"]))
        self.assertFalse(self.store.get_run(details["run_id"])["resume_required"])

    def test_restart_marks_active_runs_interrupted_once(self):
        details = self.store.sync_snapshot(self.repo, snapshot())
        self.assertEqual(self.store.recover_interrupted(now=50), 1)
        self.assertEqual(self.store.recover_interrupted(now=60), 0)
        run = self.store.get_run(details["run_id"])
        self.assertEqual(run["status"], "interrupted")
        self.assertTrue(run["resume_required"])

    def test_repeated_identical_failure_pauses_at_configured_threshold(self):
        details = self.store.sync_snapshot(self.repo, snapshot())
        first = self.store.record_failure(details["run_id"], "same failing test", now=10)
        second = self.store.record_failure(details["run_id"], "same failing test", now=20)
        third = self.store.record_failure(details["run_id"], "same failing test", now=30)
        self.assertEqual(first["occurrences"], 1)
        self.assertEqual(second["occurrences"], 2)
        self.assertEqual(third["occurrences"], 3)
        self.assertTrue(third["paused"])
        self.assertTrue(self.store.get_run(details["run_id"])["resume_required"])

    def test_preferences_survive_index_rebuild(self):
        self.store.set_preference("theme", {"name": "system"})
        self.store.sync_snapshot(self.repo, snapshot())
        self.assertEqual(self.store.rebuild([(self.repo, snapshot())]), 1)
        self.assertEqual(self.store.preferences(), {"theme": {"name": "system"}})
        self.assertEqual(len(self.store.list_runs()), 1)

    def test_run_can_be_archived_and_reopened_without_losing_history(self):
        details = self.store.sync_snapshot(self.repo, snapshot())
        self.assertTrue(self.store.archive(details["run_id"], now=50))
        self.assertEqual(self.store.get_run(details["run_id"])["archived_at"], 50)
        self.assertFalse(self.store.archive(details["run_id"], now=60))
        self.assertTrue(self.store.reopen(details["run_id"]))
        self.assertIsNone(self.store.get_run(details["run_id"])["archived_at"])

    def test_online_backup_is_verified_and_restorable(self):
        details = self.store.sync_snapshot(self.repo, snapshot())
        backup = self.store.backup(self.root / "backup.sqlite3")
        self.store.pause(details["run_id"], "temporary")
        self.store.restore(backup)
        self.assertFalse(self.store.get_run(details["run_id"])["resume_required"])

    def test_unknown_future_schema_is_refused(self):
        future = self.root / "future"
        future.mkdir(mode=0o700)
        path = future / "operations.sqlite3"
        with sqlite3.connect(path) as connection:
            connection.execute(f"PRAGMA user_version = {LATEST_SCHEMA_VERSION + 1}")
        path.chmod(0o600)
        with self.assertRaisesRegex(ValueError, "unsupported operational database"):
            OperationalStore(future)

    def test_version_one_database_migrates_to_current_schema(self):
        legacy = self.root / "legacy"
        legacy.mkdir(mode=0o700)
        path = legacy / "operations.sqlite3"
        with sqlite3.connect(path) as connection:
            connection.executescript(
                """
                CREATE TABLE runs (
                    run_id TEXT PRIMARY KEY,
                    repository_id TEXT NOT NULL,
                    goal_id TEXT NOT NULL,
                    starting_ref TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    ended_at REAL,
                    last_seen_at REAL NOT NULL,
                    last_change_at REAL NOT NULL,
                    state_digest TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL
                );
                PRAGMA user_version = 1;
                """
            )
        path.chmod(0o600)

        migrated = OperationalStore(legacy)

        self.assertEqual(migrated.schema_version, LATEST_SCHEMA_VERSION)
        with sqlite3.connect(path) as connection:
            run_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(runs)")
            }
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertTrue({"pause_reason", "resume_required", "policy_json"} <= run_columns)
        self.assertTrue(
            {"preferences", "notifications", "process_instances", "failure_signatures"}
            <= tables
        )

    def test_version_two_database_migrates_to_current_schema(self):
        legacy = self.root / "legacy-v2"
        legacy.mkdir(mode=0o700)
        path = legacy / "operations.sqlite3"
        with sqlite3.connect(path) as connection:
            connection.executescript(
                """
                CREATE TABLE repositories (
                    repository_id TEXT PRIMARY KEY,
                    path TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    last_seen_at REAL NOT NULL
                );
                CREATE TABLE runs (
                    run_id TEXT PRIMARY KEY,
                    repository_id TEXT NOT NULL REFERENCES repositories(repository_id),
                    goal_id TEXT NOT NULL,
                    starting_ref TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    ended_at REAL,
                    last_seen_at REAL NOT NULL,
                    last_change_at REAL NOT NULL,
                    state_digest TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    pause_reason TEXT,
                    resume_required INTEGER NOT NULL DEFAULT 0,
                    policy_json TEXT NOT NULL DEFAULT '{}'
                );
                PRAGMA user_version = 2;
                """
            )
        path.chmod(0o600)
        migrated = OperationalStore(legacy)
        self.assertEqual(migrated.schema_version, LATEST_SCHEMA_VERSION)
        with sqlite3.connect(path) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertTrue({"process_instances", "failure_signatures"} <= tables)

    def test_version_three_database_migrates_to_current_schema(self):
        legacy = self.root / "legacy-v3"
        legacy.mkdir(mode=0o700)
        path = legacy / "operations.sqlite3"
        with sqlite3.connect(path) as connection:
            connection.executescript(
                """
                CREATE TABLE runs (
                    run_id TEXT PRIMARY KEY,
                    repository_id TEXT NOT NULL,
                    goal_id TEXT NOT NULL,
                    starting_ref TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    ended_at REAL,
                    last_seen_at REAL NOT NULL,
                    last_change_at REAL NOT NULL,
                    state_digest TEXT NOT NULL,
                    snapshot_json TEXT NOT NULL,
                    pause_reason TEXT,
                    resume_required INTEGER NOT NULL DEFAULT 0,
                    policy_json TEXT NOT NULL DEFAULT '{}'
                );
                PRAGMA user_version = 3;
                """
            )
        path.chmod(0o600)
        migrated = OperationalStore(legacy)
        self.assertEqual(migrated.schema_version, LATEST_SCHEMA_VERSION)
        with sqlite3.connect(path) as connection:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
        self.assertIn("archived_at", columns)

    def test_guardrails_reject_nonpositive_limits_and_bad_warning_ratio(self):
        with self.assertRaisesRegex(ValueError, "positive"):
            GuardrailPolicy(input_tokens=0)
        with self.assertRaisesRegex(ValueError, "between"):
            GuardrailPolicy(warning_ratio=1)

    def test_guardrail_evaluation_warns_then_stops_on_observed_values(self):
        value = snapshot()
        value["runtime"].update(
            {
                "timing": {
                    "turn": {"seconds": 81},
                    "overall": {"seconds": 200},
                },
                "tokens": {
                    "output_tokens": 500,
                    "input_tokens": 100,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
            }
        )
        warning = evaluate_guardrails(
            value,
            GuardrailPolicy(turn_seconds=100, generated_tokens=1000),
            last_change_at=190,
            now=200,
        )
        self.assertEqual(warning["status"], "warning")
        self.assertEqual(warning["findings"][0]["metric"], "turn_seconds")

        value["runtime"]["tokens"]["output_tokens"] = 1000
        stopped = evaluate_guardrails(
            value,
            GuardrailPolicy(turn_seconds=100, generated_tokens=1000),
            last_change_at=190,
            now=200,
        )
        self.assertEqual(stopped["status"], "stop")
        self.assertIn("generated_tokens", {item["metric"] for item in stopped["findings"]})

    def test_corrupt_backup_is_refused(self):
        corrupt = self.root / "corrupt.sqlite3"
        corrupt.write_text(json.dumps({"not": "sqlite"}), encoding="utf-8")
        with self.assertRaises((ValueError, sqlite3.DatabaseError)):
            self.store.restore(corrupt)


if __name__ == "__main__":
    unittest.main()
