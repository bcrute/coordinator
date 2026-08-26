# Cycle and finding history

Only runs started under this protocol count toward the clean passing streak.
Earlier exploratory Solitaire work is useful diagnostic context but is not a
protocol cycle because it did not start with a fresh target and report contract.

## Cycles

| Cycle | Outcome | Terminal stage | Coordinator fix prompted | Clean pass streak |
| --- | --- | --- | --- | --- |
| 1 | Blocked | implementation | Bounded mini-swe role profiles and process-tree supervision (`2b0b4ab`) | 0 |
| 2 | Failed | routing | Persist selected repository across application restarts (`6cbe93a`) | 0 |
| 3 | Running | setup | — | 0 |

## Findings

| ID | Cycle | Category | Severity | Blocks | Observation | Disposition | Regression evidence | Fresh verification |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| SOL-001 | 1 | executor | medium | yes | Repeated Qwen handoffs exhausted 12 steps or the 900-second limit; some rounds made no edits and the build never completed. | Cycle archived; Coordinator now supplies role-specific bounded mini-swe profiles and owns descendant cleanup. | `tests.test_executor_adapters`, `tests.test_process_guard` | Pending cycle 3 |
| SOL-002 | 2 | coordinator | high | yes | Restarting Coordinator discarded the selected Solitaire repository and launched the managed terminal against the configured default repository. | Fixed by persisting only a validated catalog repository path in the owner-only operational store; cycle 2 archived. | `tests.test_web_repository_switching.RepositorySwitchingTests.test_selected_repository_survives_application_restart` | Verified during cycle 3 setup |

For each accepted finding, record its `SOL-NNN` identifier, cycle, category,
severity, blocking status, short observation, disposition, regression-test
reference, and the first fresh cycle that verifies the fix. Keep bulky logs and
the complete old repository in the external cycle archive rather than copying
them into this ledger.
