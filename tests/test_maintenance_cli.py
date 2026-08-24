"""End-to-end contracts for operational data maintenance commands."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from coordinator.init_project import main as initialize_project
from coordinator.maintenance import main
from coordinator.operational_store import OperationalStore


def active_snapshot() -> dict[str, object]:
    return {
        "goal": {"id": "maintenance-test", "state": "active", "starting_ref": "abc"},
        "task": {"id": "task-1", "state": "implementing", "review_round": "0"},
        "coder": {"state": "implementing", "matches_current_task": True},
        "review": {"verdict": "not_reviewed"},
        "runtime": {},
        "completion": {"present": False},
        "workflow": {"phase": "implementing", "active": True},
    }


class MaintenanceCLITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def invoke(self, arguments: list[str]) -> tuple[int, dict[str, object]]:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = main(["--state-dir", str(self.state), *arguments])
        return result, json.loads(output.getvalue())

    def test_backup_verify_and_restore_round_trip_preserves_the_backup_snapshot(self) -> None:
        store = OperationalStore(self.state)
        store.set_preference("theme", "dark")
        backup = self.root / "backups" / "operational.sqlite3"

        result, payload = self.invoke(["backup", str(backup)])
        self.assertEqual(result, 0)
        self.assertEqual(payload["action"], "backup")
        self.assertTrue(backup.is_file())

        store.set_preference("theme", "light")
        result, payload = self.invoke(["verify", str(backup)])
        self.assertEqual(result, 0)
        self.assertEqual(payload["path"], str(backup))

        result, payload = self.invoke(["restore", str(backup)])
        self.assertEqual(result, 0)
        self.assertEqual(payload["action"], "restore")
        self.assertEqual(OperationalStore(self.state).preferences()["theme"], "dark")

    def test_rebuild_indexes_valid_repositories_and_rejects_missing_ones(self) -> None:
        repo = self.root / "project"
        repo.mkdir()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(
                initialize_project(
                    [
                        str(repo),
                        "--project-name",
                        "Maintenance project",
                        "--github-ci",
                        "skip",
                    ]
                ),
                0,
            )

        result, payload = self.invoke(["rebuild", str(repo)])
        self.assertEqual(result, 0)
        self.assertEqual(payload, {"ok": True, "action": "rebuild", "repositories": 1})

        result, payload = self.invoke(["rebuild", str(self.root / "missing")])
        self.assertEqual(result, 1)
        self.assertFalse(payload["ok"])
        self.assertIn("repository does not exist", str(payload["error"]))

    def test_prune_deletes_old_events_and_rejects_nonpositive_retention(self) -> None:
        repo = self.root / "project"
        repo.mkdir()
        store = OperationalStore(self.state)
        store.sync_snapshot(repo, active_snapshot(), now=1.0)

        result, payload = self.invoke(["prune", "--days", "1"])
        self.assertEqual(result, 0)
        self.assertGreaterEqual(int(payload["deleted"]), 1)

        result, payload = self.invoke(["prune", "--days", "0"])
        self.assertEqual(result, 1)
        self.assertEqual(payload["error"], "--days must be positive")

    def test_verify_defaults_to_live_index_and_reports_corrupt_input(self) -> None:
        OperationalStore(self.state)
        result, payload = self.invoke(["verify"])
        self.assertEqual(result, 0)
        self.assertEqual(payload["action"], "verify")

        corrupt = self.root / "corrupt.sqlite3"
        corrupt.write_text("not a database", encoding="utf-8")
        result, payload = self.invoke(["verify", str(corrupt)])
        self.assertEqual(result, 1)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "database could not be read as SQLite")

    def test_compact_removes_duplicate_events_and_reports_file_sizes(self) -> None:
        repo = self.root / "project"
        repo.mkdir()
        store = OperationalStore(self.state)
        details = store.sync_snapshot(repo, active_snapshot(), now=1.0)
        with store._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM events WHERE run_id = ?", (details["run_id"],)
            ).fetchone()
            connection.execute(
                "INSERT INTO events(event_uid, run_id, created_at, event_type, payload_json) "
                "VALUES (?, ?, ?, ?, ?)",
                ("old-duplicate", details["run_id"], 2.0, "state_changed", row[0]),
            )
        result, payload = self.invoke(["compact"])
        self.assertEqual(result, 0)
        self.assertEqual(payload["action"], "compact")
        self.assertEqual(payload["deleted"], 1)
        self.assertLessEqual(payload["after_bytes"], payload["before_bytes"])
