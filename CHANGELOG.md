# Changelog

All notable changes are recorded here. Coordinator follows Semantic Versioning while
its public interfaces mature; pre-1.0 minor releases may intentionally revise an
interface with migration notes.

## Unreleased

### Added

- A three-stage role-assignment UI for Codex review, Claude supervision, and direct or
  delegated implementation, including editable presets, persisted reviewer model
  selection, runtime readiness checks, and local endpoint discovery.
- Controlled model selectors populated from Codex's authenticated app-server picker,
  Claude Code's installed alias list, and each configured OpenAI-compatible endpoint.
- Independent effort selectors for Codex review, Claude supervision, Claude's native
  implementation subagent, and compatible LiteLLM-backed executors, carried through
  the watcher into each provider's real invocation.
- Claude-supervised local implementation through an invocation-scoped stdio MCP
  server, with an enforced 8–10 routing threshold, isolated Git worktrees, explicit
  path/test contracts, compact patches, and dashboard-visible routing evidence.

- Built-in executor adapter seam with an optional, bounded mini-swe-agent runtime for
  local or API-backed implementation models.
- Persisted Settings-page executor selection, bounded OpenAI-compatible model
  discovery, and runtime watcher rebinding without storing endpoint secrets.
- Provider-neutral executor telemetry, per-turn mini-swe-agent trajectories, and
  adapter diagnostics while retaining Claude as the default executor.
- Risk-based testing guidance, branch/subprocess coverage reporting, and executor
  lifecycle, ownership, validation, replay, failure, and secret-handling contracts.
- A live Terminal-page activity panel for agents, reported models, and background
  terminals, scoped to the exact process tree of each managed terminal session.
- Safe GitHub Actions setup during project initialization, including existing-workflow
  discovery, explicit coexist/skip choices, and a packaged coordination validator.
- Application-wide behavioral test ownership and risk coverage for setup, runners,
  maintenance, provider usage, process discovery, web controls, and failure recovery.
- A Firefox-specific browser regression for automatic terminal socket reconnection,
  alongside the complete Chromium journey.
- A provider-neutral historical Usage screen with dynamic provider tabs, native Codex
  and Claude session importers, API-equivalent cost estimates, pricing coverage, raw
  token fallback for local/custom adapters, and an owner-only incremental SQLite index.

### Changed

- Provider usage projections now use a reset-aware, six-hour weighted velocity from
  persisted hourly observations, show the current percentage-point burn rate, compare
  it with the sustainable pace, and retain the reset-average estimate only as a
  clearly labeled startup fallback. Zero or negative projected allowance means the
  current pace exhausts it before reset.
- Codex GPT-5.6 API-equivalent values now use OpenAI's published Standard short- and
  long-context rates, and a pricing revision revalues previously imported history.
- Historical usage records preserve five-minute, one-hour, and unclassified cache
  writes separately while retaining the combined cache-write API total.

### Fixed

- Claude reset timestamps are canonicalized before velocity-history grouping, so
  harmless sub-second API jitter no longer strands every sample in a separate reset
  cycle; existing observations are migrated automatically.
- Historical watcher status files can no longer claim a watcher is currently running
  when the corresponding watcher lock is absent; stale records remain available to
  diagnostics but are omitted from the dashboard.
- Web-server shutdown now has a bounded graceful deadline so open event streams cannot
  indefinitely stall a service restart.
- Transient provider-limit refresh failures now retain the last successful header
  values with a stale warning instead of replacing them with unavailable placeholders.
- Clearing the browser terminal now discards its retained server replay buffer, so
  cleared output does not return after a page refresh while the Codex process continues.
- Large terminal replays are emitted as bounded, cursor-contiguous WebSocket frames
  with explicit event-loop yields, keeping health and control requests responsive.
- Corrupt operational databases now produce a stable validation error instead of
  leaking a raw SQLite exception.
- Claude handoffs now fail closed when the executor changes Coordinator-owned planner
  or review files.

## [0.3.0] - 2026-08-21

### Added

- Installable `coordinator` CLI with serve, doctor, init, and data-maintenance commands.
- Durable run, event, policy, recovery, archive, evidence, and preference indexing.
- Workspace, run-history, timeline, settings, diagnostics, and terminal web views.
- Versioned `/api/v1`, resumable SSE events, `terminal.v1`, OpenAPI, readiness, metrics,
  structured request correlation, and shared state reconstruction.
- Generic OIDC authorization, owner/group policy, opaque sessions, CSRF controls,
  configurable rate limits, RP logout, and signed back-channel logout.
- Locked dependency graph and tag-driven build, SBOM, checksum, provenance, and release
  automation.
- Compact Codex and Claude remaining-usage indicators backed by an hourly shared cache,
  provider reset details, and a manual refresh control that never creates model turns.

### Changed

- Starlette/Uvicorn is the sole web runtime; terminal traffic uses a WebSocket and live
  dashboard state uses Server-Sent Events.
- Session persistence no longer recreates records revoked during an in-flight request.
- Python 3.14 is the sole supported interpreter; package metadata, the lock, CI, and
  release builds reject or omit older runtimes.

### Migration

- Existing operational indexes migrate automatically through schema version 4.
- Existing security indexes migrate automatically through schema version 2.
- Keep a verified state-directory backup before upgrading and use `coordinator data
  verify` after migration.

## [0.2.0] - 2026-08-20

- Initial installable, local-first coordination dashboard baseline.

[0.3.0]: https://github.com/bcrute/coordinator/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/bcrute/coordinator/releases/tag/v0.2.0
