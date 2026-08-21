# ADR 0001: authenticated ASGI runtime

- Status: accepted
- Date: 2026-08-20

## Context

The existing dashboard is intentionally a standard-library `http.server`
application bound to loopback. It is useful locally, but it is not an acceptable
network authentication boundary. The browser terminal and watcher controls make
every authenticated request security-sensitive.

The first network-capable runtime needs generic OIDC support, server-side opaque
sessions, CSRF protection, default-deny route enforcement, and a production-capable
HTTP server. The coordination goal/task documents themselves remain ordinary files.

## Decision

Add an authenticated ASGI path using:

- Starlette as the small ASGI application framework;
- Authlib as the maintained OAuth 2.0/OIDC and JOSE implementation;
- Uvicorn as the ASGI server;
- SQLite, through Python's standard library, for opaque sessions and redacted audit
  events.

The SQLite file lives in a configured state directory outside the repository. The
directory and database are created with owner-only permissions. No OAuth token is
returned to JavaScript or deliberately retained after the callback. The database is
not used for coordination documents, project source, task state, or relay logs.

The original `http.server` path initially remained available for loopback
development. ADR 0002 subsequently consolidated both modes on the ASGI runtime.
A routable bind is refused unless OIDC mode is active.

## Why SQLite is justified

Sessions and revocation need to survive a service restart, and audit events need a
transactional append-only record. SQLite provides both without operating a separate
database service. It matches the intended single-host deployment and can later be
replaced behind a narrow store interface if multi-host operation becomes a real goal.

## Consequences

- A fresh clone now has Python dependencies and an installation step.
- Authenticated deployments use one application process initially. SQLite supports
  concurrent request threads, but multi-host deployment is explicitly out of scope.
- The OIDC issuer, client identifier, client secret environment-variable name,
  external URL, and allow policy remain deployment settings rather than source code.
- TLS still terminates at a reverse proxy. The application binds loopback/private
  transport and trusts forwarded headers only from explicitly configured peers.
