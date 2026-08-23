"""Historical usage adapter, valuation, persistence, and aggregation tests."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import dataclass
from pathlib import Path

from starlette.testclient import TestClient

from coordinator.authenticated_web_app import create_authenticated_app
from coordinator.security import LocalSettings
from coordinator.usage_history import (
    ClaudeUsageHistoryAdapter,
    CodexUsageHistoryAdapter,
    UsageBatch,
    UsageHistoryService,
    UsageRecord,
)


def jsonl(path: Path, events: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")


class NativeUsageAdapterTests(unittest.TestCase):
    def test_codex_uses_cumulative_deltas_deduplicates_copied_events_and_estimates_value(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            events = [
                {
                    "timestamp": "2026-08-22T12:00:00Z",
                    "type": "turn_context",
                    "payload": {"model": "gpt-5.3-codex"},
                },
                {
                    "timestamp": "2026-08-22T12:00:01Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 100,
                                "cached_input_tokens": 40,
                                "cache_write_input_tokens": 0,
                                "output_tokens": 10,
                            }
                        },
                    },
                },
                {
                    "timestamp": "2026-08-22T12:00:02Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 150,
                                "cached_input_tokens": 60,
                                "cache_write_input_tokens": 0,
                                "output_tokens": 20,
                            }
                        },
                    },
                },
            ]
            jsonl(root / "one.jsonl", events)
            jsonl(root / "copied.jsonl", events)

            records = CodexUsageHistoryAdapter(root).collect({}).records

            self.assertEqual(len(records), 4)
            unique = {record.event_uid: record for record in records}
            self.assertEqual(len(unique), 2)
            first = unique[next(uid for uid, value in unique.items() if value.input_tokens == 60)]
            self.assertEqual(first.cache_read_tokens, 40)
            self.assertEqual(first.output_tokens, 10)
            self.assertAlmostEqual(first.cost_usd or 0, 0.000252)
            second = next(value for value in unique.values() if value.input_tokens == 30)
            self.assertEqual(second.cache_read_tokens, 20)
            self.assertEqual(second.output_tokens, 10)

    def test_codex_5_6_uses_published_standard_context_prices(self) -> None:
        cases = (
            ("gpt-5.6-sol", 100_000, 40_000, 10_000, 0.456),
            ("gpt-5.6-terra", 100_000, 40_000, 10_000, 0.248),
            ("gpt-5.6-luna", 100_000, 40_000, 10_000, 0.0248),
            ("gpt-5.6-sol", 300_000, 100_000, 10_000, 1.98),
            ("gpt-5.6-terra", 300_000, 100_000, 10_000, 1.02),
            ("gpt-5.6-luna", 300_000, 100_000, 10_000, 0.102),
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index, case in enumerate(cases):
                model, input_tokens, cached_tokens, output_tokens, _ = case
                usage = {
                    "input_tokens": input_tokens,
                    "cached_input_tokens": cached_tokens,
                    "cache_write_input_tokens": 0,
                    "output_tokens": output_tokens,
                }
                jsonl(
                    root / f"{index}.jsonl",
                    [
                        {
                            "timestamp": f"2026-08-23T12:00:{index:02d}Z",
                            "type": "turn_context",
                            "payload": {"model": model},
                        },
                        {
                            "timestamp": f"2026-08-23T12:01:{index:02d}Z",
                            "type": "event_msg",
                            "payload": {
                                "type": "token_count",
                                "info": {
                                    "last_token_usage": usage,
                                    "total_token_usage": usage,
                                },
                            },
                        },
                    ],
                )

            records = CodexUsageHistoryAdapter(root).collect({}).records

            self.assertEqual(len(records), len(cases))
            records_by_timestamp = sorted(records, key=lambda record: record.occurred_at)
            for record, case in zip(records_by_timestamp, cases, strict=True):
                model, _, _, _, expected_cost = case
                with self.subTest(model=model, expected_cost=expected_cost):
                    self.assertEqual(record.model, model)
                    self.assertAlmostEqual(record.cost_usd or 0, expected_cost)
                    expected_tier = (
                        "long-context" if case[1] > 272_000 else "short-context"
                    )
                    self.assertIn(expected_tier, record.cost_basis or "")

    def test_claude_keeps_last_usage_update_per_message_and_preserves_unknown_models(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "project" / "session.jsonl"
            base = {
                "type": "assistant",
                "sessionId": "session-1",
                "timestamp": "2026-08-22T12:00:00Z",
                "message": {
                    "id": "message-1",
                    "model": "claude-opus-5",
                    "usage": {
                        "input_tokens": 2,
                        "cache_creation_input_tokens": 150,
                        "cache_creation": {
                            "ephemeral_5m_input_tokens": 50,
                            "ephemeral_1h_input_tokens": 100,
                        },
                        "cache_read_input_tokens": 1000,
                        "output_tokens": 20,
                    },
                },
            }
            updated = json.loads(json.dumps(base))
            updated["timestamp"] = "2026-08-22T12:00:02Z"
            updated["message"]["usage"]["output_tokens"] = 30
            unknown = json.loads(json.dumps(base))
            unknown["timestamp"] = "2026-08-22T12:00:03Z"
            unknown["message"]["id"] = "message-2"
            unknown["message"]["model"] = "local-unknown"
            jsonl(path, [base, updated, unknown, {"type": "user", "message": {}}])

            records = ClaudeUsageHistoryAdapter(root).collect({}).records

            self.assertEqual(len(records), 2)
            priced = next(record for record in records if record.model == "claude-opus-5")
            self.assertEqual(priced.output_tokens, 30)
            self.assertEqual(priced.cache_write_tokens, 0)
            self.assertEqual(priced.cache_write_5m_tokens, 50)
            self.assertEqual(priced.cache_write_1h_tokens, 100)
            self.assertAlmostEqual(priced.cost_usd or 0, 0.0025725)
            unpriced = next(record for record in records if record.model == "local-unknown")
            self.assertIsNone(unpriced.cost_usd)

    def test_claude_preserves_unclassified_cache_writes_when_breakdown_is_absent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            jsonl(
                root / "session.jsonl",
                [
                    {
                        "type": "assistant",
                        "sessionId": "session",
                        "timestamp": "2026-08-22T12:00:00Z",
                        "message": {
                            "id": "message",
                            "model": "claude-opus-5",
                            "usage": {
                                "cache_creation_input_tokens": 100,
                            },
                        },
                    }
                ],
            )

            record = ClaudeUsageHistoryAdapter(root).collect({}).records[0]

            self.assertEqual(record.cache_write_tokens, 100)
            self.assertEqual(record.cache_write_5m_tokens, 0)
            self.assertEqual(record.cache_write_1h_tokens, 0)
            self.assertAlmostEqual(record.cost_usd or 0, 0.000625)

    def test_claude_sonnet_five_uses_its_published_standard_price(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            events = []
            for message_id, timestamp in (
                ("before-announcement", "2026-08-31T23:59:59Z"),
                ("after-announcement", "2026-09-01T00:00:00Z"),
            ):
                events.append(
                    {
                        "type": "assistant",
                        "sessionId": "session",
                        "timestamp": timestamp,
                        "message": {
                            "id": message_id,
                            "model": "claude-sonnet-5",
                            "usage": {
                                "input_tokens": 1_000_000,
                                "cache_creation_input_tokens": 0,
                                "cache_read_input_tokens": 0,
                                "output_tokens": 1_000_000,
                            },
                        },
                    }
                )
            jsonl(root / "session.jsonl", events)

            records = ClaudeUsageHistoryAdapter(root).collect({}).records

            by_uid = {record.event_uid: record for record in records}
            costs = sorted(record.cost_usd for record in by_uid.values())
            self.assertEqual(costs, [12.0, 12.0])


@dataclass
class FakeAdapter:
    records: tuple[UsageRecord, ...]
    id: str = "local-qwen"
    display_name: str = "Local Qwen"
    calls: int = 0

    def collect(self, known_files):
        self.calls += 1
        self.known_files = dict(known_files)
        return UsageBatch(self.records, {"telemetry": (123, 456)})


class UsageHistoryServiceTests(unittest.TestCase):
    def test_custom_adapter_is_dynamic_and_falls_back_to_raw_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            adapter = FakeAdapter(
                (
                    UsageRecord(
                        "event-1",
                        1_777_000_000,
                        "qwen-27b",
                        input_tokens=100,
                        output_tokens=25,
                    ),
                )
            )
            service = UsageHistoryService(
                Path(temporary), [adapter], clock=lambda: 1_777_000_100
            )

            payload = service.refresh()
            provider = payload["providers"][0]

            self.assertEqual(provider["id"], "local-qwen")
            self.assertEqual(provider["name"], "Local Qwen")
            self.assertEqual(provider["metric"], "tokens")
            self.assertEqual(provider["totals"]["total_tokens"], 125)
            self.assertIsNone(provider["coverage_percent"])
            self.assertEqual(adapter.calls, 1)

            service.refresh()
            self.assertEqual(adapter.known_files, {"telemetry": (123, 456)})
            self.assertEqual(service.history("24h")["providers"][0]["totals"]["total_tokens"], 125)

    def test_mixed_pricing_reports_coverage_without_inventing_cost(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            adapter = FakeAdapter(
                (
                    UsageRecord("priced", 1_777_000_000, "priced", output_tokens=80, cost_usd=1.5, cost_basis="native"),
                    UsageRecord("raw", 1_777_000_001, "raw", output_tokens=20),
                )
            )
            service = UsageHistoryService(Path(temporary), [adapter], clock=lambda: 1_777_000_100)

            provider = service.refresh()["providers"][0]

            self.assertEqual(provider["metric"], "cost")
            self.assertEqual(provider["coverage_percent"], 80.0)
            self.assertEqual(provider["totals"]["cost_usd"], 1.5)

    def test_cache_write_durations_are_aggregated_without_losing_legacy_total(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            adapter = FakeAdapter(
                (
                    UsageRecord(
                        "event",
                        1_777_000_000,
                        "model",
                        cache_write_tokens=10,
                        cache_write_5m_tokens=20,
                        cache_write_1h_tokens=30,
                    ),
                )
            )
            service = UsageHistoryService(
                Path(temporary), [adapter], clock=lambda: 1_777_000_100
            )

            totals = service.refresh()["providers"][0]["totals"]

            self.assertEqual(totals["cache_write_tokens"], 60)
            self.assertEqual(totals["cache_write_unclassified_tokens"], 10)
            self.assertEqual(totals["cache_write_5m_tokens"], 20)
            self.assertEqual(totals["cache_write_1h_tokens"], 30)
            self.assertEqual(totals["total_tokens"], 60)

    def test_existing_usage_database_gains_cache_duration_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary)
            database = state_dir / "usage.sqlite3"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """
                    CREATE TABLE usage_records (
                        provider_id TEXT NOT NULL,
                        event_uid TEXT NOT NULL,
                        occurred_at REAL NOT NULL,
                        model TEXT NOT NULL,
                        input_tokens INTEGER NOT NULL,
                        cache_read_tokens INTEGER NOT NULL,
                        cache_write_tokens INTEGER NOT NULL,
                        output_tokens INTEGER NOT NULL,
                        cost_usd REAL,
                        cost_basis TEXT,
                        PRIMARY KEY(provider_id, event_uid)
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO usage_records VALUES (
                        'local-qwen', 'legacy', 1777000000, 'qwen',
                        0, 0, 12, 0, NULL, NULL
                    )
                    """
                )

            service = UsageHistoryService(
                state_dir, [FakeAdapter(())], clock=lambda: 1_777_000_100
            )
            totals = service.history("24h")["providers"][0]["totals"]

            self.assertEqual(totals["cache_write_tokens"], 12)
            self.assertEqual(totals["cache_write_unclassified_tokens"], 12)
            self.assertEqual(totals["cache_write_5m_tokens"], 0)
            self.assertEqual(totals["cache_write_1h_tokens"], 0)

    def test_rejects_bad_ranges_and_duplicate_adapter_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            first = FakeAdapter(())
            with self.assertRaisesRegex(ValueError, "unique"):
                UsageHistoryService(Path(temporary), [first, FakeAdapter(())])
            service = UsageHistoryService(Path(temporary), [])
            with self.assertRaisesRegex(ValueError, "range"):
                service.history("year")

    def test_background_import_starts_once_and_shuts_down_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            imported = threading.Event()

            class SignalingAdapter(FakeAdapter):
                def collect(self, known_files):
                    result = super().collect(known_files)
                    imported.set()
                    return result

            adapter = SignalingAdapter(())
            service = UsageHistoryService(
                Path(temporary), [adapter], refresh_seconds=60
            )

            service.start()
            service.start()
            self.assertTrue(imported.wait(2), "background import did not start")
            service.shutdown()

            self.assertEqual(adapter.calls, 1)
            self.assertFalse(service._thread and service._thread.is_alive())


class FakeHistoryService:
    def __init__(self) -> None:
        self.started = 0
        self.refreshed = 0
        self.stopped = 0

    def history(self, range_name="7d"):
        if range_name == "bad":
            raise ValueError("range must be one of: 24h, 7d, 30d, all")
        return {
            "generated_at": "2026-08-22T12:00:00+00:00",
            "refreshing": False,
            "range": range_name,
            "from": "2026-08-21T12:00:00+00:00",
            "to": "2026-08-22T12:00:00+00:00",
            "bucket_seconds": 900,
            "providers": [{"id": "custom", "name": "Custom"}],
        }

    def refresh(self):
        self.refreshed += 1
        return self.history()

    def start(self):
        self.started += 1

    def shutdown(self):
        self.stopped += 1


class UsageHistoryHttpTests(unittest.TestCase):
    def test_history_routes_use_one_dynamic_contract_and_protect_manual_import(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            repo.mkdir()
            (repo / ".git").mkdir()
            service = FakeHistoryService()
            settings = LocalSettings(
                external_url="http://127.0.0.1",
                state_dir=root / "state",
                secure_cookie=False,
                trusted_hosts=("127.0.0.1",),
            )
            app = create_authenticated_app(
                repo,
                settings,
                repositories_root=root,
                usage_history_service=service,
            )
            with TestClient(app, base_url="http://127.0.0.1") as client:
                response = client.get("/api/v1/usage-history?range=24h")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["providers"][0]["id"], "custom")
                self.assertEqual(client.get("/api/usage-history?range=bad").status_code, 400)
                self.assertEqual(
                    client.post("/api/usage-history/refresh?range=24h").status_code,
                    403,
                )
                csrf = client.get("/api/state").json()["security"]["csrf_token"]
                refreshed = client.post(
                    "/api/usage-history/refresh?range=24h",
                    headers={"X-CSRF-Token": csrf},
                )
                self.assertEqual(refreshed.status_code, 200, refreshed.text)
                self.assertEqual(service.refreshed, 1)
            self.assertEqual(service.stopped, 1)


if __name__ == "__main__":
    unittest.main()
