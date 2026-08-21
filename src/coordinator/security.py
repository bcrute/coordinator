"""Security settings, sessions, authorization, and HTTP enforcement."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import sqlite3
import stat
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.websockets import WebSocket

LOG = logging.getLogger("coordinator.auth")
PUBLIC_PATHS = frozenset({"/healthz", "/readyz", "/auth/login", "/auth/callback"})
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
SESSION_COOKIE = "__Host-coordinator_session"
DEV_SESSION_COOKIE = "coordinator_session"
LOCAL_SESSION_COOKIE = "coordinator_local_session"
STATE_STREAM_SECONDS = 1.0
STATE_HEARTBEAT_SECONDS = 15.0
TERMINAL_WAIT_SECONDS = 1.0
TERMINAL_MAX_MESSAGE_BYTES = 64 * 1024
SESSION_PAGE_LIMIT = 200
AUDIT_PAGE_LIMIT = 200
SETUP_BODY_BYTES = 16 * 1024
REPOSITORY_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")


@dataclass(frozen=True)
class LocalSettings:
    """Validated settings for the loopback-only ASGI runtime."""

    external_url: str
    state_dir: Path
    session_idle_seconds: int = 3600
    session_absolute_seconds: int = 43200
    secure_cookie: bool = False
    trusted_hosts: tuple[str, ...] = ("127.0.0.1", "localhost", "::1")

    def __post_init__(self) -> None:
        external = self.external_url.rstrip("/")
        object.__setattr__(self, "external_url", external)
        parsed = urllib.parse.urlsplit(external)
        if parsed.scheme != "http" or not parsed.hostname:
            raise ValueError("local external_url must be an absolute HTTP URL")
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname.lower() == "localhost"
        if not loopback:
            raise ValueError("local runtime must use a loopback external URL")
        if self.session_idle_seconds <= 0 or self.session_absolute_seconds <= 0:
            raise ValueError("session lifetimes must be positive")
        if self.session_idle_seconds > self.session_absolute_seconds:
            raise ValueError("session idle lifetime must not exceed absolute lifetime")
        if any("*" in host for host in self.trusted_hosts):
            raise ValueError("trusted_hosts must not contain wildcards")

    @property
    def origin(self) -> str:
        parsed = urllib.parse.urlsplit(self.external_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    @property
    def cookie_name(self) -> str:
        return LOCAL_SESSION_COOKIE

    @property
    def hosts(self) -> tuple[str, ...]:
        return self.trusted_hosts

    @property
    def auth_mode(self) -> str:
        return "local"


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

    @property
    def auth_mode(self) -> str:
        return "oidc"


@dataclass
class StoredSession:
    session_id: str
    data: dict[str, Any]
    created_at: float
    last_seen_at: float


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
            session_id,
            data if isinstance(data, dict) else {},
            float(created_at),
            current,
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

    def is_active(self, session_id: str | None, now: float | None = None) -> bool:
        """Check revocation/expiry without extending the idle lifetime."""

        if not session_id or len(session_id) > 256:
            return False
        current = time.time() if now is None else now
        with self._connect() as connection:
            row = connection.execute(
                "SELECT created_at, last_seen_at FROM sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            if row is None:
                return False
            created_at, last_seen_at = row
            if (
                current - float(last_seen_at) > self.idle_seconds
                or current - float(created_at) > self.absolute_seconds
            ):
                connection.execute(
                    "DELETE FROM sessions WHERE session_id = ?", (session_id,)
                )
                return False
        return True

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

    @staticmethod
    def session_handle(session_id: str) -> str:
        return hashlib.sha256(session_id.encode("utf-8")).hexdigest()

    def list_sessions(self, current_session_id: str | None) -> list[dict[str, object]]:
        now = time.time()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT session_id, data_json, created_at, last_seen_at "
                "FROM sessions ORDER BY last_seen_at DESC LIMIT ?",
                (SESSION_PAGE_LIMIT,),
            ).fetchall()
        sessions: list[dict[str, object]] = []
        for session_id, data_json, created_at, last_seen_at in rows:
            if (
                now - float(last_seen_at) > self.idle_seconds
                or now - float(created_at) > self.absolute_seconds
            ):
                self.delete(str(session_id))
                continue
            try:
                data = json.loads(str(data_json))
            except (TypeError, ValueError, json.JSONDecodeError):
                data = {}
            user = data.get("user") if isinstance(data, dict) else None
            sessions.append(
                {
                    "handle": self.session_handle(str(session_id)),
                    "current": hmac.compare_digest(
                        str(session_id), current_session_id or ""
                    ),
                    "created_at": float(created_at),
                    "last_seen_at": float(last_seen_at),
                    "subject": user.get("sub") if isinstance(user, dict) else None,
                    "display": user.get("display") if isinstance(user, dict) else None,
                }
            )
        return sessions

    def revoke_session_handle(self, handle: str) -> bool:
        if re.fullmatch(r"[0-9a-f]{64}", handle) is None:
            return False
        with self._connect() as connection:
            rows = connection.execute("SELECT session_id FROM sessions").fetchall()
            for (session_id,) in rows:
                if hmac.compare_digest(self.session_handle(str(session_id)), handle):
                    connection.execute(
                        "DELETE FROM sessions WHERE session_id = ?", (session_id,)
                    )
                    return True
        return False

    def revoke_other_sessions(self, current_session_id: str | None) -> int:
        with self._connect() as connection:
            if current_session_id:
                cursor = connection.execute(
                    "DELETE FROM sessions WHERE session_id <> ?", (current_session_id,)
                )
            else:
                cursor = connection.execute("DELETE FROM sessions")
            return max(0, int(cursor.rowcount))

    def list_audit_events(
        self, *, after_id: int = 0, limit: int = AUDIT_PAGE_LIMIT
    ) -> list[dict[str, object]]:
        bounded = max(1, min(limit, AUDIT_PAGE_LIMIT))
        with self._connect() as connection:
            if after_id > 0:
                rows = connection.execute(
                    "SELECT event_id, created_at, event, outcome, issuer, subject, source, detail "
                    "FROM audit_events WHERE event_id > ? ORDER BY event_id ASC LIMIT ?",
                    (after_id, bounded),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT event_id, created_at, event, outcome, issuer, subject, source, detail "
                    "FROM audit_events ORDER BY event_id DESC LIMIT ?",
                    (bounded,),
                ).fetchall()
                rows.reverse()
        return [
            {
                "id": int(row[0]),
                "created_at": float(row[1]),
                "event": row[2],
                "outcome": row[3],
                "issuer": row[4],
                "subject": row[5],
                "source": row[6],
                "detail": row[7],
            }
            for row in rows
        ]


class ServerSideSessionMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app: Any,
        *,
        store: SQLiteSecurityStore,
        settings: OIDCSettings | LocalSettings,
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
        self,
        app: Any,
        *,
        settings: OIDCSettings | LocalSettings,
        store: SQLiteSecurityStore,
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
        oidc_mode = isinstance(self.settings, OIDCSettings)
        if (
            oidc_mode
            and path not in PUBLIC_PATHS
            and not isinstance(request.session.get("user"), dict)
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

        protected_path = not oidc_mode or path not in PUBLIC_PATHS
        if protected_path and request.method.upper() in UNSAFE_METHODS:
            site = (request.headers.get("sec-fetch-site") or "").strip().lower()
            origin = (request.headers.get("origin") or "").strip().lower()
            if site and site not in {"same-origin", "none"}:
                await self.audit_denial(request, "csrf", "cross-site fetch metadata")
                return _forbidden("cross-site control requests are refused")
            if origin and origin != _request_origin(request, self.settings).lower():
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
            "form-action 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        )
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        return response


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Attach a correlation identifier and emit one bounded structured access event."""

    async def dispatch(self, request: Request, call_next: Callable[..., Any]):
        request_id = secrets.token_hex(12)
        request.state.request_id = request_id
        started = time.monotonic()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        LOG.info(
            json.dumps(
                {
                    "event": "http_request",
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": round((time.monotonic() - started) * 1000, 3),
                },
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return response


def _forbidden(message: str) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "outcome": "forbidden", "message": message}, status_code=403
    )


def _client_source(request: Request) -> str | None:
    return request.client.host if request.client else None


def _request_origin(request: Request, settings: OIDCSettings | LocalSettings) -> str:
    """Return the canonical control origin for this request's runtime mode."""

    if isinstance(settings, OIDCSettings):
        return settings.origin
    host = (request.headers.get("host") or "").strip()
    return f"{request.url.scheme}://{host}" if host else settings.origin


def _websocket_origin(
    websocket: WebSocket, settings: OIDCSettings | LocalSettings
) -> str:
    if isinstance(settings, OIDCSettings):
        return settings.origin
    host = (websocket.headers.get("host") or "").strip()
    scheme = "https" if websocket.url.scheme == "wss" else "http"
    return f"{scheme}://{host}" if host else settings.origin


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
