"""Ensure every production module has an explicit behavioral test owner."""

from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "src" / "coordinator"

TEST_OWNERS: dict[str, tuple[str, ...]] = {
    "__init__": ("test_coordinator_cli.py",),
    "__main__": ("test_coordinator_cli.py",),
    "api_contract": ("test_authenticated_web_app.py",),
    "authenticated_web_app": ("test_authenticated_web_app.py",),
    "cli": ("test_coordinator_cli.py",),
    "codex_session": ("test_codex_session.py", "test_authenticated_web_app.py"),
    "configuration": ("test_web_settings.py",),
    "coordination_dashboard": ("test_workflow.py",),
    "doctor": ("test_coordinator_cli.py",),
    "executor_adapters": ("test_executor_adapters.py",),
    "github_ci": ("test_github_ci.py", "test_authenticated_web_app.py"),
    "init_project": ("test_coordinator_cli.py", "test_workflow.py"),
    "maintenance": ("test_maintenance_cli.py",),
    "operational_store": ("test_operational_store.py",),
    "process_activity": ("test_process_activity.py",),
    "processes": ("test_workflow.py",),
    "provider_usage": ("test_provider_usage.py",),
    "repositories": ("test_web_repository_switching.py",),
    "run_claude_turn": ("test_workflow.py", "test_workflow_runners.py"),
    "run_codex_review": ("test_workflow.py", "test_workflow_runners.py"),
    "run_mini_swe_turn": ("test_executor_adapters.py",),
    "security": ("test_authenticated_web_app.py",),
    "start_claude_team": ("test_workflow.py", "test_workflow_runners.py"),
    "watch_coordination": ("test_workflow.py", "test_workflow_runners.py"),
    "web_app": ("test_workflow.py", "test_web_repository_switching.py"),
    "workflow_state": ("test_web_workflow_state.py",),
}


class TestOwnershipTests(unittest.TestCase):
    def test_every_production_module_has_an_existing_behavioral_test_owner(self) -> None:
        modules = {path.stem for path in PACKAGE.glob("*.py")}
        self.assertEqual(set(TEST_OWNERS), modules)
        for module, owners in TEST_OWNERS.items():
            with self.subTest(module=module):
                self.assertTrue(owners)
                for owner in owners:
                    self.assertTrue((ROOT / "tests" / owner).is_file(), owner)
                    self.assertNotEqual(owner, Path(__file__).name)
