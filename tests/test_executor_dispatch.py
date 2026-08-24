"""The interactive handoff path honors the application's saved executor."""

from __future__ import annotations

import argparse
import contextlib
import io
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest import mock

from coordinator.executor_settings import EXECUTOR_PREFERENCE_KEY, ExecutorConfiguration
from coordinator.operational_store import OperationalStore
from coordinator.run_executor_turn import run


class ExecutorDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.state = self.root / "state"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def arguments(self, *, dry_run: bool = False) -> argparse.Namespace:
        return argparse.Namespace(repo=self.repo, state_dir=self.state, dry_run=dry_run)

    def test_refuses_to_invent_missing_settings(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = run(self.arguments())
        self.assertEqual(result, 2)
        self.assertIn("settings do not exist", output.getvalue())
        self.assertFalse((self.state / "operations.sqlite3").exists())

        OperationalStore(self.state)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = run(self.arguments())
        self.assertEqual(result, 2)
        self.assertIn("no executor is saved", output.getvalue())

    def test_saved_mini_executor_is_the_dispatched_command(self) -> None:
        configuration = ExecutorConfiguration(
            executor_adapter="mini-swe-agent",
            mini_swe_model="Qwen/Qwen3.8-27B",
            mini_swe_api_base="http://qwen.test:8000/v1",
        )
        store = OperationalStore(self.state)
        store.set_preference(EXECUTOR_PREFERENCE_KEY, asdict(configuration))
        completed = mock.Mock(returncode=7)
        with mock.patch(
            "coordinator.run_executor_turn.subprocess.run", return_value=completed
        ) as invoke:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                result = run(self.arguments())
        self.assertEqual(result, 7)
        command = invoke.call_args.args[0]
        self.assertIn("coordinator.run_mini_swe_turn", command)
        self.assertIn("Qwen/Qwen3.8-27B", command)
        self.assertIn(
            str(Path(__file__).parents[1] / "src"),
            invoke.call_args.kwargs["env"]["PYTHONPATH"],
        )
        self.assertIn("Configured executor: mini-swe-agent", output.getvalue())

    def test_dry_run_reports_selection_without_starting_process(self) -> None:
        store = OperationalStore(self.state)
        store.set_preference(EXECUTOR_PREFERENCE_KEY, asdict(ExecutorConfiguration()))
        with mock.patch("coordinator.run_executor_turn.subprocess.run") as invoke:
            with contextlib.redirect_stdout(io.StringIO()) as output:
                result = run(self.arguments(dry_run=True))
        self.assertEqual(result, 0)
        invoke.assert_not_called()
        self.assertIn("coordinator.run_claude_turn", output.getvalue())


if __name__ == "__main__":
    unittest.main()
