"""Provider usage collection and HTTP contract tests."""

from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import tempfile
import threading
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from starlette.testclient import TestClient

from coordinator.authenticated_web_app import create_authenticated_app
from coordinator.provider_usage import (
    ProviderUsageError,
    ProviderUsageService,
    ProviderUsageVelocityStore,
    _claude_windows,
    _codex_windows,
    _read_claude_usage,
    _rolling_velocity_forecast,
    collect_claude_usage,
    collect_codex_usage,
)
from coordinator.process_guard import guarded_command
from coordinator.security import LocalSettings


class FakeCodexProcess:
    def __init__(self) -> None:
        self.stdin = io.StringIO()
        default = {
            "limitId": "codex",
            "planType": "pro",
            "primary": {
                "usedPercent": 27,
                "windowDurationMins": 300,
                "resetsAt": 1787334983,
            },
            "secondary": {
                "usedPercent": 41,
                "windowDurationMins": 10080,
                "resetsAt": 1787921783,
            },
        }
        special = {
            "limitId": "codex_special",
            "limitName": "GPT-5.3-Codex-Spark",
            "planType": "pro",
            "primary": {
                "usedPercent": 2,
                "windowDurationMins": 300,
                "resetsAt": 1787335983,
            },
            "secondary": {
                "usedPercent": 3,
                "windowDurationMins": 10080,
                "resetsAt": 1787922783,
            },
        }
        self.stdout = io.StringIO(
            json.dumps({"id": 1, "result": {"userAgent": "test"}})
            + "\n"
            + json.dumps({"method": "ignored", "params": {}})
            + "\n"
            + json.dumps(
                {
                    "id": 2,
                    "result": {
                        "rateLimits": default,
                        "rateLimitsByLimitId": {
                            "codex": default,
                            "codex_special": special,
                        },
                    },
                }
            )
            + "\n"
        )
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        return 0


