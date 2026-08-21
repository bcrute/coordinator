"""Authenticated ASGI runtime for the coordination dashboard."""

from __future__ import annotations

import base64
import hmac
import ipaddress
import json
import logging
import os
import secrets
import sqlite3
import stat
import time
import urllib.parse
from collections.abc import Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import web_app
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
)
from starlette.routing import Route

LOG = logging.getLogger("coordinator.auth")
PUBLIC_PATHS = frozenset({"/healthz", "/auth/login", "/auth/callback"})
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
SESSION_COOKIE = "__Host-coordinator_session"
DEV_SESSION_COOKIE = "coordinator_session"


@dataclass(frozen=True)
class OIDCSettings:
    """Validated settings for one authenticated deployment."""

    issuer: str
    client_id: str
    client_secret: str = field(repr=False)
    external_url: str
    allowed_subjects: frozenset[str]
    allowed_groups: frozenset[str]
    groups_claim: str = "groups"
    scopes: tuple[str, ...] = ("openid", "profile")
    state_dir: Path = Path(".coordinator-state")
    session_idle_seconds: int = 3600
    session_absolute_seconds: int = 43200
    secure_cookie: bool = True
    trusted_hosts: tuple[str, ...] = ()
    id_token_algorithms: tuple[str, ...] = ("RS256",)

    def __post_init__(self) -> None:
        issuer = self.issuer.strip()
        external = self.external_url.rstrip("/")
        normalized_hosts = tuple(
            host.strip().lower() for host in self.trusted_hosts if host.strip()
        )
        object.__setattr__(self, "issuer", issuer)
        object.__setattr__(self, "external_url", external)
        object.__setattr__(self, "trusted_hosts", normalized_hosts)
        if not issuer or not self.client_id or not self.client_secret:
            raise ValueError("OIDC issuer, client id, and client secret are required")
        if not (self.allowed_subjects or self.allowed_groups):
            raise ValueError("OIDC mode requires at least one allowed subject or group")
        if not self.groups_claim.strip():
            raise ValueError("groups_claim must not be empty")
        if "openid" not in self.scopes:
            raise ValueError("OIDC scopes must include openid")
        if self.session_idle_seconds <= 0 or self.session_absolute_seconds <= 0:
            raise ValueError("session lifetimes must be positive")
        if self.session_idle_seconds > self.session_absolute_seconds:
            raise ValueError("session idle lifetime must not exceed absolute lifetime")
        if not self.id_token_algorithms or any(
            algorithm in {"none", "HS256", "HS384", "HS512"}
            for algorithm in self.id_token_algorithms
        ):
            raise ValueError(
                "id_token_algorithms must contain only expected asymmetric algorithms"
            )
        parsed_issuer = urllib.parse.urlsplit(issuer)
        parsed_external = urllib.parse.urlsplit(external)
        for label, parsed in (
            ("issuer", parsed_issuer),
            ("external_url", parsed_external),
        ):
            if parsed.scheme not in (
                {"https"} if self.secure_cookie else {"http", "https"}
            ):
                raise ValueError(f"{label} must use HTTPS")
            if not parsed.hostname or parsed.query or parsed.fragment:
                raise ValueError(
                    f"{label} must be an absolute URL without query or fragment"
                )
            if parsed.username is not None or parsed.password is not None:
                raise ValueError(f"{label} must not contain URL credentials")
        if parsed_external.path not in ("", "/"):
            raise ValueError("external_url must not contain a path")
        if not self.secure_cookie:
            hostname = parsed_external.hostname or ""
            try:
                loopback = ipaddress.ip_address(hostname).is_loopback
            except ValueError:
                loopback = hostname.lower() == "localhost"
            if not loopback:
                raise ValueError("insecure OIDC HTTP is permitted only on loopback")
        if any("*" in host for host in self.hosts):
            raise ValueError("trusted_hosts must not contain wildcards")
        if parsed_external.hostname not in self.hosts:
            raise ValueError("trusted_hosts must include the external URL hostname")

    @property
    def origin(self) -> str:
        parsed = urllib.parse.urlsplit(self.external_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    @property
    def callback_url(self) -> str:
        return f"{self.external_url}/auth/callback"

    @property
    def cookie_name(self) -> str:
        return SESSION_COOKIE if self.secure_cookie else DEV_SESSION_COOKIE

    @property
    def hosts(self) -> tuple[str, ...]:
        configured = tuple(host for host in self.trusted_hosts if host)
        if configured:
            return configured
        hostname = urllib.parse.urlsplit(self.external_url).hostname
        return (hostname,) if hostname else ()


@dataclass
class StoredSession:
    session_id: str
    data: dict[str, Any]
    created_at: float


class SQLiteSecurityStore:
    """Owner-readable SQLite storage for opaque sessions and audit events."""

    def __init__(
        self, state_dir: Path, idle_seconds: int, absolute_seconds: int
    ) -> None:
        self.state_dir = state_dir.resolve()
        self.path = self.state_dir / "security.sqlite3"
        self.idle_seconds = idle_seconds
        self.absolute_seconds = absolute_seconds
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            self.state_dir.chmod(0o700)
        except OSError as error:
            raise ValueError(f"cannot make state_dir owner-only: {error}") from error
        directory_stat = self.state_dir.stat()
        if (
            directory_stat.st_uid != os.geteuid()
            or stat.S_IMODE(directory_stat.st_mode) & 0o077
        ):
            raise ValueError(
                "state_dir must be owned by the service user and mode 0700"
            )
        if self.path.is_symlink():
            raise ValueError("security database path must not be a symbolic link")
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10.0)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            version = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version not in {0, 1}:
                raise ValueError(
                    f"unsupported security database schema version: {version}"
                )
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    data_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at REAL NOT NULL,
                    event TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    issuer TEXT,
                    subject TEXT,
                    source TEXT,
                    detail TEXT
                );
                CREATE INDEX IF NOT EXISTS audit_events_created_at
                    ON audit_events(created_at);
                PRAGMA user_version = 1;
                """
            )
        try:
            self.path.chmod(0o600)
        except OSError as error:
            raise ValueError(
                f"cannot make security database owner-only: {error}"
            ) from error
        file_stat = self.path.stat()
        if file_stat.st_uid != os.geteuid() or stat.S_IMODE(file_stat.st_mode) & 0o077:
            raise ValueError(
                "security database must be owned by the service user and mode 0600"
            )

    def load(
        self, session_id: str | None, now: float | None = None
    ) -> StoredSession | None:
        if not session_id or len(session_id) > 256:
            return None
        current = time.time() if now is None else now
        with self._connect() as connection:
            row = connection.execute(
                "SELECT data_json, created_at, last_seen_at FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            data_json, created_at, last_seen_at = row
            expired = (
                current - float(last_seen_at) > self.idle_seconds
                or current - float(created_at) > self.absolute_seconds
            )
            if expired:
                connection.execute(
                    "DELETE FROM sessions WHERE session_id = ?", (session_id,)
                )
                return None
            connection.execute(
                "UPDATE sessions SET last_seen_at = ? WHERE session_id = ?",
                (current, session_id),
            )
        try:
            data = json.loads(str(data_json))
        except (TypeError, ValueError, json.JSONDecodeError):
            self.delete(session_id)
            return None
        return StoredSession(
            session_id, data if isinstance(data, dict) else {}, float(created_at)
        )

    def save(
        self,
        session_id: str,
        data: Mapping[str, Any],
        created_at: float,
        now: float | None = None,
    ) -> None:
        current = time.time() if now is None else now
        encoded = json.dumps(dict(data), separators=(",", ":"), sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions(session_id, data_json, created_at, last_seen_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    data_json = excluded.data_json,
                    last_seen_at = excluded.last_seen_at
                """,
                (session_id, encoded, created_at, current),
            )

    def rotate(
        self,
        old_session_id: str | None,
        data: Mapping[str, Any],
        now: float | None = None,
    ) -> tuple[str, float]:
        current = time.time() if now is None else now
        new_session_id = secrets.token_urlsafe(32)
        encoded = json.dumps(dict(data), separators=(",", ":"), sort_keys=True)
        with self._connect() as connection:
            if old_session_id:
                connection.execute(
                    "DELETE FROM sessions WHERE session_id = ?", (old_session_id,)
                )
            connection.execute(
                "INSERT INTO sessions(session_id, data_json, created_at, last_seen_at) "
                "VALUES (?, ?, ?, ?)",
                (new_session_id, encoded, current, current),
            )
        return new_session_id, current

    def delete(self, session_id: str | None) -> None:
        if not session_id:
            return
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM sessions WHERE session_id = ?", (session_id,)
            )

    def audit(
        self,
        event: str,
        outcome: str,
        *,
        issuer: str | None = None,
        subject: str | None = None,
        source: str | None = None,
        detail: str | None = None,
    ) -> None:
        safe_detail = detail[:500] if detail else None
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO audit_events(created_at, event, outcome, issuer, subject, source, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (time.time(), event, outcome, issuer, subject, source, safe_detail),
            )


