"""Versioned, rebuildable operational index for coordination runs and events."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import time
import uuid
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

LATEST_SCHEMA_VERSION = 4
IDENTIFIER_NAMESPACE = uuid.UUID("f3288cb2-d2d3-4b2f-a0c5-dc5d15d76b7f")


def stable_id(kind: str, *parts: object) -> str:
    """Return a deterministic, opaque identifier for a portable domain entity."""

    material = "\x1f".join([kind, *(str(part) for part in parts)])
    return f"{kind}_{uuid.uuid5(IDENTIFIER_NAMESPACE, material).hex}"


@dataclass(frozen=True)
class GuardrailPolicy:
    """Optional hard limits; ``None`` means the provider owns that dimension."""

    turn_seconds: float | None = None
    overall_seconds: float | None = None
    generated_tokens: int | None = None
    input_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None
    correction_rounds: int | None = None
    concurrent_workers: int | None = None
    no_progress_seconds: float | None = None
    warning_ratio: float = 0.8

    def __post_init__(self) -> None:
        values = (
            self.turn_seconds,
            self.overall_seconds,
            self.generated_tokens,
            self.input_tokens,
            self.cache_read_tokens,
            self.cache_write_tokens,
            self.correction_rounds,
            self.concurrent_workers,
            self.no_progress_seconds,
        )
        if any(value is not None and value <= 0 for value in values):
            raise ValueError("guardrail limits must be positive")
        if not 0 < self.warning_ratio < 1:
            raise ValueError("warning_ratio must be between zero and one")

    def as_dict(self) -> dict[str, object]:
        return {
            "turn_seconds": self.turn_seconds,
            "overall_seconds": self.overall_seconds,
            "generated_tokens": self.generated_tokens,
            "input_tokens": self.input_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "correction_rounds": self.correction_rounds,
            "concurrent_workers": self.concurrent_workers,
            "no_progress_seconds": self.no_progress_seconds,
            "warning_ratio": self.warning_ratio,
        }


def evaluate_guardrails(
    snapshot: dict[str, object],
    policy: GuardrailPolicy,
    *,
    last_change_at: float,
    now: float | None = None,
) -> dict[str, object]:
    """Evaluate observable provider metrics without estimating unavailable values."""

    current = time.time() if now is None else now
    runtime = snapshot.get("runtime") if isinstance(snapshot.get("runtime"), dict) else {}
    timing = runtime.get("timing") if isinstance(runtime.get("timing"), dict) else {}
    tokens = runtime.get("tokens") if isinstance(runtime.get("tokens"), dict) else {}
    task = snapshot.get("task") if isinstance(snapshot.get("task"), dict) else {}
    workflow = (
        snapshot.get("workflow") if isinstance(snapshot.get("workflow"), dict) else {}
    )
    subagents = runtime.get("subagents") if isinstance(runtime.get("subagents"), list) else []

    def seconds(name: str) -> float:
        value = timing.get(name)
        if not isinstance(value, dict):
            return 0.0
        raw = value.get("seconds")
        return float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else 0.0

    review_round = task.get("review_round")
    try:
        corrections = max(0, int(str(review_round)))
    except ValueError:
        corrections = 0
    workers = sum(
        1 for agent in subagents if isinstance(agent, dict) and agent.get("state") == "running"
    )
    if workflow.get("active"):
        workers += 1
    observed: dict[str, float | int] = {
        "turn_seconds": seconds("turn"),
        "overall_seconds": seconds("overall"),
        "generated_tokens": int(tokens.get("output_tokens") or 0),
        "input_tokens": int(tokens.get("input_tokens") or 0),
        "cache_read_tokens": int(tokens.get("cache_read_input_tokens") or 0),
        "cache_write_tokens": int(tokens.get("cache_creation_input_tokens") or 0),
        "correction_rounds": corrections,
        "concurrent_workers": workers,
        "no_progress_seconds": max(0.0, current - last_change_at),
    }
    limits = policy.as_dict()
    findings: list[dict[str, object]] = []
    for metric, value in observed.items():
        limit = limits.get(metric)
        if not isinstance(limit, (int, float)) or isinstance(limit, bool):
            continue
        ratio = float(value) / float(limit)
        if ratio >= policy.warning_ratio:
            findings.append(
                {
                    "metric": metric,
                    "value": value,
                    "limit": limit,
                    "ratio": round(ratio, 4),
                    "severity": "stop" if ratio >= 1 else "warning",
                }
            )
    status = "stop" if any(item["severity"] == "stop" for item in findings) else (
        "warning" if findings else "ok"
    )
    return {"status": status, "observed": observed, "findings": findings}


class OperationalStore:
    """Owner-only SQLite index that can be rebuilt from coordination snapshots."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir.resolve()
        self.path = self.state_dir / "operations.sqlite3"
        self._revision = 0
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state_dir.chmod(0o700)
        directory_stat = self.state_dir.stat()
        if (
            directory_stat.st_uid != os.geteuid()
            or stat.S_IMODE(directory_stat.st_mode) & 0o077
        ):
            raise ValueError("state_dir must be owned by the service user and mode 0700")
        if self.path.is_symlink():
            raise ValueError("operational database path must not be a symbolic link")
        self._migrate()

    @property
    def revision(self) -> int:
        return self._revision

    def _changed(self) -> None:
        self._revision += 1

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 10000")
            with connection:
                yield connection
        finally:
            connection.close()

    def _migrate(self) -> None:
        with self._connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version > LATEST_SCHEMA_VERSION:
                raise ValueError(
                    f"unsupported operational database schema version: {version}"
                )
            connection.execute("PRAGMA journal_mode = WAL")
            if version < 1:
                connection.executescript(
                    """
                    CREATE TABLE repositories (
                        repository_id TEXT PRIMARY KEY,
                        path TEXT NOT NULL UNIQUE,
                        display_name TEXT NOT NULL,
                        last_seen_at REAL NOT NULL
                    );
                    CREATE TABLE runs (
                        run_id TEXT PRIMARY KEY,
                        repository_id TEXT NOT NULL REFERENCES repositories(repository_id)
                            ON DELETE CASCADE,
                        goal_id TEXT NOT NULL,
                        starting_ref TEXT NOT NULL,
                        status TEXT NOT NULL,
                        started_at REAL NOT NULL,
                        ended_at REAL,
                        last_seen_at REAL NOT NULL,
                        last_change_at REAL NOT NULL,
                        state_digest TEXT NOT NULL,
                        snapshot_json TEXT NOT NULL,
                        UNIQUE(repository_id, goal_id, starting_ref)
                    );
                    CREATE TABLE turns (
                        turn_id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                        task_id TEXT NOT NULL,
                        state TEXT NOT NULL,
                        review_round INTEGER NOT NULL DEFAULT 0,
                        snapshot_json TEXT NOT NULL,
                        UNIQUE(run_id, task_id)
                    );
                    CREATE TABLE objectives (
                        objective_id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                        ordinal INTEGER NOT NULL,
                        description TEXT NOT NULL,
                        state TEXT NOT NULL,
                        UNIQUE(run_id, ordinal)
                    );
                    CREATE TABLE agents (
                        agent_id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                        parent_agent_id TEXT REFERENCES agents(agent_id),
                        ordinal INTEGER NOT NULL,
                        model TEXT NOT NULL,
                        description TEXT NOT NULL,
                        state TEXT NOT NULL,
                        snapshot_json TEXT NOT NULL
                    );
                    CREATE TABLE artifacts (
                        artifact_id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                        kind TEXT NOT NULL,
                        locator TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        UNIQUE(run_id, kind, locator)
                    );
                    CREATE TABLE events (
                        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_uid TEXT NOT NULL UNIQUE,
                        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                        created_at REAL NOT NULL,
                        event_type TEXT NOT NULL,
                        payload_json TEXT NOT NULL
                    );
                    CREATE INDEX events_run_id_event_id ON events(run_id, event_id);
                    PRAGMA user_version = 1;
                    """
                )
                version = 1
            if version < 2:
                connection.executescript(
                    """
                    ALTER TABLE runs ADD COLUMN pause_reason TEXT;
                    ALTER TABLE runs ADD COLUMN resume_required INTEGER NOT NULL DEFAULT 0;
                    ALTER TABLE runs ADD COLUMN policy_json TEXT NOT NULL DEFAULT '{}';
                    CREATE TABLE preferences (
                        preference_key TEXT PRIMARY KEY,
                        value_json TEXT NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    CREATE TABLE notifications (
                        notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        run_id TEXT REFERENCES runs(run_id) ON DELETE CASCADE,
                        kind TEXT NOT NULL,
                        status TEXT NOT NULL,
                        created_at REAL NOT NULL,
                        acknowledged_at REAL,
                        payload_json TEXT NOT NULL
                    );
                    PRAGMA user_version = 2;
                    """
                )
                version = 2
            if version < 3:
                connection.executescript(
                    """
                    CREATE TABLE process_instances (
                        process_id TEXT PRIMARY KEY,
                        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                        provider TEXT NOT NULL,
                        pid INTEGER NOT NULL,
                        process_group INTEGER,
                        status TEXT NOT NULL,
                        started_at REAL,
                        ended_at REAL,
                        UNIQUE(run_id, provider, pid)
                    );
                    CREATE TABLE failure_signatures (
                        run_id TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
                        signature TEXT NOT NULL,
                        occurrences INTEGER NOT NULL,
                        first_seen_at REAL NOT NULL,
                        last_seen_at REAL NOT NULL,
                        PRIMARY KEY(run_id, signature)
                    );
                    PRAGMA user_version = 3;
                    """
                )
                version = 3
            if version < 4:
                connection.executescript(
                    """
                    ALTER TABLE runs ADD COLUMN archived_at REAL;
                    PRAGMA user_version = 4;
                    """
                )
        self.path.chmod(0o600)
        file_stat = self.path.stat()
        if file_stat.st_uid != os.geteuid() or stat.S_IMODE(file_stat.st_mode) & 0o077:
            raise ValueError(
                "operational database must be owned by the service user and mode 0600"
            )

    @property
    def schema_version(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])

    @staticmethod
    def identifiers(repo: Path, snapshot: dict[str, object]) -> dict[str, str]:
        resolved = repo.resolve()
        goal = snapshot.get("goal") if isinstance(snapshot.get("goal"), dict) else {}
        task = snapshot.get("task") if isinstance(snapshot.get("task"), dict) else {}
        repository_id = stable_id("repo", resolved)
        goal_key = str(goal.get("id") or "none")
        starting_ref = str(goal.get("starting_ref") or "not-recorded")
        run_id = stable_id("run", repository_id, goal_key, starting_ref)
        task_key = str(task.get("id") or "none")
        return {
            "repository_id": repository_id,
            "run_id": run_id,
            "turn_id": stable_id("turn", run_id, task_key),
        }

    @staticmethod
    def _projection(snapshot: dict[str, object]) -> dict[str, object]:
        return {
            name: snapshot.get(name)
            for name in (
                "goal",
                "roadmap",
                "task",
                "coder",
                "review",
                "runtime",
                "completion",
                "workflow",
                "managed_watcher",
                "codex_session",
            )
        }

    @staticmethod
    def _transition_digest(projection: dict[str, object]) -> str:
        """Hash semantic changes while ignoring timers that advance every refresh."""

        stable = json.loads(json.dumps(projection))
        runtime = stable.get("runtime")
        if isinstance(runtime, dict):
            runtime.pop("timing", None)
            subagents = runtime.get("subagents")
            if isinstance(subagents, list):
                for agent in subagents:
                    if isinstance(agent, dict):
                        agent.pop("elapsed", None)
        encoded = json.dumps(stable, separators=(",", ":"), sort_keys=True)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def sync_snapshot(
        self, repo: Path, snapshot: dict[str, object], now: float | None = None
    ) -> dict[str, object]:
        """Upsert a file-derived snapshot and append one immutable transition event."""

        current = time.time() if now is None else now
        ids = self.identifiers(repo, snapshot)
        projection = self._projection(snapshot)
        encoded = json.dumps(projection, separators=(",", ":"), sort_keys=True)
        digest = self._transition_digest(projection)
        goal = projection.get("goal") if isinstance(projection.get("goal"), dict) else {}
        task = projection.get("task") if isinstance(projection.get("task"), dict) else {}
        workflow = (
            projection.get("workflow")
            if isinstance(projection.get("workflow"), dict)
            else {}
        )
        status = str(goal.get("state") or workflow.get("phase") or "unknown")
        ended_at = current if status in {"done", "blocked"} else None
        resolved = repo.resolve()

        with self._connect() as connection:
            previous = connection.execute(
                "SELECT state_digest, last_change_at, resume_required, pause_reason "
                "FROM runs WHERE run_id = ?",
                (ids["run_id"],),
            ).fetchone()
            changed = previous is None or str(previous["state_digest"]) != digest
            last_change_at = current if changed else float(previous["last_change_at"])
            connection.execute(
                """
                INSERT INTO repositories(repository_id, path, display_name, last_seen_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(repository_id) DO UPDATE SET
                    path = excluded.path,
                    display_name = excluded.display_name,
                    last_seen_at = excluded.last_seen_at
                """,
                (ids["repository_id"], str(resolved), resolved.name, current),
            )
            connection.execute(
                """
                INSERT INTO runs(
                    run_id, repository_id, goal_id, starting_ref, status, started_at,
                    ended_at, last_seen_at, last_change_at, state_digest, snapshot_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    status = CASE WHEN runs.resume_required = 1 THEN runs.status
                                  ELSE excluded.status END,
                    ended_at = excluded.ended_at,
                    last_seen_at = excluded.last_seen_at,
                    last_change_at = excluded.last_change_at,
                    state_digest = excluded.state_digest,
                    snapshot_json = excluded.snapshot_json
                """,
                (
                    ids["run_id"],
                    ids["repository_id"],
                    str(goal.get("id") or "none"),
                    str(goal.get("starting_ref") or "not-recorded"),
                    status,
                    current,
                    ended_at,
                    current,
                    last_change_at,
                    digest,
                    encoded,
                ),
            )
            task_id = str(task.get("id") or "none")
            review_round = task.get("review_round")
            connection.execute(
                """
                INSERT INTO turns(turn_id, run_id, task_id, state, review_round, snapshot_json)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(turn_id) DO UPDATE SET
                    state = excluded.state,
                    review_round = excluded.review_round,
                    snapshot_json = excluded.snapshot_json
                """,
                (
                    ids["turn_id"],
                    ids["run_id"],
                    task_id,
                    str(task.get("state") or "unknown"),
                    int(review_round) if isinstance(review_round, int) else 0,
                    json.dumps(task, separators=(",", ":"), sort_keys=True),
                ),
            )
            self._sync_objectives(connection, ids["run_id"], projection)
            self._sync_agents(connection, ids["run_id"], projection)
            self._sync_processes(connection, ids["run_id"], projection, current)
            if changed:
                event_type = "run_discovered" if previous is None else "state_changed"
                event_uid = stable_id("event", ids["run_id"], digest)
                connection.execute(
                    "INSERT OR IGNORE INTO events(event_uid, run_id, created_at, event_type, "
                    "payload_json) VALUES (?, ?, ?, ?, ?)",
                    (event_uid, ids["run_id"], current, event_type, encoded),
                )
            row = connection.execute(
                "SELECT status, pause_reason, resume_required, last_change_at, policy_json "
                "FROM runs WHERE run_id = ?",
                (ids["run_id"],),
            ).fetchone()
        return {
            **ids,
            "status": row["status"],
            "pause_reason": row["pause_reason"],
            "resume_required": bool(row["resume_required"]),
            "last_change_at": float(row["last_change_at"]),
            "policy": json.loads(row["policy_json"]),
            "changed": changed,
        }

    def _sync_objectives(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        projection: dict[str, object],
    ) -> None:
        roadmap = projection.get("roadmap")
        if not isinstance(roadmap, list):
            return
        connection.execute("DELETE FROM objectives WHERE run_id = ?", (run_id,))
        for ordinal, item in enumerate(roadmap):
            if not isinstance(item, dict):
                continue
            description = str(item.get("objective") or item.get("title") or item)
            connection.execute(
                "INSERT INTO objectives(objective_id, run_id, ordinal, description, state) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    stable_id("objective", run_id, ordinal, description),
                    run_id,
                    ordinal,
                    description,
                    str(item.get("state") or "pending"),
                ),
            )

    def _sync_agents(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        projection: dict[str, object],
    ) -> None:
        runtime = projection.get("runtime")
        runtime = runtime if isinstance(runtime, dict) else {}
        connection.execute("DELETE FROM agents WHERE run_id = ?", (run_id,))
        primary_id = stable_id("agent", run_id, "primary")
        primary = {
            "model": str(runtime.get("primary_model") or "provider-primary"),
            "description": "Primary coding agent",
            "state": str(runtime.get("state") or "unknown"),
        }
        connection.execute(
            "INSERT INTO agents(agent_id, run_id, parent_agent_id, ordinal, model, "
            "description, state, snapshot_json) VALUES (?, ?, NULL, 0, ?, ?, ?, ?)",
            (
                primary_id,
                run_id,
                primary["model"],
                primary["description"],
                primary["state"],
                json.dumps(primary, separators=(",", ":"), sort_keys=True),
            ),
        )
        subagents = runtime.get("subagents")
        if not isinstance(subagents, list):
            return
        for ordinal, agent in enumerate(subagents, start=1):
            if not isinstance(agent, dict):
                continue
            description = str(agent.get("description") or f"Subagent {ordinal}")
            connection.execute(
                "INSERT INTO agents(agent_id, run_id, parent_agent_id, ordinal, model, "
                "description, state, snapshot_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    stable_id("agent", run_id, ordinal, description),
                    run_id,
                    primary_id,
                    ordinal,
                    str(agent.get("model") or "provider-selected"),
                    description,
                    str(agent.get("state") or "unknown"),
                    json.dumps(agent, separators=(",", ":"), sort_keys=True),
                ),
            )

    def _sync_processes(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        projection: dict[str, object],
        current: float,
    ) -> None:
        for provider, key in (("watcher", "managed_watcher"), ("codex", "codex_session")):
            value = projection.get(key)
            if not isinstance(value, dict):
                continue
            pid = value.get("pid")
            if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
                continue
            running = bool(value.get("running") or value.get("active"))
            status = str(value.get("state") or ("running" if running else "stopped"))
            started_at = value.get("started_at_epoch") or value.get("started_at")
            group = value.get("process_group") or value.get("group")
            connection.execute(
                """
                INSERT INTO process_instances(
                    process_id, run_id, provider, pid, process_group, status,
                    started_at, ended_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id, provider, pid) DO UPDATE SET
                    process_group = excluded.process_group,
                    status = excluded.status,
                    ended_at = excluded.ended_at
                """,
                (
                    stable_id("process", run_id, provider, pid),
                    run_id,
                    provider,
                    pid,
                    group if isinstance(group, int) and not isinstance(group, bool) else None,
                    status,
                    float(started_at)
                    if isinstance(started_at, (int, float)) and not isinstance(started_at, bool)
                    else None,
                    None if running else current,
                ),
            )

    def set_policy(self, run_id: str, policy: GuardrailPolicy) -> bool:
        encoded = json.dumps(policy.as_dict(), separators=(",", ":"), sort_keys=True)
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE runs SET policy_json = ? WHERE run_id = ?", (encoded, run_id)
            )
        if cursor.rowcount:
            self._changed()
        return cursor.rowcount == 1

    def pause(self, run_id: str, reason: str, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE runs SET status = 'paused', pause_reason = ?, resume_required = 1 "
                "WHERE run_id = ?",
                (reason[:500], run_id),
            )
            if cursor.rowcount:
                connection.execute(
                    "INSERT INTO events(event_uid, run_id, created_at, event_type, payload_json) "
                    "VALUES (?, ?, ?, 'run_paused', ?)",
                    (
                        stable_id("event", run_id, "pause", current),
                        run_id,
                        current,
                        json.dumps({"reason": reason[:500]}, separators=(",", ":")),
                    ),
                )
        if cursor.rowcount:
            self._changed()
        return cursor.rowcount == 1

    def resume(self, run_id: str, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE runs SET status = 'active', pause_reason = NULL, resume_required = 0, "
                "last_change_at = ? WHERE run_id = ? AND resume_required = 1",
                (current, run_id),
            )
            if cursor.rowcount:
                connection.execute(
                    "INSERT INTO events(event_uid, run_id, created_at, event_type, payload_json) "
                    "VALUES (?, ?, ?, 'run_resumed', '{}')",
                    (stable_id("event", run_id, "resume", current), run_id, current),
                )
        if cursor.rowcount:
            self._changed()
        return cursor.rowcount == 1

    def archive(self, run_id: str, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE runs SET archived_at = ? WHERE run_id = ? AND archived_at IS NULL",
                (current, run_id),
            )
        if cursor.rowcount:
            self._changed()
        return cursor.rowcount == 1

    def reopen(self, run_id: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE runs SET archived_at = NULL WHERE run_id = ? AND archived_at IS NOT NULL",
                (run_id,),
            )
        if cursor.rowcount:
            self._changed()
        return cursor.rowcount == 1

    def recover_interrupted(self, now: float | None = None) -> int:
        """Require explicit resume for runs that were active when the service stopped."""

        current = time.time() if now is None else now
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT run_id FROM runs WHERE status IN ('active', 'implementing', 'running') "
                "AND resume_required = 0"
            ).fetchall()
            for row in rows:
                run_id = str(row["run_id"])
                connection.execute(
                    "UPDATE runs SET status = 'interrupted', pause_reason = ?, "
                    "resume_required = 1 WHERE run_id = ?",
                    ("service restarted while the run was active", run_id),
                )
                connection.execute(
                    "INSERT INTO events(event_uid, run_id, created_at, event_type, payload_json) "
                    "VALUES (?, ?, ?, 'run_interrupted', '{}')",
                    (stable_id("event", run_id, "interrupted", current), run_id, current),
                )
        return len(rows)

    def record_failure(
        self,
        run_id: str,
        signature: str,
        *,
        stop_after: int = 3,
        now: float | None = None,
    ) -> dict[str, object]:
        """Count identical failures and pause after a bounded repeat threshold."""

        if not signature.strip() or len(signature) > 500:
            raise ValueError("failure signature must contain 1-500 characters")
        if stop_after <= 0:
            raise ValueError("stop_after must be positive")
        current = time.time() if now is None else now
        digest = hashlib.sha256(signature.strip().encode("utf-8")).hexdigest()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO failure_signatures(
                    run_id, signature, occurrences, first_seen_at, last_seen_at
                ) VALUES (?, ?, 1, ?, ?)
                ON CONFLICT(run_id, signature) DO UPDATE SET
                    occurrences = failure_signatures.occurrences + 1,
                    last_seen_at = excluded.last_seen_at
                """,
                (run_id, digest, current, current),
            )
            row = connection.execute(
                "SELECT occurrences FROM failure_signatures WHERE run_id = ? AND signature = ?",
                (run_id, digest),
            ).fetchone()
            occurrences = int(row["occurrences"])
        paused = False
        if occurrences >= stop_after:
            paused = self.pause(
                run_id,
                f"identical failure repeated {occurrences} times: {signature.strip()[:200]}",
                now=current,
            )
        return {"occurrences": occurrences, "paused": paused, "signature": digest}

    def list_runs(self, limit: int = 100) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT r.*, p.path, p.display_name FROM runs r JOIN repositories p "
                "USING(repository_id) ORDER BY r.last_seen_at DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [self._run_row(row, include_snapshot=False) for row in rows]

    def get_run(self, run_id: str) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT r.*, p.path, p.display_name FROM runs r JOIN repositories p "
                "USING(repository_id) WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return self._run_row(row, include_snapshot=True) if row is not None else None

    @staticmethod
    def _run_row(row: sqlite3.Row, *, include_snapshot: bool) -> dict[str, object]:
        snapshot = json.loads(row["snapshot_json"])
        workflow = snapshot.get("workflow") if isinstance(snapshot.get("workflow"), dict) else {}
        runtime = snapshot.get("runtime") if isinstance(snapshot.get("runtime"), dict) else {}
        review = snapshot.get("review") if isinstance(snapshot.get("review"), dict) else {}
        value = {
            "run_id": row["run_id"],
            "repository_id": row["repository_id"],
            "repository": row["display_name"],
            "path": row["path"],
            "goal_id": row["goal_id"],
            "starting_ref": row["starting_ref"],
            "status": row["status"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "last_seen_at": row["last_seen_at"],
            "last_change_at": row["last_change_at"],
            "pause_reason": row["pause_reason"],
            "resume_required": bool(row["resume_required"]),
            "archived_at": row["archived_at"],
            "policy": json.loads(row["policy_json"]),
            "summary": {
                "workflow": {
                    "phase": workflow.get("phase"),
                    "label": workflow.get("label"),
                    "detail": workflow.get("detail"),
                },
                "timing": runtime.get("timing") if isinstance(runtime.get("timing"), dict) else {},
                "tokens": runtime.get("tokens") if isinstance(runtime.get("tokens"), dict) else {},
                "review": {
                    "verdict": review.get("verdict"),
                    "examined_ref": review.get("examined_ref"),
                },
            },
        }
        if include_snapshot:
            value["snapshot"] = snapshot
        return value

    def list_events(self, run_id: str, after_id: int = 0, limit: int = 200):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT event_id, event_uid, created_at, event_type, payload_json "
                "FROM events WHERE run_id = ? AND event_id > ? ORDER BY event_id LIMIT ?",
                (run_id, max(0, after_id), max(1, min(limit, 1000))),
            ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "event_uid": row["event_uid"],
                "created_at": row["created_at"],
                "type": row["event_type"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    def latest_event_id(self, run_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(event_id), 0) FROM events WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return int(row[0])

    def statistics(self) -> dict[str, int]:
        with self._connect() as connection:
            runs = int(connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0])
            events = int(connection.execute("SELECT COUNT(*) FROM events").fetchone()[0])
            paused = int(
                connection.execute(
                    "SELECT COUNT(*) FROM runs WHERE resume_required = 1"
                ).fetchone()[0]
            )
        return {"runs": runs, "events": events, "paused_runs": paused}

    def diagnostics(self) -> dict[str, object]:
        """Return bounded, read-only health details for the operational index."""

        with self._connect() as connection:
            integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            page_count = int(connection.execute("PRAGMA page_count").fetchone()[0])
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            event = connection.execute(
                "SELECT event_id, created_at FROM events ORDER BY event_id DESC LIMIT 1"
            ).fetchone()
        return {
            "ok": integrity == "ok",
            "integrity": integrity,
            "schema_version": self.schema_version,
            "database_bytes": page_count * page_size,
            "latest_event_id": int(event["event_id"]) if event else 0,
            "latest_event_at": float(event["created_at"]) if event else None,
        }

    def set_preference(self, key: str, value: object) -> None:
        if not key or len(key) > 100:
            raise ValueError("preference key must contain 1-100 characters")
        encoded = json.dumps(value, separators=(",", ":"), sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO preferences(preference_key, value_json, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(preference_key) DO UPDATE SET "
                "value_json = excluded.value_json, updated_at = excluded.updated_at",
                (key, encoded, time.time()),
            )
        self._changed()

    def preferences(self) -> dict[str, object]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT preference_key, value_json FROM preferences ORDER BY preference_key"
            ).fetchall()
        return {str(row["preference_key"]): json.loads(row["value_json"]) for row in rows}

    def prune_events(self, before: float) -> int:
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM events WHERE created_at < ?", (before,))
        return cursor.rowcount

    def backup(self, destination: Path) -> Path:
        target = destination.resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as source, closing(sqlite3.connect(target)) as backup:
            source.backup(backup)
        target.chmod(0o600)
        self.verify_database(target)
        return target

    @staticmethod
    def verify_database(path: Path) -> None:
        if not path.is_file() or path.is_symlink():
            raise ValueError("backup must be a regular file")
        try:
            with closing(sqlite3.connect(path)) as connection:
                result = connection.execute("PRAGMA integrity_check").fetchone()[0]
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        except sqlite3.DatabaseError as error:
            raise ValueError("database could not be read as SQLite") from error
        if result != "ok":
            raise ValueError(f"database integrity check failed: {result}")
        if version != LATEST_SCHEMA_VERSION:
            raise ValueError(f"backup schema version {version} is not supported")

    def restore(self, source: Path) -> None:
        self.verify_database(source)
        with closing(sqlite3.connect(source)) as backup, self._connect() as destination:
            backup.backup(destination)
            destination.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.path.chmod(0o600)
        self._migrate()

    def rebuild(self, snapshots: list[tuple[Path, dict[str, object]]]) -> int:
        """Replace rebuildable entities, retaining preferences and notifications."""

        with self._connect() as connection:
            connection.execute("DELETE FROM repositories")
        for repo, snapshot in snapshots:
            self.sync_snapshot(repo, snapshot)
        return len(snapshots)
