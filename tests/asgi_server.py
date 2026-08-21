"""Real-socket ASGI server helper for compatibility-oriented HTTP tests."""

from __future__ import annotations

import socket
import tempfile
import threading
import json
import urllib.request
from http.cookies import SimpleCookie
from urllib.parse import urlsplit
from collections.abc import Callable
from pathlib import Path

import uvicorn

from coordinator import web_app
from coordinator.authenticated_web_app import LocalSettings, create_authenticated_app


class ASGITestServer:
    def __init__(
        self,
        app,
        listener: socket.socket,
        state_directory: tempfile.TemporaryDirectory[str],
        quiet: bool,
    ) -> None:
        self.app = app
        self.context = app.state.context
        self._listener = listener
        self._state_directory = state_directory
        self.server_address = listener.getsockname()
        self._server = uvicorn.Server(
            uvicorn.Config(
                app,
                access_log=False,
                log_level="error" if quiet else "warning",
                server_header=False,
            )
        )
        self._closed = False
        self._started = threading.Event()
        self._stopped = threading.Event()

    @property
    def watcher(self):
        return self.context.watcher

    @watcher.setter
    def watcher(self, value) -> None:
        self.context.watcher = value

    @property
    def codex_session(self):
        return self.context.codex_session

    @codex_session.setter
    def codex_session(self, value) -> None:
        self.context.codex_session = value

    def serve_forever(self) -> None:
        self._started.set()
        try:
            self._server.run(sockets=[self._listener])
        finally:
            self._stopped.set()

    def shutdown(self) -> None:
        self._server.should_exit = True

    def server_close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._server.should_exit = True
        if self._started.is_set():
            self._stopped.wait(5)
        try:
            self.context.shutdown()
        finally:
            self._listener.close()
            self._state_directory.cleanup()


def create_server(
    repo: Path,
    host: str = "127.0.0.1",
    port: int = 0,
    relay_log_lines: int = web_app.RELAY_LOG_LINES,
    quiet: bool = False,
    assets: Path = web_app.ASSETS,
    watcher_command: list[str] | None = None,
    stop_timeout: float = web_app.STOP_TIMEOUT_SECONDS,
    start_grace: float = web_app.START_GRACE_SECONDS,
    codex_command: list[str] | None = None,
    repositories_root: Path | None = None,
    watcher_command_for_repo: Callable[[Path], list[str] | None] | None = None,
    codex_command_for_repo: Callable[[Path], list[str]] | None = None,
) -> ASGITestServer:
    root = repo.resolve()
    root_dir = repositories_root.resolve() if repositories_root is not None else root.parent

    if watcher_command_for_repo is not None:
        watcher_factory = watcher_command_for_repo
    elif watcher_command is not None:
        fixed_watcher = list(watcher_command)
        watcher_factory = lambda target: fixed_watcher  # noqa: E731
    else:
        watcher_factory = lambda target: None  # noqa: E731

    if codex_command_for_repo is not None:
        codex_factory = codex_command_for_repo
    elif codex_command is not None:
        fixed_codex = list(codex_command)
        codex_factory = lambda target: fixed_codex  # noqa: E731
    else:
        codex_factory = web_app.default_codex_command

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((host, port))
    listener.listen(128)
    actual_port = int(listener.getsockname()[1])
    state_directory = tempfile.TemporaryDirectory(prefix="coordinator-asgi-test-")
    settings = LocalSettings(
        external_url=f"http://{host}:{actual_port}",
        state_dir=Path(state_directory.name),
        trusted_hosts=(host, "127.0.0.1", "localhost"),
    )
    try:
        app = create_authenticated_app(
            root,
            settings,
            repositories_root=root_dir,
            relay_log_lines=relay_log_lines,
            assets=assets,
            watcher_command_for_repo=watcher_factory,
            codex_command_for_repo=codex_factory,
            stop_timeout=stop_timeout,
            start_grace=start_grace,
        )
    except Exception:
        listener.close()
        state_directory.cleanup()
        raise
    return ASGITestServer(app, listener, state_directory, quiet)


def authorized_headers(url: str, headers: dict[str, str] | None = None) -> dict[str, str]:
    """Create a valid local browser-session/CSRF header set for one POST."""

    parsed = urlsplit(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    with urllib.request.urlopen(f"{origin}/api/state") as response:
        state = json.loads(response.read().decode("utf-8"))
        cookie = SimpleCookie()
        for value in response.headers.get_all("Set-Cookie", []):
            cookie.load(value)
    session = cookie.get("coordinator_local_session")
    if session is None:
        raise AssertionError("local ASGI test server did not issue its session cookie")
    merged = {
        "Cookie": f"coordinator_local_session={session.value}",
        "X-CSRF-Token": state["security"]["csrf_token"],
        "Origin": origin,
    }
    merged.update(headers or {})
    return merged
