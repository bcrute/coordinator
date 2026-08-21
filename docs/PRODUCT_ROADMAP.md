# Coordinator professional-product execution roadmap

## Purpose

This document turns the current dashboard into a dependable single-owner application
for planning, running, observing, and reviewing coordinated coding work. It is an
execution plan, not a wishlist: each phase has an observable result and verification
gate, and later phases build on the contracts established earlier.

The target remains a self-hosted application operated by its owner. Multi-tenancy,
billing, hosted execution, and reimplementation of model-provider orchestration are
not product goals.

## Product model

The application will present one consistent hierarchy:

```text
Workspace -> Repository -> Goal -> Run -> Turn -> Objective -> Agent -> Artifact
```

- `.coordination/` remains the portable authority for goal, assignment, handoff, and
  review state.
- SQLite stores security state and a rebuildable operational index: run/event history,
  resource budgets, notification state, and ordinary application preferences.
- Codex and Claude remain opaque local CLI providers. Coordinator supervises their
  lifecycle and records their exposed events; it does not replace their native agent,
  team, context, or scheduling behavior.
- A browser terminal is a separately configurable high-risk capability, not a
  prerequisite for read-only monitoring.

## Delivery rules

- Work lands as coherent, independently tested commits on a feature branch and is
  pushed after each passing milestone.
- Schema and protocol changes require a migration or compatibility decision.
- A check not run is reported as not run. Passing narrow tests cannot prove a broad
  milestone.
- The public roadmap is updated as a phase begins and completes.
- Secrets, private infrastructure identifiers, local coordination state, and
  proprietary fixtures never enter a commit.

## Phase 0 - Realtime authenticated baseline

**Status: complete.**

The local and OIDC modes share Starlette/Uvicorn, dashboard state uses Server-Sent
Events, the browser terminal uses a WebSocket, setup/activity/session/diagnostic views
exist, and Chromium exercises the principal local workflow.

Exit evidence:

- Full unit suite, Python compilation, JavaScript syntax, dependency audit, and
  opt-in Chromium workflow pass.
- The coherent baseline is pushed before architectural restructuring begins.

## Phase 1 - Installable and maintainable application core

**Status: complete.**

Progress:

- The installable `coordinator` package, console command, wheel resources, and
  compatibility launchers are complete.
- Starlette/Uvicorn is now the only request implementation; the legacy stdlib server
  and terminal HTTP polling/input routes are removed.
- Security settings, server-side sessions, authorization policy, and security
  middleware now have an independent application module with compatibility exports.
- Configuration, repository discovery, watcher supervision, workflow-state parsing,
  terminal management, and HTTP routing now have bounded package modules.

Exit evidence:

- An isolated wheel installation on Python 3.14 runs `coordinator --help`, reports a
  passing `coordinator doctor --json`, and initializes every packaged coordination
  template in a new Git repository.
- The 388-test unit suite, Python/JavaScript syntax checks, and Chromium journey pass
  with Starlette/Uvicorn as the only production request runtime.

Deliverables:

1. Add a conventional `pyproject.toml`, `src/coordinator/` package, version metadata,
   and installed `coordinator` console command.
2. Provide `coordinator serve`, `coordinator doctor`, and `coordinator init` commands.
3. Move application responsibilities into bounded modules for configuration,
   repositories, security, terminal processes, workflow state, and HTTP routes.
4. Keep the Codex skill as instructions and thin launch adapters that import the
   application package.
5. Remove the unused legacy `http.server` runtime and the browser-obsolete terminal
   polling/input endpoints after tests and documentation use the ASGI contracts.
6. Preserve a documented compatibility launcher for existing clone-based users.

Exit gate:

- A clean environment can install the project and run `coordinator --help`,
  `coordinator doctor`, and `coordinator serve`.
- No production request path is implemented twice.
- Existing local/OIDC behavior and the Chromium workflow remain green.

## Phase 2 - Durable runs, recovery, and resource guardrails

**Status: in progress.**

Deliverables:

1. Introduce stable repository, goal, run, turn, objective, agent, artifact, and event
   identifiers without changing the `.coordination/` file contract.
2. Add explicit SQLite migrations and a rebuildable operational index derived from
   coordination files and runtime events.
3. Record immutable state transitions and enough process identity to diagnose and
   recover interrupted runs and stale locks after restart.
