# Changelog

All notable changes are recorded here. Coordinator follows Semantic Versioning while
its public interfaces mature; pre-1.0 minor releases may intentionally revise an
interface with migration notes.

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
