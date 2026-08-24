# ADR 0005: MCP boundary for supervisor-to-local-model delegation

- Status: accepted
- Date: 2026-08-24

## Context

Coordinator can use Claude Code or mini-swe-agent as the implementation executor. The
owner also wants a layered path in which Codex owns the overall goal and review,
Claude supervises a coding turn, and Claude delegates routine bounded implementation
to a local Qwen model instead of spending a native frontier-model subagent on it.

A plain shell command would start the worker, but would not give Claude a stable typed
contract, auditable routing evidence, or a provider-neutral integration seam. Several
young third-party MCP delegators already demonstrate the useful shape. `cc-delegate`
is the closest match, with asynchronous tasks, worktrees, grading, persistence, and a
worker hierarchy. Adopting it would also install a second orchestration state model,
worker runtime, polling protocol, and status UI beside Coordinator.

## Decision

Coordinator supplies a local stdio MCP server using the official MCP Python SDK. The
Claude runner injects it through invocation-scoped `--mcp-config`; it does not alter a
project `.mcp.json` or the user's global Claude configuration. The first tool is
`delegate_implementation`.

The tool uses Coordinator's existing mini-swe-agent/OpenAI-compatible settings. Each
call requires:

- one bounded objective;
- narrow repository-relative allowed path patterns;
- one or more validation commands represented as argument arrays, never shell text;
- a routing score from 8 through 10; and
- a concise routing rationale.

The score is 0–2 in each of five dimensions: specification completeness, edit
locality, deterministic verification, reversibility, and low blast radius. A hard gate
retains architecture/product decisions, ambiguous requirements, authentication or
security-boundary work, data migrations, destructive/external actions, and work with
no deterministic review path in Claude. A score of 7 is decomposed before routing;
0–6 remains with Claude. Very small tasks stay in Claude because delegation overhead
dominates, and work expected to be large is decomposed first.

Coordinator creates a detached temporary Git worktree at the current `HEAD`, runs one
bounded mini-swe-agent process, records trajectory-derived steps and token usage,
checks every changed path, runs the supplied validation argument arrays without a
shell, writes a binary-safe patch and compact JSON record, and removes the worktree.
The MCP result points Claude to the saved patch. The tool never applies changes to the
supervisor's working tree; Claude reviews, integrates, and independently verifies the
result. The worker receives a minimal runtime environment plus only the endpoint key
named in Settings; unrelated Claude, Codex, OIDC, and provider credentials are not
inherited by the child process.

Recent delegation records are displayed on the Agents page with their model, routing
score/rationale, state, timer, steps, tokens, and changed-file count. Prompts, full
logs, and patch contents stay out of the dashboard response.

## Consequences

- MCP is the typed provider-neutral boundary; mini-swe-agent remains the replaceable
  worker adapter and Qwen remains a configured inference endpoint.
- Claude retains native subagents and teams for reasoning-heavy work. Coordinator does
  not attempt to reproduce their scheduler.
- Worktree isolation prevents the local worker from racing Claude's live working tree.
  It is not an operating-system sandbox, so the worker still runs as the Coordinator
  service account and must receive bounded, non-sensitive tasks.
- Synchronous tool execution avoids model-token polling. Claude Code can wait on the
  stdio call while the dashboard observes the same file-backed state.
- Routing outcomes can be compared with failures and review corrections later; the
  initial 8/10 threshold can be changed only with evidence, not ad hoc per turn.

## Alternatives considered

- **`cc-delegate`:** closest feature match, but currently young and duplicates
  Coordinator's orchestration, persistence, worker hierarchy, and status surface.
- **Codex Agent Delegator:** provides worktree-isolated MCP delegation to several CLI
  agents, but would add another run store and custom-executable trust boundary rather
  than reuse the validated mini-swe/Qwen adapter.
- **Direct shell invocation from Claude:** simpler startup, but lacks a typed tool
  schema, routing record, normalized telemetry, and a stable adapter boundary.
- **Per-completion model routing:** too fine-grained; a bounded implementation task and
  reviewable patch are the useful unit of work here.

## References

- [Claude Code MCP documentation](https://code.claude.com/docs/en/mcp)
- [Official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [cc-delegate](https://github.com/EtienneLescot/cc-delegate)
- [Codex Agent Delegator](https://github.com/swjturay/codex-agy-delegator)
