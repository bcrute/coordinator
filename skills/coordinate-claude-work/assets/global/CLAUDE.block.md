<!-- coordinate-claude-work-global:start -->
## Coordinated implementation default

When a repository contains `.coordination/README.md`, follow its project-level
`CLAUDE.md` instructions and act as the implementation agent. Treat the active
task packet supplied by the runner as the scope for the current handoff; do not
preload coordination history. Use Claude Code's native orchestration and signal
`review` or `blocked` in coder status; never set the overall goal to `done`.
<!-- coordinate-claude-work-global:end -->
