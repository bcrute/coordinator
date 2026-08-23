"""Provider-neutral historical token usage and subscription-value estimates.

Usage adapters import native, append-only agent telemetry.  The web application
only consumes the normalized records, so adding a provider does not require a
new route or provider-specific UI.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import threading
import time
from collections.abc import Iterable, Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol


HISTORY_REFRESH_SECONDS = 300
HISTORY_RANGES = {
    "24h": (24 * 60 * 60, 15 * 60),
    "7d": (7 * 24 * 60 * 60, 60 * 60),
    "30d": (30 * 24 * 60 * 60, 6 * 60 * 60),
    "all": (None, 24 * 60 * 60),
}


def _timestamp(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _integer(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _opaque(*parts: object) -> str:
    material = "\x1f".join(str(part) for part in parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class UsageRecord:
    """One deduplicated provider response, normalized into token categories."""

    event_uid: str
    occurred_at: float
    model: str
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    cost_basis: str | None = None
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
            + self.cache_write_5m_tokens
            + self.cache_write_1h_tokens
            + self.output_tokens
        )


@dataclass(frozen=True)
class UsageBatch:
    records: tuple[UsageRecord, ...]
    files: Mapping[str, tuple[int, int]]


class UsageHistoryAdapter(Protocol):
    """Stable extension seam for native or custom usage importers."""

    id: str
    display_name: str

    def collect(self, known_files: Mapping[str, tuple[int, int]]) -> UsageBatch: ...


@dataclass(frozen=True)
class TokenRates:
    """USD per million tokens for one model and execution mode."""

    input: float
    cache_read: float
    cache_write: float
    output: float
    cache_write_1h: float | None = None

    def cost(self, record: UsageRecord) -> float:
        return round(
            (
                record.input_tokens * self.input
                + record.cache_read_tokens * self.cache_read
                + (record.cache_write_tokens + record.cache_write_5m_tokens)
                * self.cache_write
                + record.cache_write_1h_tokens
                * (self.cache_write_1h if self.cache_write_1h is not None else self.cache_write)
                + record.output_tokens * self.output
            )
            / 1_000_000,
            9,
        )


CODEX_RATES: tuple[tuple[str, TokenRates], ...] = (
    ("gpt-5.6-sol", TokenRates(4.00, 0.40, 5.00, 20.00)),
    ("gpt-5.6-terra", TokenRates(2.00, 0.20, 2.50, 12.00)),
    ("gpt-5.6-luna", TokenRates(0.20, 0.02, 0.25, 1.20)),
    ("gpt-5.5-pro", TokenRates(15.00, 15.00, 15.00, 90.00)),
    ("gpt-5.5", TokenRates(2.50, 0.25, 2.50, 15.00)),
    ("gpt-5.4-pro", TokenRates(15.00, 15.00, 15.00, 90.00)),
    ("gpt-5.4", TokenRates(1.25, 0.125, 1.25, 7.50)),
    ("gpt-5.3-codex", TokenRates(1.75, 0.175, 1.75, 14.00)),
    ("gpt-5.2-codex", TokenRates(1.75, 0.175, 1.75, 14.00)),
    ("codex-mini-latest", TokenRates(1.50, 0.375, 1.50, 6.00)),
)


CODEX_LONG_CONTEXT_RATES: tuple[tuple[str, TokenRates], ...] = (
    ("gpt-5.6-sol", TokenRates(8.00, 0.80, 10.00, 30.00)),
    ("gpt-5.6-terra", TokenRates(4.00, 0.40, 5.00, 18.00)),
    ("gpt-5.6-luna", TokenRates(0.40, 0.04, 0.50, 1.80)),
)
CODEX_LONG_CONTEXT_THRESHOLD = 272_000


CLAUDE_RATES: tuple[tuple[str, TokenRates], ...] = (
    ("claude-fable-5", TokenRates(10.00, 1.00, 12.50, 50.00, 20.00)),
    ("claude-mythos-5", TokenRates(10.00, 1.00, 12.50, 50.00, 20.00)),
    ("claude-opus-5", TokenRates(5.00, 0.50, 6.25, 25.00, 10.00)),
    ("claude-opus-4-8", TokenRates(5.00, 0.50, 6.25, 25.00, 10.00)),
    ("claude-opus-4-7", TokenRates(5.00, 0.50, 6.25, 25.00, 10.00)),
    ("claude-opus-4-6", TokenRates(5.00, 0.50, 6.25, 25.00, 10.00)),
    ("claude-opus-4-5", TokenRates(5.00, 0.50, 6.25, 25.00, 10.00)),
    ("claude-opus-4-1", TokenRates(15.00, 1.50, 18.75, 75.00, 30.00)),
    ("claude-sonnet-5", TokenRates(2.00, 0.20, 2.50, 10.00, 4.00)),
    ("claude-sonnet-4-6", TokenRates(3.00, 0.30, 3.75, 15.00, 6.00)),
    ("claude-sonnet-4-5", TokenRates(3.00, 0.30, 3.75, 15.00, 6.00)),
    ("claude-sonnet-4", TokenRates(3.00, 0.30, 3.75, 15.00, 6.00)),
    ("claude-haiku-4-5", TokenRates(1.00, 0.10, 1.25, 5.00, 2.00)),
    ("claude-haiku-3-5", TokenRates(0.80, 0.08, 1.00, 4.00, 1.60)),
)
def _rates(model: str, catalog: tuple[tuple[str, TokenRates], ...]) -> TokenRates | None:
    normalized = model.casefold().replace(".", "-")
    for prefix, rates in catalog:
        if normalized.startswith(prefix.casefold().replace(".", "-")):
            return rates
    return None


def _claude_rates(model: str, _occurred_at: float) -> TokenRates | None:
    return _rates(model, CLAUDE_RATES)


def _codex_rates(
    model: str, context_input_tokens: int
) -> tuple[TokenRates | None, str]:
    if context_input_tokens > CODEX_LONG_CONTEXT_THRESHOLD:
        long_rates = _rates(model, CODEX_LONG_CONTEXT_RATES)
        if long_rates is not None:
            return long_rates, "long-context"
    return _rates(model, CODEX_RATES), "short-context"


class _JsonlAdapter:
    id = ""
    display_name = ""

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser()

    def _files(self) -> Iterable[Path]:
        if not self.root.is_dir():
            return ()
        return self.root.rglob("*.jsonl")

    def collect(self, known_files: Mapping[str, tuple[int, int]]) -> UsageBatch:
        records: list[UsageRecord] = []
        fingerprints: dict[str, tuple[int, int]] = {}
        for path in self._files():
            try:
                details = path.stat()
            except OSError:
                continue
            file_id = _opaque(path)
            fingerprint = (details.st_mtime_ns, details.st_size)
            fingerprints[file_id] = fingerprint
            if known_files.get(file_id) == fingerprint:
                continue
            try:
                records.extend(self._read_file(path))
            except OSError:
                continue
        return UsageBatch(tuple(records), fingerprints)

    def _read_file(self, path: Path) -> Iterable[UsageRecord]:
        raise NotImplementedError


class CodexUsageHistoryAdapter(_JsonlAdapter):
    id = "codex"
    display_name = "Codex"
    revision = "standard-context-pricing-2026-08-23"

    def __init__(self, root: Path | None = None) -> None:
        super().__init__(root or Path.home() / ".codex" / "sessions")

    def _read_file(self, path: Path) -> Iterable[UsageRecord]:
        model = "unknown"
        previous = {name: 0 for name in ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens")}
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict):
                    continue
                payload = event.get("payload")
                if event.get("type") == "turn_context" and isinstance(payload, dict):
                    candidate = payload.get("model")
                    if isinstance(candidate, str) and candidate:
                        model = candidate
                    continue
                if not isinstance(payload, dict) or payload.get("type") != "token_count":
                    continue
                info = payload.get("info")
                totals = info.get("total_token_usage") if isinstance(info, dict) else None
                occurred_at = _timestamp(event.get("timestamp"))
                if not isinstance(totals, dict) or occurred_at is None:
                    continue
                current = {name: _integer(totals.get(name)) for name in previous}
                delta = {
                    name: value - previous[name] if value >= previous[name] else value
                    for name, value in current.items()
                }
                previous = current
                cached = delta["cached_input_tokens"]
                record = UsageRecord(
                    event_uid=_opaque(event.get("timestamp"), *(current.values())),
                    occurred_at=occurred_at,
                    model=model,
                    input_tokens=max(0, delta["input_tokens"] - cached),
                    cache_read_tokens=cached,
                    cache_write_tokens=delta["cache_write_input_tokens"],
                    output_tokens=delta["output_tokens"],
                )
                last_usage = info.get("last_token_usage")
                context_input_tokens = (
                    _integer(last_usage.get("input_tokens"))
                    if isinstance(last_usage, dict)
                    else record.input_tokens
                    + record.cache_read_tokens
                    + record.cache_write_tokens
                )
                rates, context_tier = _codex_rates(model, context_input_tokens)
                yield UsageRecord(
                    **{**record.__dict__, "cost_usd": rates.cost(record) if rates else None,
                       "cost_basis": (
                           f"estimated_api_price:openai-standard:{context_tier}:2026-08-23"
                           if rates
                           else None
                       )}
                )


class ClaudeUsageHistoryAdapter(_JsonlAdapter):
    id = "claude"
    display_name = "Claude"
    revision = "cache-duration-pricing-2026-08-22"

    def __init__(self, root: Path | None = None) -> None:
        super().__init__(root or Path.home() / ".claude" / "projects")

    def _read_file(self, path: Path) -> Iterable[UsageRecord]:
        latest: dict[str, UsageRecord] = {}
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict) or event.get("type") != "assistant":
                    continue
                message = event.get("message")
                if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
                    continue
                occurred_at = _timestamp(event.get("timestamp"))
                message_id = message.get("id")
                if occurred_at is None or not isinstance(message_id, str):
                    continue
                usage = message["usage"]
                model = str(message.get("model") or "unknown")
                session = str(event.get("sessionId") or path.stem)
                cache_creation = usage.get("cache_creation")
                if not isinstance(cache_creation, dict):
                    cache_creation = {}
                cache_write_5m = _integer(
                    cache_creation.get("ephemeral_5m_input_tokens")
                )
                cache_write_1h = _integer(
                    cache_creation.get("ephemeral_1h_input_tokens")
                )
                cache_write_total = _integer(usage.get("cache_creation_input_tokens"))
                record = UsageRecord(
                    event_uid=_opaque(session, message_id),
                    occurred_at=occurred_at,
                    model=model,
                    input_tokens=_integer(usage.get("input_tokens")),
                    cache_read_tokens=_integer(usage.get("cache_read_input_tokens")),
                    cache_write_tokens=max(
                        0, cache_write_total - cache_write_5m - cache_write_1h
                    ),
                    output_tokens=_integer(usage.get("output_tokens")),
                    cache_write_5m_tokens=cache_write_5m,
                    cache_write_1h_tokens=cache_write_1h,
                )
                rates = _claude_rates(model, occurred_at)
                latest[record.event_uid] = UsageRecord(
                    **{**record.__dict__, "cost_usd": rates.cost(record) if rates else None,
                       "cost_basis": "estimated_api_price" if rates else None}
                )
        return latest.values()


class UsageHistoryService:
    """Persist normalized usage and provide chart-ready provider summaries."""

    def __init__(
        self,
        state_dir: Path,
        adapters: Iterable[UsageHistoryAdapter] | None = None,
        *,
        refresh_seconds: int = HISTORY_REFRESH_SECONDS,
        clock=time.time,
    ) -> None:
        if refresh_seconds <= 0:
            raise ValueError("refresh_seconds must be positive")
        self.state_dir = state_dir.resolve()
        self.path = self.state_dir / "usage.sqlite3"
        self.adapters = tuple(
            (CodexUsageHistoryAdapter(), ClaudeUsageHistoryAdapter())
            if adapters is None
            else adapters
        )
        ids = [adapter.id for adapter in self.adapters]
        if any(not value or not value.replace("-", "").replace("_", "").isalnum() for value in ids):
            raise ValueError("usage adapter IDs must be simple names")
        if len(set(ids)) != len(ids):
            raise ValueError("usage adapter IDs must be unique")
        self.refresh_seconds = refresh_seconds
        self._clock = clock
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._refreshing = False
        self._generated_at: float | None = None
        self._errors: dict[str, str] = {}
        self._prepare_store()

    def _prepare_store(self) -> None:
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state_dir.chmod(0o700)
        details = self.state_dir.stat()
        if details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) & 0o077:
            raise ValueError("state_dir must be owned by the service user and mode 0700")
        if self.path.is_symlink():
            raise ValueError("usage database path must not be a symbolic link")
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS usage_records (
                    provider_id TEXT NOT NULL,
                    event_uid TEXT NOT NULL,
                    occurred_at REAL NOT NULL,
                    model TEXT NOT NULL,
                    input_tokens INTEGER NOT NULL,
                    cache_read_tokens INTEGER NOT NULL,
                    cache_write_tokens INTEGER NOT NULL,
                    cache_write_5m_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_write_1h_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL,
                    cost_usd REAL,
                    cost_basis TEXT,
                    PRIMARY KEY(provider_id, event_uid)
                );
                CREATE INDEX IF NOT EXISTS usage_records_time
                    ON usage_records(provider_id, occurred_at);
                CREATE TABLE IF NOT EXISTS usage_files (
                    provider_id TEXT NOT NULL,
                    file_id TEXT NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    size INTEGER NOT NULL,
                    PRIMARY KEY(provider_id, file_id)
                );
                """
            )
            columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(usage_records)")
            }
            for name in ("cache_write_5m_tokens", "cache_write_1h_tokens"):
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE usage_records ADD COLUMN {name} "
                        "INTEGER NOT NULL DEFAULT 0"
                    )
            connection.commit()
        self.path.chmod(0o600)

    def _known_files(self, provider_id: str) -> dict[str, tuple[int, int]]:
        with closing(sqlite3.connect(self.path)) as connection:
            rows = connection.execute(
                "SELECT file_id, mtime_ns, size FROM usage_files WHERE provider_id = ?",
                (provider_id,),
            ).fetchall()
        return {str(row[0]): (int(row[1]), int(row[2])) for row in rows}

    def refresh(self) -> dict[str, object]:
        with self._refresh_lock:
            with self._lock:
                self._refreshing = True
            errors: dict[str, str] = {}
            for adapter in self.adapters:
                try:
                    revision = str(getattr(adapter, "revision", "1"))
                    file_scope = f"{adapter.id}@{revision}"
                    batch = adapter.collect(self._known_files(file_scope))
                    with closing(sqlite3.connect(self.path, timeout=10.0)) as connection:
                        connection.executemany(
                            """
                            INSERT INTO usage_records(
                                provider_id, event_uid, occurred_at, model, input_tokens,
                                cache_read_tokens, cache_write_tokens,
                                cache_write_5m_tokens, cache_write_1h_tokens,
                                output_tokens, cost_usd, cost_basis
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(provider_id, event_uid) DO UPDATE SET
                                occurred_at=excluded.occurred_at, model=excluded.model,
                                input_tokens=excluded.input_tokens,
                                cache_read_tokens=excluded.cache_read_tokens,
                                cache_write_tokens=excluded.cache_write_tokens,
                                cache_write_5m_tokens=excluded.cache_write_5m_tokens,
                                cache_write_1h_tokens=excluded.cache_write_1h_tokens,
                                output_tokens=excluded.output_tokens,
                                cost_usd=excluded.cost_usd, cost_basis=excluded.cost_basis
                            """,
                            [
                                (
                                    adapter.id, record.event_uid, record.occurred_at,
                                    record.model, record.input_tokens, record.cache_read_tokens,
                                    record.cache_write_tokens,
                                    record.cache_write_5m_tokens,
                                    record.cache_write_1h_tokens,
                                    record.output_tokens,
                                    record.cost_usd, record.cost_basis,
                                )
                                for record in batch.records
                            ],
                        )
                        connection.executemany(
                            """
                            INSERT INTO usage_files(provider_id, file_id, mtime_ns, size)
                            VALUES (?, ?, ?, ?)
                            ON CONFLICT(provider_id, file_id) DO UPDATE SET
                                mtime_ns=excluded.mtime_ns, size=excluded.size
                            """,
                            [
                                (file_scope, file_id, values[0], values[1])
                                for file_id, values in batch.files.items()
                            ],
                        )
                        connection.commit()
                except Exception:
                    errors[adapter.id] = "Native usage telemetry could not be imported."
            with self._lock:
                self._generated_at = self._clock()
                self._refreshing = False
                self._errors = errors
            return self.history("7d")

    def _run(self) -> None:
        with self._lock:
            already_imported = self._generated_at is not None
        if already_imported and self._stop.wait(self.refresh_seconds):
            return
        while not self._stop.is_set():
            self.refresh()
            self._stop.wait(self.refresh_seconds)

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run, name="coordinator-usage-history", daemon=True
            )
            self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

    def history(self, range_name: str = "7d") -> dict[str, object]:
        if range_name not in HISTORY_RANGES:
            raise ValueError("range must be one of: 24h, 7d, 30d, all")
        duration, bucket_seconds = HISTORY_RANGES[range_name]
        now = self._clock()
        start = 0.0 if duration is None else now - duration
        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM usage_records WHERE occurred_at >= ? ORDER BY occurred_at",
                (start,),
            ).fetchall()
        by_provider: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            by_provider.setdefault(str(row["provider_id"]), []).append(row)
        with self._lock:
            generated_at = self._generated_at
            refreshing = self._refreshing
            errors = dict(self._errors)
        providers: list[dict[str, object]] = []
        for adapter in self.adapters:
            provider_rows = by_provider.get(adapter.id, [])
            buckets: dict[int, dict[str, object]] = {}
            totals = {
                "input_tokens": 0,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "cache_write_5m_tokens": 0,
                "cache_write_1h_tokens": 0,
                "cache_write_unclassified_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "valued_tokens": 0,
                "cost_usd": 0.0,
            }
            models: dict[str, dict[str, object]] = {}
            for row in provider_rows:
                cache_write_unclassified = int(row["cache_write_tokens"])
                cache_write_5m = int(row["cache_write_5m_tokens"])
                cache_write_1h = int(row["cache_write_1h_tokens"])
                token_total = sum(
                    int(row[name])
                    for name in ("input_tokens", "cache_read_tokens", "output_tokens")
                ) + cache_write_unclassified + cache_write_5m + cache_write_1h
                cost = float(row["cost_usd"]) if row["cost_usd"] is not None else None
                bucket = int(float(row["occurred_at"]) // bucket_seconds) * bucket_seconds
                point = buckets.setdefault(
                    bucket,
                    {"timestamp": datetime.fromtimestamp(bucket, timezone.utc).isoformat(),
                     "tokens": 0, "cost_usd": 0.0},
                )
                point["tokens"] = int(point["tokens"]) + token_total
                if cost is not None:
                    point["cost_usd"] = round(float(point["cost_usd"]) + cost, 9)
                    totals["valued_tokens"] = int(totals["valued_tokens"]) + token_total
                    totals["cost_usd"] = round(float(totals["cost_usd"]) + cost, 9)
                for name in ("input_tokens", "cache_read_tokens", "output_tokens"):
                    totals[name] = int(totals[name]) + int(row[name])
                totals["cache_write_unclassified_tokens"] = (
                    int(totals["cache_write_unclassified_tokens"])
                    + cache_write_unclassified
                )
                totals["cache_write_5m_tokens"] = (
                    int(totals["cache_write_5m_tokens"]) + cache_write_5m
                )
                totals["cache_write_1h_tokens"] = (
                    int(totals["cache_write_1h_tokens"]) + cache_write_1h
                )
                totals["cache_write_tokens"] = (
                    int(totals["cache_write_tokens"])
                    + cache_write_unclassified
                    + cache_write_5m
                    + cache_write_1h
                )
                totals["total_tokens"] = int(totals["total_tokens"]) + token_total
                model = str(row["model"])
                summary = models.setdefault(
                    model,
                    {"model": model, "tokens": 0, "valued_tokens": 0, "cost_usd": 0.0},
                )
                summary["tokens"] = int(summary["tokens"]) + token_total
                if cost is not None:
                    summary["cost_usd"] = round(float(summary["cost_usd"]) + cost, 9)
                    summary["valued_tokens"] = int(summary["valued_tokens"]) + token_total
            total_tokens = int(totals["total_tokens"])
            valued_tokens = int(totals["valued_tokens"])
            providers.append(
                {
                    "id": adapter.id,
                    "name": adapter.display_name,
                    "status": "error" if adapter.id in errors else "available",
                    "message": errors.get(adapter.id),
                    "metric": "cost" if valued_tokens else "tokens",
                    "cost_label": "Estimated API value" if valued_tokens else None,
                    "coverage_percent": (
                        round(valued_tokens * 100 / total_tokens, 1)
                        if total_tokens and valued_tokens
                        else None
                    ),
                    "totals": totals,
                    "series": list(buckets.values()),
                    "models": sorted(models.values(), key=lambda item: int(item["tokens"]), reverse=True),
                }
            )
        return {
            "generated_at": datetime.fromtimestamp(generated_at, timezone.utc).isoformat()
            if generated_at is not None else None,
            "refreshing": refreshing,
            "range": range_name,
            "from": datetime.fromtimestamp(start, timezone.utc).isoformat() if start else None,
            "to": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "bucket_seconds": bucket_seconds,
            "providers": providers,
        }
