<!-- coordinate-claude-work-global:start -->
## Coordinated implementation default

When a repository contains `.coordination/README.md`, use the
`$coordinate-claude-work` workflow: Codex/ChatGPT owns the overall goal, assigns
and reviews one Claude-sized subgoal at a time, and Claude Code edits product
code. Every Claude handoff receives a Codex review. Only Codex may set the
repository goal to `done` and write the final completion summary.

When the user asks to start a new Codex–Claude coordinated project, initialize the
workflow rather than asking them to restate this role split.
<!-- coordinate-claude-work-global:end -->
