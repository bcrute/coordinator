"""Focused tests for safe, project-local GitHub CI setup."""

from __future__ import annotations

import contextlib
import io
import subprocess
import sys
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

    def test_remote_discovery_accepts_normal_github_forms_without_leaking_credentials(
        self,
    ) -> None:
        accepted = {
            "git@github.com:example/project.git": "example/project",
            "ssh://git@github.com/example/project.git": "example/project",
            "https://github.com/example/project.git": "example/project",
        }
        for remote, expected in accepted.items():
            with self.subTest(remote=remote):
                subprocess.run(
                    ["git", "-C", str(self.repo), "remote", "remove", "origin"],
                    check=False,
                    capture_output=True,
                )
                subprocess.run(
                    ["git", "-C", str(self.repo), "remote", "add", "origin", remote],
                    check=True,
                )
                self.assertEqual(
                    inspect_github_ci(self.repo).github_repository, expected
                )

        subprocess.run(
            [
                "git",
                "-C",
                str(self.repo),
                "remote",
                "set-url",
                "origin",
                "https://credential@github.com/example/project.git",
            ],
            check=True,
        )
        self.assertIsNone(inspect_github_ci(self.repo).github_repository)

    @mock.patch("coordinator.github_ci.subprocess.run", side_effect=OSError("git missing"))
    def test_inspection_remains_available_when_git_remote_lookup_fails(self, _run) -> None:
        status = inspect_github_ci(self.repo)
        self.assertEqual(status.outcome, "available")
        self.assertIsNone(status.github_repository)

    def test_discovery_ignores_non_workflows_directories_and_symlinks(self) -> None:
        workflows = self.repo / ".github" / "workflows"
        workflows.mkdir(parents=True)
        (workflows / "notes.txt").write_text("not a workflow\n", encoding="utf-8")
        (workflows / "nested.yml").mkdir()
        outside = self.repo / "outside.yml"
        outside.write_text("name: Outside\n", encoding="utf-8")
        (workflows / "linked.yml").symlink_to(outside)

        status = inspect_github_ci(self.repo)

        self.assertEqual(status.workflows, ())
        self.assertEqual(status.outcome, "available")

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
        with contextlib.redirect_stdout(io.StringIO()), mock.patch(
            "builtins.input", return_value="yes"
        ):
            status = configure_github_ci(self.repo, "auto", interactive=True)

        self.assertEqual(status.outcome, "installed")
        self.assertTrue(existing.exists())
        self.assertTrue((self.repo / WORKFLOW_RELATIVE_PATH).is_file())

    def test_declining_or_explicitly_skipping_keeps_existing_ci_unchanged(self) -> None:
        existing = self.repo / ".github" / "workflows" / "tests.yml"
        existing.parent.mkdir(parents=True)
        existing.write_text("name: Existing\n", encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()), mock.patch(
            "builtins.input", return_value="no"
        ):
            declined = configure_github_ci(self.repo, "auto", interactive=True)
        skipped = configure_github_ci(self.repo, "skip", interactive=False)

        self.assertEqual(declined.outcome, "confirmation_required")
        self.assertEqual(skipped.outcome, "skipped")
        self.assertEqual(existing.read_text(encoding="utf-8"), "name: Existing\n")
        self.assertFalse((self.repo / WORKFLOW_RELATIVE_PATH).exists())

    def test_direct_install_is_idempotent_for_coordinator_managed_workflow(self) -> None:
        first = install_github_ci(self.repo)
        original = (self.repo / WORKFLOW_RELATIVE_PATH).read_bytes()
        second = install_github_ci(self.repo)

        self.assertEqual(first.outcome, "installed")
        self.assertEqual(second.outcome, "configured")
        self.assertEqual((self.repo / WORKFLOW_RELATIVE_PATH).read_bytes(), original)

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

    def test_install_refuses_unsafe_parent_and_destination_shapes(self) -> None:
        cases = ("github_file", "workflows_file", "destination_directory")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                repo = Path(directory)
                if case == "github_file":
                    (repo / ".github").write_text("not a directory\n", encoding="utf-8")
                    expected = ".github exists but is not a directory"
                elif case == "workflows_file":
                    (repo / ".github").mkdir()
                    (repo / ".github" / "workflows").write_text(
                        "not a directory\n", encoding="utf-8"
                    )
                    expected = ".github/workflows exists but is not a directory"
                else:
                    (repo / WORKFLOW_RELATIVE_PATH).mkdir(parents=True)
                    expected = "workflow path exists but is not a regular file"
                with self.assertRaisesRegex(ValueError, expected):
                    install_github_ci(repo)

    def test_binary_user_owned_coordinator_workflow_is_never_overwritten(self) -> None:
        destination = self.repo / WORKFLOW_RELATIVE_PATH
        destination.parent.mkdir(parents=True)
        original = b"\xff\xfeuser-owned"
        destination.write_bytes(original)

        with self.assertRaisesRegex(ValueError, "not Coordinator-managed"):
            install_github_ci(self.repo)

        self.assertEqual(destination.read_bytes(), original)


class CoordinationValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.repo = Path(self.temporary.name)
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "coordinator.init_project",
                str(self.repo),
                "--project-name",
                "Validator test",
                "--github-ci",
                "skip",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            self.fail(result.stderr)
        self.validator = self.repo / ".coordination" / "scripts" / "validate.py"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def run_validator(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(self.validator)],
            check=False,
            capture_output=True,
            text=True,
        )

    def test_validator_accepts_a_freshly_initialized_project(self) -> None:
        result = self.run_validator()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("structurally valid", result.stdout)

    def test_validator_reports_missing_empty_and_malformed_managed_files(self) -> None:
        (self.repo / ".coordination" / "PROJECT.md").unlink()
        (self.repo / ".coordination" / "coder" / "status.md").write_text(
            "", encoding="utf-8"
        )
        (self.repo / "AGENTS.md").write_text(
            "<!-- coordinate-claude-work:end -->\n"
            "<!-- coordinate-claude-work:start -->\n",
            encoding="utf-8",
        )

        result = self.run_validator()

        self.assertEqual(result.returncode, 1)
        self.assertIn("missing .coordination/PROJECT.md", result.stderr)
        self.assertIn("empty .coordination/coder/status.md", result.stderr)
        self.assertIn("AGENTS.md must contain one complete", result.stderr)
