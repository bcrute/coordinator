"""Persisted, runtime-switchable implementation executor configuration."""

from __future__ import annotations

import json
import os
import re
import threading
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Mapping
from urllib.parse import urlsplit

from .executor_adapters import (
    ClaudeExecutorAdapter,
    ExecutorAdapter,
    MiniSweAgentExecutorAdapter,
    resolve_executable,
)
from .operational_store import OperationalStore

EXECUTOR_PREFERENCE_KEY = "executor_configuration_v1"
MODEL_DISCOVERY_BYTES = 1024 * 1024
MODEL_DISCOVERY_TIMEOUT_SECONDS = 5.0
MODEL_NAME_LIMIT = 240


def _text(value: object, name: str, *, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    normalized = value.strip()
    if required and not normalized:
        raise ValueError(f"{name} must not be empty")
    if len(normalized) > MODEL_NAME_LIMIT or any(ord(character) < 32 for character in normalized):
        raise ValueError(f"{name} is invalid")
    return normalized


def _integer(value: object, name: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def _number(value: object, name: str, minimum: float, maximum: float) -> float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not minimum <= float(value) <= maximum
    ):
        raise ValueError(f"{name} must be a number from {minimum:g} to {maximum:g}")
    return float(value)


def validate_api_base(value: object, *, required: bool = False) -> str:
    api_base = _text(value, "mini_swe_api_base", required=required).rstrip("/")
    if not api_base:
        return ""
    endpoint = urlsplit(api_base)
    if endpoint.scheme not in {"http", "https"} or not endpoint.netloc:
        raise ValueError("mini_swe_api_base must be an absolute HTTP(S) URL")
    if endpoint.username is not None or endpoint.password is not None:
        raise ValueError("mini_swe_api_base must not contain embedded credentials")
    if endpoint.query or endpoint.fragment:
        raise ValueError("mini_swe_api_base must not contain a query or fragment")
    return api_base


@dataclass(frozen=True)
class ExecutorConfiguration:
    """The non-secret settings required to construct either built-in adapter."""

    executor_adapter: str = "claude"
    claude_model: str = "opus"
    claude_subagent_model: str = "sonnet"
    claude_max_turns: int = 40
    mini_swe_model: str = ""
    mini_swe_api_base: str = ""
    mini_swe_provider: str = "openai"
    mini_swe_api_key_env: str = ""
    mini_swe_step_limit: int = 12
    mini_swe_cost_limit: float = 0.0
    mini_swe_timeout_seconds: int = 900

    @classmethod
    def from_adapter(cls, adapter: ExecutorAdapter) -> "ExecutorConfiguration":
        if isinstance(adapter, ClaudeExecutorAdapter):
            return cls(
                executor_adapter="claude",
                claude_model=adapter.model,
                claude_subagent_model=adapter.subagent_model,
                claude_max_turns=adapter.max_turns,
            )
        if isinstance(adapter, MiniSweAgentExecutorAdapter):
            return cls(
                executor_adapter="mini-swe-agent",
                mini_swe_model=adapter.model,
                mini_swe_api_base=adapter.api_base,
                mini_swe_provider=adapter.provider,
                mini_swe_api_key_env=adapter.api_key_env,
                mini_swe_step_limit=adapter.step_limit,
                mini_swe_cost_limit=adapter.cost_limit,
                mini_swe_timeout_seconds=adapter.timeout_seconds,
            )
        raise ValueError("unsupported executor adapter")

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, object],
        fallback: "ExecutorConfiguration | None" = None,
    ) -> "ExecutorConfiguration":
        allowed = set(cls.__dataclass_fields__)
        if set(value) - allowed:
            raise ValueError("executor settings contain unknown fields")
        baseline = fallback or cls()
        merged = {**asdict(baseline), **dict(value)}
        selected = _text(merged["executor_adapter"], "executor_adapter", required=True)
        if selected not in {"claude", "mini-swe-agent"}:
            raise ValueError("executor_adapter must be claude or mini-swe-agent")
        provider = _text(merged["mini_swe_provider"], "mini_swe_provider", required=True)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", provider):
            raise ValueError("mini_swe_provider has invalid characters")
        key_env = _text(merged["mini_swe_api_key_env"], "mini_swe_api_key_env")
        if key_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_env):
            raise ValueError("mini_swe_api_key_env must be an environment-variable name")
        model = _text(
            merged["mini_swe_model"],
            "mini_swe_model",
            required=selected == "mini-swe-agent",
        )
        return cls(
            executor_adapter=selected,
            claude_model=_text(merged["claude_model"], "claude_model", required=True),
            claude_subagent_model=_text(
                merged["claude_subagent_model"],
                "claude_subagent_model",
                required=True,
            ),
            claude_max_turns=_integer(
                merged["claude_max_turns"], "claude_max_turns", 1, 200
            ),
            mini_swe_model=model,
            mini_swe_api_base=validate_api_base(merged["mini_swe_api_base"]),
            mini_swe_provider=provider,
            mini_swe_api_key_env=key_env,
            mini_swe_step_limit=_integer(
                merged["mini_swe_step_limit"], "mini_swe_step_limit", 1, 200
            ),
            mini_swe_cost_limit=_number(
                merged["mini_swe_cost_limit"], "mini_swe_cost_limit", 0, 1_000_000
            ),
            mini_swe_timeout_seconds=_integer(
                merged["mini_swe_timeout_seconds"],
                "mini_swe_timeout_seconds",
                10,
                86_400,
            ),
        )

    def adapter(self) -> ExecutorAdapter:
        if self.executor_adapter == "claude":
            return ClaudeExecutorAdapter(
                model=self.claude_model,
                subagent_model=self.claude_subagent_model,
                max_turns=self.claude_max_turns,
            )
        return MiniSweAgentExecutorAdapter(
            model=self.mini_swe_model,
            api_base=self.mini_swe_api_base,
            provider=self.mini_swe_provider,
            api_key_env=self.mini_swe_api_key_env,
            step_limit=self.mini_swe_step_limit,
            cost_limit=self.mini_swe_cost_limit,
            timeout_seconds=self.mini_swe_timeout_seconds,
        )


