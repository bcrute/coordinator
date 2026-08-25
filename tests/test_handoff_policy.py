"""Contracts for budget-aware, provider-neutral implementation handoffs."""

from __future__ import annotations

import unittest

from coordinator.executor_settings import ExecutorConfiguration
from coordinator.handoff_policy import (
    handoff_budget,
    policy_catalog_instruction,
    validate_handoff_task,
)


def task(scope: list[str], acceptance: list[str] | None = None) -> str:
    work = [f"Implement and verify {item.lower()}" for item in scope]
    accepted = acceptance or [f"{item} works." for item in scope]
    return (
        "# Current task\n\n"
        "- Task ID: `TEST-001`\n- State: `ready`\n- Review round: `0`\n"
        "- Executor: `configured`\n\n"
        "## Objective\n\nComplete one bounded slice.\n\n"
        "## In scope\n\n"
        + "\n".join(f"- {item}" for item in scope)
        + "\n\n## Work units\n\n"
        + "\n".join(f"- [ ] {item}" for item in work)
        + "\n\n## Acceptance criteria\n\n"
        + "\n".join(f"- {item}" for item in accepted)
        + "\n"
    )


class HandoffBudgetTests(unittest.TestCase):
    def test_saved_qwen_step_limit_determines_units_and_verification_reserve(self) -> None:
        twelve = handoff_budget(
            ExecutorConfiguration(
                executor_adapter="mini-swe-agent",
                mini_swe_model="local",
                mini_swe_step_limit=12,
            )
        )
        twenty_four = handoff_budget(
            ExecutorConfiguration(
                executor_adapter="mini-swe-agent",
                mini_swe_model="local",
                mini_swe_step_limit=24,
            )
        )

        self.assertEqual((twelve.verification_reserve, twelve.maximum_work_units), (3, 2))
        self.assertEqual(
            (twenty_four.verification_reserve, twenty_four.maximum_work_units),
            (6, 4),
        )

    def test_task_at_limit_passes_and_compound_task_is_rejected(self) -> None:
        configuration = ExecutorConfiguration(
            executor_adapter="mini-swe-agent",
            mini_swe_model="local",
            mini_swe_step_limit=24,
        )
        validate_handoff_task(task(["Deck model", "Deal", "Moves", "Focused tests"]), configuration)

        with self.assertRaisesRegex(ValueError, "5 work units.*at most 4; split the task"):
            validate_handoff_task(
                task(["Deck model", "Deal", "Moves", "Browser UI", "Focused tests"]),
                configuration,
            )

    def test_structure_cannot_hide_unmapped_scope_or_excess_acceptance(self) -> None:
        configuration = ExecutorConfiguration(
            executor_adapter="mini-swe-agent",
            mini_swe_model="local",
            mini_swe_step_limit=12,
        )
        malformed = task(["Deck", "Deal"]).replace(
            "- [ ] Implement and verify deal\n", ""
        )
        with self.assertRaisesRegex(ValueError, "one Work units checklist item"):
            validate_handoff_task(malformed, configuration)
        with self.assertRaisesRegex(ValueError, "acceptance criteria.*at most 3"):
            validate_handoff_task(
                task(["Deck"], ["One", "Two", "Three", "Four"]), configuration
            )

    def test_policy_prompt_describes_each_runtime_without_naming_a_primary_provider(self) -> None:
        instruction = policy_catalog_instruction(
            ExecutorConfiguration(mini_swe_model="local", mini_swe_step_limit=24)
        )
        self.assertIn("24 model steps", instruction)
        self.assertIn("40 model turns", instruction)
        self.assertNotIn("Codex", instruction)
        self.assertNotIn("Claude", instruction)


if __name__ == "__main__":
    unittest.main()
