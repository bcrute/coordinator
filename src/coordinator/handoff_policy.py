"""Hard, provider-neutral sizing rules for one implementation handoff."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

from .executor_settings import ExecutorConfiguration, load_project_executor_settings

CHECKBOX_ITEM = re.compile(r"^- \[ \] (\S.*)$")
BULLET_ITEM = re.compile(r"^- (?!\[[ xX]\] )(\S.*)$")


@dataclass(frozen=True)
class HandoffBudget:
    """A runtime budget translated into a maximum number of work units."""

    runtime: str
    limit_name: str
    limit: int
    verification_reserve: int
    cost_per_work_unit: int
    maximum_work_units: int


def load_handoff_configuration(
    repo: Path,
    fallback: ExecutorConfiguration | None = None,
) -> ExecutorConfiguration:
    """Load project settings, allowing a trusted caller-provided legacy fallback."""

    try:
        return load_project_executor_settings(repo)
    except ValueError as error:
        if "do not exist" not in str(error):
            raise
        return fallback or ExecutorConfiguration()


def handoff_budget(
    configuration: ExecutorConfiguration,
    selected_executor: str = "configured",
) -> HandoffBudget:
    """Return the hard work-unit budget for the selected implementation runtime."""

    selected = (
        configuration.executor_adapter
        if selected_executor == "configured"
        else selected_executor
    )
    if selected == "mini-swe-agent":
        limit = configuration.mini_swe_step_limit
        limit_name = "model steps"
        cost_per_unit = 4
    elif selected == "claude":
        limit = configuration.claude_max_turns
        limit_name = "model turns"
        cost_per_unit = 6
    else:
        raise ValueError(f"unknown task executor: {selected_executor!r}")
    if limit < cost_per_unit + 2:
        raise ValueError(
            f"{selected} requires at least {cost_per_unit + 2} {limit_name} "
            "for one work unit plus verification reserve"
        )
    reserve = max(2, math.ceil(limit * 0.25))
    maximum = max(1, (limit - reserve) // cost_per_unit)
    return HandoffBudget(
        runtime=selected,
        limit_name=limit_name,
        limit=limit,
        verification_reserve=reserve,
        cost_per_work_unit=cost_per_unit,
        maximum_work_units=maximum,
    )


def markdown_section(text: str, heading: str) -> str | None:
    match = re.search(
        rf"^## {re.escape(heading)}\s*$\n(.*?)(?=^## |\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    return match.group(1).strip() if match else None


def list_items(section: str, pattern: re.Pattern[str]) -> list[str]:
    return [match.group(1).strip() for line in section.splitlines() if (match := pattern.match(line))]


def validate_handoff_task(
    task: str,
    configuration: ExecutorConfiguration,
    selected_executor: str = "configured",
) -> HandoffBudget:
    """Reject a runnable task that cannot fit its configured executor budget.

    The primary model must expose independently testable work as a one-to-one
    checklist with the task's in-scope list. This is deliberately structural:
    Coordinator cannot reliably infer semantic complexity from prose, but it can
    prevent a primary from silently handing an entire roadmap to one bounded turn.
    """

    budget = handoff_budget(configuration, selected_executor)
    objective = markdown_section(task, "Objective")
    scope = markdown_section(task, "In scope")
    work = markdown_section(task, "Work units")
    acceptance = markdown_section(task, "Acceptance criteria")
    if not objective or objective.lower().startswith("no task"):
        raise ValueError("handoff policy requires one concrete Objective")
    if len(" ".join(objective.split())) > 500:
        raise ValueError("handoff policy Objective must be at most 500 characters")
    if scope is None or work is None or acceptance is None:
        raise ValueError(
            "handoff policy requires In scope, Work units, and Acceptance criteria sections"
        )
    scope_items = list_items(scope, BULLET_ITEM)
    work_items = list_items(work, CHECKBOX_ITEM)
    acceptance_items = list_items(acceptance, BULLET_ITEM)
    if not scope_items:
        raise ValueError("handoff policy requires at least one In scope bullet")
    if not work_items:
        raise ValueError("handoff policy requires at least one unchecked Work units item")
    if len(scope_items) != len(work_items):
        raise ValueError(
            "handoff policy requires one Work units checklist item per In scope bullet"
        )
    if len(work_items) > budget.maximum_work_units:
        raise ValueError(
            f"handoff has {len(work_items)} work units but {budget.limit} "
            f"{budget.limit_name} allow at most {budget.maximum_work_units}; split the task"
        )
    if not acceptance_items:
        raise ValueError("handoff policy requires at least one Acceptance criteria bullet")
    if len(acceptance_items) > budget.maximum_work_units + 1:
        raise ValueError(
            f"handoff has {len(acceptance_items)} acceptance criteria but this budget allows "
            f"at most {budget.maximum_work_units + 1}; split the task"
        )
    too_long = [item for item in (*scope_items, *work_items, *acceptance_items) if len(item) > 240]
    if too_long:
        raise ValueError("handoff policy list items must be at most 240 characters")
    return budget


def policy_instruction(budget: HandoffBudget) -> str:
    """Return a concise prompt fragment describing the enforced budget."""

    preference = (
        " For this local executor, default to one or two tightly scoped work units; "
        "use three or four only when they are inseparable and share one narrow "
        "verification path. The maximum is a ceiling, not a target."
        if budget.runtime == "mini-swe-agent" and budget.maximum_work_units > 2
        else " The maximum is a ceiling, not a target."
    )
    return (
        f"The selected executor has a hard limit of {budget.limit} {budget.limit_name}. "
        f"Reserve at least {budget.verification_reserve} for verification and recovery. "
        f"The next task may contain at most {budget.maximum_work_units} independently "
        "testable work units. Include a `## Work units` checklist with exactly one "
        "unchecked item per `## In scope` bullet; split larger work into later task IDs."
        + preference
    )


def policy_catalog_instruction(configuration: ExecutorConfiguration) -> str:
    """Describe every selectable executor budget to the primary model."""

    return "\n".join(
        policy_instruction(handoff_budget(configuration, selected))
        for selected in ("mini-swe-agent", "claude")
    )
