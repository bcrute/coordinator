# ADR 0006: Primary adapters and persistent handoff budgets

- Status: accepted
- Date: 2026-08-25

## Context

The original review runner named Codex directly and relied on prompt language
such as “bounded subgoal.” That was not a sufficient process boundary: a primary
could assign an entire product slice to a local executor with a small fixed step
limit. Raising the limit per task makes the owner continually retune a runtime
setting and still does not ensure decomposition. The primary role also needs to
be replaceable independently of the implementation executor.

## Decision

Coordinator persists a `primary_adapter` independently of its implementation
executor. Codex CLI, Claude Code, and mini-swe-agent are primary adapters. The
mini-swe-agent adapter can target Qwen or any compatible local/API model and has
its own model, effort, step, and wall-time settings; it shares only endpoint
transport settings with local execution. Existing Codex command names and the
watcher `codex` role remain compatibility surfaces, not architectural role
restrictions.

Each runnable task must declare one-to-one `In scope` bullets and unchecked
`Work units`, plus bounded acceptance criteria. Coordinator translates the
selected executor's persisted step or turn limit into a structural ceiling:

- reserve 25 percent, with a minimum of two calls, for verification/recovery;
- budget four mini-swe-agent steps per work unit;
- budget six Claude turns per work unit; and
- reject the task after primary review and again immediately before launch when
  its structure exceeds the calculated ceiling.

The primary receives the calculated policy in its review prompt and must create
later task IDs for remaining work. The owner sets runtime limits once; changing a
saved limit automatically changes subsequent ceilings.

## Consequences

Task complexity cannot be inferred perfectly from prose, but compound scope can
no longer be hidden in an unconstrained list. Invalid handoffs fail before model
usage begins and expose a specific split-task error. Existing initialized targets
must adopt the `Work units` section before their next runnable handoff. Additional
primary providers can implement the same review process boundary without changing
the coordination state machine. A model's provider does not determine its
authority: a configured local model may own goals and issue review verdicts just
as a frontier CLI model may.
