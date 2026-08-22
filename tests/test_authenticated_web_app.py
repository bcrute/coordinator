"""Security contracts for the authenticated ASGI dashboard."""

# ruff: noqa: E402 -- the script directory is intentionally injected below.

from __future__ import annotations

import json
import re
import stat
import sys
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from authlib.integrations.base_client.errors import OAuthError
from joserfc import jwt
from joserfc.jwk import RSAKey
from starlette.responses import RedirectResponse
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "coordinate-claude-work" / "scripts"
sys.path.insert(0, str(SCRIPTS))

from authenticated_web_app import (
    PUBLIC_PATHS,
    LocalSettings,
    OIDCSettings,
    SQLiteSecurityStore,
    _local_destination,
    _validated_forwarded_allow_ips,
    create_authenticated_app,
)
from coordinator.authenticated_web_app import (
    TERMINAL_OUTPUT_CHUNK_CHARS,
    _terminal_output_chunks,
)


class FakeOIDCClient:
    def __init__(
        self,
        claims: dict[str, object],
        algorithm: str = "RS256",
        end_session_endpoint: str | None = None,
        jwks: dict[str, object] | None = None,
    ) -> None:
        self.claims = claims
        self.algorithm = algorithm
        self.end_session_endpoint = end_session_endpoint
        self.jwks = jwks

    async def authorize_redirect(self, request, redirect_uri):
        request.session["fake_oidc_state"] = {
            "state": "test-state",
            "nonce": "test-nonce",
            "code_verifier": "test-verifier",
            "redirect_uri": redirect_uri,
        }
        return RedirectResponse(
            "https://idp.example/authorize?response_type=code&state=test-state"
            "&code_challenge_method=S256",
            status_code=302,
        )

    async def authorize_access_token(self, request):
        state = request.session.pop("fake_oidc_state", None)
        if (
            not isinstance(state, dict)
            or request.query_params.get("state") != state.get("state")
            or request.query_params.get("code") != "test-code"
        ):
            raise OAuthError(description="invalid callback")
        header = (
            "eyJhbGciOiJSUzI1NiJ9"
            if self.algorithm == "RS256"
            else "eyJhbGciOiJIUzI1NiJ9"
        )
        return {
            "access_token": "must-not-be-persisted-or-returned",
            "id_token": f"{header}.e30.must-not-be-persisted-or-returned",
            "userinfo": dict(self.claims),
        }

    async def load_server_metadata(self):
        if self.end_session_endpoint is None:
            raise RuntimeError("no end-session endpoint")
        return {"end_session_endpoint": self.end_session_endpoint}

    async def fetch_jwk_set(self, force=False):
        if self.jwks is None:
            raise RuntimeError("no JWKS configured")
        return self.jwks


class TerminalOutputChunkTests(unittest.TestCase):
    def test_large_reset_replay_is_bounded_and_cursor_contiguous(self) -> None:
        text = "x" * (TERMINAL_OUTPUT_CHUNK_CHARS * 2 + 17)
        output = {
            "text": text,
            "base_cursor": 400,
            "next_cursor": 400 + len(text),
            "reset": True,
        }

        chunks = _terminal_output_chunks(output)

        self.assertEqual(len(chunks), 3)
        self.assertEqual("".join(str(chunk["text"]) for chunk in chunks), text)
        self.assertTrue(chunks[0]["reset"])
        self.assertTrue(all(chunk["reset"] is False for chunk in chunks[1:]))
        self.assertTrue(
            all(len(str(chunk["text"])) <= TERMINAL_OUTPUT_CHUNK_CHARS for chunk in chunks)
        )
        self.assertEqual(
            [chunk["next_cursor"] for chunk in chunks],
            [
                400 + TERMINAL_OUTPUT_CHUNK_CHARS,
                400 + TERMINAL_OUTPUT_CHUNK_CHARS * 2,
                400 + len(text),
            ],
        )

    def test_empty_reset_is_preserved_and_empty_delta_is_suppressed(self) -> None:
        reset = {
            "text": "",
            "base_cursor": 12,
            "next_cursor": 12,
            "reset": True,
        }
        delta = {**reset, "reset": False}

        self.assertEqual(_terminal_output_chunks(reset), [reset])
        self.assertEqual(_terminal_output_chunks(delta), [])

    def test_invalid_chunk_limit_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be positive"):
            _terminal_output_chunks(
                {"text": "output", "next_cursor": 6, "reset": False},
                limit=0,
            )


class AuthenticatedAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        self.issuer = "https://idp.example/application/o/coordinator/"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def settings(
        self,
        *,
        allowed_subjects: frozenset[str] = frozenset({"owner-subject"}),
        allowed_groups: frozenset[str] = frozenset(),
    ) -> OIDCSettings:
        return OIDCSettings(
            issuer=self.issuer,
            client_id="coordinator-test",
            client_secret="test-only-secret",
            external_url="http://127.0.0.1",
            allowed_subjects=allowed_subjects,
            allowed_groups=allowed_groups,
            state_dir=self.base / "state",
            secure_cookie=False,
            trusted_hosts=("127.0.0.1",),
            terminal_enabled=True,
        )

    def client(
        self, claims: dict[str, object], *, algorithm: str = "RS256", **settings_kwargs
    ):
        app = create_authenticated_app(
            self.repo,
            self.settings(**settings_kwargs),
            repositories_root=self.base,
            oidc_client=FakeOIDCClient(claims, algorithm),
            codex_command_for_repo=lambda repo: [
                sys.executable,
                "-c",
                "import os; exec('while True:\\n data=os.read(0,1024)\\n "
                "if not data: break\\n os.write(1,data)')",
            ],
        )
        return TestClient(app, base_url="http://127.0.0.1")

    def owner_claims(self) -> dict[str, object]:
        return {
            "iss": self.issuer,
            "sub": "owner-subject",
            "preferred_username": "owner",
            "groups": ["coordinator-users"],
        }

    def login(self, client: TestClient) -> tuple[str, str]:
        response = client.get("/auth/login?next=%2F%23terminal", follow_redirects=False)
        self.assertEqual(response.status_code, 302, response.text)
        self.assertIn("code_challenge_method=S256", response.headers["location"])
        before = client.cookies.get("coordinator_session")
        self.assertIsInstance(before, str)
        self.assertIn("HttpOnly", response.headers["set-cookie"])
        self.assertIn("SameSite=lax", response.headers["set-cookie"])

        response = client.get(
            "/auth/callback?state=test-state&code=test-code", follow_redirects=False
        )
        self.assertEqual(response.status_code, 303, response.text)
        self.assertEqual(response.headers["location"], "/#terminal")
        after = client.cookies.get("coordinator_session")
        self.assertIsInstance(after, str)
        self.assertNotEqual(before, after, "the session id must rotate after login")
        return before, after

    def test_default_deny_login_rotation_csrf_and_logout(self) -> None:
        with self.client(self.owner_claims()) as client:
            response = client.get("/api/state")
            self.assertEqual(response.status_code, 401)
            response = client.get("/app.js", follow_redirects=False)
            self.assertEqual(response.status_code, 303)
            self.assertTrue(
                response.headers["location"].startswith("/auth/login?next=")
            )

            self.login(client)
            response = client.get("/api/state")
            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            security = payload["security"]
            self.assertTrue(security["authenticated"])
            self.assertEqual(security["user"]["sub"], "owner-subject")
            serialized = json.dumps(payload)
            self.assertNotIn("must-not-be-persisted-or-returned", serialized)
            csrf = security["csrf_token"]

            response = client.post("/auth/logout")
            self.assertEqual(response.status_code, 403)
            response = client.post(
                "/auth/logout",
                headers={"X-CSRF-Token": csrf, "Origin": "http://127.0.0.1"},
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["redirect"], "/auth/login")
            self.assertEqual(client.get("/api/state").status_code, 401)

    def test_logout_uses_provider_end_session_endpoint_when_advertised(self) -> None:
        app = create_authenticated_app(
            self.repo,
            self.settings(),
            repositories_root=self.base,
            oidc_client=FakeOIDCClient(
                self.owner_claims(),
                end_session_endpoint="https://idp.example/end-session/",
            ),
        )
        with TestClient(app, base_url="http://127.0.0.1") as client:
            self.login(client)
            csrf = client.get("/api/state").json()["security"]["csrf_token"]
            response = client.post(
                "/auth/logout",
                headers={"X-CSRF-Token": csrf, "Origin": "http://127.0.0.1"},
            )
            self.assertEqual(response.status_code, 200, response.text)
            redirect = response.json()["redirect"]
            self.assertTrue(redirect.startswith("https://idp.example/end-session/?"))
            self.assertIn("client_id=coordinator-test", redirect)
            self.assertIn(
                "post_logout_redirect_uri=http%3A%2F%2F127.0.0.1%2F", redirect
            )

    def test_signed_backchannel_logout_revokes_session_and_rejects_replay(self) -> None:
        key = RSAKey.generate_key(auto_kid=True)
        public_key = key.as_dict(private=False)
        claims = {**self.owner_claims(), "sid": "provider-session-1"}
        app = create_authenticated_app(
            self.repo,
            self.settings(),
            repositories_root=self.base,
            oidc_client=FakeOIDCClient(
                claims,
                jwks={"keys": [public_key]},
            ),
        )
        now = int(time.time())
        logout_claims = {
            "iss": self.issuer,
            "aud": "coordinator-test",
            "iat": now,
            "exp": now + 120,
            "jti": "logout-token-1",
            "sid": "provider-session-1",
            "events": {
                "http://schemas.openid.net/event/backchannel-logout": {}
            },
        }
        compact = jwt.encode(
            {"alg": "RS256", "kid": public_key["kid"]}, logout_claims, key
        )
        with TestClient(app, base_url="http://127.0.0.1") as client:
            self.login(client)
            response = client.post(
                "/auth/backchannel-logout", data={"logout_token": compact}
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(client.get("/api/state").status_code, 401)
            replay = client.post(
                "/auth/backchannel-logout", data={"logout_token": compact}
            )
            self.assertEqual(replay.status_code, 400, replay.text)
            events = app.state.security_store.list_audit_events()
            outcomes = [
                item["outcome"]
                for item in events
                if item["event"] == "backchannel_logout"
            ]
            self.assertEqual(outcomes, ["success", "invalid"])

    def test_backchannel_logout_rejects_invalid_claims_and_signature(self) -> None:
        key = RSAKey.generate_key(auto_kid=True)
        other_key = RSAKey.generate_key(auto_kid=True)
        public_key = key.as_dict(private=False)
        app = create_authenticated_app(
            self.repo,
            self.settings(),
            repositories_root=self.base,
            oidc_client=FakeOIDCClient(
                self.owner_claims(), jwks={"keys": [public_key]}
            ),
        )
        now = int(time.time())
        base_claims = {
            "iss": self.issuer,
            "aud": "coordinator-test",
            "iat": now,
            "jti": "logout-invalid",
            "sub": "owner-subject",
            "events": {
                "http://schemas.openid.net/event/backchannel-logout": {}
            },
        }
        cases = [
            ({**base_claims, "aud": "different-client"}, key),
            ({**base_claims, "iat": now - 600}, key),
            ({**base_claims, "nonce": "not-allowed"}, key),
            ({**base_claims, "events": {}}, key),
            (base_claims, other_key),
        ]
        with TestClient(app, base_url="http://127.0.0.1") as client:
            for index, (case, signing_key) in enumerate(cases):
                case["jti"] = f"logout-invalid-{index}"
                signing_public = signing_key.as_dict(private=False)
                compact = jwt.encode(
                    {"alg": "RS256", "kid": signing_public["kid"]},
                    case,
                    signing_key,
                )
                with self.subTest(index=index):
                    response = client.post(
                        "/auth/backchannel-logout",
                        data={"logout_token": compact},
                    )
                    self.assertEqual(response.status_code, 400, response.text)

    def test_group_can_authorize_when_subject_is_not_allowlisted(self) -> None:
        claims = self.owner_claims()
        claims["sub"] = "different-subject"
        with self.client(
            claims,
            allowed_subjects=frozenset(),
            allowed_groups=frozenset({"coordinator-users"}),
        ) as client:
            self.login(client)
            self.assertEqual(client.get("/api/state").status_code, 200)

    def test_wrong_issuer_and_unallowed_identity_are_denied(self) -> None:
        cases = [
            {**self.owner_claims(), "iss": "https://wrong.example"},
            {**self.owner_claims(), "sub": "not-allowed", "groups": []},
        ]
        for claims in cases:
            with self.subTest(claims=claims), self.client(claims) as client:
                client.get("/auth/login", follow_redirects=False)
                response = client.get(
                    "/auth/callback?state=test-state&code=test-code",
                    follow_redirects=False,
                )
                self.assertEqual(response.status_code, 403)
                self.assertEqual(client.get("/api/state").status_code, 401)

    def test_bad_callback_is_generic_and_does_not_authenticate(self) -> None:
        with self.client(self.owner_claims()) as client:
            client.get("/auth/login", follow_redirects=False)
            response = client.get(
                "/auth/callback?state=wrong&code=test-code", follow_redirects=False
            )
            self.assertEqual(response.status_code, 401)
            self.assertNotIn("invalid callback", response.text)
            self.assertIsNone(client.cookies.get("coordinator_session"))
            self.assertEqual(client.get("/api/state").status_code, 401)

    def test_post_login_destination_rejects_external_and_backslash_forms(self) -> None:
        for destination in (
            "https://evil.example/path",
            "//evil.example/path",
            "/\\evil.example/path",
            "/\r\nLocation: https://evil.example",
        ):
            with self.subTest(destination=destination):
                self.assertEqual(_local_destination(destination), "/")

    def test_every_nonpublic_route_is_denied_without_a_session(self) -> None:
        with self.client(self.owner_claims()) as client:
            for route in client.app.routes:
                if not hasattr(route, "methods"):
                    continue
                path = route.path
                if path in PUBLIC_PATHS:
                    continue
                concrete = path.replace("{action:str}", "start").replace(
                    "{path:path}", "app.js"
                )
                for method in sorted(route.methods or {"GET"}):
                    with self.subTest(path=path, method=method):
                        response = client.request(
                            method, concrete, follow_redirects=False
                        )
                        expected = 401 if concrete.startswith("/api/") else 303
                        self.assertEqual(response.status_code, expected)

    def test_callback_state_is_single_use(self) -> None:
        with self.client(self.owner_claims()) as client:
            self.login(client)
            response = client.get(
                "/auth/callback?state=test-state&code=test-code", follow_redirects=False
            )
            self.assertEqual(response.status_code, 401)

    def test_symmetric_id_token_algorithm_is_denied(self) -> None:
        with self.client(self.owner_claims(), algorithm="HS256") as client:
            client.get("/auth/login", follow_redirects=False)
            response = client.get(
                "/auth/callback?state=test-state&code=test-code", follow_redirects=False
            )
            self.assertEqual(response.status_code, 403)
            self.assertEqual(client.get("/api/state").status_code, 401)

    def test_csrf_origin_and_security_headers_cover_control_routes(self) -> None:
        with self.client(self.owner_claims()) as client:
            self.login(client)
            state = client.get("/api/state")
            csrf = state.json()["security"]["csrf_token"]
            for header in (
                "cache-control",
                "content-security-policy",
                "x-content-type-options",
                "referrer-policy",
                "permissions-policy",
            ):
                self.assertIn(header, state.headers)

            response = client.post(
                "/api/watcher/start",
                headers={"X-CSRF-Token": csrf, "Origin": "https://evil.example"},
            )
            self.assertEqual(response.status_code, 403)
            response = client.post(
                "/api/watcher/start",
                headers={
                    "X-CSRF-Token": csrf,
                    "Origin": "http://127.0.0.1",
                    "Sec-Fetch-Site": "cross-site",
                },
            )
            self.assertEqual(response.status_code, 403)
            response = client.post(
                "/api/watcher/start",
                headers={"X-CSRF-Token": csrf, "Origin": "http://127.0.0.1"},
            )
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["outcome"], "validation")

    def test_every_current_state_changing_route_is_behind_csrf(self) -> None:
        paths = (
            "/auth/logout",
            "/api/watcher/start",
            "/api/watcher/stop",
            "/api/codex/start",
            "/api/codex/stop",
            "/api/codex/clear",
            "/api/repository/select",
        )
        with self.client(self.owner_claims()) as client:
            self.login(client)
            for path in paths:
                with self.subTest(path=path):
                    self.assertEqual(client.post(path).status_code, 403)

            csrf = client.get("/api/state").json()["security"]["csrf_token"]
            response = client.post(
                "/api/codex/stop",
                headers={"X-CSRF-Token": csrf, "Origin": "http://127.0.0.1"},
            )
            self.assertEqual(response.status_code, 409)
            self.assertEqual(response.json()["action"], "codex_stop")

    def test_untrusted_host_is_rejected_before_application_content(self) -> None:
        with self.client(self.owner_claims()) as client:
            response = client.get("http://evil.test/healthz")
            self.assertEqual(response.status_code, 400)

    def test_terminal_websocket_requires_auth_origin_and_csrf(self) -> None:
        with self.client(self.owner_claims()) as client:
            with self.assertRaises(WebSocketDisconnect) as unauthenticated:
                with client.websocket_connect(
                    "ws://127.0.0.1/ws/terminal",
                    headers={"Origin": "http://127.0.0.1"},
                ):
                    pass
            self.assertEqual(unauthenticated.exception.code, 1008)

            self.login(client)
            with self.assertRaises(WebSocketDisconnect) as wrong_origin:
                with client.websocket_connect(
                    "ws://127.0.0.1/ws/terminal",
                    headers={"Origin": "http://evil.test"},
                ):
                    pass
            self.assertEqual(wrong_origin.exception.code, 1008)

            with client.websocket_connect(
                "ws://127.0.0.1/ws/terminal",
                headers={"Origin": "http://127.0.0.1"},
            ) as socket:
                socket.send_json({"type": "hello", "csrf_token": "wrong"})
                with self.assertRaises(WebSocketDisconnect) as raised:
                    socket.receive_json()
                self.assertEqual(raised.exception.code, 1008)

    def test_database_permissions_and_audit_records(self) -> None:
        with self.client(self.owner_claims()) as client:
            self.assertEqual(client.get("/api/state").status_code, 401)
            self.login(client)
            state_path = self.base / "state" / "security.sqlite3"
            self.assertTrue(state_path.is_file())
            self.assertEqual(stat.S_IMODE(state_path.stat().st_mode), 0o600)
            connection = __import__("sqlite3").connect(state_path)
            try:
                events = connection.execute(
                    "SELECT event, outcome, subject FROM audit_events ORDER BY event_id"
                ).fetchall()
                session_rows = connection.execute(
                    "SELECT data_json FROM sessions"
                ).fetchall()
            finally:
                connection.close()
            self.assertIn(("login_callback", "success", "owner-subject"), events)
            self.assertIn(("authorization", "denied", None), events)
            self.assertNotIn(
                "must-not-be-persisted-or-returned", json.dumps(session_rows)
            )

    def test_production_cookie_has_host_prefix_and_security_attributes(self) -> None:
        settings = OIDCSettings(
            issuer=self.issuer,
            client_id="coordinator-test",
            client_secret="test-only-secret",
            external_url="https://coordinator.example",
            allowed_subjects=frozenset({"owner-subject"}),
            allowed_groups=frozenset(),
            state_dir=self.base / "secure-state",
            trusted_hosts=("coordinator.example",),
        )
        app = create_authenticated_app(
            self.repo,
            settings,
            repositories_root=self.base,
            oidc_client=FakeOIDCClient(self.owner_claims()),
        )
        with TestClient(app, base_url="https://coordinator.example") as client:
            response = client.get("/auth/login", follow_redirects=False)
            cookie = response.headers["set-cookie"]
            self.assertIn("__Host-coordinator_session=", cookie)
            self.assertIn("HttpOnly", cookie)
            self.assertIn("Secure", cookie)
            self.assertIn("SameSite=lax", cookie)
            self.assertIn("Path=/", cookie)
            self.assertNotIn("Domain=", cookie)


