# ADR 0004: Executor adapters and mini-swe-agent

- Status: accepted
- Date: 2026-08-21

## Context

Coordinator originally encoded Claude Code as the only implementation runtime. That
coupled task relaying, process launch, telemetry, UI wording, and provider-specific
features. It also meant that connecting an OpenAI-compatible local model would require
Coordinator to implement another shell/edit/test agent loop.

mini-swe-agent already supplies that loop and supports local models through LiteLLM and
OpenAI-compatible endpoints. It exposes a noninteractive task mode, bounded agent
settings, and a JSON trajectory. It is therefore a runtime adapter, not an inference
model and not Coordinator's workflow authority.

## Decision

Separate execution into three layers:

1. **Coordinator workflow:** owns the overall goal, bounded assignments, process
   supervision, durable state, Codex review, budgets, and the final done signal.
2. **Executor adapter/runtime:** translates one assignment into a provider-native
   process and normalizes observable results. Built-in adapters initially include
   `claude` and `mini-swe-agent`.
3. **Inference provider:** generates model output. For mini-swe-agent this may be a
   local or remote LiteLLM-supported, OpenAI-compatible endpoint.

Claude remains the default. Selecting mini-swe-agent is an explicit trusted server
configuration choice. Coordinator passes the task to `mini --task` in unattended
`--yolo --exit-immediately` mode, imposes step and wall-time limits, captures a
per-task trajectory, and normalizes token telemetry into
`.coordination/runtime/executor-progress.json`.

The mini-swe-agent adapter, rather than the model, writes `coder/status.md` and
`coder/latest-report.md`. The model is instructed not to touch `.coordination/`.
Coordinator detects modifications to its planner/review files and reports the handoff
as blocked. Codex still reviews the actual repository diff before accepting work.

Endpoint credentials are inherited from a named environment variable. Secret values
are never accepted in TOML or placed in process arguments. mini-swe-agent is installed
and upgraded separately and is not a required Coordinator dependency.

## Consequences

- A local model can perform bounded implementation turns without Coordinator
  reimplementing an agent tool loop.
- The same watcher and Codex review state machine works with either built-in executor.
- Provider-specific features remain capability-specific: mini-swe-agent currently
  reports no nested workers or subscription quota.
- The initial adapter registry is built in. A stable third-party plugin SDK and remote
  agent-service protocol remain future work.
- `coder/` and several historical API phase names remain compatibility terms even when
  the selected executor is not Claude; new telemetry uses provider-neutral naming.

## References

- [mini-swe-agent CLI](https://mini-swe-agent.com/latest/usage/mini/)
- [mini-swe-agent local-model guide](https://github.com/SWE-agent/mini-swe-agent/blob/main/docs/models/local_models.md)
