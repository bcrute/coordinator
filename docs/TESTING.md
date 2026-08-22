# Testing strategy

Coordinator treats coverage as a map for review, not a product objective. A test is
required when it protects an externally observable behavior, security boundary,
durable-state transition, process lifecycle, compatibility contract, or important
failure/recovery path. Executing a line is not sufficient evidence by itself.

## Required evidence matrix

| Risk boundary | Required evidence | Current automated coverage |
|---|---|---|
| OIDC authorization and sessions | Default deny, PKCE/state, identity allow policy, CSRF, expiry, revocation, logout, migration, and secure cookies | `tests.test_authenticated_web_app` |
| Repository scope | Direct-child discovery, exact catalog selection, uninitialized setup, lease-safe switching, and shutdown | `tests.test_web_repository_switching` |
| Browser terminal | Fixed command, bounded protocol, ownership, UTF-8, resize, reconnect, copy, and process-group cleanup | `tests.test_codex_session`, `tests.test_web_terminal_contract`, Chromium journey |
| Watcher lifecycle | Lock contention, start/stop, process-group cleanup, handoff signals, and failure stop | `tests.test_workflow` |
| Durable operational state | Idempotent indexing, migrations, interruption recovery, guardrails, archive/reopen, backup verification | `tests.test_operational_store` |
| Executor adapters | Default Claude compatibility, trusted selection, bounded command construction, and diagnostics | `tests.test_executor_adapters` |
| mini-swe-agent handoff | Timeout and signal cleanup, no orphan, replay/lock refusal, planner ownership, malformed/nonzero outcomes, secret non-persistence, normalized telemetry | `tests.test_executor_adapters` |
| Provider allowance display | Provider parsing/cache failures, remaining-window rendering, pace projection, and manual refresh | `tests.test_provider_usage`, Chromium journey |
| Project initialization and CI | Existing-file preservation, managed-block idempotence, GitHub remote sanitization, CI discovery/add/skip, unsafe-path refusal, validator failures, and browser confirmation | `tests.test_coordinator_cli`, `tests.test_github_ci`, `tests.test_authenticated_web_app`, Chromium journey |
| Planner and team runners | Handoff identity/state gates, complete/next/correct/block transition validity, planner/review ownership, incomplete and nonzero exits, duplicate locks, native-team TTY requirements, environment, and cleanup | `tests.test_workflow`, `tests.test_workflow_runners` |
| Data maintenance commands | Verified backup/restore, live and backup verification, repository rebuild, retention pruning, and failure reporting | `tests.test_maintenance_cli`, `tests.test_operational_store` |
| Complete browser journey | Setup, workspace/run discovery, preferences, guardrails, terminal copy, usage projection, administration views, and basic accessibility | `tests.test_web_e2e` in CI |
| Public distribution | Package contents, locked Python 3.14 environment, public test set, docs, CI, release metadata, and secret exclusions | `tests.test_distribution` |

New behavior must extend this matrix when it introduces a new boundary. Tests should
assert public responses, persisted records, process state, or rendered browser
behavior. Source-text assertions are appropriate only for static distribution or
markup-schema contracts; they do not substitute for runtime behavior.

`tests.test_test_ownership` maps every top-level production module to at least one
behavioral suite. Adding a module requires an explicit test owner; the ownership test
cannot own another module itself. This is an inventory guard, not evidence that the
named suite is sufficient—the risk matrix and review of public/failure behavior remain
the standard.

## Coverage workflow

The main run measures statement and branch coverage without changing child-process
topology. A second, narrow run traces the subprocess-heavy executor contracts. Their
data is merged for the final report:

```bash
uv run coverage erase
uv run coverage run -m unittest discover -s tests -q
mv .coverage .coverage.union.full

COVERAGE_FILE=.coverage.executor uv run coverage run \
  --rcfile=.coveragerc-subprocess -m unittest -v tests.test_executor_adapters
COVERAGE_FILE=.coverage.executor uv run coverage combine \
  --rcfile=.coveragerc-subprocess

cp .coverage.executor .coverage.union.executor
COVERAGE_FILE=.coverage.union uv run coverage combine --keep
COVERAGE_FILE=.coverage.union uv run coverage report
```

Blanket subprocess tracing is deliberately avoided: instrumentation wraps process
launches and can alter the topology that lifecycle tests are intended to verify. The
focused run is safe because its process assertions were designed and verified under
that instrumentation.

The configured floor is a regression tripwire for the branch-aware aggregate, not a
target. Raise it only after adding useful tests. Do not add tests for one-line entry
points, unreachable defensive branches, third-party vendored code, or private call
order solely to increase the percentage.

## External acceptance evidence

CI intentionally does not contact a real identity provider, model API, Codex, Claude,
or mini-swe-agent deployment. Sanitized fake runtimes and protocol fixtures cover the
application boundary deterministically. A deployment using Authentik or a local model
still needs a separate operator smoke test for its issuer, claims, endpoint, model,
and credentials; that evidence is environment-specific and is not counted as unit
coverage.
