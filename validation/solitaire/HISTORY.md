# Cycle and finding history

Only runs started under this protocol count toward the clean passing streak.
Earlier exploratory Solitaire work is useful diagnostic context but is not a
protocol cycle because it did not start with a fresh target and report contract.

## Cycles

| Cycle | Outcome | Terminal stage | Coordinator fix prompted | Clean pass streak |
| --- | --- | --- | --- | --- |
| 1 | Blocked | implementation | Bounded mini-swe role profiles and process-tree supervision (`2b0b4ab`) | 0 |
| 2 | Failed | routing | Persist selected repository across application restarts (`6cbe93a`) | 0 |
| 3 | Failed | routing | Enforce policy at every executor entry point, reserve launches for the watcher, and terminate session-escaping descendants | 0 |
| 4 | Failed | setup | Reclaim the exact dead app-owned watcher lock after confirmed process exit | 0 |
| 5 | Failed | planning | Project exact handoff ceilings and require atomic in-place coordination updates | 0 |
| 6 | Failed | implementation | Cap bounded local-model responses and allow descendant TERM cleanup before escalation | 0 |
| 7 | Passed | completion | Tighten bounded local responses to 3,072 tokens and require a tool call before narration | 1 |

## Findings

| ID | Cycle | Category | Severity | Blocks | Observation | Disposition | Regression evidence | Fresh verification |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| SOL-001 | 1 | executor | medium | yes | Repeated Qwen handoffs exhausted 12 steps or the 900-second limit; some rounds made no edits and the build never completed. | Coordinator now supplies role-specific bounded profiles, retains partial work for review, and supports clean bounded correction rounds; response-efficiency tuning continues separately. | `tests.test_executor_adapters`, `tests.test_process_guard` | Verified as recoverable during cycle 7: two initial rounds exhausted limits, but primary review and bounded retries completed the product |
| SOL-002 | 2 | coordinator | high | yes | Restarting Coordinator discarded the selected Solitaire repository and launched the managed terminal against the configured default repository. | Fixed by persisting only a validated catalog repository path in the owner-only operational store; cycle 2 archived. | `tests.test_web_repository_switching.RepositorySwitchingTests.test_selected_repository_survives_application_restart` | Verified during cycle 3 setup |
| SOL-003 | 3 | routing | high | yes | The watcher rejected an eight-unit task against a four-unit budget, but the primary directly invoked the lower-level mini-swe runner, which accepted it and retried outside watcher control. | Direct mini-swe and Claude runners now enforce the same saved route and runtime-sized handoff policy; generated primary instructions reserve executor launches for the app watcher. | `tests.test_executor_adapters.MiniRunnerTests.test_direct_runner_rejects_oversized_handoff_before_model_launch`, `test_direct_runner_rejects_task_routed_to_another_adapter`, `test_direct_claude_runner_enforces_the_same_handoff_policy`, and `tests.test_workflow.WorkflowTests.test_init_preserves_existing_instructions_and_is_idempotent` | Verified during cycle 6 routing |
| SOL-004 | 3 | process lifecycle | high | yes | Terminating the primary session left its separately-sessioned direct executor runner alive and reparented to the user service manager. | Process supervision now snapshots and terminates all descendant process groups, including descendants that created a new session. | `tests.test_process_guard.ProcessGuardTests.test_session_escaping_descendant_terminates_with_guard_owner` | Verified during cycle 7: every executor remained under the watcher-owned guard and no runner survived completion |
| SOL-005 | 4 | process lifecycle | high | yes | Gracefully stopping Coordinator terminated its app-owned watcher but left `runtime/watcher-executor.lock` with the dead watcher PID. | WatcherManager now reclaims only a confirmed-stale lock after stop and shutdown, preserving any live or replaced owner. | `tests.test_workflow.WatcherControlTests.test_stop_reclaims_the_exact_dead_watcher_lock` | Verified during cycle 5 shutdown |
| SOL-006 | 5 | planning | high | yes | The primary planned six mini-swe work units and claimed they fit 24 steps, while the enforced ceiling was four. | The non-secret project settings snapshot now exposes the same exact precomputed ceilings used by watcher and runner validation, and primary instructions require reading them. | `tests.test_executor_settings.ExecutorSettingsUnitTests.test_project_snapshot_is_bounded_non_secret_and_atomically_replaceable` | Verified during cycle 6 planning |
| SOL-007 | 5 | coordination lifecycle | high | yes | The primary responded to a rejected combined replacement patch by deleting three live coordination files before a later recreation step. | Generated primary and coordination instructions now require atomic in-place updates and prohibit deleting a live file as an intermediate step. | `tests.test_workflow.WorkflowTests.test_init_preserves_existing_instructions_and_is_idempotent` | Verified during cycle 6 planning |
| SOL-008 | 6 | executor efficiency | high | yes | A bounded Qwen response ran with `max_tokens=-1` and exceeded 17,000 decoded tokens without producing a tool action or product edit. | Cycle 7 verified the hard cap on the live llama slot; follow-up tuning lowers it from 4,096 to 3,072 tokens and explicitly requires a tool call before narration. | `tests.test_executor_adapters.MiniTrajectoryTests.test_command_uses_default_config_then_bounded_overrides` and `test_bounded_prompt_requires_a_tool_call_before_narration` | Verified during cycle 7; tighter follow-up tuning awaits the next local handoff |
| SOL-009 | 6 | process lifecycle | high | yes | App shutdown killed the nested mini runner before its `finally` block could persist blocked state and remove its turn lock. | Process supervision now gives saved descendant groups a bounded TERM cleanup grace before SIGKILL escalation. | `tests.test_process_guard.ProcessGuardTests.test_session_escaping_descendant_gets_term_cleanup_grace` | Verified during cycle 7: timeout and step-limit exits persisted blocked state, removed turn locks, and allowed clean retries; final watcher exit left no lock or runner |

For each accepted finding, record its `SOL-NNN` identifier, cycle, category,
severity, blocking status, short observation, disposition, regression-test
reference, and the first fresh cycle that verifies the fix. Keep bulky logs and
the complete old repository in the external cycle archive rather than copying
them into this ledger.
