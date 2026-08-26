"""Persisted, runtime-switchable implementation executor configuration."""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from .executor_adapters import (
    ClaudeExecutorAdapter,
    ExecutorAdapter,
    MiniSweAgentExecutorAdapter,
    resolve_executable,
)
from .mini_swe_profiles import MINI_SWE_PROFILES
from .operational_store import OperationalStore
from .process_guard import guarded_command

EXECUTOR_PREFERENCE_KEY = "executor_configuration_v1"
PROJECT_EXECUTOR_SETTINGS = Path(".coordination/runtime/executor-settings.json")
PROJECT_EXECUTOR_SETTINGS_VERSION = 1
PROJECT_EXECUTOR_SETTINGS_BYTES = 64 * 1024
MODEL_DISCOVERY_BYTES = 1024 * 1024
MODEL_DISCOVERY_TIMEOUT_SECONDS = 5.0
MODEL_NAME_LIMIT = 240
CLI_MODEL_LIMIT = 100
EFFORT_LEVELS = frozenset({"low", "medium", "high", "xhigh", "max"})
CODEX_PERMISSION_MODES = frozenset(
    {"ask-for-approval", "approve-for-me", "full-access"}
)


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


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def _effort(value: object, name: str, *, allow_ultra: bool = False) -> str:
    effort = _text(value, name)
    allowed = EFFORT_LEVELS | ({"none", "ultra"} if allow_ultra else set())
    if effort and effort not in allowed:
        raise ValueError(f"{name} must be one of {', '.join(sorted(allowed))} or blank")
    return effort


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

    primary_adapter: str = "codex"
    primary_claude_model: str = "opus"
    primary_claude_effort: str = ""
    primary_local_model: str = ""
    primary_local_effort: str = ""
    primary_local_step_limit: int = 24
    primary_local_timeout_seconds: int = 900
    codex_model: str = ""
    codex_effort: str = ""
    codex_permission_mode: str = "ask-for-approval"
    executor_adapter: str = "claude"
    claude_model: str = "opus"
    claude_effort: str = ""
    claude_subagent_model: str = "sonnet"
    claude_subagent_effort: str = ""
    claude_max_turns: int = 40
    claude_local_delegation: bool = False
    mini_swe_model: str = ""
    mini_swe_effort: str = ""
    mini_swe_profile: str = "bounded"
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
                claude_effort=adapter.effort,
                claude_subagent_model=adapter.subagent_model,
                claude_subagent_effort=adapter.subagent_effort,
                claude_max_turns=adapter.max_turns,
                claude_local_delegation=adapter.delegation_enabled,
                mini_swe_model=adapter.delegate_model,
                mini_swe_effort=adapter.delegate_effort,
                mini_swe_profile="bounded",
                mini_swe_api_base=adapter.delegate_api_base,
                mini_swe_provider=adapter.delegate_provider,
                mini_swe_api_key_env=adapter.delegate_api_key_env,
                mini_swe_step_limit=adapter.delegate_step_limit,
                mini_swe_cost_limit=adapter.delegate_cost_limit,
                mini_swe_timeout_seconds=adapter.delegate_timeout_seconds,
            )
        if isinstance(adapter, MiniSweAgentExecutorAdapter):
            return cls(
                executor_adapter="mini-swe-agent",
                mini_swe_model=adapter.model,
                mini_swe_effort=adapter.effort,
                mini_swe_profile=adapter.profile,
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
        normalized = dict(value)
        legacy_sandbox = normalized.pop("codex_sandbox", None)
        legacy_approval = normalized.pop("codex_approval_policy", None)
        if "codex_permission_mode" not in normalized and (
            legacy_sandbox is not None or legacy_approval is not None
        ):
            normalized["codex_permission_mode"] = (
                "full-access"
                if legacy_sandbox == "danger-full-access" and legacy_approval == "never"
                else "approve-for-me"
                if legacy_approval == "never"
                else "ask-for-approval"
            )
        allowed = set(cls.__dataclass_fields__)
        if set(normalized) - allowed:
            raise ValueError("executor settings contain unknown fields")
        baseline = fallback or cls()
        merged = {**asdict(baseline), **normalized}
        selected = _text(merged["executor_adapter"], "executor_adapter", required=True)
        if selected not in {"claude", "mini-swe-agent"}:
            raise ValueError("executor_adapter must be claude or mini-swe-agent")
        primary = _text(merged["primary_adapter"], "primary_adapter", required=True)
        if primary not in {"codex", "claude", "mini-swe-agent"}:
            raise ValueError("primary_adapter must be codex, claude, or mini-swe-agent")
        provider = _text(merged["mini_swe_provider"], "mini_swe_provider", required=True)
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", provider):
            raise ValueError("mini_swe_provider has invalid characters")
        profile = _text(merged["mini_swe_profile"], "mini_swe_profile", required=True)
        if profile not in MINI_SWE_PROFILES:
            raise ValueError(
                f"mini_swe_profile must be one of {', '.join(MINI_SWE_PROFILES)}"
            )
        key_env = _text(merged["mini_swe_api_key_env"], "mini_swe_api_key_env")
        if key_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_env):
            raise ValueError("mini_swe_api_key_env must be an environment-variable name")
        model = _text(
            merged["mini_swe_model"],
            "mini_swe_model",
            required=selected == "mini-swe-agent" or bool(merged["claude_local_delegation"]),
        )
        primary_local_model = _text(
            merged["primary_local_model"],
            "primary_local_model",
            required=primary == "mini-swe-agent",
        )
        codex_permission_mode = _text(
            merged["codex_permission_mode"], "codex_permission_mode", required=True
        )
        if codex_permission_mode not in CODEX_PERMISSION_MODES:
            raise ValueError(
                "codex_permission_mode must be ask-for-approval, approve-for-me, or full-access"
            )
        return cls(
            primary_adapter=primary,
            primary_claude_model=_text(
                merged["primary_claude_model"],
                "primary_claude_model",
                required=primary == "claude",
            ),
            primary_claude_effort=_effort(
                merged["primary_claude_effort"], "primary_claude_effort"
            ),
            primary_local_model=primary_local_model,
            primary_local_effort=_effort(
                merged["primary_local_effort"], "primary_local_effort"
            ),
            primary_local_step_limit=_integer(
                merged["primary_local_step_limit"], "primary_local_step_limit", 6, 200
            ),
            primary_local_timeout_seconds=_integer(
                merged["primary_local_timeout_seconds"],
                "primary_local_timeout_seconds",
                10,
                86_400,
            ),
            codex_model=_text(merged["codex_model"], "codex_model"),
            codex_effort=_effort(merged["codex_effort"], "codex_effort", allow_ultra=True),
            codex_permission_mode=codex_permission_mode,
            executor_adapter=selected,
            claude_model=_text(merged["claude_model"], "claude_model", required=True),
            claude_effort=_effort(merged["claude_effort"], "claude_effort"),
            claude_subagent_model=_text(
                merged["claude_subagent_model"],
                "claude_subagent_model",
                required=True,
            ),
            claude_subagent_effort=_effort(
                merged["claude_subagent_effort"], "claude_subagent_effort"
            ),
            claude_max_turns=_integer(
                merged["claude_max_turns"], "claude_max_turns", 8, 200
            ),
            claude_local_delegation=_boolean(
                merged["claude_local_delegation"], "claude_local_delegation"
            ),
            mini_swe_model=model,
            mini_swe_effort=_effort(merged["mini_swe_effort"], "mini_swe_effort"),
            mini_swe_profile=profile,
            mini_swe_api_base=validate_api_base(merged["mini_swe_api_base"]),
            mini_swe_provider=provider,
            mini_swe_api_key_env=key_env,
            mini_swe_step_limit=_integer(
                merged["mini_swe_step_limit"], "mini_swe_step_limit", 6, 200
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
                effort=self.claude_effort,
                subagent_model=self.claude_subagent_model,
                subagent_effort=self.claude_subagent_effort,
                max_turns=self.claude_max_turns,
                delegation_enabled=self.claude_local_delegation,
                delegate_model=self.mini_swe_model,
                delegate_effort=self.mini_swe_effort,
                delegate_api_base=self.mini_swe_api_base,
                delegate_provider=self.mini_swe_provider,
                delegate_api_key_env=self.mini_swe_api_key_env,
                delegate_step_limit=self.mini_swe_step_limit,
                delegate_cost_limit=self.mini_swe_cost_limit,
                delegate_timeout_seconds=self.mini_swe_timeout_seconds,
            )
        return MiniSweAgentExecutorAdapter(
            model=self.mini_swe_model,
            effort=self.mini_swe_effort,
            profile=self.mini_swe_profile,
            api_base=self.mini_swe_api_base,
            provider=self.mini_swe_provider,
            api_key_env=self.mini_swe_api_key_env,
            step_limit=self.mini_swe_step_limit,
            cost_limit=self.mini_swe_cost_limit,
            timeout_seconds=self.mini_swe_timeout_seconds,
        )


def project_executor_settings_path(repo: Path) -> Path:
    """Return the fixed handoff-facing settings path inside `repo`."""

    return repo.resolve() / PROJECT_EXECUTOR_SETTINGS


def publish_project_executor_settings(
    repo: Path,
    configuration: ExecutorConfiguration,
    *,
    replace: bool = True,
) -> Path | None:
    """Atomically publish non-secret executor settings inside an initialized repo.

    The global operational database remains the application's source of truth,
    but project agents never need to open it. Uninitialized repositories are a
    truthful no-op because creating coordination state is an explicit action.
    """

    root = repo.resolve()
    coordination = root / ".coordination"
    if not (coordination / "README.md").is_file():
        return None
    if coordination.is_symlink():
        raise ValueError("coordination directory must not be a symbolic link")
    runtime = coordination / "runtime"
    if runtime.exists() and (runtime.is_symlink() or not runtime.is_dir()):
        raise ValueError("coordination runtime must be a real directory")
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = project_executor_settings_path(root)
    if destination.is_symlink():
        raise ValueError("project executor settings must not be a symbolic link")
    if destination.exists() and not replace:
        return destination
    payload = {
        "schema_version": PROJECT_EXECUTOR_SETTINGS_VERSION,
        "configuration": asdict(configuration),
    }
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    temporary = runtime / (
        f".{destination.name}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp"
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = None
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        with suppress(FileNotFoundError):
            temporary.unlink()
    return destination


def load_project_executor_settings(repo: Path) -> ExecutorConfiguration:
    """Load and validate the bounded project-local executor snapshot."""

    root = repo.resolve()
    coordination = root / ".coordination"
    runtime = coordination / "runtime"
    path = project_executor_settings_path(root)
    if coordination.is_symlink() or runtime.is_symlink():
        raise ValueError("project executor settings must remain inside the repository")
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"project executor settings do not exist: {path}")
    raw = path.read_bytes()
    if len(raw) > PROJECT_EXECUTOR_SETTINGS_BYTES:
        raise ValueError("project executor settings are too large")
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("project executor settings are not valid JSON") from error
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schema_version", "configuration"}
        or payload.get("schema_version") != PROJECT_EXECUTOR_SETTINGS_VERSION
        or not isinstance(payload.get("configuration"), dict)
    ):
        raise ValueError("project executor settings have an unsupported schema")
    return ExecutorConfiguration.from_mapping(payload["configuration"])


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
        delegation_executable = (
            resolve_executable(
                MiniSweAgentExecutorAdapter(
                    model=configuration.mini_swe_model,
                    api_base=configuration.mini_swe_api_base,
                    provider=configuration.mini_swe_provider,
                    api_key_env=configuration.mini_swe_api_key_env,
                )
            )
            if configuration.executor_adapter == "claude"
            and configuration.claude_local_delegation
            else None
        )
        return {
            "configuration": asdict(configuration),
            "status": {
                "adapter": adapter.id,
                "display_name": adapter.display_name,
                "executable": executable,
                "executable_available": executable is not None,
                "api_key_env_configured": bool(key_name and os.environ.get(key_name)),
                "delegation_enabled": configuration.claude_local_delegation,
                "delegation_model": configuration.mini_swe_model,
                "delegation_executable": delegation_executable,
                "delegation_executable_available": delegation_executable is not None,
                "load_warning": self.load_warning,
                "roles": {
                    "reviewer": {
                        "adapter": (
                            f"{configuration.primary_adapter}-cli"
                            if configuration.primary_adapter in {"codex", "claude"}
                            else "mini-swe-agent"
                        ),
                        "display_name": (
                            "Codex CLI"
                            if configuration.primary_adapter == "codex"
                            else "Claude Code"
                            if configuration.primary_adapter == "claude"
                            else "Local / API via mini-swe-agent"
                        ),
                        "model": (
                            configuration.codex_model or "CLI default"
                            if configuration.primary_adapter == "codex"
                            else configuration.primary_claude_model
                            if configuration.primary_adapter == "claude"
                            else configuration.primary_local_model
                        ),
                        "effort": (
                            configuration.codex_effort or "model default"
                            if configuration.primary_adapter == "codex"
                            else configuration.primary_claude_effort or "model default"
                            if configuration.primary_adapter == "claude"
                            else configuration.primary_local_effort or "endpoint default"
                        ),
                        "executable_available": resolve_executable_name(
                            "mini"
                            if configuration.primary_adapter == "mini-swe-agent"
                            else configuration.primary_adapter
                        )
                        is not None,
                    },
                    "supervisor": {
                        "adapter": "claude-cli",
                        "display_name": "Claude Code",
                        "model": configuration.claude_model,
                        "effort": configuration.claude_effort or "model default",
                        "active": configuration.executor_adapter == "claude",
                        "executable_available": resolve_executable_name("claude") is not None,
                    },
                    "executor": {
                        "adapter": adapter.id,
                        "display_name": adapter.display_name,
                        "model": (
                            configuration.mini_swe_model
                            if configuration.executor_adapter == "mini-swe-agent"
                            or configuration.claude_local_delegation
                            else configuration.claude_model
                        ),
                        "effort": (
                            configuration.mini_swe_effort or "endpoint default"
                            if configuration.executor_adapter == "mini-swe-agent"
                            or configuration.claude_local_delegation
                            else configuration.claude_effort or "model default"
                        ),
                        "profile": (
                            configuration.mini_swe_profile
                            if configuration.executor_adapter == "mini-swe-agent"
                            else "bounded"
                            if configuration.claude_local_delegation
                            else "provider native"
                        ),
                        "native_subagent_model": configuration.claude_subagent_model,
                        "native_subagent_effort": (
                            configuration.claude_subagent_effort or "inherit supervisor"
                        ),
                        "delegated": configuration.claude_local_delegation,
                        "executable_available": (
                            delegation_executable is not None
                            if configuration.claude_local_delegation
                            else executable is not None
                        ),
                    },
                },
            },
        }