class ServerSideSessionMiddleware(BaseHTTPMiddleware):
    def __init__(
        self, app: Any, *, store: SQLiteSecurityStore, settings: OIDCSettings
    ) -> None:
        super().__init__(app)
        self.store = store
        self.settings = settings

    async def dispatch(self, request: Request, call_next: Callable[..., Any]):
        supplied_id = request.cookies.get(self.settings.cookie_name)
        stored = await run_in_threadpool(self.store.load, supplied_id)
        request.scope["session"] = dict(stored.data) if stored else {}
        request.scope["coordinator.session_id"] = stored.session_id if stored else None
        request.scope["coordinator.session_created_at"] = (
            stored.created_at if stored else time.time()
        )
        response = await call_next(request)

        session = request.scope["session"]
        session_id = request.scope.get("coordinator.session_id")
        if request.scope.get("coordinator.session_destroy"):
            await run_in_threadpool(self.store.delete, session_id)
            response.delete_cookie(
                self.settings.cookie_name,
                path="/",
                secure=self.settings.secure_cookie,
                httponly=True,
                samesite="lax",
            )
            return response

        if not session:
            if session_id:
                await run_in_threadpool(self.store.delete, session_id)
                response.delete_cookie(
                    self.settings.cookie_name,
                    path="/",
                    secure=self.settings.secure_cookie,
                    httponly=True,
                    samesite="lax",
                )
            return response

        rotate = bool(request.scope.get("coordinator.session_rotate"))
        if rotate or not session_id:
            session_id, created_at = await run_in_threadpool(
                self.store.rotate, session_id, session
            )
            request.scope["coordinator.session_id"] = session_id
            request.scope["coordinator.session_created_at"] = created_at
            response.set_cookie(
                self.settings.cookie_name,
                session_id,
                max_age=self.settings.session_absolute_seconds,
                path="/",
                secure=self.settings.secure_cookie,
                httponly=True,
                samesite="lax",
            )
        else:
            await run_in_threadpool(
                self.store.save,
                session_id,
                session,
                float(request.scope["coordinator.session_created_at"]),
            )
        return response


