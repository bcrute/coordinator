# Release-readiness evidence

## 2026-08-21 — `0.3.0` feature checkpoint

This record separates evidence produced from the committed application tree from
acceptance work that can only be performed on the eventual Authentik/TLS host. It is
not a release announcement and no version tag was created.

### Automated and clean-tree evidence

- The committed public suite contains 323 tests. A clean `git archive` export was
  installed with `uv sync --locked --extra dev`, passed that suite, built the wheel
  and source distribution, and reported `coordinator 0.3.0` on Python 3.14.7.
- The development workspace additionally ran 100 ignored local corpus tests. The
  combined 423-test suite passed with one intentional opt-in browser skip.
- The focused SQLite/security/web suites passed on Python 3.14 with
  `ResourceWarning` promoted to an error after connection ownership was made
  explicit.
- Python byte compilation and the Node syntax check for the dashboard client passed.
- The dependency audit reported no known vulnerabilities. The local project itself
  is reported as unauditable because it is not a third-party PyPI dependency.
- Python 3.14 is the sole supported runtime. CI exercises the application and a
  separate Chromium journey on 3.14; compatibility with older interpreters is not a
  product goal.

### Public-tree review

- No private-key header, common provider-token shape, AWS access-key shape, tracked
  `.env`, local `workflow.toml`, SQLite database, PEM key, or SSH private-key filename
  was found in the committed tree.
- Private IPv4 literals occur only in tests that prove routable binds and trusted CIDR
  parsing. Product defaults and examples use loopback or documented placeholders.
- The apparent token-pattern hit in the dashboard HTML was the ordinary identifier
  `task-out-of-scope-heading`, not a credential.
- The source distribution manifest excludes local coordination state, deployment
  material, documentation, repository-local settings, build outputs, and ignored
  workspace fixtures. The packaged `.coordination` tree is the intentional new-project
  template.

### Deployment acceptance still required

- Complete the real Authentik success and negative-token/claim-mapping matrix.
- Prove TLS termination and that the application upstream is unreachable except from
  the reverse proxy; verify forwarded and inbound identity-header behavior.
- Install under the dedicated service identity and record the effective systemd,
  filesystem, firewall, and secret-file controls.
- Rehearse secret rotation, immediate session revocation, verified backup/restore,
  rollback, and incident stop on that host.
- Run the tag-triggered release workflow, verify checksums and both attestations, and
  install the resulting wheel before declaring a release candidate.

Until those items have dated evidence, local mode remains loopback-only and OIDC mode
is application-ready but not an accepted network deployment.
