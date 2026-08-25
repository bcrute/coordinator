# Solitaire continuous validation

This directory is the durable memory for an intentionally repetitive test of
Coordinator. The product under test is a small Klondike Solitaire application;
the actual subject of the test is Coordinator's ability to carry a goal from a
fresh discussion through planning, routing, implementation, review, correction,
and completion without manual repair.

There are two deliberately different kinds of context:

- The **continuous-context model** works in the Coordinator repository. It keeps
  the history here, diagnoses Coordinator defects, fixes them, and decides when
  a cycle has passed.
- The **validation model** runs inside a disposable Solitaire repository. It
  starts with no conversation history, follows only the checked-in validation
  brief and the repository's generated coordination files, and writes one
  structured report before it exits.

Fresh context is a feature of this test. A cycle is not resumed after a
Coordinator defect is fixed. The old target is archived, a fresh target and
model session are created, and the scenario begins again. This proves that the
pipeline works from its entry point instead of proving only that a damaged run
can be manually rescued.

Read these files in order:

1. [CONTEXT.md](CONTEXT.md) defines the stable product goal and boundaries.
2. [LOOP.md](LOOP.md) defines cycle ownership, reset rules, and the pass gate.
3. [REPORTING.md](REPORTING.md) tells the validation model what to report.
4. [CURRENT.md](CURRENT.md) is the handoff point the continuous-context model
   updates after every diagnosis, fix, and cycle transition.
5. [HISTORY.md](HISTORY.md) is the compact cycle and finding ledger.
6. [report.schema.json](report.schema.json) is the machine-readable report
   contract.
7. `target/` contains the small contract copied into each disposable target.

`cycle.py` prepares targets and performs a guarded, recoverable restart. It does
not stop Coordinator, switch the selected repository, or kill a model session.
Those are explicit operator actions so an active run cannot be destroyed by an
accidental command.

```bash
# One-time transition from an exploratory repository to clean protocol cycle 1.
# The old repository is archived, not deleted.
python3.14 validation/solitaire/cycle.py bootstrap \
  --target /path/to/solitaire-test \
  --archive-root /path/to/solitaire-cycle-archive \
  --confirm /absolute/path/to/solitaire-test

# Add the validation contract to a new or existing disposable Git repository.
python3.14 validation/solitaire/cycle.py prepare \
  --target /path/to/solitaire-test --cycle 1

# After the run has stopped and produced a report, archive the whole target and
# recreate a clean repository at the same path. The confirmation must be the
# target's exact resolved path.
python3.14 validation/solitaire/cycle.py restart \
  --target /path/to/solitaire-test \
  --archive-root /path/to/solitaire-cycle-archive \
  --next-cycle 2 \
  --confirm /absolute/path/to/solitaire-test
```

The restart is recoverable: the previous repository is moved into the archive
root rather than deleted. It refuses to run when coordination lock files are
present, the report is missing or nonterminal, the target is not marked as
disposable, or the confirmation does not match.

`bootstrap` is only for the one-time transition from an unmarked exploratory
repository. It refuses a target already managed by this protocol; later cycles
must use `restart` so a terminal report is always required.
