<!-- coordinate-claude-work:start -->
## Codex planner/reviewer workflow

When `.coordination/README.md` exists, read it and the live files it names before
acting. Codex/ChatGPT is the planner and reviewer; Claude Code is the product-code
writer.

- Codex owns `.coordination/planner/` and `.coordination/reviews/`.
- Claude owns product edits and `.coordination/coder/`.
- Codex owns the overall goal, assigns one bounded Claude subgoal at a time, and
  sets `planner/goal.md` to `done` only after its completion criteria are met.
- After each Claude implementation handoff, Codex reviews the complete diff and
  evidence, then requests corrections, assigns the next subgoal, or records the
  overall completion in `reviews/completion.md`.
- Codex does not edit product code unless the user explicitly suspends this role
  boundary.
- Neither agent commits, pushes, deploys, or changes external systems unless the
  active assignment explicitly authorizes it.
<!-- coordinate-claude-work:end -->