class AccessControlMiddleware(BaseHTTPMiddleware):
    def __init__(
        self, app: Any, *, settings: OIDCSettings, store: SQLiteSecurityStore
    ) -> None:
        super().__init__(app)
        self.settings = settings
        self.store = store

    async def audit_denial(self, request: Request, event: str, detail: str) -> None:
        issuer, subject = _audit_user(request)
        await run_in_threadpool(
            self.store.audit,
            event,
            "denied",
            issuer=issuer,
            subject=subject,
            source=_client_source(request),
            detail=f"{request.method} {request.url.path}: {detail}",
        )

    async def dispatch(self, request: Request, call_next: Callable[..., Any]):
        path = request.url.path
        if path not in PUBLIC_PATHS and not isinstance(
            request.session.get("user"), dict
        ):
            await self.audit_denial(request, "authorization", "authentication required")
            if path.startswith("/api/"):
                return JSONResponse(
                    {
                        "ok": False,
                        "outcome": "unauthenticated",
                        "message": "sign in required",
                    },
                    status_code=401,
                )
            destination = path
            if request.url.query:
                destination += f"?{request.url.query}"
            return RedirectResponse(
                "/auth/login?next=" + urllib.parse.quote(destination, safe="/"),
                status_code=303,
            )

        if path not in PUBLIC_PATHS and request.method.upper() in UNSAFE_METHODS:
            site = (request.headers.get("sec-fetch-site") or "").strip().lower()
            origin = (request.headers.get("origin") or "").strip().lower()
            if site and site not in {"same-origin", "none"}:
                await self.audit_denial(request, "csrf", "cross-site fetch metadata")
                return _forbidden("cross-site control requests are refused")
            if origin and origin != self.settings.origin.lower():
                await self.audit_denial(request, "csrf", "origin mismatch")
                return _forbidden(
                    "request origin is not the configured application origin"
                )
            expected = request.session.get("csrf_token")
            supplied = request.headers.get("x-csrf-token")
            if (
                not isinstance(expected, str)
                or not isinstance(supplied, str)
                or not hmac.compare_digest(expected, supplied)
            ):
                await self.audit_denial(request, "csrf", "missing or mismatched token")
                return _forbidden("a valid CSRF token is required")
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable[..., Any]):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Pragma"] = "no-cache"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; "
            "form-action 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
            "connect-src 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        )
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        return response


