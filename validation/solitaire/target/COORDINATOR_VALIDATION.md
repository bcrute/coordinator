# Coordinator validation target

This is a disposable Klondike Solitaire project used to validate Coordinator.
Build the product described in the normal repository goal and
`validation-brief.md`, using only the roles, models, permissions, and executors
configured through Coordinator.

Do not repair Coordinator by editing generated coordination state, deleting
locks, changing routing, switching executors, or modifying the Coordinator
source repository. If the pipeline behaves incorrectly, preserve evidence and
record the problem in `.coordinator-validation/report.json` according to
`reporting.md` and `report.schema.json`.

Keep the report current, but produce only one terminal report. Use relative
paths and concise log excerpts, and never include credentials or environment
dumps. End with a short summary matching the report's `summary` field.
