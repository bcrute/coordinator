"""Published HTTP API contract for Coordinator."""

from __future__ import annotations

from typing import Any


def _json_response(schema: str, description: str = "Successful response") -> dict[str, Any]:
    return {
        "description": description,
        "content": {
            "application/json": {
                "schema": {"$ref": f"#/components/schemas/{schema}"}
            }
        },
    }


def _errors(*statuses: str) -> dict[str, Any]:
    return {
        status: {
            "description": "Request failed",
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/Error"}
                }
            },
        }
        for status in statuses
    }


def _operation(
    summary: str,
    schema: str = "Operation",
    *,
    request_schema: str | None = None,
    success_status: str = "200",
    statuses: tuple[str, ...] = ("400", "401", "403", "404"),
) -> dict[str, Any]:
    operation: dict[str, Any] = {
        "summary": summary,
        "responses": {success_status: _json_response(schema), **_errors(*statuses)},
    }
    if request_schema:
        operation["requestBody"] = {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {"$ref": f"#/components/schemas/{request_schema}"}
                }
            },
        }
    return operation


def openapi_document() -> dict[str, Any]:
    """Return the source-controlled OpenAPI 3.1 contract for `/api/v1`."""

    run_parameter = {
        "name": "run_id",
        "in": "path",
        "required": True,
        "schema": {"type": "string", "minLength": 1},
    }
    action_parameter = {
        "name": "action",
        "in": "path",
        "required": True,
        "schema": {"type": "string"},
    }
    paths: dict[str, Any] = {
        "/api/v1/state": {"get": _operation("Current workspace state", "State")},
        "/api/v1/provider-usage": {
            "get": _operation("Cached Codex and Claude usage", "ProviderUsage")
        },
        "/api/v1/provider-usage/refresh": {
            "post": _operation("Refresh Codex and Claude usage", "ProviderUsage")
        },
        "/api/v1/usage-history": {
            "get": _operation("Historical provider usage and estimated value", "UsageHistory")
        },
        "/api/v1/usage-history/refresh": {
            "post": _operation("Import native provider usage telemetry", "UsageHistory")
        },
        "/api/v1/events": {
            "get": {
                "summary": "Resumable state and transition stream",
                "parameters": [
                    {
                        "name": "Last-Event-ID",
                        "in": "header",
                        "schema": {"type": "integer", "minimum": 0},
                    }
                ],
                "responses": {
                    "200": {
                        "description": "Server-Sent Events stream",
                        "content": {"text/event-stream": {"schema": {"type": "string"}}},
                    },
                    **_errors("400", "401", "403"),
                },
            }
        },
        "/api/v1/activity": {"get": _operation("Audit activity", "EventList")},
        "/api/v1/sessions": {"get": _operation("Active sessions", "SessionList")},
        "/api/v1/sessions/revoke": {
            "post": _operation("Revoke one session", request_schema="SessionRevoke")
        },
        "/api/v1/sessions/revoke-others": {
            "post": _operation("Revoke all other sessions")
        },
        "/api/v1/runs": {"get": _operation("Durable run history", "RunList")},
        "/api/v1/runs/{run_id}": {
            "parameters": [run_parameter],
            "get": _operation("Durable run detail", "RunDetail"),
        },
        "/api/v1/runs/{run_id}/events": {
            "parameters": [run_parameter],
            "get": _operation("Durable run transitions", "EventList"),
        },
        "/api/v1/runs/{run_id}/resume": {
            "parameters": [run_parameter],
            "post": _operation("Resume a paused run"),
        },
        "/api/v1/runs/{run_id}/policy": {
            "parameters": [run_parameter],
            "post": _operation("Replace run guardrails", request_schema="GuardrailPolicy"),
        },
        "/api/v1/runs/{run_id}/{action}": {
            "parameters": [run_parameter, action_parameter],
            "post": _operation("Archive or reopen a run"),
        },
        "/api/v1/repository/diff": {
            "get": _operation("Bounded repository status and diff", "RepositoryDiff")
        },
        "/api/v1/repository/create": {
            "post": _operation(
                "Create and select a repository",
                request_schema="RepositoryCreate",
                success_status="201",
                statuses=("400", "401", "403", "409"),
            )
        },
        "/api/v1/repository/initialize": {
            "post": _operation("Initialize coordination files", request_schema="RepositoryInitialize")
        },
        "/api/v1/repository/select": {
            "post": _operation("Select a repository", request_schema="RepositorySelect")
        },
        "/api/v1/preferences": {
            "get": _operation("Non-secret preferences", "Preferences"),
            "post": _operation("Update non-secret preferences", "Preferences", request_schema="PreferencePatch"),
        },
        "/api/v1/executor-settings": {
            "get": _operation("Persisted implementation executor settings", "ExecutorSettings"),
            "post": _operation(
                "Update the executor used by future watcher starts",
                "ExecutorSettings",
                request_schema="ExecutorSettingsPatch",
                statuses=("400", "401", "403", "409"),
            ),
        },
        "/api/v1/executor-settings/discover": {
            "post": _operation(
                "Discover models from an OpenAI-compatible endpoint",
                "ExecutorModelList",
                request_schema="ExecutorDiscovery",
            )
        },
        "/api/v1/executor-settings/models": {
            "get": _operation(
                "List models advertised by an installed orchestration CLI",
                "ExecutorCliModelList",
            )
        },
        "/api/v1/diagnostics": {
            "get": _operation("Bounded operational diagnostics", "Diagnostics")
        },
        "/api/v1/watcher/{action}": {
            "parameters": [action_parameter],
            "post": _operation("Control the coordination watcher"),
        },
        "/api/v1/codex/{action}": {
            "parameters": [action_parameter],
            "post": _operation("Control the Codex terminal process or replay buffer"),
        },
    }
    object_schema = {"type": "object", "additionalProperties": True}
    schemas: dict[str, Any] = {
        "Error": {
            "type": "object",
            "required": ["ok", "error"],
            "properties": {
                "ok": {"const": False},
                "error": {
                    "type": "object",
                    "required": ["code", "message"],
                    "properties": {
                        "code": {"type": "string"},
                        "message": {"type": "string"},
                        "details": object_schema,
                    },
                    "additionalProperties": False,
                },
            },
            "additionalProperties": False,
        },
        "Operation": {
            "type": "object",
            "required": ["ok"],
            "properties": {"ok": {"type": "boolean"}},
            "additionalProperties": True,
        },
        "State": {
            "type": "object",
            "required": ["api_version", "repo", "workflow", "run", "security"],
            "properties": {
                "api_version": {"const": "v1"},
                "repo": {"type": "string"},
                "workflow": object_schema,
                "run": object_schema,
                "security": object_schema,
            },
            "additionalProperties": True,
        },
        "ProviderUsage": {
            "type": "object",
            "required": [
                "ok",
                "generated_at",
                "next_refresh_at",
                "refresh_interval_seconds",
                "refreshing",
                "providers",
            ],
            "properties": {
                "ok": {"const": True},
                "generated_at": {"type": ["string", "null"], "format": "date-time"},
                "next_refresh_at": {"type": ["string", "null"], "format": "date-time"},
                "refresh_interval_seconds": {"type": "integer", "minimum": 1},
                "refreshing": {"type": "boolean"},
                "providers": {"type": "array", "items": object_schema},
            },
            "additionalProperties": False,
        },
        "UsageHistory": {
            "type": "object",
            "required": [
                "ok",
                "generated_at",
                "refreshing",
                "range",
                "bucket_seconds",
                "providers",
            ],
            "properties": {
                "ok": {"const": True},
                "generated_at": {"type": ["string", "null"], "format": "date-time"},
                "refreshing": {"type": "boolean"},
                "range": {"enum": ["24h", "7d", "30d", "all"]},
                "from": {"type": ["string", "null"], "format": "date-time"},
                "to": {"type": "string", "format": "date-time"},
                "bucket_seconds": {"type": "integer", "minimum": 1},
                "providers": {"type": "array", "items": object_schema},
            },
            "additionalProperties": False,
        },
        "RunList": {
            "type": "object",
            "required": ["ok", "runs"],
            "properties": {"ok": {"const": True}, "runs": {"type": "array", "items": object_schema}},
            "additionalProperties": False,
        },
        "RunDetail": {
            "type": "object",
            "required": ["ok", "run"],
            "properties": {"ok": {"const": True}, "run": object_schema},
            "additionalProperties": False,
        },
        "EventList": {
            "type": "object",
            "required": ["ok", "events"],
            "properties": {"ok": {"const": True}, "events": {"type": "array", "items": object_schema}},
            "additionalProperties": False,
        },
        "SessionList": {
            "type": "object",
            "required": ["ok", "sessions"],
            "properties": {"ok": {"const": True}, "sessions": {"type": "array", "items": object_schema}},
            "additionalProperties": False,
        },
        "Preferences": {
            "type": "object",
            "required": ["ok", "preferences"],
            "properties": {"ok": {"const": True}, "preferences": object_schema},
            "additionalProperties": False,
        },
        "ExecutorSettings": {
            "type": "object",
            "required": ["ok", "configuration", "status"],
            "properties": {
                "ok": {"const": True},
                "configuration": {"$ref": "#/components/schemas/ExecutorSettingsPatch"},
                "status": object_schema,
                "outcome": {"type": "string"},
                "message": {"type": "string"},
                "managed_watcher": object_schema,
            },
            "additionalProperties": False,
        },
        "ExecutorModelList": {
            "type": "object",
            "required": ["ok", "models"],
            "properties": {
                "ok": {"const": True},
                "models": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                },
            },
            "additionalProperties": False,
        },
        "ExecutorCliModelList": {
            "type": "object",
            "required": ["ok", "source", "models"],
            "properties": {
                "ok": {"const": True},
                "source": {"enum": ["codex", "claude"]},
                "models": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "required": ["id", "label", "description"],
                        "properties": {
                            "id": {"type": "string", "minLength": 1},
                            "label": {"type": "string", "minLength": 1},
                            "description": {"type": "string"},
                            "default": {"type": "boolean"},
                            "default_effort": {"type": "string"},
                            "efforts": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "required": ["id", "description"],
                                    "properties": {
                                        "id": {"type": "string", "minLength": 1},
                                        "description": {"type": "string"},
                                    },
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "additionalProperties": False,
                    },
                },
            },
            "additionalProperties": False,
        },
        "Diagnostics": {
            "type": "object",
            "required": ["ok", "mode", "summary", "checks"],
            "properties": {
                "ok": {"type": "boolean"},
                "mode": {"enum": ["local", "oidc"]},
                "summary": object_schema,
                "checks": {"type": "array", "items": object_schema},
            },
            "additionalProperties": True,
        },
        "RepositoryDiff": object_schema,
        "GuardrailPolicy": {"type": "object", "additionalProperties": {"type": ["integer", "null"], "minimum": 1}},
        "PreferencePatch": {
            "type": "object",
            "minProperties": 1,
            "properties": {
                "browser_notifications": {"type": "boolean"},
                "theme": {"enum": ["system", "dark", "light"]},
                "log_lines": {"type": "integer", "minimum": 50, "maximum": 200},
            },
            "additionalProperties": False,
        },
        "ExecutorSettingsPatch": {
            "type": "object",
            "properties": {
                "codex_model": {"type": "string"},
                "codex_effort": {"type": "string"},
                "executor_adapter": {"enum": ["claude", "mini-swe-agent"]},
                "claude_model": {"type": "string", "minLength": 1},
                "claude_effort": {"type": "string"},
                "claude_subagent_model": {"type": "string", "minLength": 1},
                "claude_subagent_effort": {"type": "string"},
                "claude_max_turns": {"type": "integer", "minimum": 1, "maximum": 200},
                "claude_local_delegation": {"type": "boolean"},
                "mini_swe_model": {"type": "string"},
                "mini_swe_effort": {"type": "string"},
                "mini_swe_api_base": {"type": "string"},
                "mini_swe_provider": {"type": "string", "minLength": 1},
                "mini_swe_api_key_env": {"type": "string"},
                "mini_swe_step_limit": {"type": "integer", "minimum": 1, "maximum": 200},
                "mini_swe_cost_limit": {"type": "number", "minimum": 0},
                "mini_swe_timeout_seconds": {"type": "integer", "minimum": 10, "maximum": 86400},
            },
            "additionalProperties": False,
        },
        "ExecutorDiscovery": {
            "type": "object",
            "required": ["api_base"],
            "properties": {
                "api_base": {"type": "string", "minLength": 1},
                "api_key_env": {"type": "string"},
            },
            "additionalProperties": False,
        },
        "RepositoryCreate": {
            "type": "object",
            "required": ["name"],
            "properties": {"name": {"type": "string", "minLength": 1, "maxLength": 80}},
            "additionalProperties": False,
        },
        "RepositoryInitialize": {
            "type": "object",
            "required": ["project_name"],
            "properties": {
                "project_name": {"type": "string", "minLength": 1},
                "ci_action": {"enum": ["auto", "add", "skip"]},
            },
            "additionalProperties": False,
        },
        "RepositorySelect": {
            "type": "object",
            "required": ["path"],
            "properties": {"path": {"type": "string", "minLength": 1}},
            "additionalProperties": False,
        },
        "SessionRevoke": {
            "type": "object",
            "required": ["session_id"],
            "properties": {"session_id": {"type": "string", "minLength": 1}},
            "additionalProperties": False,
        },
    }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Coordinator API",
            "version": "1.0.0",
            "description": "Versioned owner-operated workflow control API.",
        },
        "paths": paths,
        "components": {"schemas": schemas},
    }
