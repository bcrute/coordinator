# ADR 0003: professional application core and durable run model

- Status: accepted
- Date: 2026-08-20

## Context

The dashboard grew inside a distributable Codex skill. Its HTTP routes, security
store, repository lifecycle, PTY management, state parsing, and frontend are now large
enough that the skill-directory layout and duplicated compatibility runtime impede
safe changes. The UI also emphasizes the latest file snapshot, while routine use needs
stable runs, recovery, history, and resource limits.

The coordination files are intentionally understandable and portable across fresh
model sessions. Replacing them with a database would weaken that property. Conversely,
using files as a query index, audit log, preference store, and migration mechanism would
make the application increasingly fragile.

## Decision

- Make Coordinator an installable Python application under `src/coordinator/`, with a
  `coordinator` console command and conventional package metadata.
- Keep `skills/coordinate-claude-work/` as the versioned instructions, templates, and
  compatibility launchers. Launchers import the installed application rather than
  carrying a second request implementation.
- Use Starlette/Uvicorn as the only HTTP runtime. Retire the legacy stdlib HTTP server
  and browser-obsolete terminal HTTP transport after the compatibility transition.
- Preserve `.coordination/` as the authoritative goal/turn/review contract.
- Extend the existing SQLite state with versioned migrations and a rebuildable
  operational index for stable runs/events, guardrail state, notifications, and
  non-secret preferences. Database loss must not rewrite or invalidate authoritative
  coordination documents.
- Treat Codex and Claude as opaque provider adapters. Coordinator may launch, stop,
  observe, budget, and record them, but does not recreate native context management,
  subagent scheduling, or teams.
- Separate read/monitor capabilities from the high-impact terminal capability in
  configuration and authorization policy.
- Version public HTTP, SSE, and WebSocket contracts before adding third-party
  integrations.

## Consequences

- Clone-based launch commands remain temporarily supported while installation becomes
  the primary path.
- Packaging and migration compatibility become release responsibilities.
- Some tests must move from implementation-specific imports to public application
  contracts.
- SQLite becomes more useful but not more authoritative: the operational index can be
  rebuilt from coordination files and retained runtime records.
- Provider-specific features are exposed only when the provider reports them; the UI
  must distinguish unavailable telemetry from zero activity.