class SQLiteSecurityStoreTests(unittest.TestCase):
    def test_idle_and_absolute_expiry_delete_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteSecurityStore(Path(tmp), idle_seconds=10, absolute_seconds=20)
            store.save("idle", {"user": {}}, created_at=90, now=100)
            self.assertIsNotNone(store.load("idle", now=109))
            self.assertIsNone(store.load("idle", now=120))
            store.save("absolute", {"user": {}}, created_at=100, now=115)
            self.assertIsNone(store.load("absolute", now=121))

    def test_database_symbolic_link_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            state.mkdir()
            target = Path(tmp) / "target.sqlite3"
            target.touch()
            (state / "security.sqlite3").symlink_to(target)
            with self.assertRaisesRegex(ValueError, "symbolic link"):
                SQLiteSecurityStore(state, idle_seconds=10, absolute_seconds=20)

    def test_non_touching_active_check_observes_expiry_and_revocation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteSecurityStore(Path(tmp), idle_seconds=10, absolute_seconds=20)
            store.save("active", {"user": {}}, created_at=90, now=100)
            self.assertTrue(store.is_active("active", now=109))
            self.assertFalse(store.is_active("active", now=111))
            store.save("revoked", {"user": {}}, created_at=100, now=100)
            handle = store.session_handle("revoked")
            self.assertTrue(store.revoke_session_handle(handle))
            self.assertFalse(store.is_active("revoked", now=101))

    def test_revoked_session_cannot_be_resurrected_by_inflight_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteSecurityStore(Path(tmp), idle_seconds=10, absolute_seconds=20)
            store.save("revoked", {"user": {"sub": "owner"}}, created_at=100, now=100)
            store.delete("revoked")
            self.assertFalse(
                store.update_existing(
                    "revoked", {"user": {"sub": "owner"}}, now=101
                )
            )
            self.assertIsNone(store.load("revoked", now=101))

    def test_security_schema_migrates_and_refuses_future_versions(self) -> None:
        sqlite3 = __import__("sqlite3")
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            database = base / "security.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("PRAGMA user_version = 1")
            SQLiteSecurityStore(base, idle_seconds=10, absolute_seconds=20)
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(
                    connection.execute("PRAGMA user_version").fetchone()[0], 2
                )
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
            self.assertIn("oidc_logout_tokens", tables)

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            database = base / "security.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("PRAGMA user_version = 3")
            with self.assertRaisesRegex(ValueError, "unsupported security database"):
                SQLiteSecurityStore(base, idle_seconds=10, absolute_seconds=20)


class LocalAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        (self.repo / ".git").mkdir()
        settings = LocalSettings(
            external_url="http://127.0.0.1:8765",
            state_dir=self.base / "state",
        )
        self.app = create_authenticated_app(
            self.repo,
            settings,
            repositories_root=self.base,
            codex_command_for_repo=lambda repo: [
                sys.executable,
                "-c",
                "import time; time.sleep(60)",
            ],
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def headers(self, csrf: str) -> dict[str, str]:
        return {"X-CSRF-Token": csrf, "Origin": "http://127.0.0.1"}

    def test_local_state_setup_activity_sessions_and_diagnostics(self) -> None:
        with TestClient(self.app, base_url="http://127.0.0.1") as client:
            state = client.get("/api/state")
            self.assertEqual(state.status_code, 200)
            csrf = state.json()["security"]["csrf_token"]
            self.assertEqual(state.json()["security"]["mode"], "local")

            response = client.post(
                "/api/repository/create",
                json={"name": "sample-project"},
                headers=self.headers(csrf),
            )
            self.assertEqual(response.status_code, 201, response.text)
            created = self.base / "sample-project"
            self.assertTrue((created / ".git").is_dir())

            response = client.post(
                "/api/repository/initialize",
                json={"project_name": "Sample project"},
                headers=self.headers(csrf),
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertTrue((created / ".coordination").is_dir())
            self.assertEqual(response.json()["ci"]["outcome"], "installed")
            self.assertTrue(
                (created / ".github" / "workflows" / "coordinator.yml").is_file()
            )

            diagnostics = client.get("/api/diagnostics").json()
            self.assertEqual(diagnostics["mode"], "local")
            self.assertEqual(diagnostics["summary"]["required_failures"], 0)
            self.assertTrue(diagnostics["ok"])
            self.assertTrue(
                any(
                    check["name"] == "coordination files"
                    for check in diagnostics["checks"]
                )
            )
            names = {check["name"] for check in diagnostics["checks"]}
            self.assertTrue(
                {
                    "free disk space",
                    "operational index",
                    "security index",
                    "watcher lock",
                    "event index",
                    "terminal provider",
                }.issubset(names)
            )
            self.assertEqual(client.get("/api/v1/diagnostics").status_code, 200)

            sessions = client.get("/api/sessions").json()["sessions"]
            self.assertEqual(len(sessions), 1)
            self.assertTrue(sessions[0]["current"])
            self.assertEqual(len(sessions[0]["handle"]), 64)

            self.app.state.security_store.save(
                "other-session", {"csrf_token": "other"}, time.time()
            )
            response = client.post(
                "/api/sessions/revoke-others", headers=self.headers(csrf)
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["count"], 1)
            self.assertIsNone(self.app.state.security_store.load("other-session"))

            activity = client.get("/api/activity").json()["events"]
            self.assertTrue(
                any(event["event"] == "repository_create" for event in activity)
            )
            self.assertTrue(
                any(event["event"] == "repository_initialize" for event in activity)
            )

    def test_repository_initialize_reports_existing_ci_and_reuses_same_endpoint(self) -> None:
        existing = self.repo / ".github" / "workflows" / "tests.yml"
        existing.parent.mkdir(parents=True)
        existing.write_text("name: Existing tests\n", encoding="utf-8")
        with TestClient(self.app, base_url="http://127.0.0.1") as client:
            csrf = client.get("/api/state").json()["security"]["csrf_token"]
            response = client.post(
                "/api/repository/initialize",
                json={"project_name": "Existing CI"},
                headers=self.headers(csrf),
            )
            self.assertEqual(response.status_code, 200, response.text)
            discovery = response.json()["ci"]
            self.assertTrue(discovery["requires_confirmation"])
            self.assertEqual(discovery["workflows"], [".github/workflows/tests.yml"])
            self.assertFalse((existing.parent / "coordinator.yml").exists())

            response = client.post(
                "/api/repository/initialize",
                json={"project_name": "Existing CI", "ci_action": "skip"},
                headers=self.headers(csrf),
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["ci"]["outcome"], "skipped")
            self.assertFalse((existing.parent / "coordinator.yml").exists())

            response = client.post(
                "/api/repository/initialize",
                json={"project_name": "Existing CI", "ci_action": "add"},
                headers=self.headers(csrf),
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["ci"]["outcome"], "installed")
            self.assertTrue((existing.parent / "coordinator.yml").is_file())
            self.assertEqual(existing.read_text(encoding="utf-8"), "name: Existing tests\n")

    def test_repository_initialize_rejects_unknown_ci_action(self) -> None:
        with TestClient(self.app, base_url="http://127.0.0.1") as client:
            csrf = client.get("/api/state").json()["security"]["csrf_token"]
            response = client.post(
                "/api/repository/initialize",
                json={"project_name": "Project", "ci_action": "replace"},
                headers=self.headers(csrf),
            )
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(response.json()["outcome"], "validation")

            for body in (
                {},
                {"project_name": 7},
                {"project_name": "Project", "unexpected": True},
                {"project_name": "bad\nname"},
                {"project_name": "x" * 121},
            ):
                with self.subTest(body=body):
                    response = client.post(
                        "/api/repository/initialize",
                        json=body,
                        headers=self.headers(csrf),
                    )
                    self.assertEqual(response.status_code, 400, response.text)

    def test_repository_initialize_refuses_ci_overwrite_and_reports_init_failure(self) -> None:
        destination = self.repo / ".github" / "workflows" / "coordinator.yml"
        destination.parent.mkdir(parents=True)
        destination.write_text("name: User owned\n", encoding="utf-8")
        with TestClient(self.app, base_url="http://127.0.0.1") as client:
            csrf = client.get("/api/state").json()["security"]["csrf_token"]
            response = client.post(
                "/api/repository/initialize",
                json={"project_name": "Project", "ci_action": "add"},
                headers=self.headers(csrf),
            )
            self.assertEqual(response.status_code, 409, response.text)
            self.assertIn("not Coordinator-managed", response.json()["message"])
            self.assertEqual(destination.read_text(encoding="utf-8"), "name: User owned\n")

            (self.repo / "AGENTS.md").write_text(
                "<!-- coordinate-claude-work:start -->\nbroken\n", encoding="utf-8"
            )
            response = client.post(
                "/api/repository/initialize",
                json={"project_name": "Project", "ci_action": "skip"},
                headers=self.headers(csrf),
            )
            self.assertEqual(response.status_code, 500, response.text)
            self.assertEqual(response.json()["message"], "Coordination initialization failed.")

    def test_versioned_contract_readiness_metrics_and_correlation(self) -> None:
        with TestClient(self.app, base_url="http://127.0.0.1") as client:
            ready = client.get("/readyz")
            self.assertEqual(ready.status_code, 200, ready.text)
            self.assertEqual(ready.json()["status"], "ready")
            state = client.get("/api/v1/state")
            self.assertEqual(state.status_code, 200, state.text)
            self.assertEqual(state.json()["api_version"], "v1")
            self.assertEqual(len(state.headers["x-request-id"]), 24)
            self.assertEqual(client.get("/api/v1/runs").status_code, 200)
            document = client.get("/api/v1/openapi.json").json()
            self.assertEqual(document["openapi"], "3.1.0")
            documented = {
                (path, method.upper())
                for path, item in document["paths"].items()
                for method in item
                if method in {"get", "post", "put", "patch", "delete"}
            }
            routed = set()
            for route in self.app.routes:
                if not route.path.startswith("/api/v1/") or route.path.endswith(
                    "openapi.json"
                ):
                    continue
                path = re.sub(r"{([^}:]+):[^}]+}", r"{\1}", route.path)
                routed.update(
                    (path, method)
                    for method in route.methods
                    if method in {"GET", "POST", "PUT", "PATCH", "DELETE"}
                )
            self.assertEqual(documented, routed)
            schemas = document["components"]["schemas"]
            for path, method in documented:
                operation = document["paths"][path][method.lower()]
                self.assertIn("responses", operation)
                for response in operation["responses"].values():
                    for media in response.get("content", {}).values():
                        reference = media.get("schema", {}).get("$ref")
                        if reference:
                            self.assertIn(reference.rsplit("/", 1)[-1], schemas)
            error = client.get("/api/v1/runs?limit=invalid")
            self.assertEqual(error.status_code, 400)
            self.assertEqual(
                set(error.json()), {"ok", "error"}, "v1 failures use one envelope"
            )
            self.assertEqual(error.json()["error"]["code"], "validation")
            metrics = client.get("/metrics")
            self.assertEqual(metrics.status_code, 200)
            self.assertIn("coordinator_runs", metrics.text)

    def test_control_rate_limit_uses_versioned_error_and_retry_header(self) -> None:
        settings = LocalSettings(
            external_url="http://127.0.0.1",
            state_dir=self.base / "limited-state",
            secure_cookie=False,
            trusted_hosts=("127.0.0.1",),
            rate_limit_control_attempts=2,
        )
        app = create_authenticated_app(
            self.repo,
            settings,
            repositories_root=self.base,
            codex_command_for_repo=lambda repo: [sys.executable, "-c", "pass"],
        )
        with TestClient(app, base_url="http://127.0.0.1") as client:
            csrf = client.get("/api/v1/state").json()["security"]["csrf_token"]
            for _ in range(2):
                response = client.post(
                    "/api/v1/codex/invalid", headers=self.headers(csrf)
                )
                self.assertEqual(response.status_code, 404, response.text)
            limited = client.post(
                "/api/v1/codex/invalid", headers=self.headers(csrf)
            )
            self.assertEqual(limited.status_code, 429, limited.text)
            self.assertEqual(limited.json()["error"]["code"], "rate_limited")
            self.assertGreaterEqual(int(limited.headers["retry-after"]), 1)
            self.assertEqual(limited.headers["x-ratelimit-remaining"], "0")
            activity = client.get("/api/activity").json()["events"]
            self.assertTrue(any(item["event"] == "rate_limit" for item in activity))

    def test_terminal_capability_can_be_disabled_at_server_boundary(self) -> None:
        settings = LocalSettings(
            external_url="http://127.0.0.1",
            state_dir=self.base / "no-terminal-state",
            secure_cookie=False,
            trusted_hosts=("127.0.0.1",),
            terminal_enabled=False,
        )
        app = create_authenticated_app(
            self.repo,
            settings,
            repositories_root=self.base,
            codex_command_for_repo=lambda repo: [sys.executable, "-c", "pass"],
        )
        with TestClient(app, base_url="http://127.0.0.1") as client:
            state = client.get("/api/v1/state").json()
            self.assertFalse(state["capabilities"]["terminal"])
            csrf = state["security"]["csrf_token"]
            response = client.post(
                "/api/v1/codex/start", headers=self.headers(csrf)
            )
            self.assertEqual(response.status_code, 404, response.text)
            self.assertEqual(response.json()["error"]["code"], "not_available")
            with self.assertRaises(WebSocketDisconnect) as closed:
                with client.websocket_connect(
                    "ws://127.0.0.1/ws/terminal",
                    headers={"Origin": "http://127.0.0.1"},
                ):
                    pass
            self.assertEqual(closed.exception.code, 1008)

    def test_state_reconstruction_is_coalesced_and_file_changes_invalidate_it(self) -> None:
        import coordinator.authenticated_web_app as runtime

        with mock.patch.object(
            runtime.web_app,
            "build_state",
            wraps=runtime.web_app.build_state,
        ) as build_state, mock.patch.object(
            runtime.time,
            "monotonic",
            return_value=1_000.0,
        ):
            with TestClient(self.app, base_url="http://127.0.0.1") as client:
                for _ in range(8):
                    self.assertEqual(client.get("/api/v1/state").status_code, 200)
                self.assertEqual(build_state.call_count, 1)

                goal = self.repo / ".coordination" / "planner" / "goal.md"
                goal.parent.mkdir(parents=True)
                goal.write_text("# Goal\n\n- Goal ID: `cache-test`\n", encoding="utf-8")
                self.assertEqual(client.get("/api/v1/state").status_code, 200)
                self.assertEqual(build_state.call_count, 2)

    def test_run_history_policy_events_and_explicit_resume(self) -> None:
        with TestClient(self.app, base_url="http://127.0.0.1") as client:
            state = client.get("/api/state").json()
            csrf = state["security"]["csrf_token"]
            run_id = state["run"]["run_id"]
            self.assertTrue(run_id.startswith("run_"))
            self.assertTrue(state["run"]["turn_id"].startswith("turn_"))

            history = client.get("/api/runs")
            self.assertEqual(history.status_code, 200)
            self.assertEqual(history.json()["runs"][0]["run_id"], run_id)
            detail = client.get(f"/api/runs/{run_id}")
            self.assertEqual(detail.status_code, 200)
            events = client.get(f"/api/runs/{run_id}/events")
            self.assertEqual(events.status_code, 200)
            self.assertEqual(events.json()["events"][0]["type"], "run_discovered")

            response = client.post(
                f"/api/runs/{run_id}/policy",
                json={"generated_tokens": 1000, "warning_ratio": 0.75},
                headers=self.headers(csrf),
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["policy"]["generated_tokens"], 1000)

            response = client.post(
                "/api/preferences",
                json={
                    "browser_notifications": False,
                    "theme": "dark",
                    "log_lines": 100,
                },
                headers=self.headers(csrf),
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(
                client.get("/api/preferences").json()["preferences"]["theme"],
                "dark",
            )

            response = client.post(
                f"/api/runs/{run_id}/archive",
                content=b"",
                headers=self.headers(csrf),
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIsNotNone(client.get(f"/api/runs/{run_id}").json()["run"]["archived_at"])
            response = client.post(
                f"/api/runs/{run_id}/reopen",
                content=b"",
                headers=self.headers(csrf),
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIsNone(client.get(f"/api/runs/{run_id}").json()["run"]["archived_at"])

            diff = client.get("/api/repository/diff")
            self.assertEqual(diff.status_code, 409, diff.text)
            self.assertFalse(diff.json()["ok"])
            self.assertIn("diff", diff.json())

            self.app.state.operational_store.pause(run_id, "test pause")
            paused = client.get("/api/state").json()
            self.assertEqual(paused["guardrails"]["status"], "paused")
            self.assertTrue(paused["run"]["resume_required"])
            response = client.post(
                f"/api/runs/{run_id}/resume",
                content=b"",
                headers=self.headers(csrf),
            )
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["outcome"], "resumed")

    def test_hard_guardrail_stops_and_requires_explicit_resume(self) -> None:
        coordination = self.repo / ".coordination"
        (coordination / "planner").mkdir(parents=True)
        (coordination / "coder").mkdir()
        (coordination / "reviews").mkdir()
        (coordination / "runtime").mkdir()
        (coordination / "README.md").write_text("# Coordination\n", encoding="utf-8")
        (coordination / "planner" / "goal.md").write_text(
            "# Goal\n\n- Goal ID: `bounded`\n- State: `active`\n"
            "- Starting ref: `abc`\n",
            encoding="utf-8",
        )
        (coordination / "planner" / "current-task.md").write_text(
            "# Task\n\n- Task ID: `task-1`\n- State: `ready`\n"
            "- Review round: `0`\n",
            encoding="utf-8",
        )
        (coordination / "runtime" / "claude-progress.json").write_text(
            json.dumps(
                {
                    "task_id": "task-1",
                    "state": "running",
                    "usage": {"output_tokens": 100},
                }
            ),
            encoding="utf-8",
        )
        with TestClient(self.app, base_url="http://127.0.0.1") as client:
            initial = client.get("/api/state").json()
            run_id = initial["run"]["run_id"]
            csrf = initial["security"]["csrf_token"]
            response = client.post(
                f"/api/runs/{run_id}/policy",
                json={"generated_tokens": 100},
                headers=self.headers(csrf),
            )
            self.assertEqual(response.status_code, 200, response.text)

            stopped = client.get("/api/state").json()
            self.assertEqual(stopped["guardrails"]["status"], "paused")
            self.assertEqual(
                stopped["guardrails"]["findings"][0]["severity"], "stop"
            )
            self.assertTrue(stopped["run"]["resume_required"])
            self.assertIn("generated_tokens", stopped["run"]["pause_reason"])

    def test_terminal_websocket_uses_session_csrf_handshake(self) -> None:
        with TestClient(self.app, base_url="http://127.0.0.1") as client:
            csrf = client.get("/api/state").json()["security"]["csrf_token"]
            response = client.post("/api/codex/start", headers=self.headers(csrf))
            self.assertEqual(response.status_code, 200, response.text)
            started = response.json()["codex_session"]
            self.assertIsInstance(started["session_id"], str)
            self.assertEqual(
                started["process_activity"]["session_id"], started["session_id"]
            )
            self.assertEqual(started["process_activity"]["root_pid"], started["pid"])
            with client.websocket_connect(
                "ws://127.0.0.1/ws/terminal",
                headers={"Origin": "http://127.0.0.1"},
            ) as socket:
                socket.send_json(
                    {
                        "type": "hello",
                        "protocol": "terminal.v1",
                        "csrf_token": csrf,
                        "cursor": 0,
                    }
                )
                message = socket.receive_json()
                self.assertIn(message["type"], {"output", "session"})
                self.assertEqual(message["protocol"], "terminal.v1")
                self.assertIsInstance(message["sequence"], int)
                if message["type"] == "output":
                    session = socket.receive_json()
                    self.assertEqual(session["type"], "session")
                    self.assertEqual(session["protocol"], "terminal.v1")
                    self.assertIsInstance(session["sequence"], int)
                socket.send_json({"type": "input", "data": "socket-roundtrip\n"})
                chunks = []
                for _ in range(10):
                    message = socket.receive_json()
                    if message["type"] == "output":
                        chunks.append(message["output"]["text"])
                        if "socket-roundtrip" in "".join(chunks):
                            break
                self.assertIn("socket-roundtrip", "".join(chunks))

    def test_terminal_websocket_grants_one_input_owner(self) -> None:
        with TestClient(self.app, base_url="http://127.0.0.1") as client:
            csrf = client.get("/api/state").json()["security"]["csrf_token"]
            client.post("/api/codex/start", headers=self.headers(csrf))

            def session_message(socket):
                for _ in range(4):
                    message = socket.receive_json()
                    if message["type"] == "session":
                        return message["session"]
                self.fail("terminal did not send a session snapshot")

            with client.websocket_connect(
                "ws://127.0.0.1/ws/terminal",
                headers={"Origin": "http://127.0.0.1"},
            ) as owner:
                owner.send_json({"type": "hello", "csrf_token": csrf})
                self.assertEqual(
                    session_message(owner)["attachment"]["mode"], "read_write"
                )
                with client.websocket_connect(
                    "ws://127.0.0.1/ws/terminal",
                    headers={"Origin": "http://127.0.0.1"},
                ) as observer:
                    observer.send_json({"type": "hello", "csrf_token": csrf})
                    self.assertEqual(
                        session_message(observer)["attachment"]["mode"], "read_only"
                    )
                    observer.send_json({"type": "input", "data": "refused"})
                    response = observer.receive_json()
                    while response["type"] == "session":
                        response = observer.receive_json()
                    self.assertEqual(response["type"], "error")
                    self.assertEqual(response["protocol"], "terminal.v1")
                    self.assertIsInstance(response["sequence"], int)
                    self.assertIn("read-only", response["message"])

    def test_terminal_websocket_replays_from_reconnect_cursor(self) -> None:
        marker = "terminal-replay-marker"
        with TestClient(self.app, base_url="http://127.0.0.1") as client:
            csrf = client.get("/api/state").json()["security"]["csrf_token"]
            client.post("/api/codex/start", headers=self.headers(csrf))
            with client.websocket_connect(
                "ws://127.0.0.1/ws/terminal",
                headers={"Origin": "http://127.0.0.1"},
            ) as socket:
                socket.send_json(
                    {
                        "type": "hello",
                        "protocol": "terminal.v1",
                        "csrf_token": csrf,
                        "cursor": 0,
                    }
                )
                socket.send_json(
                    {"type": "input", "protocol": "terminal.v1", "data": marker + "\n"}
                )
                for _ in range(12):
                    message = socket.receive_json()
                    if message["type"] == "output" and marker in message["output"]["text"]:
                        break
                else:
                    self.fail("terminal did not echo replay marker")

            with client.websocket_connect(
                "ws://127.0.0.1/ws/terminal",
                headers={"Origin": "http://127.0.0.1"},
            ) as replay:
                replay.send_json(
                    {
                        "type": "hello",
                        "protocol": "terminal.v1",
                        "csrf_token": csrf,
                        "cursor": 0,
                    }
                )
                for _ in range(6):
                    message = replay.receive_json()
                    if message["type"] == "output":
                        self.assertIn(marker, message["output"]["text"])
                        self.assertGreater(message["sequence"], 0)
                        break
                else:
                    self.fail("terminal reconnect did not replay buffered output")

    def test_terminal_websocket_bounds_large_replay_frames(self) -> None:
        output_chars = TERMINAL_OUTPUT_CHUNK_CHARS * 2 + 17
        settings = LocalSettings(
            external_url="http://127.0.0.1:8765",
            state_dir=self.base / "large-replay-state",
        )
        app = create_authenticated_app(
            self.repo,
            settings,
            repositories_root=self.base,
            codex_command_for_repo=lambda repo: [
                sys.executable,
                "-c",
                (
                    "import sys,time; "
                    f"sys.stdout.write('x' * {output_chars}); "
                    "sys.stdout.flush(); time.sleep(60)"
                ),
            ],
        )

        with TestClient(app, base_url="http://127.0.0.1") as client:
            csrf = client.get("/api/state").json()["security"]["csrf_token"]
            response = client.post("/api/codex/start", headers=self.headers(csrf))
            self.assertEqual(response.status_code, 200, response.text)

            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                session = client.get("/api/state").json()["codex_session"]
                if session["buffer_next_cursor"] >= output_chars:
                    break
                time.sleep(0.01)
            else:
                self.fail("fake terminal did not produce the large replay fixture")

            with client.websocket_connect(
                "ws://127.0.0.1/ws/terminal",
                headers={"Origin": "http://127.0.0.1"},
            ) as replay:
                replay.send_json(
                    {
                        "type": "hello",
                        "protocol": "terminal.v1",
                        "csrf_token": csrf,
                        "cursor": None,
                    }
                )
                frames = []
                while sum(len(frame["text"]) for frame in frames) < output_chars:
                    message = replay.receive_json()
                    if message["type"] != "output":
                        continue
                    frame = message["output"]
                    self.assertLessEqual(
                        len(frame["text"]), TERMINAL_OUTPUT_CHUNK_CHARS
                    )
                    self.assertEqual(message["sequence"], frame["next_cursor"])
                    frames.append(frame)

            self.assertEqual(len(frames), 3)
            self.assertEqual("".join(frame["text"] for frame in frames), "x" * output_chars)
            self.assertTrue(frames[0]["reset"])
            self.assertTrue(all(frame["reset"] is False for frame in frames[1:]))

    def test_terminal_clear_removes_reconnect_replay_without_stopping(self) -> None:
        marker = "terminal-clear-marker"
        with TestClient(self.app, base_url="http://127.0.0.1") as client:
            csrf = client.get("/api/state").json()["security"]["csrf_token"]
            client.post("/api/codex/start", headers=self.headers(csrf))
            with client.websocket_connect(
                "ws://127.0.0.1/ws/terminal",
                headers={"Origin": "http://127.0.0.1"},
            ) as socket:
                socket.send_json(
                    {"type": "hello", "protocol": "terminal.v1", "csrf_token": csrf}
                )
                socket.send_json(
                    {"type": "input", "protocol": "terminal.v1", "data": marker + "\n"}
                )
                for _ in range(12):
                    message = socket.receive_json()
                    if message["type"] == "output" and marker in message["output"]["text"]:
                        break
                else:
                    self.fail("terminal did not echo clear marker")

            response = client.post("/api/codex/clear", headers=self.headers(csrf))
            self.assertEqual(response.status_code, 200, response.text)
            payload = response.json()
            self.assertEqual(payload["outcome"], "cleared")
            self.assertGreater(payload["cleared_through_cursor"], 0)
            self.assertTrue(payload["codex_session"]["running"])

            with client.websocket_connect(
                "ws://127.0.0.1/ws/terminal",
                headers={"Origin": "http://127.0.0.1"},
            ) as replay:
                replay.send_json(
                    {
                        "type": "hello",
                        "protocol": "terminal.v1",
                        "csrf_token": csrf,
                        "cursor": 0,
                    }
                )
                message = replay.receive_json()
                self.assertEqual(message["type"], "output")
                self.assertTrue(message["output"]["reset"])
                self.assertNotIn(marker, message["output"]["text"])


class SettingsValidationTests(unittest.TestCase):
    def test_client_secret_is_redacted_from_settings_repr(self) -> None:
        settings = OIDCSettings(
            issuer="https://idp.example/application/o/coordinator/",
            client_id="client",
            client_secret="must-never-appear",
            external_url="https://app.example",
            allowed_subjects=frozenset({"owner"}),
            allowed_groups=frozenset(),
        )
        self.assertNotIn("must-never-appear", repr(settings))

    def test_forwarded_proxy_list_accepts_only_literal_ips_and_cidrs(self) -> None:
        self.assertEqual(
            _validated_forwarded_allow_ips("127.0.0.1, 10.0.0.0/24, ::1"),
            "127.0.0.1,10.0.0.0/24,::1",
        )
        for value in ("*", "proxy.internal", "127.0.0.1,not-an-ip"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _validated_forwarded_allow_ips(value)

    def test_oidc_urls_must_not_contain_embedded_credentials(self) -> None:
        with self.assertRaisesRegex(ValueError, "must not contain URL credentials"):
            OIDCSettings(
                issuer="https://user:password@idp.example/",
                client_id="client",
                client_secret="secret",
                external_url="https://app.example",
                allowed_subjects=frozenset({"owner"}),
                allowed_groups=frozenset(),
            )

    def test_trusted_hosts_are_exact_and_cannot_use_wildcards(self) -> None:
        for host in ("*", "*.example", "app.*"):
            with (
                self.subTest(host=host),
                self.assertRaisesRegex(ValueError, "wildcards"),
            ):
                OIDCSettings(
                    issuer="https://idp.example/",
                    client_id="client",
                    client_secret="secret",
                    external_url="https://app.example",
                    allowed_subjects=frozenset({"owner"}),
                    allowed_groups=frozenset(),
                    trusted_hosts=("app.example", host),
                )

    def test_default_deny_requires_an_allow_policy(self) -> None:
        with self.assertRaisesRegex(ValueError, "allowed subject or group"):
            OIDCSettings(
                issuer="https://idp.example/application/o/coordinator",
                client_id="client",
                client_secret="secret",
                external_url="https://app.example",
                allowed_subjects=frozenset(),
                allowed_groups=frozenset(),
            )

    def test_https_is_required_for_secure_cookie_mode(self) -> None:
        with self.assertRaisesRegex(ValueError, "external_url must use HTTPS"):
            OIDCSettings(
                issuer="https://idp.example/application/o/coordinator",
                client_id="client",
                client_secret="secret",
                external_url="http://app.example",
                allowed_subjects=frozenset({"owner"}),
                allowed_groups=frozenset(),
            )

    def test_insecure_http_escape_hatch_is_loopback_only(self) -> None:
        with self.assertRaisesRegex(ValueError, "only on loopback"):
            OIDCSettings(
                issuer="https://idp.example/application/o/coordinator/",
                client_id="client",
                client_secret="secret",
                external_url="http://coordinator.example",
                allowed_subjects=frozenset({"owner"}),
                allowed_groups=frozenset(),
                secure_cookie=False,
            )
