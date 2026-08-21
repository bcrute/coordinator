"""Focused tests for safe, project-local GitHub CI setup."""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from coordinator.github_ci import (
    GENERATED_MARKER,
    WORKFLOW_RELATIVE_PATH,
    configure_github_ci,
    inspect_github_ci,
    install_github_ci,
)


class GitHubCITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_inspection_reports_sanitized_github_origin_and_existing_workflows(self) -> None:
        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "remote",
                "add",
                "origin",
                "git@github.com:example/project.git",
            ],
            check=True,
        )
        workflow = self.repo / ".github" / "workflows" / "tests.yaml"
        workflow.parent.mkdir(parents=True)
        workflow.write_text("name: Tests\n", encoding="utf-8")

        status = inspect_github_ci(self.repo)

        self.assertEqual(status.github_repository, "example/project")
        self.assertEqual(status.workflows, (".github/workflows/tests.yaml",))
        self.assertTrue(status.requires_confirmation)
        self.assertFalse(status.coordinator_configured)

    def test_auto_installs_when_no_ci_exists_and_is_idempotent(self) -> None:
        first = configure_github_ci(self.repo, "auto", interactive=False)
        second = configure_github_ci(self.repo, "auto", interactive=False)
        workflow = self.repo / WORKFLOW_RELATIVE_PATH

        self.assertEqual(first.outcome, "installed")
        self.assertEqual(second.outcome, "configured")
        self.assertTrue(workflow.read_text(encoding="utf-8").startswith(GENERATED_MARKER))
        self.assertIn('python-version: "3.14"', workflow.read_text(encoding="utf-8"))
        self.assertIn("permissions:\n  contents: read", workflow.read_text(encoding="utf-8"))

    def test_auto_does_not_modify_existing_ci_without_confirmation(self) -> None:
        existing = self.repo / ".github" / "workflows" / "tests.yml"
        existing.parent.mkdir(parents=True)
        existing.write_text("name: Existing\n", encoding="utf-8")

        status = configure_github_ci(self.repo, "auto", interactive=False)

        self.assertEqual(status.outcome, "confirmation_required")
        self.assertFalse((self.repo / WORKFLOW_RELATIVE_PATH).exists())
        self.assertEqual(existing.read_text(encoding="utf-8"), "name: Existing\n")

    def test_interactive_confirmation_adds_alongside_existing_ci(self) -> None:
        existing = self.repo / ".github" / "workflows" / "tests.yml"
        existing.parent.mkdir(parents=True)
        existing.write_text("name: Existing\n", encoding="utf-8")
        with mock.patch("builtins.input", return_value="yes"):
            status = configure_github_ci(self.repo, "auto", interactive=True)

        self.assertEqual(status.outcome, "installed")
        self.assertTrue(existing.exists())
        self.assertTrue((self.repo / WORKFLOW_RELATIVE_PATH).is_file())

    def test_install_refuses_user_owned_target_and_symlinked_directory(self) -> None:
        destination = self.repo / WORKFLOW_RELATIVE_PATH
        destination.parent.mkdir(parents=True)
        destination.write_text("name: User owned\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "not Coordinator-managed"):
            install_github_ci(self.repo)

        destination.unlink()
        (self.repo / ".github" / "workflows").rmdir()
        outside = self.repo / "outside"
        outside.mkdir()
        (self.repo / ".github" / "workflows").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            install_github_ci(self.repo)
