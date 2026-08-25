# Validation-model reporting contract

The validation model is an observer and normal Coordinator user. When it finds
a problem, it should record observable facts instead of attempting to repair
Coordinator's private state.

## Where to report

Write exactly one current report to:

```text
.coordinator-validation/report.json
```

Update that report in place as the cycle progresses. Do not create a stream of
nearly identical reports. When the cycle ends, set `finished_at`, choose one
terminal `outcome`, and make the operator-facing final message match `summary`.

## Finding categories

- `coordinator`: routing, persistence, watcher, session, terminal, dashboard,
  permissions, lifecycle, or coordination-state behavior.
- `provider`: provider CLI/API/authentication/network behavior outside
  Coordinator's control.
- `executor`: the configured model ignored or failed its bounded assignment.
- `solitaire`: a defect in the generated test application.
- `environment`: missing tools, ports, filesystem state, or machine setup.
- `uncertain`: evidence is insufficient to assign an owner.

Do not call every model or network failure a Coordinator bug. Coordinator is at
fault when it launches the wrong configured runtime, misrepresents the failure,
leaves stale state, cannot recover as documented, or loses a reportable error.

## Useful evidence

Each finding should include a concise observation, expected behavior, exact
reproduction steps, and the smallest useful evidence. Prefer stable identifiers,
timestamps, status codes, command exit codes, visible UI labels, and relative
repository paths. Never include secrets, auth tokens, full environment dumps,
or needlessly large logs.

Use severities consistently:

- `critical`: destructive behavior, secret exposure, or an unsafe boundary
  failure.
- `high`: the end-to-end pipeline cannot continue or reports false completion.
- `medium`: important behavior is wrong but a documented recovery remains.
- `low`: localized presentation, diagnostics, or usability defect.

Set `blocks_cycle` independently of severity. A provider outage can block the
cycle without being a Coordinator defect; a low-severity display bug may not
block the Solitaire build but still deserves a finding.

## Terminal outcomes

- `passed`: every pass-gate item that the model can observe is satisfied.
- `failed`: the run reached a reproducible product or Coordinator failure.
- `blocked`: progress cannot continue because of provider, environment, owner
  input, or a failure whose ownership is not yet established.
- `running`: temporary state only; it is not valid for archiving or restart.