class ExecutorSettingsService:
    """Own the persisted configuration without retaining endpoint secrets."""

    def __init__(self, store: OperationalStore, initial: ExecutorAdapter) -> None:
        self.store = store
        self._lock = threading.RLock()
        fallback = ExecutorConfiguration.from_adapter(initial)
        stored = store.preferences().get(EXECUTOR_PREFERENCE_KEY)
        try:
            self._configuration = (
                ExecutorConfiguration.from_mapping(stored, fallback)
                if isinstance(stored, dict)
                else fallback
            )
            self.load_warning: str | None = None
        except ValueError as error:
            self._configuration = fallback
            self.load_warning = f"ignored invalid persisted executor settings: {error}"

    def configuration(self) -> ExecutorConfiguration:
        with self._lock:
            return self._configuration

    def adapter(self) -> ExecutorAdapter:
        return self.configuration().adapter()

    def candidate(self, value: object) -> ExecutorConfiguration:
        if not isinstance(value, dict):
            raise ValueError("expected an executor settings object")
        return ExecutorConfiguration.from_mapping(value, self.configuration())

    def save(self, configuration: ExecutorConfiguration) -> None:
        encoded = asdict(configuration)
        self.store.set_preference(EXECUTOR_PREFERENCE_KEY, encoded)
        with self._lock:
            self._configuration = configuration
            self.load_warning = None

    def snapshot(self) -> dict[str, object]:
        configuration = self.configuration()
        adapter = configuration.adapter()
        key_name = configuration.mini_swe_api_key_env
        executable = resolve_executable(adapter)
        return {
            "configuration": asdict(configuration),
            "status": {
                "adapter": adapter.id,
                "display_name": adapter.display_name,
                "executable": executable,
                "executable_available": executable is not None,
                "api_key_env_configured": bool(key_name and os.environ.get(key_name)),
                "load_warning": self.load_warning,
            },
        }


def discover_models(
    api_base: object,
    api_key_env: object = "",
    *,
    timeout: float = MODEL_DISCOVERY_TIMEOUT_SECONDS,
) -> list[str]:
    """Return bounded model IDs from an OpenAI-compatible models endpoint."""

    base = validate_api_base(api_base, required=True)
    key_name = _text(api_key_env, "mini_swe_api_key_env")
    if key_name and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_name):
        raise ValueError("mini_swe_api_key_env must be an environment-variable name")
    headers = {"Accept": "application/json"}
    if key_name:
        secret = os.environ.get(key_name)
        if not secret:
            raise ValueError(f"environment variable {key_name!r} is not set")
        headers["Authorization"] = f"Bearer {secret}"
    request = urllib.request.Request(f"{base}/models", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MODEL_DISCOVERY_BYTES + 1)
    except (OSError, urllib.error.HTTPError, urllib.error.URLError) as error:
        raise ValueError(f"model discovery failed: {error}") from error
    if len(raw) > MODEL_DISCOVERY_BYTES:
        raise ValueError("model discovery response is too large")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("model discovery response is not valid JSON") from error
    entries = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise ValueError("model discovery response does not contain a data list")
    models = sorted(
        {
            identifier
            for entry in entries
            if isinstance(entry, dict)
            and isinstance((identifier := entry.get("id")), str)
            and identifier.strip()
            and len(identifier) <= MODEL_NAME_LIMIT
        }
    )
    if not models:
        raise ValueError("model discovery returned no model IDs")
    return models