4. Add configurable per-turn and overall wall-clock, generated-token, input-token,
   cache-read, cache-write, correction-round, and concurrent-worker limits.
5. Stop safely on a hard limit, repeated identical failure, or no-progress timeout;
   persist the cause and require an explicit resume.
6. Add online backup, restore verification, index rebuild, and retention commands.

Exit gate:

- Restart tests prove active/interrupted/done runs recover truthfully.
- Migration tests cover every supported schema version and refuse unknown versions.
- Guardrail tests prove warning, stop, and explicit-resume behavior without launching
  a provider CLI.
- Deleting the operational index and rebuilding it does not lose authoritative
  coordination state.

## Phase 3 - Daily-driver workspace and run experience

**Status: planned.**

Deliverables:

1. Replace the single-repository landing view with a workspace home showing every
   repository's current goal, run state, review result, timers, usage, and required
   owner action.
2. Add run detail and history views with a chronological assignment, handoff, review,
   test, diff, agent, limit, and completion timeline.
3. Check off objectives in place and show a stable lead/subagent hierarchy rather than
   replacing the task display on every event.
4. Add search/filtering, log virtualization, structured test evidence, Git diff review,
   completion export, archive/reopen, and browser notifications.
5. Add first-run onboarding and settings for repository roots, provider availability,
   models, budgets, validation commands, notifications, OIDC mode, and terminal
   capability. Secret values stay outside browser-managed settings.
6. Add terminal reconnect/replay, explicit attachment ownership, keyboard shortcuts,
   responsive layouts, and automated WCAG 2.2-oriented checks.

Exit gate:

- A new user can install, configure, initialize a repository, start a bounded run,
  observe it, inspect evidence, and recover after a browser/server restart without
  editing TOML by hand except for secret delivery.
- Chromium tests cover the complete journey and the major failure/recovery paths.

## Phase 4 - Versioned interfaces and operations

**Status: planned.**

Deliverables:

1. Introduce typed, versioned `/api/v1` request/response and event schemas with one
   documented error envelope and a published OpenAPI document.
2. Give SSE events monotonically increasing IDs, replay through `Last-Event-ID`,
   bounded retention, heartbeats, and backpressure behavior.
3. Version the terminal WebSocket protocol with sequence numbers, replay cursors,
   bounded buffers, explicit attach/detach, and flow-control behavior.
4. Replace per-client state reconstruction with one lifecycle-managed filesystem/event
   broadcaster.
5. Add structured JSON application logs, request/repository/run/turn correlation IDs,
   `/healthz`, `/readyz`, metrics, and optional OpenTelemetry traces and metrics.
6. Surface disk, database, CLI, state-directory, lock, event-lag, and provider
   readiness in Diagnostics.

Exit gate:

- Contract tests validate the published schema against every HTTP route and event.
- Load/reconnect tests demonstrate bounded memory and correct event/terminal replay.
- Liveness remains cheap; readiness fails precisely when a required dependency cannot
  support work.

## Phase 5 - Network and release readiness

**Status: planned.**

Deliverables:

1. Complete the live Authentik negative-token matrix, claim mapping, RP-initiated
   logout, and signed back-channel logout handling.
2. Add rate limits and abuse controls for login, terminal, watcher, repository, and
   session-administration endpoints.
3. Run the service as a dedicated identity behind TLS and a proxy-only upstream, with
   reviewed secret injection, filesystem scope, process limits, and service hardening.
4. Rehearse session revocation, secret rotation, backup, restore, rollback, dependency
   update, and incident-stop procedures.
5. Add release versioning, changelog, locked dependency artifacts, SBOM, provenance
   attestations for consumable artifacts, and documented upgrade support.
6. Complete accessibility, adversarial, documentation, clean-install, and public-tree
   scans before the first non-experimental release candidate.

Exit gate:

- Every item in `docs/SECURITY_ROADMAP.md`'s network-ready checklist has dated evidence.
- A clean clone follows the documented install/upgrade/rollback path successfully.
- Release artifacts are reproducible enough to identify their source commit and
  dependency inventory.

## Completion definition

This roadmap is complete only when every phase exit gate is evidenced, the full test
and browser suites pass from a clean installation, the network-ready checklist is
closed or deliberately descoped by the owner, and the application can complete and
recover a real bounded coordination run without hidden manual state repair.
