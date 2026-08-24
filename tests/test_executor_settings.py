"""Useful contracts for persisted, runtime-switchable executor settings."""

from __future__ import annotations

import io
import json
import os
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
    discover_claude_models,
    discover_codex_models,
    discover_models,
    load_project_executor_settings,
    project_executor_settings_path,
    publish_project_executor_settings,
)
from coordinator.operational_store import OperationalStore
from coordinator.provider_usage import ProviderUsageService
from coordinator.web_app import default_codex_command, default_codex_resume_command


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
        "codex_model": "gpt-5.6-sol",
        "codex_effort": "max",
        "codex_sandbox": "danger-full-access",
        "codex_approval_policy": "never",
        "executor_adapter": "mini-swe-agent",
        "claude_model": "opus",
        "claude_effort": "high",
        "claude_subagent_model": "sonnet",
        "claude_subagent_effort": "medium",
        "claude_max_turns": 40,
        "claude_local_delegation": False,
        "mini_swe_model": "Qwen/Qwen3.8-27B",
        "mini_swe_effort": "low",
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
    def test_project_snapshot_is_bounded_non_secret_and_atomically_replaceable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".coordination/runtime").mkdir(parents=True)
            (repo / ".coordination/README.md").write_text("ready\n", encoding="utf-8")
            first = ExecutorConfiguration()
            path = publish_project_executor_settings(repo, first)
            self.assertEqual(path, project_executor_settings_path(repo))
            self.assertEqual(load_project_executor_settings(repo), first)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("secret", path.read_text(encoding="utf-8").lower())

            second = ExecutorConfiguration(
                executor_adapter="mini-swe-agent",
                mini_swe_model="Qwen/Qwen3.8-27B",
            )
            publish_project_executor_settings(repo, second)
            self.assertEqual(load_project_executor_settings(repo), second)
            self.assertEqual(list(path.parent.glob(".*.tmp")), [])
            publish_project_executor_settings(repo, first, replace=False)
            self.assertEqual(load_project_executor_settings(repo), second)

    def test_project_snapshot_refuses_uninitialized_and_symlink_targets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            self.assertIsNone(
                publish_project_executor_settings(repo, ExecutorConfiguration())
            )
            (repo / ".coordination/runtime").mkdir(parents=True)
            (repo / ".coordination/README.md").write_text("ready\n", encoding="utf-8")
            outside = Path(tmp) / "outside.json"
            outside.write_text("untouched\n", encoding="utf-8")
            path = project_executor_settings_path(repo)
            os.symlink(outside, path)
            with self.assertRaisesRegex(ValueError, "symbolic link"):
                publish_project_executor_settings(repo, ExecutorConfiguration())
            self.assertEqual(outside.read_text(encoding="utf-8"), "untouched\n")

    def test_project_snapshot_rejects_malformed_and_oversized_documents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".coordination/runtime").mkdir(parents=True)
            (repo / ".coordination/README.md").write_text("ready\n", encoding="utf-8")
            path = project_executor_settings_path(repo)
            path.write_text("not json", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not valid JSON"):
                load_project_executor_settings(repo)
            path.write_text(
                json.dumps({"schema_version": 999, "configuration": {}}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "unsupported schema"):
                load_project_executor_settings(repo)
            path.write_text("x" * (64 * 1024 + 1), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "too large"):
                load_project_executor_settings(repo)

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
            self.assertEqual(adapter.effort, "low")
            self.assertEqual(adapter.api_key_env, "")
            self.assertEqual(restored.configuration().codex_model, "gpt-5.6-sol")
            self.assertEqual(restored.configuration().codex_effort, "max")
            self.assertEqual(restored.configuration().codex_sandbox, "danger-full-access")
            self.assertEqual(restored.configuration().codex_approval_policy, "never")
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
        with self.assertRaisesRegex(ValueError, "codex_effort"):
            ExecutorConfiguration.from_mapping(mini_payload(codex_effort="unbounded"))
        with self.assertRaisesRegex(ValueError, "codex_sandbox"):
            ExecutorConfiguration.from_mapping(mini_payload(codex_sandbox="unbounded"))
        with self.assertRaisesRegex(ValueError, "codex_approval_policy"):
            ExecutorConfiguration.from_mapping(
                mini_payload(codex_approval_policy="sometimes")
            )

    def test_claude_can_reuse_the_configured_mini_backend_for_mcp_delegation(self) -> None:
        configuration = ExecutorConfiguration.from_mapping(
            mini_payload(
                executor_adapter="claude",
                claude_local_delegation=True,
            )
        )
        adapter = configuration.adapter()
        self.assertIsInstance(adapter, ClaudeExecutorAdapter)
        self.assertTrue(adapter.delegation_enabled)
        command = adapter.command(Path("/tmp/project"))
        self.assertIn("--delegation-enabled", command)
        self.assertIn("Qwen/Qwen3.8-27B", command)
        self.assertIn("http://127.0.0.1:8000/v1", command)

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

    def test_cli_model_discovery_uses_advertised_picker_options(self) -> None:
        process = mock.MagicMock()
        process.stdin = io.StringIO()
        process.stdout = io.StringIO()
        process.wait.return_value = 0
        with (
            mock.patch(
                "coordinator.executor_settings.resolve_executable_name",
                return_value="/opt/codex",
            ),
            mock.patch("coordinator.executor_settings.subprocess.Popen", return_value=process),
            mock.patch(
                "coordinator.executor_settings._wait_for_model_response",
                side_effect=[
                    {},
                    {
                        "data": [
                            {
                                "id": "gpt-frontier",
                                "displayName": "GPT Frontier",
                                "description": "Current model",
                                "isDefault": True,
                                "hidden": False,
                                "defaultReasoningEffort": "medium",
                                "supportedReasoningEfforts": [
                                    {"reasoningEffort": "low", "description": "Fast"},
                                    {"reasoningEffort": "high", "description": "Deep"},
                                ],
                            },
                            {"id": "hidden", "hidden": True},
                        ]
                    },
                ],
            ),
        ):
            self.assertEqual(
                discover_codex_models(),
                [
                    {
                        "id": "gpt-frontier",
                        "label": "GPT Frontier",
                        "description": "Current model",
                        "default": True,
                        "efforts": [
                            {"id": "low", "description": "Fast"},
                            {"id": "high", "description": "Deep"},
                        ],
                        "default_effort": "medium",
                    }
                ],
            )

        claude_help = "--model <model> alias (e.g. 'fable', 'opus', or 'sonnet')\n  -n"
        with (
            mock.patch(
                "coordinator.executor_settings.resolve_executable_name",
                return_value="/opt/claude",
            ),
            mock.patch(
                "coordinator.executor_settings.subprocess.run",
                return_value=mock.Mock(returncode=0, stdout=claude_help),
            ),
        ):
            self.assertEqual(
                [model["id"] for model in discover_claude_models()],
                ["fable", "opus", "sonnet"],
            )
            self.assertEqual(
                [effort["id"] for effort in discover_claude_models()[0]["efforts"]],
                ["low", "medium", "high", "xhigh", "max"],
            )


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
        (self.repo / ".coordination/runtime").mkdir(parents=True)
        (self.repo / ".coordination/README.md").write_text(
            "# Coordination\n", encoding="utf-8"
        )
        app = self.app()
        with TestClient(app, base_url="http://127.0.0.1") as client:
            self.assertEqual(
                load_project_executor_settings(self.repo).executor_adapter,
                "claude",
            )
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
            self.assertEqual(
                response.json()["status"]["roles"]["reviewer"]["model"],
                "gpt-5.6-sol",
            )
            command = app.state.context.watcher.command
            self.assertIn("--executor-adapter", command)
            self.assertIn("mini-swe-agent", command)
            self.assertIn("Qwen/Qwen3.8-27B", command)
            self.assertIn("--codex-model", command)
            self.assertIn("gpt-5.6-sol", command)
            self.assertEqual(
                load_project_executor_settings(self.repo).executor_adapter,
                "mini-swe-agent",
            )

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

    def test_saved_permissions_rebind_new_and_resumed_codex_sessions(self) -> None:
        app = create_authenticated_app(
            self.repo,
            self.settings,
            repositories_root=self.base,
            provider_usage_service=fake_usage_service(),
        )
        with TestClient(app, base_url="http://127.0.0.1") as client:
            csrf = client.get("/api/state").json()["security"]["csrf_token"]
            response = client.post(
                "/api/executor-settings",
                json=mini_payload(),
                headers=self.headers(csrf),
            )
            self.assertEqual(response.status_code, 200, response.text)
            session = app.state.context.codex_session
            self.assertEqual(
                list(session.command),
                default_codex_command(self.repo, "danger-full-access", "never"),
            )
            self.assertEqual(
                list(session.resume_command),
                default_codex_resume_command(self.repo, "danger-full-access", "never"),
            )

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

    def test_running_codex_session_blocks_permission_changes(self) -> None:
        app = self.app()
        with TestClient(app, base_url="http://127.0.0.1") as client:
            csrf = client.get("/api/state").json()["security"]["csrf_token"]
            started = client.post("/api/codex/start", headers=self.headers(csrf))
            self.assertEqual(started.status_code, 200, started.text)
            blocked = client.post(
                "/api/executor-settings",
                json=mini_payload(),
                headers=self.headers(csrf),
            )
            self.assertEqual(blocked.status_code, 409, blocked.text)
            self.assertIn("stop the Codex session", blocked.json()["message"])

    def test_cli_model_catalog_endpoint_is_read_only_and_source_bounded(self) -> None:
        app = self.app()
        with TestClient(app, base_url="http://127.0.0.1") as client:
            with mock.patch(
                "coordinator.authenticated_web_app.discover_codex_models",
                return_value=[
                    {
                        "id": "gpt-frontier",
                        "label": "GPT Frontier",
                        "description": "Current",
                        "default": True,
                    }
                ],
            ):
                response = client.get("/api/v1/executor-settings/models?source=codex")
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["models"][0]["id"], "gpt-frontier")
            invalid = client.get("/api/executor-settings/models?source=unknown")
            self.assertEqual(invalid.status_code, 400, invalid.text)


if __name__ == "__main__":
    unittest.main()
