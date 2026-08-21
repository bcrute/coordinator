# ADR 0002: unified real-time dashboard runtime

- Status: accepted
- Date: 2026-08-20

## Context

Maintaining separate `http.server` and ASGI request implementations made local
and OIDC behavior diverge. The terminal sent every input event as a serialized
HTTP request and polled output four times per second, which added visible typing
latency and unnecessary request volume. Browser polling also obscured what a
"refresh" represented.

Repository onboarding, operational checks, the security audit trail, and session
revocation existed only as backend concepts or manual procedures.

## Decision

- Serve local and OIDC modes with the same Starlette/Uvicorn application. Local
  mode retains its enforced loopback-only boundary.
- Stream dashboard snapshots with Server-Sent Events. The server emits changed
  snapshots and heartbeats; the browser no longer polls state once per second.
- Carry terminal input, resize messages, output, and session status over one
  origin- and CSRF-validated WebSocket.
- Keep coordination documents and project state file-backed. Continue using the
  owner-only SQLite database only for opaque browser sessions and audit events.
- Add bounded endpoints and dashboard pages for repository setup, activity,
  session revocation, and runtime diagnostics. Repository creation is restricted
  to validated direct children of the configured repository catalog root.
- Exercise the complete local browser workflow with Playwright Chromium in a
  dedicated CI job.

## Consequences

- Local installs now require the ASGI dependencies and a WebSocket protocol
  implementation; there is no dependency-free server path.
- Reverse proxies must pass WebSocket upgrades and avoid buffering the event
  stream.
- The browser and server use only the WebSocket for interactive terminal I/O; the
  temporary HTTP polling/input compatibility endpoints were removed in Phase 1.
- A technical local browser session provides CSRF protection and audit continuity;
  it does not authenticate the local user.