class CollectorTests(unittest.TestCase):
    def test_provider_window_normalization_clamps_values_and_labels_custom_limits(
        self,
    ) -> None:
        codex = _codex_windows(
            {
                "rateLimitsByLimitId": {
                    "special": {
                        "limitId": "special",
                        "limitName": "Review model",
                        "primary": {
                            "usedPercent": 140,
                            "windowDurationMins": 1440,
                            "resetsAt": "not-a-date",
                        },
                        "individualLimit": {
                            "remainingPercent": 120,
                            "resetsAt": 0,
                        },
                    }
                }
            }
        )
        self.assertEqual(
            [(window["label"], window["remaining_percent"]) for window in codex],
            [("Review model · 1-day", 0.0), ("Review model · Spend", 100.0)],
        )
        self.assertIsNone(codex[0]["resets_at"])
        self.assertEqual(codex[1]["resets_at"], "1970-01-01T00:00:00+00:00")

        claude = _claude_windows(
            {
                "limits": [
                    {
                        "kind": "weekly_scoped",
                        "group": "weekly",
                        "percent": -10,
                        "scope": {"surface": "claude_code"},
                    },
                    {"kind": "ignored", "percent": True},
                ]
            }
        )
        self.assertEqual(len(claude), 1)
        self.assertEqual(claude[0]["label"], "Claude Code")
        self.assertEqual(claude[0]["remaining_percent"], 100.0)

    def test_claude_window_identity_is_stable_when_provider_order_changes(self) -> None:
        limits = [
            {
                "kind": "weekly_scoped",
                "group": "weekly",
                "percent": 10,
                "scope": {"model": {"display_name": "Fable"}},
            },
            {
                "kind": "weekly_scoped",
                "group": "weekly",
                "percent": 20,
                "scope": {"model": {"display_name": "Opus"}},
            },
        ]
        first = {window["label"]: window["id"] for window in _claude_windows({"limits": limits})}
        second = {
            window["label"]: window["id"]
            for window in _claude_windows({"limits": list(reversed(limits))})
        }
        self.assertEqual(first, second)

    @mock.patch("coordinator.provider_usage.subprocess.Popen")
    def test_codex_uses_app_server_and_returns_remaining_windows(self, popen) -> None:
        process = FakeCodexProcess()
        popen.return_value = process

        payload = collect_codex_usage(command="/usr/bin/codex-test")

        popen.assert_called_once()
        self.assertEqual(
            popen.call_args.args[0],
            guarded_command(["/usr/bin/codex-test", "app-server", "--stdio"]),
        )
        self.assertIn('"account/rateLimits/read"', process.stdin.getvalue())
        self.assertNotIn("exec", process.stdin.getvalue())
        self.assertTrue(process.terminated)
        self.assertEqual(payload["remaining_percent"], 59.0)
        self.assertEqual(
            [(window["label"], window["remaining_percent"]) for window in payload["windows"]],
            [
                ("Session (5h)", 73.0),
                ("Weekly (7d)", 59.0),
            ],
        )
        self.assertEqual(
            [window["duration_minutes"] for window in payload["windows"]],
            [300, 10080],
        )
        self.assertNotIn("Spark", json.dumps(payload["windows"]))

    @mock.patch("coordinator.provider_usage._read_claude_usage")
    @mock.patch("coordinator.provider_usage.subprocess.run")
    def test_claude_checks_cli_auth_then_reads_subscription_usage(
        self, run, read_usage
    ) -> None:
        run.return_value = subprocess.CompletedProcess(
            ["claude", "auth", "status", "--json"],
            0,
            stdout=json.dumps(
                {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max"}
            ),
            stderr="",
        )
        read_usage.return_value = {
            "five_hour": {"utilization": 20, "resets_at": "2026-08-21T14:00:00Z"},
            "seven_day": {"utilization": 17, "resets_at": "2026-08-27T21:00:00Z"},
            "limits": [
                {
                    "group": "session",
                    "kind": "session",
                    "percent": 20,
                    "resets_at": "2026-08-21T14:00:00Z",
                    "is_active": True,
                    "severity": "normal",
                    "scope": None,
                },
                {
                    "group": "weekly",
                    "kind": "weekly_all",
                    "percent": 17,
                    "resets_at": "2026-08-27T21:00:00Z",
                    "is_active": False,
                    "severity": "normal",
                    "scope": None,
                },
                {
                    "group": "weekly",
                    "kind": "weekly_scoped",
                    "percent": 0,
                    "resets_at": None,
                    "is_active": False,
                    "severity": "normal",
                    "scope": {"model": {"display_name": "Fable", "id": None}},
                },
            ],
        }
        with tempfile.TemporaryDirectory() as temporary:
            credential_file = Path(temporary) / ".credentials.json"
            credential_file.write_text(
                json.dumps({"claudeAiOauth": {"accessToken": "test-secret-token"}}),
                encoding="utf-8",
            )
            payload = collect_claude_usage(
                command="/usr/bin/claude-test", credentials_path=credential_file
            )

        run.assert_called_once_with(
            ["/usr/bin/claude-test", "auth", "status", "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=10.0,
            check=False,
        )
        read_usage.assert_called_once_with("test-secret-token", 10.0)
        self.assertEqual(payload["remaining_percent"], 80.0)
        self.assertEqual(payload["plan"], "max")
        self.assertEqual(
            [(window["label"], window["remaining_percent"]) for window in payload["windows"]],
            [("Session", 80.0), ("Weekly", 83.0), ("Fable", 100.0)],
        )
        self.assertEqual(
            [window["duration_minutes"] for window in payload["windows"]],
            [300, 10080, 10080],
        )
        self.assertNotIn("test-secret-token", json.dumps(payload))

    @mock.patch("coordinator.provider_usage._read_claude_usage")
    @mock.patch("coordinator.provider_usage.subprocess.run")
    def test_claude_legacy_windows_include_named_model_limits(
        self, run, read_usage
    ) -> None:
        run.return_value = subprocess.CompletedProcess(
            ["claude", "auth", "status", "--json"],
            0,
            stdout=json.dumps(
                {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "pro"}
            ),
            stderr="",
        )
        read_usage.return_value = {
            "five_hour": {"utilization": 10},
            "seven_day": {"utilization": 20},
            "seven_day_opus": {"utilization": 30},
        }
        with tempfile.TemporaryDirectory() as temporary:
            credential_file = Path(temporary) / ".credentials.json"
            credential_file.write_text(
                json.dumps({"claudeAiOauth": {"accessToken": "test-secret-token"}}),
                encoding="utf-8",
            )
            payload = collect_claude_usage(
                command="/usr/bin/claude-test", credentials_path=credential_file
            )

        self.assertEqual(
            [(window["label"], window["remaining_percent"]) for window in payload["windows"]],
            [("Session", 90.0), ("Weekly", 80.0), ("Opus", 70.0)],
        )

    @mock.patch("coordinator.provider_usage.subprocess.run")
    def test_claude_api_key_login_reports_subscription_usage_unavailable(self, run) -> None:
        run.return_value = subprocess.CompletedProcess(
            ["claude", "auth", "status", "--json"],
            0,
            stdout=json.dumps(
                {"loggedIn": True, "authMethod": "api_key", "subscriptionType": None}
            ),
            stderr="",
        )
        with self.assertRaisesRegex(ProviderUsageError, "claude.ai subscriptions"):
            collect_claude_usage(command="/usr/bin/claude-test")

    @mock.patch("coordinator.provider_usage.subprocess.Popen")
    def test_codex_start_stream_and_protocol_failures_are_bounded(self, popen) -> None:
        popen.side_effect = OSError("private launch detail")
        with self.assertRaisesRegex(ProviderUsageError, "could not be started"):
            collect_codex_usage(command="/usr/bin/codex-test")

        streamless = mock.Mock(stdin=None, stdout=None)
        popen.side_effect = None
        popen.return_value = streamless
        with self.assertRaisesRegex(ProviderUsageError, "streams are unavailable"):
            collect_codex_usage(command="/usr/bin/codex-test")
        streamless.kill.assert_called_once_with()

        rejected = FakeCodexProcess()
        rejected.stdout = io.StringIO(json.dumps({"id": 1, "error": {}}) + "\n")
        popen.return_value = rejected
        with self.assertRaisesRegex(ProviderUsageError, "rejected the usage request"):
            collect_codex_usage(command="/usr/bin/codex-test")
        self.assertTrue(rejected.terminated)

    @mock.patch("coordinator.provider_usage._read_claude_usage")
    @mock.patch("coordinator.provider_usage.subprocess.run")
    def test_claude_rejects_invalid_auth_and_missing_oauth_credentials(
        self, run, read_usage
    ) -> None:
        run.return_value = subprocess.CompletedProcess(
            ["claude", "auth", "status", "--json"], 0, stdout="not-json", stderr=""
        )
        with self.assertRaisesRegex(ProviderUsageError, "status was invalid"):
            collect_claude_usage(command="/usr/bin/claude-test")

        run.return_value = subprocess.CompletedProcess(
            ["claude", "auth", "status", "--json"],
            0,
            stdout=json.dumps({"loggedIn": True, "authMethod": "claude.ai"}),
            stderr="",
        )
        with tempfile.TemporaryDirectory() as temporary:
            credentials = Path(temporary) / "credentials.json"
            credentials.write_text("{}", encoding="utf-8")
            with self.assertRaisesRegex(ProviderUsageError, "credentials are unavailable"):
                collect_claude_usage(
                    command="/usr/bin/claude-test", credentials_path=credentials
                )
        read_usage.assert_not_called()

    def test_claude_http_errors_and_response_bounds_have_safe_messages(self) -> None:
        for status, expected in (
            (401, "auth login"),
            (429, "rate limited"),
            (500, "HTTP 500"),
        ):
            failure = urllib.error.HTTPError(
                "https://example.invalid", status, "failure", {}, None
            )
            try:
                with self.subTest(status=status), mock.patch(
                    "coordinator.provider_usage.urllib.request.urlopen",
                    side_effect=failure,
                ):
                    with self.assertRaisesRegex(ProviderUsageError, expected):
                        _read_claude_usage("secret", 1.0)
            finally:
                failure.close()

        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"x" * (1024 * 1024 + 1)
        with mock.patch(
            "coordinator.provider_usage.urllib.request.urlopen", return_value=response
        ):
            with self.assertRaisesRegex(ProviderUsageError, "unexpectedly large"):
                _read_claude_usage("secret", 1.0)

        response.__enter__.return_value.read.return_value = b"[]"
        with mock.patch(
            "coordinator.provider_usage.urllib.request.urlopen", return_value=response
        ):
            with self.assertRaisesRegex(ProviderUsageError, "invalid usage data"):
                _read_claude_usage("secret", 1.0)


class ServiceTests(unittest.TestCase):
    def test_rolling_velocity_uses_recent_smoothed_slope_and_sustainable_ratio(self) -> None:
        now = 1_000_000.0
        forecast = _rolling_velocity_forecast(
            {
                "remaining_percent": 50.0,
                "duration_minutes": 10_080,
                "resets_at": datetime.fromtimestamp(
                    now + 4 * 3600, timezone.utc
                ).isoformat(),
            },
            [
                (now - 5 * 3600, 70.0),
                (now - 4 * 3600, 66.0),
                (now - 3 * 3600, 62.0),
                (now - 2 * 3600, 58.0),
                (now - 1 * 3600, 54.0),
                (now, 50.0),
            ],
            now,
        )
        self.assertEqual(forecast["method"], "rolling_velocity")
        self.assertEqual(forecast["burn_rate_percent_per_hour"], 4.0)
        self.assertEqual(forecast["sustainable_rate_percent_per_hour"], 12.5)
        self.assertEqual(forecast["velocity_ratio"], 0.32)
        self.assertEqual(forecast["projected_remaining"], 34.0)
        self.assertEqual(forecast["confidence"], "high")

    def test_velocity_history_survives_restart_and_never_crosses_a_reset(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "state"
            now = [1_000_000.0]
            remaining = [80.0]
            reset = [
                datetime.fromtimestamp(now[0] + 24 * 3600, timezone.utc).isoformat()
            ]

            def collect():
                return {
                    "id": "codex",
                    "name": "Codex",
                    "status": "available",
                    "remaining_percent": remaining[0],
                    "windows": [
                        {
                            "id": "codex:weekly",
                            "label": "Weekly",
                            "remaining_percent": remaining[0],
                            "duration_minutes": 10_080,
                            "resets_at": reset[0],
                        }
                    ],
                }

            service = ProviderUsageService(
                3600,
                collectors={"codex": collect},
                clock=lambda: now[0],
                state_dir=state_dir,
            )
            first = service.refresh()["providers"][0]["windows"][0]["forecast"]
            self.assertEqual(first["method"], "reset_average")

            now[0] += 3600
            remaining[0] = 76.0
            rolling = service.refresh()["providers"][0]["windows"][0]["forecast"]
            self.assertEqual(rolling["method"], "rolling_velocity")
            self.assertEqual(rolling["burn_rate_percent_per_hour"], 4.0)
            self.assertEqual(rolling["sample_count"], 2)

            now[0] += 3600
            remaining[0] = 100.0
            reset[0] = datetime.fromtimestamp(
                now[0] + 7 * 24 * 3600, timezone.utc
            ).isoformat()
            after_reset = service.refresh()["providers"][0]["windows"][0]["forecast"]
            self.assertEqual(after_reset["method"], "unavailable")
            self.assertIsNone(after_reset["projected_remaining"])

            now[0] += 3600
            remaining[0] = 96.0
            restarted = ProviderUsageService(
                3600,
                collectors={"codex": collect},
                clock=lambda: now[0],
                state_dir=state_dir,
            )
            restored = restarted.refresh()["providers"][0]["windows"][0]["forecast"]
            self.assertEqual(restored["method"], "rolling_velocity")
            self.assertEqual(restored["sample_count"], 2)
            self.assertEqual(restored["burn_rate_percent_per_hour"], 4.0)

    def test_fractional_reset_jitter_shares_velocity_history_and_migrates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            state_dir = Path(temporary) / "state"
            store = ProviderUsageVelocityStore(state_dir)
            with sqlite3.connect(store.path) as connection:
                connection.executemany(
                    """
                    INSERT INTO provider_limit_observations(
                        provider_id, window_id, reset_key, observed_at,
                        remaining_percent
                    ) VALUES ('claude', 'weekly', ?, ?, ?)
                    """,
                    [
                        ("2026-08-27T21:00:00.431377+00:00", 1_000_000.0, 45.0),
                        ("2026-08-27T21:00:00.272086+00:00", 1_003_600.0, 43.0),
                    ],
                )
                connection.commit()

            migrated = ProviderUsageVelocityStore(state_dir)
            provider = migrated.enrich(
                {
                    "id": "claude",
                    "windows": [
                        {
                            "id": "weekly",
                            "remaining_percent": 42.0,
                            "duration_minutes": 10_080,
                            "resets_at": "2026-08-27T21:00:00.526340+00:00",
                        }
                    ],
                },
                1_007_200.0,
            )

            forecast = provider["windows"][0]["forecast"]
            self.assertEqual(forecast["method"], "rolling_velocity")
            self.assertEqual(forecast["sample_count"], 3)
            with sqlite3.connect(migrated.path) as connection:
                reset_keys = connection.execute(
                    "SELECT DISTINCT reset_key FROM provider_limit_observations"
                ).fetchall()
            self.assertEqual(reset_keys, [("2026-08-27T21:00:00+00:00",)])

    def test_velocity_store_failure_keeps_the_reset_average_fallback(self) -> None:
        now = 1_000_000.0

        def collect():
            return {
                "id": "codex",
                "name": "Codex",
                "status": "available",
                "remaining_percent": 75.0,
                "windows": [
                    {
                        "id": "codex:weekly",
                        "remaining_percent": 75.0,
                        "duration_minutes": 10_080,
                        "resets_at": datetime.fromtimestamp(
                            now + 5 * 24 * 3600, timezone.utc
                        ).isoformat(),
                    }
                ],
            }

        with tempfile.TemporaryDirectory() as temporary:
            service = ProviderUsageService(
                3600,
                collectors={"codex": collect},
                clock=lambda: now,
                state_dir=Path(temporary) / "state",
            )
            with mock.patch.object(
                service._velocity_store,
                "enrich",
                side_effect=sqlite3.OperationalError("locked"),
            ):
                provider = service.refresh()["providers"][0]
        self.assertEqual(provider["forecast_history_status"], "unavailable")
        self.assertEqual(provider["windows"][0]["forecast"]["method"], "reset_average")

    def test_refresh_is_shared_and_provider_failures_are_bounded(self) -> None:
        def codex():
            return {
                "id": "codex",
                "name": "Codex",
                "status": "available",
                "remaining_percent": 72.0,
                "windows": [],
            }

        def claude():
            raise ProviderUsageError("Claude CLI is not logged in.")

        service = ProviderUsageService(
            3600, collectors={"codex": codex, "claude": claude}, clock=lambda: 1000.0
        )
        payload = service.refresh()

        self.assertEqual(payload["generated_at"], "1970-01-01T00:16:40+00:00")
        self.assertEqual(payload["next_refresh_at"], "1970-01-01T01:16:40+00:00")
        self.assertFalse(payload["refreshing"])
        self.assertEqual(payload["providers"][0]["remaining_percent"], 72.0)
        self.assertEqual(payload["providers"][1]["status"], "unavailable")
        self.assertEqual(payload["providers"][1]["message"], "Claude CLI is not logged in.")

    def test_snapshot_is_isolated_and_unexpected_failures_are_redacted(self) -> None:
        def broken():
            raise RuntimeError("secret provider detail")

        service = ProviderUsageService(60, collectors={"codex": broken}, clock=lambda: 1.0)
        initial = service.snapshot()
        initial["providers"].clear()
        self.assertEqual(len(service.snapshot()["providers"]), 1)

        payload = service.refresh()
        provider = payload["providers"][0]
        self.assertEqual(provider["status"], "unavailable")
        self.assertNotIn("secret provider detail", str(provider["message"]))

        with self.assertRaisesRegex(ValueError, "must be positive"):
            ProviderUsageService(0)

    def test_refresh_failure_retains_last_successful_provider_values_as_stale(self) -> None:
        now = [1000.0]
        failing = [False]

        def codex():
            if failing[0]:
                raise ProviderUsageError("Codex usage request timed out.")
            return {
                "id": "codex",
                "name": "Codex",
                "status": "available",
                "plan": "pro",
                "remaining_percent": 32.0,
                "windows": [
                    {"label": "Weekly (7d)", "remaining_percent": 32.0}
                ],
            }

        service = ProviderUsageService(
            3600, collectors={"codex": codex}, clock=lambda: now[0]
        )
        current = service.refresh()["providers"][0]
        failing[0] = True
        now[0] = 2000.0

        stale = service.refresh()["providers"][0]

        self.assertEqual(stale["status"], "stale")
        self.assertTrue(stale["stale"])
        self.assertEqual(stale["plan"], "pro")
        self.assertEqual(stale["remaining_percent"], 32.0)
        self.assertEqual(stale["windows"], current["windows"])
        self.assertEqual(stale["message"], "Codex usage request timed out.")
        self.assertEqual(stale["last_success_at"], "1970-01-01T00:16:40+00:00")
        self.assertEqual(stale["last_error_at"], "1970-01-01T00:33:20+00:00")

    def test_success_after_stale_refresh_replaces_retained_values(self) -> None:
        remaining = [70.0]
        failing = [False]
        now = [1000.0]

        def codex():
            if failing[0]:
                raise ProviderUsageError("temporary failure")
            return {
                "id": "codex",
                "name": "Codex",
                "status": "available",
                "remaining_percent": remaining[0],
                "windows": [],
            }

        service = ProviderUsageService(
            60, collectors={"codex": codex}, clock=lambda: now[0]
        )
        service.refresh()
        failing[0] = True
        service.refresh()
        failing[0] = False
        remaining[0] = 65.0
        now[0] = 3000.0

        recovered = service.refresh()["providers"][0]

        self.assertEqual(recovered["status"], "available")
        self.assertFalse(recovered["stale"])
        self.assertEqual(recovered["remaining_percent"], 65.0)
        self.assertEqual(recovered["last_success_at"], "1970-01-01T00:50:00+00:00")
        self.assertIsNone(recovered["last_error_at"])

    def test_background_refresh_starts_once_and_shutdown_is_bounded(self) -> None:
        called = threading.Event()
        calls: list[int] = []

        def collect():
            calls.append(1)
            called.set()
            return {
                "id": "codex",
                "name": "Codex",
                "status": "available",
                "remaining_percent": 100.0,
                "windows": [],
            }

        service = ProviderUsageService(60, collectors={"codex": collect})
        service.start()
        service.start()
        self.assertTrue(called.wait(timeout=2.0))
        service.shutdown()
        self.assertEqual(len(calls), 1)
        self.assertFalse(service.snapshot()["refreshing"])


class FakeUsageService:
    def __init__(self) -> None:
        self.started = 0
        self.refreshed = 0
        self.stopped = 0

    def _payload(self) -> dict[str, object]:
        return {
            "generated_at": "2026-08-21T13:00:00+00:00",
            "next_refresh_at": "2026-08-21T14:00:00+00:00",
            "refresh_interval_seconds": 3600,
            "refreshing": False,
            "providers": [
                {
                    "id": "codex",
                    "name": "Codex",
                    "status": "available",
                    "remaining_percent": 70.0,
                    "windows": [],
                }
            ],
        }

    def start(self) -> None:
        self.started += 1

    def snapshot(self) -> dict[str, object]:
        return self._payload()

    def refresh(self) -> dict[str, object]:
        self.refreshed += 1
        return self._payload()

    def shutdown(self) -> None:
        self.stopped += 1


class ProviderUsageEndpointTests(unittest.TestCase):
    def test_cached_read_and_csrf_protected_manual_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo = root / "repo"
            repo.mkdir()
            (repo / ".git").mkdir()
            usage = FakeUsageService()
            settings = LocalSettings(
                external_url="http://127.0.0.1",
                state_dir=root / "state",
                trusted_hosts=("127.0.0.1",),
            )
            app = create_authenticated_app(
                repo,
                settings,
                repositories_root=root,
                provider_usage_service=usage,
            )
            with TestClient(app, base_url="http://127.0.0.1") as client:
                response = client.get("/api/v1/provider-usage")
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["providers"][0]["remaining_percent"], 70.0)
                self.assertEqual(usage.started, 1)

                csrf = client.get("/api/state").json()["security"]["csrf_token"]
                self.assertEqual(client.post("/api/provider-usage/refresh").status_code, 403)
                response = client.post(
                    "/api/provider-usage/refresh",
                    headers={
                        "X-CSRF-Token": csrf,
                        "Origin": "http://127.0.0.1",
                    },
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(usage.refreshed, 1)
            self.assertEqual(usage.stopped, 1)


if __name__ == "__main__":
    unittest.main()
