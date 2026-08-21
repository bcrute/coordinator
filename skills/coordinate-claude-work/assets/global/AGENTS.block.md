<!-- coordinate-claude-work-global:start -->
## Coordinated implementation default

When a repository contains `.coordination/README.md`, use the
`$coordinate-claude-work` workflow: Codex/ChatGPT owns the overall goal, assigns
and reviews one executor-sized subgoal at a time, and the configured executor edits
product code. Every implementation handoff receives a Codex review. Only Codex may set the
repository goal to `done` and write the final completion summary.

When the user asks to start a new coordinated project, initialize the
workflow rather than asking them to restate this role split.
<!-- coordinate-claude-work-global:end -->
