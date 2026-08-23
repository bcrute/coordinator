"""Useful contracts for persisted, runtime-switchable executor settings."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from starlette.testclient import TestClient

from coordinator.authenticated_web_app import LocalSettings, create_authenticated_app
from coordinator.executor_adapters import ClaudeExecutorAdapter, MiniSweAgentExecutorAdapter
from coordinator.executor_settings import (
    EXECUTOR_PREFERENCE_KEY,
    ExecutorConfiguration,
    ExecutorSettingsService,
    discover_models,
)
from coordinator.operational_store import OperationalStore
from coordinator.provider_usage import ProviderUsageService


def fake_usage_service() -> ProviderUsageService:
    return ProviderUsageService(
        3600,
        collectors={
            "fake": lambda: {
                "id": "fake",
                "name": "Fake",
                "status": "available",
                "plan": None,
                "source": "test",
                "remaining_percent": None,
                "windows": [],
                "message": None,
            }
        },
    )


def mini_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "executor_adapter": "mini-swe-agent",
        "claude_model": "opus",
        "claude_subagent_model": "sonnet",
        "claude_max_turns": 40,
        "mini_swe_model": "Qwen/Qwen3.8-27B",
        "mini_swe_api_base": "http://127.0.0.1:8000/v1",
        "mini_swe_provider": "openai",
        "mini_swe_api_key_env": "",
        "mini_swe_step_limit": 8,
        "mini_swe_cost_limit": 0,
        "mini_swe_timeout_seconds": 600,
    }
    payload.update(changes)
    return payload


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.body = json.dumps(payload).encode()

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, size: int) -> bytes:
        return self.body[:size]


class ExecutorSettingsUnitTests(unittest.TestCase):
    def test_configuration_persists_non_secret_adapter_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = OperationalStore(Path(tmp))
            service = ExecutorSettingsService(store, ClaudeExecutorAdapter())
            candidate = service.candidate(mini_payload())
            service.save(candidate)

            restored = ExecutorSettingsService(store, ClaudeExecutorAdapter())
            adapter = restored.adapter()
            self.assertIsInstance(adapter, MiniSweAgentExecutorAdapter)
            self.assertEqual(adapter.model, "Qwen/Qwen3.8-27B")
            self.assertEqual(adapter.api_key_env, "")
            self.assertEqual(
                store.preferences()[EXECUTOR_PREFERENCE_KEY]["mini_swe_api_base"],
                "http://127.0.0.1:8000/v1",
            )

    def test_validation_rejects_unknown_fields_credentials_and_bad_limits(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown fields"):
            ExecutorConfiguration.from_mapping({"unexpected": True})
        with self.assertRaisesRegex(ValueError, "embedded credentials"):
            ExecutorConfiguration.from_mapping(
                mini_payload(mini_swe_api_base="http://user:secret@host/v1")
            )
        with self.assertRaisesRegex(ValueError, "1 to 200"):
            ExecutorConfiguration.from_mapping(mini_payload(mini_swe_step_limit=0))

    def test_model_discovery_accepts_no_key_and_returns_sorted_unique_ids(self) -> None:
        response = FakeResponse(
            {"data": [{"id": "qwen-b"}, {"id": "qwen-a"}, {"id": "qwen-b"}]}
        )
        with mock.patch(
            "coordinator.executor_settings.urllib.request.urlopen",
            return_value=response,
        ) as opened:
            self.assertEqual(
                discover_models("http://heavy:8000/v1"),
                ["qwen-a", "qwen-b"],
            )
        request = opened.call_args.args[0]
        self.assertEqual(request.full_url, "http://heavy:8000/v1/models")
        self.assertNotIn("Authorization", request.headers)


class ExecutorSettingsAPITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        self.settings = LocalSettings(
            external_url="http://127.0.0.1:8765",
            state_dir=self.base / "state",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def app(self, **kwargs: object):
        return create_authenticated_app(
            self.repo,
            self.settings,
            repositories_root=self.base,
            provider_usage_service=fake_usage_service(),
            codex_command_for_repo=lambda repo: [
                sys.executable,
                "-c",
                "import time; time.sleep(60)",
            ],
            **kwargs,
        )

    @staticmethod
    def headers(csrf: str) -> dict[str, str]:
        return {"X-CSRF-Token": csrf, "Origin": "http://127.0.0.1"}

    def test_update_rebinds_future_watcher_and_survives_app_restart(self) -> None:
        app = self.app()
        with TestClient(app, base_url="http://127.0.0.1") as client:
            csrf = client.get("/api/state").json()["security"]["csrf_token"]
            response = client.post(
                "/api/executor-settings",
                json=mini_payload(),
                headers=self.headers(csrf),
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(
                response.json()["configuration"]["executor_adapter"],
                "mini-swe-agent",
            )
            command = app.state.context.watcher.command
            self.assertIn("--executor-adapter", command)
            self.assertIn("mini-swe-agent", command)
            self.assertIn("Qwen/Qwen3.8-27B", command)

        restarted = self.app()
        with TestClient(restarted, base_url="http://127.0.0.1") as client:
            response = client.get("/api/v1/executor-settings")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(
                response.json()["configuration"]["mini_swe_model"],
                "Qwen/Qwen3.8-27B",
            )
            preferences = client.get("/api/preferences").json()["preferences"]
            self.assertNotIn(EXECUTOR_PREFERENCE_KEY, preferences)

    def test_running_watcher_blocks_changes_and_discovery_is_bounded(self) -> None:
        coordination = self.repo / ".coordination"
        coordination.mkdir()
        (coordination / "README.md").write_text("# Coordination\n", encoding="utf-8")
        app = self.app(
            watcher_command_for_repo=lambda repo: [
                sys.executable,
                "-c",
                "import time; time.sleep(60)",
            ],
            start_grace=0.01,
        )
        with TestClient(app, base_url="http://127.0.0.1") as client:
            csrf = client.get("/api/state").json()["security"]["csrf_token"]
            self.assertEqual(
                client.post("/api/watcher/start", headers=self.headers(csrf)).status_code,
                200,
            )
            blocked = client.post(
                "/api/v1/executor-settings",
                json=mini_payload(),
                headers=self.headers(csrf),
            )
            self.assertEqual(blocked.status_code, 409, blocked.text)
            self.assertEqual(blocked.json()["error"]["code"], "conflict")

            with mock.patch(
                "coordinator.authenticated_web_app.discover_models",
                return_value=["Qwen/Qwen3.8-27B"],
            ):
                discovered = client.post(
                    "/api/executor-settings/discover",
                    json={"api_base": "http://127.0.0.1:8000/v1"},
                    headers=self.headers(csrf),
                )
            self.assertEqual(discovered.status_code, 200, discovered.text)
            self.assertEqual(discovered.json()["models"], ["Qwen/Qwen3.8-27B"])


if __name__ == "__main__":
    unittest.main()
