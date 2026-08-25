"""Safety and lifecycle tests for the disposable Solitaire validation loop."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "validation" / "solitaire" / "cycle.py"
SPEC = importlib.util.spec_from_file_location("solitaire_validation_cycle", MODULE_PATH)
assert SPEC and SPEC.loader
cycle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cycle)


class SolitaireValidationCycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.target = self.root / "solitaire-test"
        subprocess.run(
            ["git", "init", "--quiet", str(self.target)],
            check=True,
            capture_output=True,
            text=True,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def finish_report(self, outcome: str = "failed") -> None:
        path = self.target / ".coordinator-validation" / "report.json"
        report = json.loads(path.read_text(encoding="utf-8"))
        report.update(
            {
                "outcome": outcome,
                "stage": "implementation",
                "summary": "The cycle reached a terminal test result.",
                "finished_at": "2026-08-25T12:00:00Z",
            }
        )
        path.write_text(json.dumps(report), encoding="utf-8")

    def test_prepare_installs_a_cycle_specific_contract_without_product_files(self) -> None:
        result = cycle.prepare_target(self.target, 3)

        self.assertEqual(result["cycle_id"], 3)
        self.assertIn("complete Coordinator validation scenario", result["prompt"])
        marker = json.loads(
            (self.target / cycle.MARKER_NAME).read_text(encoding="utf-8")
        )
        report = json.loads(
            (self.target / cycle.REPORT_DIRECTORY / "report.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertTrue(marker["disposable"])
        self.assertEqual(marker["cycle_id"], 3)
        self.assertEqual(report["cycle_id"], 3)
        self.assertEqual(report["outcome"], "running")
        self.assertFalse((self.target / ".coordination").exists())

    def test_prepare_is_idempotent_for_the_same_cycle_and_refuses_a_different_one(self) -> None:
        cycle.prepare_target(self.target, 1)
        report = self.target / cycle.REPORT_DIRECTORY / "report.json"
        report.write_text(report.read_text(encoding="utf-8").replace("has started", "continues"), encoding="utf-8")

        cycle.prepare_target(self.target, 1)
        self.assertIn("continues", report.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(cycle.CycleError, "already assigned to cycle 1"):
            cycle.prepare_target(self.target, 2)

    def test_restart_archives_the_complete_terminal_cycle_and_creates_a_clean_one(self) -> None:
        cycle.prepare_target(self.target, 1)
        (self.target / "product-file.txt").write_text("evidence", encoding="utf-8")
        self.finish_report("blocked")

        result = cycle.restart_target(
            self.target,
            self.root / "archives",
            2,
            str(self.target.resolve()),
        )

        archive = Path(result["archive"])
        self.assertEqual(result["previous_outcome"], "blocked")
        self.assertEqual((archive / "product-file.txt").read_text(encoding="utf-8"), "evidence")
        self.assertFalse((self.target / "product-file.txt").exists())
        self.assertTrue((self.target / ".git").is_dir())
        new_report = json.loads(
            (self.target / cycle.REPORT_DIRECTORY / "report.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(new_report["cycle_id"], 2)
        self.assertEqual(new_report["outcome"], "running")

    def test_bootstrap_archives_an_unmanaged_experiment_and_creates_cycle_one(self) -> None:
        (self.target / "exploratory.txt").write_text("old run", encoding="utf-8")

        result = cycle.bootstrap_target(
            self.target,
            self.root / "archives",
            str(self.target.resolve()),
        )

        archive = Path(result["archive"])
        self.assertEqual(result["action"], "bootstrap")
        self.assertEqual((archive / "exploratory.txt").read_text(encoding="utf-8"), "old run")
        self.assertFalse((self.target / "exploratory.txt").exists())
        marker = json.loads(
            (self.target / cycle.MARKER_NAME).read_text(encoding="utf-8")
        )
        self.assertEqual(marker["cycle_id"], 1)

    def test_bootstrap_refuses_a_managed_target_and_coordination_locks(self) -> None:
        cycle.prepare_target(self.target, 1)
        with self.assertRaisesRegex(cycle.CycleError, "already protocol-managed"):
            cycle.bootstrap_target(
                self.target,
                self.root / "archives",
                str(self.target.resolve()),
            )

        unmanaged = self.root / "unmanaged-solitaire"
        subprocess.run(["git", "init", "--quiet", str(unmanaged)], check=True)
        lock = unmanaged / ".coordination" / "watcher.lock"
        lock.parent.mkdir(parents=True)
        lock.write_text("present", encoding="utf-8")
        with self.assertRaisesRegex(cycle.CycleError, "locks are still present"):
            cycle.bootstrap_target(
                unmanaged,
                self.root / "archives",
                str(unmanaged.resolve()),
            )

    def test_restart_requires_exact_confirmation_and_a_terminal_report(self) -> None:
        cycle.prepare_target(self.target, 1)
        with self.assertRaisesRegex(cycle.CycleError, "confirmation"):
            cycle.restart_target(self.target, self.root / "archives", 2, "yes")
        with self.assertRaisesRegex(cycle.CycleError, "terminal outcome"):
            cycle.restart_target(
                self.target,
                self.root / "archives",
                2,
                str(self.target.resolve()),
            )

    def test_restart_refuses_coordination_locks_without_moving_the_target(self) -> None:
        cycle.prepare_target(self.target, 1)
        self.finish_report()
        lock = self.target / ".coordination" / "runtime" / "executor.lock"
        lock.parent.mkdir(parents=True)
        lock.write_text("active or stale", encoding="utf-8")

        with self.assertRaisesRegex(cycle.CycleError, "locks are still present"):
            cycle.restart_target(
                self.target,
                self.root / "archives",
                2,
                str(self.target.resolve()),
            )
        self.assertTrue(self.target.is_dir())
        self.assertFalse((self.root / "archives").exists())

    def test_coordinator_repository_cannot_be_prepared_as_a_target(self) -> None:
        with self.assertRaisesRegex(cycle.CycleError, "Coordinator source"):
            cycle.prepare_target(ROOT, 1)


if __name__ == "__main__":
    unittest.main()