def _forbidden(message: str) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "outcome": "forbidden", "message": message}, status_code=403
    )


def _client_source(request: Request) -> str | None:
    return request.client.host if request.client else None


def _local_destination(raw: str | None) -> str:
    if (
        not raw
        or not raw.startswith("/")
        or raw.startswith("//")
        or "\\" in raw
        or any(ord(character) < 0x20 for character in raw)
    ):
        return "/"
    parsed = urllib.parse.urlsplit(raw)
    return raw if not parsed.scheme and not parsed.netloc else "/"


def _claim_groups(value: object) -> frozenset[str]:
    if isinstance(value, str):
        return frozenset({value})
    if isinstance(value, list):
        return frozenset(item for item in value if isinstance(item, str))
    return frozenset()


def _authorized(settings: OIDCSettings, claims: Mapping[str, Any]) -> bool:
    subject = claims.get("sub")
    groups = _claim_groups(claims.get(settings.groups_claim))
    return isinstance(subject, str) and (
        subject in settings.allowed_subjects
        or bool(groups.intersection(settings.allowed_groups))
    )


def _jwt_algorithm(compact: object) -> str | None:
    if not isinstance(compact, str):
        return None
    try:
        raw = compact.split(".", 1)[0]
        raw += "=" * (-len(raw) % 4)
        header = json.loads(base64.urlsafe_b64decode(raw).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    value = header.get("alg") if isinstance(header, dict) else None
    return value if isinstance(value, str) else None


async def _bounded_body(request: Request, limit: int) -> bytes | JSONResponse:
    header = request.headers.get("content-length")
    if header:
        try:
            declared = int(header)
        except ValueError:
            return JSONResponse(
                {
                    "ok": False,
                    "outcome": "bad_request",
                    "message": "invalid Content-Length",
                },
                status_code=400,
            )
        if declared < 0 or declared > limit:
            return JSONResponse(
                {
                    "ok": False,
                    "outcome": "too_large",
                    "message": "request body is too large",
                },
                status_code=413,
            )
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return JSONResponse(
                {
                    "ok": False,
                    "outcome": "too_large",
                    "message": "request body is too large",
                },
                status_code=413,
            )
        chunks.append(chunk)
    return b"".join(chunks)


async def _json_body(request: Request, limit: int) -> object | JSONResponse:
    raw = await _bounded_body(request, limit)
    if isinstance(raw, JSONResponse):
        return raw
    try:
        return json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JSONResponse(
            {
                "ok": False,
                "outcome": "bad_request",
                "message": "request body is not valid JSON",
            },
            status_code=400,
        )


def _audit_user(request: Request) -> tuple[str | None, str | None]:
    user = request.session.get("user")
    if not isinstance(user, dict):
        return None, None
    issuer = user.get("iss")
    subject = user.get("sub")
    return (
        issuer if isinstance(issuer, str) else None,
        subject if isinstance(subject, str) else None,
    )


def create_authenticated_app(
    repo: Path,
    settings: OIDCSettings,
    *,
    repositories_root: Path | None = None,
    relay_log_lines: int = web_app.RELAY_LOG_LINES,
    assets: Path = web_app.ASSETS,
    watcher_command_for_repo: Callable[[Path], list[str] | None] | None = None,
    codex_command_for_repo: Callable[[Path], list[str]] | None = None,
    oidc_client: Any | None = None,
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

    watcher_factory = watcher_command_for_repo or (lambda candidate: None)
    codex_factory = codex_command_for_repo or web_app.default_codex_command
    context = web_app.ApplicationContext(
        root,
        root_dir,
        watcher_command_for_repo=watcher_factory,
        codex_command_for_repo=codex_factory,
    )
    asset_routes = web_app.static_assets(assets)
    store = SQLiteSecurityStore(
        settings.state_dir,
        settings.session_idle_seconds,
        settings.session_absolute_seconds,
    )

    if oidc_client is None:
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

    async def login(request: Request):
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
        return JSONResponse({"ok": True, "redirect": "/auth/login"})

    async def state(request: Request):
        def snapshot() -> dict[str, object]:
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
            user = dict(request.session["user"])
            payload["security"] = {
                "authenticated": True,
                "user": {"display": user.get("display"), "sub": user.get("sub")},
                "csrf_token": request.session["csrf_token"],
            }
            return payload

        return JSONResponse(await run_in_threadpool(snapshot))

    async def codex_output(request: Request):
        names = list(request.query_params.keys())
        if names and names != ["cursor"]:
            return JSONResponse(
                {
                    "ok": False,
                    "outcome": "validation",
                    "message": "only cursor is accepted",
                },
                status_code=400,
            )
        cursor: int | None = None
        if names:
            raw = request.query_params["cursor"]
            if not raw.isdigit():
                return JSONResponse(
                    {"ok": False, "outcome": "validation", "message": "invalid cursor"},
                    status_code=400,
                )
            cursor = int(raw)

        def read_output() -> dict[str, object]:
            with context.lease() as active:
                return {
                    "ok": True,
                    "action": "codex_output",
                    "outcome": "read",
                    "output": active.codex_session.read(cursor),
                    "codex_session": active.codex_session.snapshot(),
                }

        return JSONResponse(await run_in_threadpool(read_output))

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

    async def codex_input(request: Request):
        value = await _json_body(request, web_app.CODEX_INPUT_BODY_BYTES)
        if isinstance(value, JSONResponse):
            return value
        if (
            not isinstance(value, dict)
            or set(value) != {"data"}
            or not isinstance(value.get("data"), str)
        ):
            return JSONResponse(
                {
                    "ok": False,
                    "outcome": "validation",
                    "message": "expected data string",
                },
                status_code=400,
            )

        def operate() -> tuple[int, dict[str, object]]:
            with context.lease() as active:
                try:
                    active.codex_session.write(value["data"])
                    outcome, message = "accepted", "wrote input to the codex session"
                except ValueError as error:
                    outcome, message = "validation", str(error)
                except RuntimeError as error:
                    outcome, message = "conflict", str(error)
                status = {"accepted": 200, "validation": 400, "conflict": 409}[outcome]
                return status, {
                    "ok": status == 200,
                    "action": "codex_input",
                    "outcome": outcome,
                    "message": message,
                    "codex_session": active.codex_session.snapshot(),
                }

        status, payload = await run_in_threadpool(operate)
        return JSONResponse(payload, status_code=status)

    async def codex_resize(request: Request):
        value = await _json_body(request, web_app.CODEX_INPUT_BODY_BYTES)
        if isinstance(value, JSONResponse):
            return value
        valid = (
            isinstance(value, dict)
            and set(value) == {"rows", "cols"}
            and all(
                isinstance(value.get(key), int) and not isinstance(value.get(key), bool)
                for key in ("rows", "cols")
            )
        )
        if not valid:
            return JSONResponse(
                {
                    "ok": False,
                    "outcome": "validation",
                    "message": "expected integer rows and cols",
                },
                status_code=400,
            )

        def operate() -> tuple[int, dict[str, object]]:
            with context.lease() as active:
                try:
                    active.codex_session.resize(value["rows"], value["cols"])
                    outcome, message = "accepted", "resized the codex session"
                except ValueError as error:
                    outcome, message = "validation", str(error)
                status = {"accepted": 200, "validation": 400}[outcome]
                return status, {
                    "ok": status == 200,
                    "action": "codex_resize",
                    "outcome": outcome,
                    "message": message,
                    "codex_session": active.codex_session.snapshot(),
                }

        status, payload = await run_in_threadpool(operate)
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

    async def asset(request: Request):
        path = "/" + request.path_params.get("path", "")
        candidate = asset_routes.get(path)
        if candidate is None:
            return PlainTextResponse("not found\n", status_code=404)
        return FileResponse(
            candidate, media_type=web_app.CONTENT_TYPES.get(candidate.suffix)
        )

    @asynccontextmanager
    async def lifespan(app: Starlette):
        yield
        await run_in_threadpool(context.shutdown)

    routes = [
        Route("/healthz", health, methods=["GET", "HEAD"]),
        Route("/auth/login", login, methods=["GET"]),
        Route("/auth/callback", callback, methods=["GET"]),
        Route("/auth/logout", logout, methods=["POST"]),
        Route("/api/state", state, methods=["GET"]),
        Route("/api/codex/output", codex_output, methods=["GET"]),
        Route("/api/watcher/{action:str}", watcher_control, methods=["POST"]),
        Route("/api/codex/input", codex_input, methods=["POST"]),
        Route("/api/codex/resize", codex_resize, methods=["POST"]),
        Route("/api/codex/{action:str}", codex_control, methods=["POST"]),
        Route("/api/repository/select", repository_select, methods=["POST"]),
        Route("/{path:path}", asset, methods=["GET", "HEAD"]),
    ]
    middleware = [
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
    app.state.oidc_settings = settings
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
    """Run the authenticated application under Uvicorn."""

    import uvicorn

    # Authlib's debug logs may include transient PKCE material, and generic
    # access logs include the callback query string (authorization code/state).
    # The authenticated runtime uses the redacted SQLite audit trail instead.
    logging.getLogger("authlib").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        settings = settings_from_args(args)
        app = create_authenticated_app(
            args.repo,
            settings,
            repositories_root=args.repositories_root,
            relay_log_lines=args.relay_log_lines,
        )
    except ValueError as error:
        print(f"error: {error}", file=os.sys.stderr)
        return 2
    print(f"Serving authenticated dashboard at {settings.external_url}/", flush=True)
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        access_log=False,
        server_header=False,
        proxy_headers=True,
        forwarded_allow_ips=args.forwarded_allow_ips,
    )
    return 0