def resolve_executable_name(command: str) -> str | None:
    """Resolve a fixed CLI name for role-readiness reporting."""

    candidate = os.path.expanduser(command)
    if os.path.isfile(candidate):
        return os.path.realpath(candidate)
    return shutil.which(command)


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


def _model_response_reader(stream: object, output: queue.Queue[object]) -> None:
    try:
        for line in stream:  # type: ignore[union-attr]
            try:
                output.put(json.loads(line))
            except json.JSONDecodeError:
                continue
    finally:
        output.put(None)


def _wait_for_model_response(
    output: queue.Queue[object], request_id: int, deadline: float
) -> dict[str, object]:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("Codex model discovery timed out")
        try:
            message = output.get(timeout=remaining)
        except queue.Empty as error:
            raise ValueError("Codex model discovery timed out") from error
        if message is None:
            raise ValueError("Codex app server closed during model discovery")
        if not isinstance(message, dict) or message.get("id") != request_id:
            continue
        if "error" in message or not isinstance(message.get("result"), dict):
            raise ValueError("Codex app server rejected model discovery")
        return message["result"]  # type: ignore[return-value]


def discover_codex_models(
    command: str = "codex", *, timeout: float = MODEL_DISCOVERY_TIMEOUT_SECONDS
) -> list[dict[str, object]]:
    """Read the installed Codex picker's visible models without starting a turn."""

    executable = resolve_executable_name(command)
    if executable is None:
        raise ValueError("Codex CLI is not installed or not on PATH")
    try:
        process = subprocess.Popen(
            guarded_command([executable, "app-server", "--stdio"]),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
    except OSError as error:
        raise ValueError("Codex app server could not be started") from error
    if process.stdin is None or process.stdout is None:
        process.kill()
        raise ValueError("Codex app server streams are unavailable")
    output: queue.Queue[object] = queue.Queue()
    reader = threading.Thread(
        target=_model_response_reader,
        args=(process.stdout, output),
        name="coordinator-codex-model-reader",
        daemon=True,
    )
    reader.start()
    deadline = time.monotonic() + timeout
    try:
        process.stdin.write(
            json.dumps(
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {"clientInfo": {"name": "coordinator", "version": "1"}},
                }
            )
            + "\n"
        )
        process.stdin.flush()
        _wait_for_model_response(output, 1, deadline)
        process.stdin.write(json.dumps({"method": "initialized"}) + "\n")
        process.stdin.write(
            json.dumps(
                {
                    "id": 2,
                    "method": "model/list",
                    "params": {"includeHidden": False, "limit": CLI_MODEL_LIMIT},
                }
            )
            + "\n"
        )
        process.stdin.flush()
        result = _wait_for_model_response(output, 2, deadline)
    except (BrokenPipeError, OSError) as error:
        raise ValueError("Codex app server closed during model discovery") from error
    finally:
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                process.kill()
            process.wait(timeout=1.0)
        with suppress(OSError):
            process.stdin.close()
        with suppress(OSError):
            process.stdout.close()
    entries = result.get("data")
    if not isinstance(entries, list):
        raise ValueError("Codex model discovery returned invalid data")
    models = [
        {
            "id": identifier,
            "label": entry.get("displayName") or identifier,
            "description": entry.get("description") or "",
            "default": entry.get("isDefault") is True,
            "efforts": [
                {
                    "id": effort["reasoningEffort"],
                    "description": effort.get("description") or "",
                }
                for effort in entry.get("supportedReasoningEfforts", [])
                if isinstance(effort, dict)
                and isinstance(effort.get("reasoningEffort"), str)
            ],
            "default_effort": entry.get("defaultReasoningEffort") or "",
        }
        for entry in entries
        if isinstance(entry, dict)
        and isinstance((identifier := entry.get("id")), str)
        and identifier
        and len(identifier) <= MODEL_NAME_LIMIT
        and entry.get("hidden") is not True
    ]
    if not models:
        raise ValueError("Codex model discovery returned no visible models")
    return models


