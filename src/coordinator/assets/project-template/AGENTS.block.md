<!-- coordinate-claude-work:start -->
## Primary planner/reviewer workflow

When `.coordination/README.md` exists, read it and the live files it names before
acting. The configured primary model is the planner/reviewer; the configured executor is the
product-code writer.

- The primary owns `.coordination/planner/` and `.coordination/reviews/`.
- The configured executor owns product edits. Its adapter owns
  `.coordination/coder/` when the runtime cannot safely do so itself.
- The primary owns the overall goal, assigns one bounded executor subgoal at a time, and
  sets `planner/goal.md` to `done` only after its completion criteria are met.
- In an app-managed workflow, the primary signals a handoff only by writing the bounded
  task and then waiting. The watcher exclusively launches executors; the primary must
  not invoke `coordinator run-turn`, an adapter runner, or an executor CLI directly.
- After each implementation handoff, the primary reviews the complete diff and evidence,
  then requests corrections, assigns the next subgoal, or records the overall
  completion in `reviews/completion.md`.
- The primary does not edit product code unless the user explicitly suspends this role
  boundary.
- No agent commits, pushes, deploys, or changes external systems unless the active
  assignment explicitly authorizes it.
<!-- coordinate-claude-work:end -->
