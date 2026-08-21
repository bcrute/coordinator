"""Public CLI contracts for the installable Coordinator package."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from coordinator import __version__
from coordinator.cli import main


class CoordinatorCLITests(unittest.TestCase):
    def invoke(self, arguments: list[str]) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = main(arguments)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_help_and_version(self) -> None:
        result, output, error = self.invoke(["--help"])
        self.assertEqual(result, 0, error)
        self.assertIn("serve", output)
        self.assertIn("doctor", output)
        self.assertIn("init", output)

        result, output, error = self.invoke(["--version"])
        self.assertEqual(result, 0, error)
        self.assertEqual(output.strip(), f"coordinator {__version__}")

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

    def test_init_uses_packaged_template_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            first, output, error = self.invoke(
                ["init", str(target), "--project-name", "CLI package test"]
            )
            self.assertEqual(first, 0, error)
            self.assertIn("Installed or updated", output)
            self.assertTrue((target / ".coordination" / "README.md").is_file())

            second, output, error = self.invoke(
                ["init", str(target), "--project-name", "CLI package test"]
            )
            self.assertEqual(second, 0, error)
            self.assertIn("already current", output)
