"""Public CLI contracts for the installable Coordinator package."""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from coordinator import MINIMUM_PYTHON, __version__
from coordinator.cli import main


class CoordinatorCLITests(unittest.TestCase):
    def invoke(self, arguments: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(arguments)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_help_and_version(self) -> None:
        self.assertEqual(MINIMUM_PYTHON, (3, 14))
        result, output, error = self.invoke(["--help"])
        self.assertEqual(result, 0, error)
        self.assertIn("serve", output)
        self.assertIn("doctor", output)
        self.assertIn("init", output)
        self.assertIn("data", output)
        self.assertIn("run-turn", output)

        result, output, error = self.invoke(["--version"])
        self.assertEqual(result, 0, error)
        self.assertEqual(output.strip(), f"coordinator {__version__}")

    def test_import_refuses_an_older_interpreter(self) -> None:
        source = (Path(__file__).parents[1] / "src" / "coordinator" / "__init__.py")
        unsupported = (MINIMUM_PYTHON[0], MINIMUM_PYTHON[1] - 1)
        with mock.patch.object(sys, "version_info", unsupported):
            with self.assertRaisesRegex(RuntimeError, "requires Python 3.14"):
                exec(compile(source.read_bytes(), str(source), "exec"), {})

    def test_doctor_reports_machine_readable_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "project"
            repo.mkdir()
            result, output, error = self.invoke(
                ["doctor", "--repo", str(repo), "--repositories-root", str(root), "--json"]
            )
        self.assertEqual(result, 0, error)
        payload = json.loads(output)
        self.assertTrue(payload["ok"])
        checks = {item["name"]: item for item in payload["checks"]}
        self.assertEqual(checks["repository"]["status"], "pass")
        self.assertEqual(checks["coordination"]["status"], "warn")

    def test_doctor_fails_only_for_missing_required_directories(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result, output, error = self.invoke(
                [
                    "doctor",
                    "--repo",
                    str(root / "missing-repo"),
                    "--repositories-root",
                    str(root / "missing-root"),
                    "--json",
                ]
            )
        self.assertEqual(result, 1, error)
        payload = json.loads(output)
        self.assertFalse(payload["ok"])
        checks = {item["name"]: item for item in payload["checks"]}
        self.assertEqual(checks["repository"]["status"], "fail")
        self.assertEqual(checks["repositories_root"]["status"], "fail")

    def test_init_uses_packaged_template_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            first, output, error = self.invoke(
                ["init", str(target), "--project-name", "CLI package test"]
            )
            self.assertEqual(first, 0, error)
            self.assertIn("Installed or updated", output)
            self.assertTrue((target / ".coordination" / "README.md").is_file())
            self.assertTrue(
                (target / ".coordination" / "scripts" / "validate.py").is_file()
            )
            self.assertTrue(
                (target / ".github" / "workflows" / "coordinator.yml").is_file()
            )

            second, output, error = self.invoke(
                ["init", str(target), "--project-name", "CLI package test"]
            )
            self.assertEqual(second, 0, error)
            self.assertIn("already current", output)
            self.assertIn("already configured", output)

    def test_init_requires_confirmation_before_adding_alongside_existing_ci(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            existing = target / ".github" / "workflows" / "tests.yml"
            existing.parent.mkdir(parents=True)
            existing.write_text("name: Existing\n", encoding="utf-8")
            with mock.patch.object(sys.stdin, "isatty", return_value=False):
                result, output, error = self.invoke(
                    ["init", str(target), "--project-name", "Existing CI"]
                )
            self.assertEqual(result, 0, error)
            self.assertIn("choose whether to add", output)
            self.assertFalse((existing.parent / "coordinator.yml").exists())

            result, output, error = self.invoke(
                [
                    "init",
                    str(target),
                    "--project-name",
                    "Existing CI",
                    "--github-ci",
                    "add",
                ]
            )
            self.assertEqual(result, 0, error)
            self.assertIn("Added .github/workflows/coordinator.yml", output)
            self.assertTrue((existing.parent / "coordinator.yml").is_file())

    def test_init_rejects_missing_target_and_honors_explicit_ci_skip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result, output, error = self.invoke(
                [
                    "init",
                    str(root / "missing"),
                    "--project-name",
                    "Missing",
                ]
            )
            self.assertEqual(result, 2)
            self.assertEqual(output, "")
            self.assertIn("target is not a directory", error)

            result, output, error = self.invoke(
                [
                    "init",
                    str(root),
                    "--project-name",
                    "Skipped CI",
                    "--github-ci",
                    "skip",
                ]
            )
            self.assertEqual(result, 0, error)
            self.assertIn("current CI configuration", output)
            self.assertFalse((root / ".github" / "workflows" / "coordinator.yml").exists())

    def test_data_command_backs_up_and_verifies_operational_index(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            backup = root / "backup.sqlite3"
            result, output, error = self.invoke(
                ["data", "--state-dir", str(state), "backup", str(backup)]
            )
            self.assertEqual(result, 0, error)
            self.assertEqual(json.loads(output)["action"], "backup")
            self.assertTrue(backup.is_file())

            result, output, error = self.invoke(
                ["data", "--state-dir", str(state), "verify", str(backup)]
            )
            self.assertEqual(result, 0, error)
            self.assertTrue(json.loads(output)["ok"])
