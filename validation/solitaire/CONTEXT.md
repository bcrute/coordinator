# Stable Solitaire test context

## Goal

Use Coordinator to build a small, polished, self-hosted browser version of
single-player Klondike Solitaire. The scope should be large enough to exercise
planning, implementation, testing, review, and correction, but small enough to
repeat from a clean repository whenever Coordinator itself is fixed.

## Product boundary

The finished test product should:

- deal a legal Klondike game and enforce the chosen draw rules;
- support stock, waste, tableau, foundation, move, flip, restart, and win flows;
- provide a usable keyboard/mouse browser interface with responsive layout;
- run locally from one documented command;
- include focused model tests and one inexpensive smoke test; and
- avoid accounts, databases, multiplayer, analytics, payments, and external
  runtime services.

Implementation language and structure are not part of the acceptance test.
The pipeline may choose them, but it must document how to run and verify the
result.

## What this experiment measures

The important outcome is not merely a working card game. A passing cycle shows
that a new user can state the goal once and Coordinator can:

1. preserve it in repository-owned state;
2. produce bounded, correctly routed work;
3. launch only the configured model and permissions;
4. surface agent, background-terminal, usage, and progress state accurately;
5. survive ordinary model/tool failures without stale or contradictory state;
6. review real evidence and request corrections when needed; and
7. reach one unambiguous terminal result without manual file edits.

The continuous-context model may fix Coordinator between cycles. It must not
quietly repair the Solitaire product or coordination files to make a cycle pass.
