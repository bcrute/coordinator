"""Authenticated ASGI runtime for the coordination dashboard."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import socket
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import web_app
from .operational_store import GuardrailPolicy, OperationalStore, evaluate_guardrails
from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.starlette_client import OAuth
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from .security import (
    AUDIT_PAGE_LIMIT,
    LOG,
    PUBLIC_PATHS,
    REPOSITORY_SLUG,
    SESSION_PAGE_LIMIT,
    SETUP_BODY_BYTES,
    STATE_HEARTBEAT_SECONDS,
    STATE_STREAM_SECONDS,
    TERMINAL_MAX_MESSAGE_BYTES,
    TERMINAL_WAIT_SECONDS,
    AccessControlMiddleware,
    LocalSettings,
    OIDCSettings,
    SQLiteSecurityStore,
    SecurityHeadersMiddleware,
    RequestContextMiddleware,
    ServerSideSessionMiddleware,
    _audit_user,
    _authorized,
    _bounded_body,
    _claim_groups,
    _client_source,
    _json_body,
    _jwt_algorithm,
    _local_destination,
    _websocket_origin,
)


def create_authenticated_app(
    repo: Path,
    settings: OIDCSettings | LocalSettings,
    *,
    repositories_root: Path | None = None,
    relay_log_lines: int = web_app.RELAY_LOG_LINES,
    assets: Path = web_app.ASSETS,
    watcher_command_for_repo: Callable[[Path], list[str] | None] | None = None,
    codex_command_for_repo: Callable[[Path], list[str]] | None = None,
    oidc_client: Any | None = None,
    stop_timeout: float = web_app.STOP_TIMEOUT_SECONDS,
    start_grace: float = web_app.START_GRACE_SECONDS,
) -> Starlette:
    """Build the default-deny authenticated ASGI application."""

    root = repo.resolve()
    if not (web_app.is_git_repository(root) or web_app.is_initialized(root)):
        raise ValueError(
            f"{root} is neither a Git repository nor coordination-initialized"
        )
    root_dir = repositories_root.resolve() if repositories_root else root.parent
    if not root_dir.is_dir():
        raise ValueError(f"repositories_root must be a directory: {root_dir}")

    watcher_factory = watcher_command_for_repo or web_app.default_watcher_command
    codex_factory = codex_command_for_repo or web_app.default_codex_command
    context = web_app.ApplicationContext(
        root,
        root_dir,
        watcher_command_for_repo=watcher_factory,
        codex_command_for_repo=codex_factory,
        stop_timeout=stop_timeout,
        start_grace=start_grace,
    )
    asset_routes = web_app.static_assets(assets)
    store = SQLiteSecurityStore(
        settings.state_dir,
        settings.session_idle_seconds,
        settings.session_absolute_seconds,
    )
    operational = OperationalStore(settings.state_dir)
    operational.recover_interrupted()
    terminal_attachment_lock = threading.Lock()
    terminal_attachment_owner: list[str | None] = [None]
    state_cache_lock = threading.Lock()
    state_cache: list[object] = [0.0, None, -1, None]

    def coordination_fingerprint(
        active: web_app.RepositoryContext,
    ) -> tuple[object, ...]:
        """Return the cheap inputs which can change a rendered state snapshot."""

        coordination = active.repo / ".coordination"
        paths = [
            coordination / "README.md",
            coordination / "planner" / "goal.md",
            coordination / "planner" / "roadmap.md",
            coordination / "planner" / "current-task.md",
            coordination / "coder" / "status.md",
            coordination / "reviews" / "latest.md",
            coordination / "reviews" / "completion.md",
            coordination / "runtime" / "claude-progress.json",
            coordination / "runtime" / "goal-timing.json",
            coordination / "runtime" / "relay.log",
        ]
        runtime = coordination / "runtime"
        if runtime.is_dir():
            paths.extend(sorted(runtime.glob("watcher-*-status.json")))
            paths.extend(sorted(runtime.glob("*.lock")))
        files: list[tuple[str, int, int]] = []
        for path in paths:
            try:
                details = path.stat()
                files.append(
                    (
                        str(path.relative_to(active.repo)),
                        details.st_mtime_ns,
                        details.st_size,
                    )
                )
            except (FileNotFoundError, OSError):
                files.append((str(path.relative_to(active.repo)), 0, 0))
        watcher = active.watcher.snapshot()
        codex = active.codex_session.snapshot()
        return (
            str(active.repo),
            watcher.get("state"),
            watcher.get("pid"),
            watcher.get("lock_present"),
            watcher.get("can_start"),
            codex.get("state"),
            codex.get("pid"),
            codex.get("buffer_base_cursor"),
            codex.get("buffer_next_cursor"),
            tuple(files),
        )

    if isinstance(settings, OIDCSettings) and oidc_client is None:
        oauth = OAuth()
        oidc_client = oauth.register(
            "coordinator",
            client_id=settings.client_id,
            client_secret=settings.client_secret,
            server_metadata_url=(
                f"{settings.issuer.rstrip('/')}/.well-known/openid-configuration"
            ),
            client_kwargs={
                "scope": " ".join(settings.scopes),
                "code_challenge_method": "S256",
                "token_endpoint_auth_method": "client_secret_basic",
            },
        )

    async def health(request: Request):
        return JSONResponse({"status": "ok"})

    async def ready(request: Request):
        checks = {
            "repository": context.catalog().get("active") is not None,
            "operational_index": operational.schema_version > 0,
            "security_state": store.path.is_file(),
        }
        return JSONResponse(
            {"status": "ready" if all(checks.values()) else "not_ready", "checks": checks},
            status_code=200 if all(checks.values()) else 503,
        )

    async def metrics(request: Request):
        values = await run_in_threadpool(operational.statistics)
        body = "".join(
            f"coordinator_{name} {value}\n" for name, value in values.items()
        )
        return PlainTextResponse(body, media_type="text/plain; version=0.0.4")

    async def openapi(request: Request):
        paths = {
            "/api/v1/state": {"get": {"summary": "Current workspace state"}},
            "/api/v1/events": {"get": {"summary": "Resumable SSE state and transition stream"}},
            "/api/v1/runs": {"get": {"summary": "Durable run history"}},
            "/api/v1/runs/{run_id}": {"get": {"summary": "Durable run detail"}},
            "/api/v1/preferences": {
                "get": {"summary": "Non-secret preferences"},
                "post": {"summary": "Update non-secret preferences"},
            },
        }
        return JSONResponse(
            {
                "openapi": "3.1.0",
                "info": {"title": "Coordinator API", "version": "1.0.0"},
                "paths": paths,
            }
        )

    async def login(request: Request):
        if not isinstance(settings, OIDCSettings) or oidc_client is None:
            return PlainTextResponse("not found\n", status_code=404)
        request.session["post_login_path"] = _local_destination(
            request.query_params.get("next")
        )
        try:
            return await oidc_client.authorize_redirect(request, settings.callback_url)
        except Exception as error:  # provider failures stay generic to clients and logs
            request.session.clear()
            await run_in_threadpool(
                store.audit,
                "login_start",
                "error",
                source=_client_source(request),
                detail="provider discovery or authorization request failed",
            )
            LOG.error("OIDC authorization request failed (%s)", type(error).__name__)
            return PlainTextResponse(
                "Sign-in is temporarily unavailable.\n", status_code=503
            )

    async def callback(request: Request):
        if not isinstance(settings, OIDCSettings) or oidc_client is None:
            return PlainTextResponse("not found\n", status_code=404)
        try:
            token = await oidc_client.authorize_access_token(request)
            claims = dict(token.get("userinfo") or {})
        except (OAuthError, KeyError, TypeError, ValueError):
            request.session.clear()
            await run_in_threadpool(
                store.audit,
                "login_callback",
                "invalid",
                source=_client_source(request),
                detail="OIDC callback validation failed",
            )
            return PlainTextResponse(
                "Sign-in could not be validated.\n", status_code=401
            )
        except Exception as error:
            request.session.clear()
            await run_in_threadpool(
                store.audit,
                "login_callback",
                "error",
                source=_client_source(request),
                detail="OIDC token exchange failed",
            )
            LOG.error("OIDC callback failed (%s)", type(error).__name__)
            return PlainTextResponse(
                "Sign-in is temporarily unavailable.\n", status_code=503
            )

        issuer = claims.get("iss")
        subject = claims.get("sub")
        algorithm = _jwt_algorithm(token.get("id_token"))
        if (
            algorithm not in settings.id_token_algorithms
            or issuer != settings.issuer
            or not isinstance(subject, str)
            or not _authorized(settings, claims)
        ):
            await run_in_threadpool(
                store.audit,
                "login_callback",
                "denied",
                issuer=issuer if isinstance(issuer, str) else None,
                subject=subject if isinstance(subject, str) else None,
                source=_client_source(request),
                detail="identity did not satisfy the configured allow policy",
            )
            request.session.clear()
            return PlainTextResponse(
                "This identity is not authorized.\n", status_code=403
            )

        destination = _local_destination(request.session.get("post_login_path"))
        groups = sorted(_claim_groups(claims.get(settings.groups_claim)))
        display = claims.get("preferred_username") or claims.get("name") or subject
        request.session.clear()
        request.session.update(
            {
                "user": {
                    "iss": settings.issuer,
                    "sub": subject,
                    "display": str(display),
                    "groups": groups,
                },
                "csrf_token": secrets.token_urlsafe(32),
            }
        )
        request.scope["coordinator.session_rotate"] = True
        await run_in_threadpool(
            store.audit,
            "login_callback",
            "success",
            issuer=settings.issuer,
            subject=subject,
            source=_client_source(request),
        )
        return RedirectResponse(destination, status_code=303)

    async def logout(request: Request):
        if not isinstance(settings, OIDCSettings):
            return JSONResponse(
                {"ok": False, "outcome": "not_available"}, status_code=404
            )
        issuer, subject = _audit_user(request)
        request.session.clear()
        request.scope["coordinator.session_destroy"] = True
        await run_in_threadpool(
            store.audit,
            "logout",
            "success",
            issuer=issuer,
            subject=subject,
            source=_client_source(request),
        )
        redirect = "/auth/login"
        try:
            metadata = await oidc_client.load_server_metadata()
            endpoint = metadata.get("end_session_endpoint")
            if isinstance(endpoint, str) and endpoint.startswith(
                ("https://", "http://")
            ):
                redirect = (
                    endpoint
                    + "?"
                    + urllib.parse.urlencode(
                        {
                            "client_id": settings.client_id,
                            "post_logout_redirect_uri": settings.external_url + "/",
                        }
                    )
                )
        except Exception as error:
            LOG.info(
                "OIDC end-session discovery unavailable (%s)", type(error).__name__
            )
        return JSONResponse({"ok": True, "redirect": redirect})

    def fresh_state_snapshot(session: Mapping[str, Any]) -> dict[str, object]:
        with context.lease() as active:
            payload = web_app.build_state(
                active.repo,
                relay_log_lines,
                active.watcher,
                active.codex_session,
            )
            entries = web_app.discover_repositories(
                context.repositories_root, active.repo
            )
            payload["repository_catalog"] = web_app.catalog_payload(
                entries, active.repo, context.repositories_root
            )
            run = operational.sync_snapshot(active.repo, payload)
            try:
                policy = GuardrailPolicy(**run["policy"])
            except (TypeError, ValueError):
                policy = GuardrailPolicy()
            guardrails = evaluate_guardrails(
                payload,
                policy,
                last_change_at=float(run["last_change_at"]),
            )
            if run["resume_required"]:
                guardrails["status"] = "paused"
                guardrails["reason"] = run["pause_reason"]
            elif guardrails["status"] == "stop":
                reasons = [
                    str(finding["metric"])
                    for finding in guardrails["findings"]
                    if finding["severity"] == "stop"
                ]
                reason = "guardrail limit reached: " + ", ".join(reasons)
                active.codex_session.stop()
                active.watcher.stop()
                operational.pause(str(run["run_id"]), reason)
                guardrails["status"] = "paused"
                guardrails["reason"] = reason
                run["status"] = "paused"
                run["pause_reason"] = reason
                run["resume_required"] = True
            payload["run"] = {
                key: run[key]
                for key in (
                    "repository_id",
                    "run_id",
                    "turn_id",
                    "status",
                    "pause_reason",
                    "resume_required",
                    "last_change_at",
                    "policy",
                )
            }
            payload["guardrails"] = guardrails
            payload["api_version"] = "v1"
        if isinstance(settings, OIDCSettings):
            user = dict(session["user"])
            payload["security"] = {
                "mode": "oidc",
                "authenticated": True,
                "user": {
                    "display": user.get("display"),
                    "sub": user.get("sub"),
                },
                "csrf_token": session["csrf_token"],
            }
        else:
            payload["security"] = {
                "mode": "local",
                "authenticated": False,
                "user": None,
                "csrf_token": session["csrf_token"],
            }
        return payload

    def state_snapshot(session: Mapping[str, Any]) -> dict[str, object]:
        """Share one short-lived filesystem reconstruction across connected clients."""

        current = time.monotonic()
        with context.lease() as active:
            fingerprint = coordination_fingerprint(active)
            with state_cache_lock:
                cached = state_cache[1]
                if (
                    isinstance(cached, dict)
                    and cached.get("repo") == str(active.repo)
                    and current - float(state_cache[0]) < 0.75
                    and state_cache[2] == operational.revision
                    and state_cache[3] == fingerprint
                ):
                    payload = json.loads(json.dumps(cached))
                    if isinstance(settings, OIDCSettings):
                        user = dict(session["user"])
                        payload["security"] = {
                            "mode": "oidc",
                            "authenticated": True,
                            "user": {
                                "display": user.get("display"),
                                "sub": user.get("sub"),
                            },
                            "csrf_token": session["csrf_token"],
                        }
                    else:
                        payload["security"] = {
                            "mode": "local",
                            "authenticated": False,
                            "user": None,
                            "csrf_token": session["csrf_token"],
                        }
                    return payload
                payload = fresh_state_snapshot(session)
                reusable = json.loads(json.dumps(payload))
                reusable.pop("security", None)
                state_cache[:] = [
                    current,
                    reusable,
                    operational.revision,
                    coordination_fingerprint(active),
                ]
                return payload

    async def state(request: Request):
        if not isinstance(request.session.get("csrf_token"), str):
            request.session["csrf_token"] = secrets.token_urlsafe(32)
        return JSONResponse(
            await run_in_threadpool(state_snapshot, dict(request.session))
        )

    async def state_events(request: Request):
        if not isinstance(request.session.get("csrf_token"), str):
            request.session["csrf_token"] = secrets.token_urlsafe(32)
        session = dict(request.session)
        raw_cursor = request.headers.get("last-event-id", "0").strip() or "0"
        if not raw_cursor.isdigit():
            return JSONResponse(
                {"ok": False, "error": {"code": "invalid_cursor", "message": "Last-Event-ID must be an integer"}},
                status_code=400,
            )
        starting_cursor = int(raw_cursor)

        async def events():
            cursor = starting_cursor
            state_cursor = starting_cursor
            last_emit = 0.0
            while not await request.is_disconnected():
                session_id = request.scope.get("coordinator.session_id")
                if session_id and not await run_in_threadpool(
                    store.is_active, session_id
                ):
                    return
                payload = await run_in_threadpool(state_snapshot, session)
                run_id = str(record.get("run_id") if (record := payload.get("run")) else "")
                transitions = await run_in_threadpool(
                    operational.list_events, run_id, cursor
                )
                for transition in transitions:
                    cursor = int(transition["event_id"])
                    encoded_transition = json.dumps(
                        transition, separators=(",", ":"), sort_keys=True
                    )
                    yield f"id: {cursor}\nevent: transition\ndata: {encoded_transition}\n\n"
                encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
                current = time.monotonic()
                latest = await run_in_threadpool(operational.latest_event_id, run_id)
                if latest > state_cursor or last_emit == 0.0:
                    cursor = max(cursor, latest)
                    yield f"id: {cursor}\nevent: state\ndata: {encoded}\n\n"
                    state_cursor = latest
                    last_emit = current
                elif current - last_emit >= STATE_HEARTBEAT_SECONDS:
                    yield ": keepalive\n\n"
                    last_emit = current
                await asyncio.sleep(STATE_STREAM_SECONDS)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"X-Accel-Buffering": "no"},
        )

    async def audit_events(request: Request):
        raw_after = request.query_params.get("after", "0")
        raw_limit = request.query_params.get("limit", str(AUDIT_PAGE_LIMIT))
        if not raw_after.isdigit() or not raw_limit.isdigit():
            return JSONResponse(
                {"ok": False, "outcome": "validation", "message": "invalid pagination"},
                status_code=400,
            )
        events = await run_in_threadpool(
            store.list_audit_events,
            after_id=int(raw_after),
            limit=int(raw_limit),
        )
        return JSONResponse({"ok": True, "events": events})

    async def sessions(request: Request):
        session_id = request.scope.get("coordinator.session_id")
        values = await run_in_threadpool(store.list_sessions, session_id)
        return JSONResponse({"ok": True, "sessions": values})

    async def run_history(request: Request):
        raw_limit = request.query_params.get("limit", "100")
        if not raw_limit.isdigit():
            return JSONResponse(
                {"ok": False, "outcome": "validation", "message": "invalid limit"},
                status_code=400,
            )
        runs = await run_in_threadpool(operational.list_runs, int(raw_limit))
        return JSONResponse({"ok": True, "runs": runs})

    async def run_detail(request: Request):
        run = await run_in_threadpool(operational.get_run, request.path_params["run_id"])
        if run is None:
            return JSONResponse({"ok": False, "outcome": "not_found"}, status_code=404)
        return JSONResponse({"ok": True, "run": run})

    async def run_events(request: Request):
        run_id = request.path_params["run_id"]
        if await run_in_threadpool(operational.get_run, run_id) is None:
            return JSONResponse({"ok": False, "outcome": "not_found"}, status_code=404)
        raw_after = request.query_params.get("after", "0")
        if not raw_after.isdigit():
            return JSONResponse(
                {"ok": False, "outcome": "validation", "message": "invalid cursor"},
                status_code=400,
            )
        events = await run_in_threadpool(
            operational.list_events, run_id, int(raw_after)
        )
        return JSONResponse({"ok": True, "events": events})

    async def run_resume(request: Request):
        body = await _bounded_body(request, web_app.CONTROL_BODY_BYTES)
        if isinstance(body, JSONResponse):
            return body
        if body:
            return JSONResponse(
                {"ok": False, "outcome": "validation", "message": "body must be empty"},
                status_code=400,
            )
        run_id = request.path_params["run_id"]
        resumed = await run_in_threadpool(operational.resume, run_id)
        return JSONResponse(
            {"ok": resumed, "outcome": "resumed" if resumed else "not_found"},
            status_code=200 if resumed else 404,
        )

    async def run_archive(request: Request):
        body = await _bounded_body(request, web_app.CONTROL_BODY_BYTES)
        if isinstance(body, JSONResponse):
            return body
        if body:
            return JSONResponse(
                {"ok": False, "outcome": "validation", "message": "body must be empty"},
                status_code=400,
            )
        run_id = request.path_params["run_id"]
        action = request.path_params["action"]
        if action not in {"archive", "reopen"}:
            return JSONResponse({"ok": False, "outcome": "not_found"}, status_code=404)
        changed = await run_in_threadpool(
            operational.archive if action == "archive" else operational.reopen,
            run_id,
        )
        return JSONResponse(
            {"ok": changed, "outcome": action if changed else "not_found"},
            status_code=200 if changed else 404,
        )

    def repository_diff_snapshot() -> dict[str, object]:
        with context.lease() as active:
            commands = (
                ["git", "status", "--short"],
                ["git", "diff", "--no-ext-diff", "--no-color", "--stat", "--patch"],
                [
                    "git",
                    "diff",
                    "--cached",
                    "--no-ext-diff",
                    "--no-color",
                    "--stat",
                    "--patch",
                ],
            )
            results = [
                subprocess.run(
                    command,
                    cwd=active.repo,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=15,
                    env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
                )
                for command in commands
            ]
            output = "\n".join(result.stdout.rstrip() for result in results if result.stdout)
            limit = 512 * 1024
            truncated = len(output.encode("utf-8")) > limit
            if truncated:
                output = output.encode("utf-8")[:limit].decode("utf-8", errors="replace")
            return {
                "ok": all(result.returncode == 0 for result in results),
                "repository": str(active.repo),
                "diff": output,
                "truncated": truncated,
                "detail": " ".join(
                    result.stderr.strip() for result in results if result.returncode
                )[:500],
            }

    async def repository_diff(request: Request):
        payload = await run_in_threadpool(repository_diff_snapshot)
        return JSONResponse(payload, status_code=200 if payload["ok"] else 409)

    async def run_policy(request: Request):
        value = await _json_body(request, SETUP_BODY_BYTES)
        if isinstance(value, JSONResponse):
            return value
        if not isinstance(value, dict):
            return JSONResponse(
                {"ok": False, "outcome": "validation", "message": "expected policy object"},
                status_code=400,
            )
        allowed = set(GuardrailPolicy().as_dict())
        if set(value) - allowed:
            return JSONResponse(
                {"ok": False, "outcome": "validation", "message": "unknown policy key"},
                status_code=400,
            )
        try:
            policy = GuardrailPolicy(**value)
        except (TypeError, ValueError) as error:
            return JSONResponse(
                {"ok": False, "outcome": "validation", "message": str(error)},
                status_code=400,
            )
        run_id = request.path_params["run_id"]
        updated = await run_in_threadpool(operational.set_policy, run_id, policy)
        return JSONResponse(
            {
                "ok": updated,
                "outcome": "updated" if updated else "not_found",
                "policy": policy.as_dict(),
            },
            status_code=200 if updated else 404,
        )

    async def preferences_get(request: Request):
        return JSONResponse(
            {"ok": True, "preferences": await run_in_threadpool(operational.preferences)}
        )

    async def preferences_update(request: Request):
        value = await _json_body(request, SETUP_BODY_BYTES)
        if isinstance(value, JSONResponse):
            return value
        allowed = {"browser_notifications", "theme", "log_lines"}
        if not isinstance(value, dict) or not value or set(value) - allowed:
            return JSONResponse(
                {
                    "ok": False,
                    "outcome": "validation",
                    "message": "expected one or more supported preferences",
                },
                status_code=400,
            )
        if "browser_notifications" in value and not isinstance(
            value["browser_notifications"], bool
        ):
            return JSONResponse(
                {"ok": False, "outcome": "validation", "message": "invalid notifications value"},
                status_code=400,
            )
        if "theme" in value and value["theme"] not in {"system", "dark", "light"}:
            return JSONResponse(
                {"ok": False, "outcome": "validation", "message": "invalid theme"},
                status_code=400,
            )
        if "log_lines" in value and (
            not isinstance(value["log_lines"], int)
            or isinstance(value["log_lines"], bool)
            or not 50 <= value["log_lines"] <= 200
        ):
            return JSONResponse(
                {"ok": False, "outcome": "validation", "message": "invalid log line limit"},
                status_code=400,
            )
        for key, preference in value.items():
            await run_in_threadpool(operational.set_preference, key, preference)
        preferences = await run_in_threadpool(operational.preferences)
        return JSONResponse(
            {"ok": True, "outcome": "updated", "preferences": preferences}
        )

    async def session_revoke(request: Request):
        value = await _json_body(request, web_app.CONTROL_BODY_BYTES)
        if isinstance(value, JSONResponse):
            return value
        if (
            not isinstance(value, dict)
            or set(value) != {"handle"}
            or not isinstance(value.get("handle"), str)
        ):
            return JSONResponse(
                {
                    "ok": False,
                    "outcome": "validation",
                    "message": "expected session handle",
                },
                status_code=400,
            )
        current_id = request.scope.get("coordinator.session_id")
        current_handle = store.session_handle(current_id) if current_id else ""
        issuer, subject = _audit_user(request)
        revoked = await run_in_threadpool(store.revoke_session_handle, value["handle"])
        if revoked and hmac.compare_digest(current_handle, value["handle"]):
            request.session.clear()
            request.scope["coordinator.session_destroy"] = True
        await run_in_threadpool(
            store.audit,
            "session_revoke",
            "success" if revoked else "not_found",
            issuer=issuer,
            subject=subject,
            source=_client_source(request),
        )
        return JSONResponse(
            {"ok": revoked, "outcome": "revoked" if revoked else "not_found"},
            status_code=200 if revoked else 404,
        )

    async def session_revoke_others(request: Request):
        body = await _bounded_body(request, web_app.CONTROL_BODY_BYTES)
        if isinstance(body, JSONResponse):
            return body
        if body:
            return JSONResponse(
                {"ok": False, "outcome": "validation", "message": "body must be empty"},
                status_code=400,
            )
        session_id = request.scope.get("coordinator.session_id")
        count = await run_in_threadpool(store.revoke_other_sessions, session_id)
        issuer, subject = _audit_user(request)
        await run_in_threadpool(
            store.audit,
            "sessions_revoke_others",
            "success",
            issuer=issuer,
            subject=subject,
            source=_client_source(request),
            detail=f"revoked {count} session(s)",
        )
        return JSONResponse({"ok": True, "outcome": "revoked", "count": count})

    def diagnostics_snapshot() -> dict[str, object]:
        with context.lease() as active:
            repo = active.repo
            coordination = repo / ".coordination"
            checks = [
                {
                    "name": "repository",
                    "ok": repo.is_dir(),
                    "detail": str(repo),
                },
                {
                    "name": "repository writable",
                    "ok": os.access(repo, os.W_OK),
                    "detail": "writable"
                    if os.access(repo, os.W_OK)
                    else "not writable",
                },
                {
                    "name": "Git metadata",
                    "ok": web_app.is_git_repository(repo),
                    "detail": "detected"
                    if web_app.is_git_repository(repo)
                    else "not detected",
                },
                {
                    "name": "coordination files",
                    "ok": coordination.is_dir(),
                    "detail": "initialized"
                    if coordination.is_dir()
                    else "setup needed",
                },
                {
                    "name": "Codex CLI",
                    "ok": shutil.which("codex") is not None,
                    "detail": shutil.which("codex") or "not found on PATH",
                },
                {
                    "name": "Claude CLI",
                    "ok": shutil.which("claude") is not None,
                    "detail": shutil.which("claude") or "not found on PATH",
                },
                {
                    "name": "security state",
                    "ok": os.access(store.state_dir, os.R_OK | os.W_OK),
                    "detail": str(store.state_dir),
                },
                {
                    "name": "operational index",
                    "ok": operational.schema_version > 0,
                    "detail": f"schema {operational.schema_version} at {operational.path}",
                },
            ]
        return {
            "ok": all(bool(check["ok"]) for check in checks),
            "mode": settings.auth_mode,
            "external_url": settings.external_url,
            "issuer": settings.issuer if isinstance(settings, OIDCSettings) else None,
            "checks": checks,
        }

    async def diagnostics(request: Request):
        return JSONResponse(await run_in_threadpool(diagnostics_snapshot))

    def create_repository(slug: str) -> tuple[str, str, dict[str, object]]:
        if not REPOSITORY_SLUG.fullmatch(slug) or slug in {".", ".."}:
            return (
                "validation",
                "Use 1-80 letters, numbers, dots, dashes, or underscores.",
                {},
            )
        target = context.repositories_root / slug
        if target.exists() or target.is_symlink():
            return "conflict", "A repository with that name already exists.", {}
        try:
            target.mkdir(mode=0o755)
            result = subprocess.run(
                ["git", "init", str(target)],
                check=False,
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode != 0:
                target.rmdir()
                return "error", "Git could not initialize the repository.", {}
            outcome, message, catalog = context.select(str(target.resolve()))
            if outcome not in {"selected", "unchanged"}:
                return outcome, message, catalog
            return "created", f"Created and selected {slug}.", catalog
        except (OSError, subprocess.SubprocessError):
            return "error", "The repository could not be created.", {}

    async def repository_create(request: Request):
        value = await _json_body(request, SETUP_BODY_BYTES)
        if isinstance(value, JSONResponse):
            return value
        if (
            not isinstance(value, dict)
            or set(value) != {"name"}
            or not isinstance(value.get("name"), str)
        ):
            return JSONResponse(
                {
                    "ok": False,
                    "outcome": "validation",
                    "message": "expected repository name",
                },
                status_code=400,
            )
        outcome, message, catalog = await run_in_threadpool(
            create_repository, value["name"]
        )
        status = {"created": 201, "validation": 400, "conflict": 409, "error": 500}.get(
            outcome, 500
        )
        issuer, subject = _audit_user(request)
        await run_in_threadpool(
            store.audit,
            "repository_create",
            outcome,
            issuer=issuer,
            subject=subject,
            source=_client_source(request),
            detail=value["name"],
        )
        return JSONResponse(
            {
                "ok": status == 201,
                "outcome": outcome,
                "message": message,
                "repository_catalog": catalog,
            },
            status_code=status,
        )

    def initialize_repository(project_name: str) -> tuple[str, str]:
        cleaned = project_name.strip()
        if (
            not cleaned
            or len(cleaned) > 120
            or any(ord(char) < 0x20 for char in cleaned)
        ):
            return "validation", "Project name must contain 1-120 printable characters."
        with context.lease() as active:
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "coordinator.init_project",
                    str(active.repo),
                    "--project-name",
                    cleaned,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=30,
            )
        if result.returncode != 0:
            return "error", "Coordination initialization failed."
        return "initialized", "Coordination files are ready."

    async def repository_initialize(request: Request):
        value = await _json_body(request, SETUP_BODY_BYTES)
        if isinstance(value, JSONResponse):
            return value
        if (
            not isinstance(value, dict)
            or set(value) != {"project_name"}
            or not isinstance(value.get("project_name"), str)
        ):
            return JSONResponse(
                {
                    "ok": False,
                    "outcome": "validation",
                    "message": "expected project name",
                },
                status_code=400,
            )
        outcome, message = await run_in_threadpool(
            initialize_repository, value["project_name"]
        )
        status = {"initialized": 200, "validation": 400, "error": 500}[outcome]
        issuer, subject = _audit_user(request)
        await run_in_threadpool(
            store.audit,
            "repository_initialize",
            outcome,
            issuer=issuer,
            subject=subject,
            source=_client_source(request),
            detail=value["project_name"],
        )
        return JSONResponse(
            {"ok": status == 200, "outcome": outcome, "message": message},
            status_code=status,
        )

    async def watcher_control(request: Request):
        action = request.path_params["action"]
        body = await _bounded_body(request, web_app.CONTROL_BODY_BYTES)
        if isinstance(body, JSONResponse):
            return body
        if action not in {"start", "stop"}:
            return JSONResponse({"ok": False, "outcome": "not_found"}, status_code=404)

        def operate() -> tuple[int, dict[str, object]]:
            with context.lease() as active:
                outcome, message = (
                    active.watcher.start()
                    if action == "start"
                    else active.watcher.stop()
                )
                status = {
                    "started": 200,
                    "stopped": 200,
                    "conflict": 409,
                    "validation": 400,
                    "error": 500,
                }[outcome]
                return status, {
                    "ok": status == 200,
                    "action": action,
                    "outcome": outcome,
                    "message": message,
                    "managed_watcher": active.watcher.snapshot(),
                }

        status, payload = await run_in_threadpool(operate)
        issuer, subject = _audit_user(request)
        await run_in_threadpool(
            store.audit,
            f"watcher_{action}",
            str(payload["outcome"]),
            issuer=issuer,
            subject=subject,
            source=_client_source(request),
        )
        return JSONResponse(payload, status_code=status)

    async def codex_control(request: Request):
        action = request.path_params["action"]
        if action not in {"start", "stop"}:
            return JSONResponse({"ok": False, "outcome": "not_found"}, status_code=404)
        body = await _bounded_body(request, web_app.CONTROL_BODY_BYTES)
        if isinstance(body, JSONResponse):
            return body
        if body:
            return JSONResponse(
                {"ok": False, "outcome": "too_large", "message": "body must be empty"},
                status_code=413,
            )

        def operate() -> tuple[int, dict[str, object]]:
            with context.lease() as active:
                try:
                    if action == "start":
                        active.codex_session.start()
                        outcome, message = "started", "started the codex session"
                    else:
                        before = active.codex_session.snapshot()
                        active.codex_session.stop()
                        if before.get("running"):
                            outcome, message = "stopped", "stopped the codex session"
                        else:
                            outcome, message = "conflict", "no codex session is running"
                except RuntimeError as error:
                    outcome, message = "conflict", str(error)
                except OSError as error:
                    outcome, message = "error", f"cannot launch codex: {error}"
                status = {
                    "started": 200,
                    "stopped": 200,
                    "conflict": 409,
                    "error": 500,
                }[outcome]
                return status, {
                    "ok": status == 200,
                    "action": f"codex_{action}",
                    "outcome": outcome,
                    "message": message,
                    "codex_session": active.codex_session.snapshot(),
                }

        status, payload = await run_in_threadpool(operate)
        issuer, subject = _audit_user(request)
        await run_in_threadpool(
            store.audit,
            f"codex_{action}",
            str(payload["outcome"]),
            issuer=issuer,
            subject=subject,
            source=_client_source(request),
        )
        return JSONResponse(payload, status_code=status)

    async def repository_select(request: Request):
        value = await _json_body(request, web_app.REPOSITORY_SELECT_BODY_BYTES)
        if isinstance(value, JSONResponse):
            return value
        if (
            not isinstance(value, dict)
            or set(value) != {"path"}
            or not isinstance(value.get("path"), str)
        ):
            return JSONResponse(
                {
                    "ok": False,
                    "outcome": "validation",
                    "message": "expected path string",
                },
                status_code=400,
            )
        outcome, message, catalog = await run_in_threadpool(
            context.select, value["path"]
        )
        status = {"selected": 200, "unchanged": 200, "validation": 400, "error": 500}[
            outcome
        ]
        issuer, subject = _audit_user(request)
        await run_in_threadpool(
            store.audit,
            "repository_select",
            outcome,
            issuer=issuer,
            subject=subject,
            source=_client_source(request),
            detail=value["path"],
        )
        return JSONResponse(
            {
                "ok": status == 200,
                "action": "repository_select",
                "outcome": outcome,
                "message": message,
                "repository_catalog": catalog,
            },
            status_code=status,
        )

    async def terminal_socket(websocket: WebSocket):
        attachment_id = secrets.token_urlsafe(18)
        writable = False
        origin = (websocket.headers.get("origin") or "").strip().lower()
        if not origin or origin != _websocket_origin(websocket, settings).lower():
            await websocket.close(code=1008, reason="origin refused")
            return
        supplied_id = websocket.cookies.get(settings.cookie_name)
        stored = await run_in_threadpool(store.load, supplied_id)
        if stored is None or (
            isinstance(settings, OIDCSettings)
            and not isinstance(stored.data.get("user"), dict)
        ):
            await websocket.close(code=1008, reason="authentication required")
            return
        expected_csrf = stored.data.get("csrf_token")
        await websocket.accept()
        try:
            raw_hello = await asyncio.wait_for(websocket.receive_text(), timeout=5.0)
            if len(raw_hello.encode("utf-8")) > TERMINAL_MAX_MESSAGE_BYTES:
                raise ValueError("message too large")
            hello = json.loads(raw_hello)
            if (
                not isinstance(hello, dict)
                or hello.get("type") != "hello"
                or hello.get("protocol", "terminal.v1") != "terminal.v1"
                or not isinstance(hello.get("csrf_token"), str)
                or not isinstance(expected_csrf, str)
                or not hmac.compare_digest(hello["csrf_token"], expected_csrf)
            ):
                await websocket.close(code=1008, reason="invalid handshake")
                return
        except (TimeoutError, ValueError, json.JSONDecodeError, WebSocketDisconnect):
            await websocket.close(code=1008, reason="invalid handshake")
            return

        with terminal_attachment_lock:
            if terminal_attachment_owner[0] is None:
                terminal_attachment_owner[0] = attachment_id
                writable = True

        with context.lease() as active:
            repository = str(active.repo)
            manager = active.codex_session
        requested_cursor = hello.get("cursor")
        if not isinstance(requested_cursor, int) or isinstance(requested_cursor, bool):
            requested_cursor = None

        async def send_output() -> None:
            cursor: int | None = requested_cursor
            while True:
                if not await run_in_threadpool(store.is_active, supplied_id):
                    await websocket.close(code=1008, reason="session expired")
                    return
                with context.lease() as active:
                    if str(active.repo) != repository:
                        await websocket.send_json({"type": "repository_changed"})
                        return
                output = await run_in_threadpool(
                    manager.wait_for_output, cursor, TERMINAL_WAIT_SECONDS
                )
                cursor_value = output.get("next_cursor")
                if isinstance(cursor_value, int):
                    cursor = cursor_value
                if output.get("text") or output.get("reset"):
                    await websocket.send_json(
                        {
                            "protocol": "terminal.v1",
                            "sequence": output.get("next_cursor", 0),
                            "type": "output",
                            "output": output,
                        }
                    )
                session = manager.snapshot()
                session["attachment"] = {
                    "mode": "read_write" if writable else "read_only",
                    "owned_by_this_connection": writable,
                }
                await websocket.send_json(
                    {
                        "protocol": "terminal.v1",
                        "sequence": session.get("buffer_next_cursor", 0),
                        "type": "session",
                        "session": session,
                    }
                )

        async def receive_input() -> None:
            while True:
                raw = await websocket.receive_text()
                if len(raw.encode("utf-8")) > TERMINAL_MAX_MESSAGE_BYTES:
                    await websocket.send_json(
                        {"type": "error", "message": "terminal message is too large"}
                    )
                    continue
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    await websocket.send_json(
                        {"type": "error", "message": "terminal message is not JSON"}
                    )
                    continue
                if not isinstance(message, dict):
                    continue
                kind = message.get("type")
                try:
                    refreshed = await run_in_threadpool(store.load, supplied_id)
                    if refreshed is None:
                        await websocket.close(code=1008, reason="session expired")
                        return
                    if kind in {"input", "resize"} and not writable:
                        await websocket.send_json(
                            {
                                "type": "error",
                                "message": "terminal is read-only; another browser connection owns input",
                            }
                        )
                        continue
                    if kind == "input" and isinstance(message.get("data"), str):
                        await run_in_threadpool(manager.write, message["data"])
                    elif kind == "resize" and all(
                        isinstance(message.get(key), int)
                        and not isinstance(message.get(key), bool)
                        for key in ("rows", "cols")
                    ):
                        await run_in_threadpool(
                            manager.resize, message["rows"], message["cols"]
                        )
                    elif kind == "ping":
                        await websocket.send_json({"type": "pong"})
                    else:
                        await websocket.send_json(
                            {"type": "error", "message": "invalid terminal message"}
                        )
                except (RuntimeError, ValueError) as error:
                    await websocket.send_json({"type": "error", "message": str(error)})

        sender = asyncio.create_task(send_output())
        receiver = asyncio.create_task(receive_input())
        try:
            done, pending = await asyncio.wait(
                {sender, receiver}, return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                try:
                    task.result()
                except (WebSocketDisconnect, asyncio.CancelledError):
                    pass
        except (WebSocketDisconnect, asyncio.CancelledError):
            pass
        finally:
            sender.cancel()
            receiver.cancel()
            await asyncio.gather(sender, receiver, return_exceptions=True)
            if writable:
                with terminal_attachment_lock:
                    if terminal_attachment_owner[0] == attachment_id:
                        terminal_attachment_owner[0] = None

    async def asset(request: Request):
        path = "/" + request.path_params.get("path", "")
        candidate = asset_routes.get(path)
        if candidate is None:
            return PlainTextResponse("not found\n", status_code=404)
        return FileResponse(
            candidate, media_type=web_app.CONTENT_TYPES.get(candidate.suffix)
        )

    async def post_only(request: Request):
        return PlainTextResponse(
            "method not allowed; use POST\n",
            status_code=405,
            headers={"Allow": "POST"},
        )

    @asynccontextmanager
    async def lifespan(app: Starlette):
        yield
        await run_in_threadpool(context.shutdown)

    routes = [
        Route("/healthz", health, methods=["GET", "HEAD"]),
        Route("/readyz", ready, methods=["GET", "HEAD"]),
        Route("/metrics", metrics, methods=["GET"]),
        Route("/api/v1/openapi.json", openapi, methods=["GET"]),
        Route("/auth/login", login, methods=["GET"]),
        Route("/auth/callback", callback, methods=["GET"]),
        Route("/auth/logout", logout, methods=["POST"]),
        Route("/api/state", state, methods=["GET"]),
        Route("/api/v1/state", state, methods=["GET"]),
        Route("/api/events", state_events, methods=["GET"]),
        Route("/api/v1/events", state_events, methods=["GET"]),
        Route("/api/activity", audit_events, methods=["GET"]),
        Route("/api/sessions", sessions, methods=["GET"]),
        Route("/api/runs", run_history, methods=["GET"]),
        Route("/api/v1/runs", run_history, methods=["GET"]),
        Route("/api/runs/{run_id:str}", run_detail, methods=["GET"]),
        Route("/api/v1/runs/{run_id:str}", run_detail, methods=["GET"]),
        Route("/api/runs/{run_id:str}/events", run_events, methods=["GET"]),
        Route("/api/v1/runs/{run_id:str}/events", run_events, methods=["GET"]),
        Route("/api/runs/{run_id:str}/resume", run_resume, methods=["POST"]),
        Route("/api/runs/{run_id:str}/policy", run_policy, methods=["POST"]),
        Route("/api/runs/{run_id:str}/{action:str}", run_archive, methods=["POST"]),
        Route("/api/repository/diff", repository_diff, methods=["GET"]),
        Route("/api/preferences", preferences_get, methods=["GET"]),
        Route("/api/preferences", preferences_update, methods=["POST"]),
        Route("/api/v1/preferences", preferences_get, methods=["GET"]),
        Route("/api/v1/preferences", preferences_update, methods=["POST"]),
        Route("/api/sessions/revoke", session_revoke, methods=["POST"]),
        Route(
            "/api/sessions/revoke-others",
            session_revoke_others,
            methods=["POST"],
        ),
        Route("/api/diagnostics", diagnostics, methods=["GET"]),
        Route("/api/repository/create", repository_create, methods=["POST"]),
        Route(
            "/api/repository/initialize",
            repository_initialize,
            methods=["POST"],
        ),
        Route("/api/watcher/{action:str}", watcher_control, methods=["POST"]),
        Route("/api/codex/{action:str}", codex_control, methods=["POST"]),
        Route("/api/repository/select", repository_select, methods=["POST"]),
        Route("/api/watcher/{action:str}", post_only, methods=["GET", "HEAD"]),
        Route("/api/codex/{action:str}", post_only, methods=["GET", "HEAD"]),
        Route("/api/repository/select", post_only, methods=["GET", "HEAD"]),
        WebSocketRoute("/ws/terminal", terminal_socket),
        Route("/{path:path}", asset, methods=["GET", "HEAD"]),
    ]
    middleware = [
        Middleware(RequestContextMiddleware),
        Middleware(SecurityHeadersMiddleware),
        Middleware(
            TrustedHostMiddleware,
            allowed_hosts=list(settings.hosts),
            www_redirect=False,
        ),
        Middleware(ServerSideSessionMiddleware, store=store, settings=settings),
        Middleware(AccessControlMiddleware, settings=settings, store=store),
    ]
    app = Starlette(routes=routes, middleware=middleware, lifespan=lifespan)
    app.state.context = context
    app.state.security_store = store
    app.state.operational_store = operational
    app.state.settings = settings
    return app


def settings_from_args(args: Any) -> OIDCSettings:
    """Resolve secret material from the environment and validate OIDC CLI settings."""

    secret_env = str(args.oidc_client_secret_env)
    secret = os.environ.get(secret_env, "")
    if not secret:
        raise ValueError(f"environment variable {secret_env!r} is not set")
    args.forwarded_allow_ips = _validated_forwarded_allow_ips(
        str(args.forwarded_allow_ips)
    )
    state_dir = Path(args.state_dir).expanduser()
    return OIDCSettings(
        issuer=str(args.oidc_issuer),
        client_id=str(args.oidc_client_id),
        client_secret=secret,
        external_url=str(args.external_url),
        allowed_subjects=frozenset(args.allowed_subject or ()),
        allowed_groups=frozenset(args.allowed_group or ()),
        groups_claim=str(args.groups_claim),
        state_dir=state_dir,
        session_idle_seconds=int(args.session_idle_seconds),
        session_absolute_seconds=int(args.session_absolute_seconds),
        secure_cookie=not bool(args.insecure_oidc_http),
        trusted_hosts=tuple(args.trusted_host or ()),
    )


def local_settings_from_args(args: Any, port: int | None = None) -> LocalSettings:
    """Build local runtime settings without granting a non-loopback bind."""

    host = str(args.host)
    if not web_app.is_loopback_host(host):
        raise ValueError(
            "unauthenticated mode refuses a non-loopback bind; configure auth_mode = 'oidc'"
        )
    actual_port = int(args.port if port is None else port)
    url_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    hosts = tuple(dict.fromkeys((host, "127.0.0.1", "localhost", "::1")))
    return LocalSettings(
        external_url=f"http://{url_host}:{actual_port}",
        state_dir=Path(args.state_dir).expanduser(),
        session_idle_seconds=int(args.session_idle_seconds),
        session_absolute_seconds=int(args.session_absolute_seconds),
        trusted_hosts=hosts,
    )


def _validated_forwarded_allow_ips(raw: str) -> str:
    """Normalize an explicit comma-separated trusted proxy IP/CIDR list."""

    entries = [entry.strip() for entry in raw.split(",") if entry.strip()]
    if "*" in entries:
        raise ValueError("forwarded_allow_ips must name trusted proxy addresses, not *")
    for entry in entries:
        try:
            ipaddress.ip_network(entry, strict=False)
        except ValueError as error:
            raise ValueError(
                "forwarded_allow_ips entries must be literal IP addresses or CIDRs"
            ) from error
    return ",".join(entries)


def serve_authenticated(args: Any) -> int:
    """Backward-compatible entry point for the OIDC ASGI runtime."""

    return serve_application(args)


def serve_application(args: Any) -> int:
    """Run the local or authenticated application under Uvicorn."""

    import uvicorn

    # Authlib's debug logs may include transient PKCE material, and generic
    # access logs include the callback query string (authorization code/state).
    # The authenticated runtime uses the redacted SQLite audit trail instead.
    logging.getLogger("authlib").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    listener: socket.socket | None = None
    try:
        if args.auth_mode == "oidc":
            settings: OIDCSettings | LocalSettings = settings_from_args(args)
        else:
            if int(args.port) == 0:
                family = socket.AF_INET6 if ":" in str(args.host) else socket.AF_INET
                listener = socket.socket(family, socket.SOCK_STREAM)
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind((str(args.host), 0))
                listener.listen(128)
                actual_port = int(listener.getsockname()[1])
            else:
                actual_port = int(args.port)
            settings = local_settings_from_args(args, actual_port)
        app = create_authenticated_app(
            args.repo,
            settings,
            repositories_root=args.repositories_root,
            relay_log_lines=args.relay_log_lines,
        )
    except ValueError as error:
        print(f"error: {error}", file=os.sys.stderr)
        if listener is not None:
            listener.close()
        return 2
    mode = "authenticated" if isinstance(settings, OIDCSettings) else "local"
    print(f"Serving {mode} dashboard at {settings.external_url}/", flush=True)
    config = uvicorn.Config(
        app,
        host=str(args.host),
        port=int(args.port),
        access_log=False,
        log_level="warning" if bool(args.quiet) else "info",
        server_header=False,
        proxy_headers=isinstance(settings, OIDCSettings),
        forwarded_allow_ips=(
            args.forwarded_allow_ips if isinstance(settings, OIDCSettings) else ""
        ),
    )
    server = uvicorn.Server(config)
    try:
        try:
            server.run(sockets=[listener] if listener is not None else None)
        except KeyboardInterrupt:
            print("Web app stopped.")
            return 130
    finally:
        if listener is not None:
            listener.close()
    return 0
