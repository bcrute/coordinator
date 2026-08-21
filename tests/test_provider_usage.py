"""Provider usage collection and HTTP contract tests."""

from __future__ import annotations

import io
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from starlette.testclient import TestClient

from coordinator.authenticated_web_app import create_authenticated_app
from coordinator.provider_usage import (
    ProviderUsageError,
    ProviderUsageService,
    collect_claude_usage,
    collect_codex_usage,
)
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
    @mock.patch("coordinator.provider_usage.subprocess.Popen")
    def test_codex_uses_app_server_and_returns_remaining_windows(self, popen) -> None:
        process = FakeCodexProcess()
        popen.return_value = process

        payload = collect_codex_usage(command="/usr/bin/codex-test")

        popen.assert_called_once()
        self.assertEqual(
            popen.call_args.args[0], ["/usr/bin/codex-test", "app-server", "--stdio"]
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
                ("GPT-5.3-Codex-Spark · Session (5h)", 98.0),
                ("GPT-5.3-Codex-Spark · Weekly (7d)", 97.0),
            ],
        )
        self.assertEqual(
            [window["duration_minutes"] for window in payload["windows"]],
            [300, 10080, 300, 10080],
        )

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


class ServiceTests(unittest.TestCase):
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
