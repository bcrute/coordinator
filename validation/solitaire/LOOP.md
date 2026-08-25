# Continuous validation loop

## Roles

The operator starts and stops the local app, selects the target repository, and
authorizes the guarded restart. The continuous-context model owns diagnosis,
Coordinator fixes, the durable issue history, and the final pass decision. The
fresh validation model behaves like a user of Coordinator and reports what it
can observe; it does not fix Coordinator.

## One cycle

1. Stop any previous target session and watcher.
2. Prepare a clean disposable target with `cycle.py prepare` (or use the clean
   target created by `cycle.py restart`).
3. Select that target in Coordinator and initialize coordination through the
   normal product UI.
4. Start a brand-new validation-model session. Do not resume a prior model
   thread and do not paste prior-cycle conversation into it.
5. Give it only the prompt in `target/START_PROMPT.md`. The target's
   `COORDINATOR_VALIDATION.md` supplies the rest of the contract.
6. Let the configured pipeline run until it passes or reaches a real terminal
   failure. Do not manually edit routing, locks, task state, or product code.
7. The validation model writes `.coordinator-validation/report.json` and gives
   the operator the same short summary.
8. The continuous-context model validates the evidence and classifies findings.
9. It updates `CURRENT.md` and `HISTORY.md`; raw logs remain in the archived
   target rather than swelling the durable context.

## After a finding

Fix only the defect in Coordinator. Add or improve a regression test when the
behavior can be tested usefully. Then stop the target session and watcher and
run `cycle.py restart`. The prior target remains recoverable in the archive;
the replacement target has a new Git identity, a new cycle identifier, and no
Solitaire or `.coordination` history.

Never reuse a failed cycle as evidence for a later pass. A fix earns confidence
only when a new context and new repository pass through the affected stage.

## Pass gate

A cycle passes only when all of the following are true:

- the Solitaire acceptance scope is complete and its checks pass;
- Coordinator reaches its documented done state without manual state repair;
- the configured roles, models, effort, permissions, and executor were honored;
- displayed progress and terminal/agent state agree with the actual run;
- no stale locks, watchers, sessions, or implementing states remain;
- the model report has `outcome: "passed"` and no blocking findings; and
- the continuous-context model independently reviews the final evidence.

One passing cycle is an end-to-end result. Two consecutive clean passing cycles
are the preferred confidence threshold before calling the pipeline continuous.
