# Upgrade and rollback

Coordinator keeps coordination intent in each repository's `.coordination/` tree and
keeps its rebuildable operational/security indexes in the configured `state_dir`.
Treat both as data: commit or back up coordination files normally, and make a verified
SQLite backup before changing application versions.

## Upgrade

1. Stop Coordinator so no managed Codex or watcher process remains attached.
2. Record the current version and source ref: `coordinator --version` and `git rev-parse
   HEAD` for clone installs.
3. Back up the operational index and verify all state:

   ```bash
   coordinator data backup /secure/backup/coordinator-operations.sqlite3
   coordinator data verify
   ```

   Also copy `security.sqlite3` with SQLite's online backup tooling or while the service
   is stopped. Keep its owner-only permissions.
4. Install the selected wheel into a fresh virtual environment. For a source install,
   check out the exact signed tag and run `uv sync --locked --extra dev` (omit the dev
   extra on a production host when using a wheel).
5. Run `coordinator doctor --json`, then start the service. Startup applies supported
   forward-only SQLite migrations.
6. Confirm `/healthz`, `/readyz`, Diagnostics, sign-in/sign-out, repository selection,
   and one read-only run-history view before starting a provider process.

Verify downloaded release files with `sha256sum -c SHA256SUMS`. When GitHub artifact
attestations are present, verify provenance with:

```bash
gh attestation verify coordinator_workflow-*.whl --repo bcrute/coordinator
```

## Rollback

Application binaries can be rolled back directly only when the older version supports
the current database schema. If startup refuses a newer schema, do not edit
`PRAGMA user_version` and do not copy tables manually.

1. Stop the service and preserve the failed-upgrade state directory for diagnosis.
2. Restore the verified pre-upgrade operational and security database backups.
3. Reinstall the previously recorded wheel or check out the recorded source ref.
4. Run `coordinator data verify` and `coordinator doctor --json` before restarting.
5. If only the rebuildable operational index is damaged, use `coordinator data rebuild`
   against the repositories root. This does not reconstruct security sessions; restore
   that database or require users to sign in again.

Never restore one SQLite main file while retaining `-wal`/`-shm` files from another
copy. Restore into an empty owner-only state directory while the service is stopped.