def discover_claude_models(
    command: str = "claude", *, timeout: float = MODEL_DISCOVERY_TIMEOUT_SECONDS
) -> list[dict[str, object]]:
    """Parse the model aliases advertised by the installed Claude Code CLI."""

    executable = resolve_executable_name(command)
    if executable is None:
        raise ValueError("Claude Code is not installed or not on PATH")
    try:
        completed = subprocess.run(
            [executable, "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError("Claude model discovery failed") from error
    marker = "--model <model>"
    if completed.returncode != 0 or marker not in completed.stdout:
        raise ValueError("Claude Code did not advertise model aliases")
    model_help = completed.stdout.split(marker, 1)[1].split("\n  -", 1)[0]
    aliases = list(dict.fromkeys(re.findall(r"'([A-Za-z][A-Za-z0-9._-]*)'", model_help)))
    aliases = [alias for alias in aliases if not alias.startswith("claude-")]
    if not aliases:
        raise ValueError("Claude Code did not advertise model aliases")
    efforts = [
        {"id": effort, "description": "Claude Code effort level"}
        for effort in ("low", "medium", "high", "xhigh", "max")
    ]
    return [
        {
            "id": alias,
            "label": alias.title(),
            "description": "Claude Code alias",
            "efforts": efforts,
            "default_effort": "",
        }
        for alias in aliases
    ]
