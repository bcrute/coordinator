#!/usr/bin/env python3.14
"""Serve the coordination dashboard in local or authenticated OIDC mode."""

from __future__ import annotations

import contextlib
import ipaddress
import shutil
import sys
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from pathlib import Path

from .coordination_dashboard import read
from .codex_session import CodexSessionManager
from .configuration import CONFIG_KEYS, load_config, parse_args
from .processes import WatcherManager, default_watcher_command
from .repositories import (
    catalog_payload,
    discover_repositories,
    is_git_repository,
    is_initialized,
)
from .workflow_state import (
    bullets,
    coder_state,
    completion_state,
    delegation_state,
    elapsed,
    goal_state,
    number,
    review_state,
    roadmap_state,
    runtime_state,
    subagent_state,
    tail,
    task_state,
    watcher_state,
    workflow_state,
)

ASSETS = Path(__file__).resolve().parent / "assets" / "web"
WATCHER_ROLES = ("executor", "claude", "codex", "both")
MANAGED_ROLE = "executor"
WATCHER_ACTIONS = {"/api/watcher/start": "start", "/api/watcher/stop": "stop"}
STOP_TIMEOUT_SECONDS = 10.0
START_GRACE_SECONDS = 1.0
CONTROL_BODY_BYTES = 64 * 1024
ACTIVE_STATES = ("starting", "running")
RELAY_LOG_LINES = 200
RELAY_LOG_BYTES = 256 * 1024
CODEX_INPUT_BODY_BYTES = 64 * 1024
CODEX_SESSION_ACTIONS = {"/api/codex/start": "codex_start", "/api/codex/stop": "codex_stop"}
REPOSITORY_SELECT_PATH = "/api/repository/select"
REPOSITORY_SELECT_BODY_BYTES = 64 * 1024
def default_codex_command(root: Path) -> list[str]:
    """Build the one fixed, non-configurable codex launch command for `root`.

    No request can change the program, its arguments, or the working
    directory that this command encodes; it is resolved once at server
    construction time only.
    """
    executable = shutil.which("codex") or "codex"
    return [executable, "-C", str(root)]


def default_codex_resume_command(root: Path) -> list[str]:
    """Build the fixed command which resumes the latest session for `root`."""
    executable = shutil.which("codex") or "codex"
    return [executable, "resume", "--last", "-C", str(root)]



def is_loopback_host(host: str) -> bool:
    """Return whether a bind name is unambiguously loopback-only."""

    if host.strip().lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.strip().strip("[]")).is_loopback
    except ValueError:
        return False


CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json",
    ".svg": "image/svg+xml",
}





class RepositoryContext:
    """One coherent set of a repository and its bound managers.

    Instances are treated as immutable by `ApplicationContext.select()`, which
    always publishes a brand-new `RepositoryContext` rather than mutating one
    in place. The manager setters are retained for explicit process-supervision
    seams and mutate only the currently active instance.
    """

    __slots__ = ("repo", "watcher", "codex_session")

    def __init__(self, repo: Path, watcher: "WatcherManager", codex_session: CodexSessionManager) -> None:
        self.repo = repo
        self.watcher = watcher
        self.codex_session = codex_session


class ApplicationContext:
    """Own the active repository plus its watcher and Codex managers.

    Every switch builds fresh managers for the target repository before
    touching shared state, then atomically publishes the new context under
    one lock, so a request captures one coherent (repo, watcher,
    codex_session) triple that can never be raced against a switch.
    """

    def __init__(
        self,
        repo: Path,
        repositories_root: Path,
        *,
        watcher_command_for_repo: Callable[[Path], list[str] | None],
        codex_command_for_repo: Callable[[Path], list[str]],
        codex_resume_command_for_repo: Callable[[Path], list[str]] | None = None,
        prepare_repo: Callable[[Path], None] | None = None,
        stop_timeout: float = STOP_TIMEOUT_SECONDS,
        start_grace: float = START_GRACE_SECONDS,
    ) -> None:
        self.repositories_root = repositories_root
        self._watcher_command_for_repo = watcher_command_for_repo
        self._codex_command_for_repo = codex_command_for_repo
        self._codex_resume_command_for_repo = codex_resume_command_for_repo
        self._prepare_repo = prepare_repo
        self.stop_timeout = stop_timeout
        self.start_grace = start_grace
        self._lock = threading.RLock()
        if prepare_repo is not None:
            prepare_repo(repo)
        self._active = RepositoryContext(
            repo,
            WatcherManager(repo, watcher_command_for_repo(repo), stop_timeout, start_grace),
            CodexSessionManager(
                str(repo),
                codex_command_for_repo(repo),
                resume_command=(
                    codex_resume_command_for_repo(repo)
                    if codex_resume_command_for_repo is not None
                    else None
                ),
            ),
        )

    def snapshot(self) -> RepositoryContext:
        """Return the one active repo/watcher/codex triple for a whole request."""
        with self._lock:
            return self._active

    @contextlib.contextmanager
    def lease(self) -> Iterator[RepositoryContext]:
        """Hold the application lock for one whole repo-bound request.

        The lock is held for the entire `with` block, so a `select()` running
        on another thread cannot publish a new context (and cannot tear down
        the old managers) until every handler currently using the leased
        context has finished. State construction, catalog generation, and
        watcher/Codex/output operations that happen inside one lease all see
        the same active repo.
        """
        with self._lock:
            yield self._active

    @property
    def repo(self) -> Path:
        return self.snapshot().repo

    @property
    def watcher(self) -> "WatcherManager":
        return self.snapshot().watcher

    @watcher.setter
    def watcher(self, value: "WatcherManager") -> None:
        with self._lock:
            self._active.watcher = value

    @property
    def codex_session(self) -> CodexSessionManager:
        return self.snapshot().codex_session

    @codex_session.setter
    def codex_session(self, value: CodexSessionManager) -> None:
        with self._lock:
            self._active.codex_session = value

    def catalog(self) -> dict[str, object]:
        with self._lock:
            active = self._active
            entries = discover_repositories(self.repositories_root, active.repo)
            return catalog_payload(entries, active.repo, self.repositories_root)

    def select(self, raw_path: str) -> tuple[str, str, dict[str, object]]:
        """Validate and, if warranted, switch to `raw_path`.

        Returns (outcome, message, catalog) where outcome is one of
        "selected", "unchanged", "validation", or "error". The whole
        validate/construct/cleanup/publish sequence runs under the
        application lock, serialized against every request lease, so a
        switch can never race a borrowed manager and a failed switch never
        disturbs the active repository.
        """
        with self._lock:
            active = self._active
            entries = discover_repositories(self.repositories_root, active.repo)
            catalog = catalog_payload(entries, active.repo, self.repositories_root)
            matches = {entry["path"] for entry in entries}
            if raw_path not in matches:
                return "validation", f"{raw_path!r} is not a known repository", catalog

            target = Path(raw_path)
            if str(active.repo) == raw_path:
                return "unchanged", f"{raw_path} is already the active repository", catalog

            try:
                if self._prepare_repo is not None:
                    self._prepare_repo(target)
                new_watcher = WatcherManager(
                    target,
                    self._watcher_command_for_repo(target),
                    self.stop_timeout,
                    self.start_grace,
                )
                new_codex = CodexSessionManager(
                    str(target),
                    self._codex_command_for_repo(target),
                    resume_command=(
                        self._codex_resume_command_for_repo(target)
                        if self._codex_resume_command_for_repo is not None
                        else None
                    ),
                )
            except Exception as error:  # noqa: BLE001 - report any construction failure
                return "error", f"cannot construct managers for {raw_path}: {error}", catalog

            errors: list[BaseException] = []
            try:
                active.codex_session.shutdown()
            except Exception as error:  # noqa: BLE001 - must still try the watcher
                errors.append(error)
            try:
                active.watcher.shutdown()
            except Exception as error:  # noqa: BLE001 - report after both attempts
                errors.append(error)

            if errors:
                # Old managers may be left partially stopped, but the active
                # repository does not change and the fresh managers must not
                # be left running unowned.
                for fresh in (new_codex, new_watcher):
                    with contextlib.suppress(Exception):
                        fresh.shutdown()
                catalog = catalog_payload(
                    discover_repositories(self.repositories_root, active.repo),
                    active.repo,
                    self.repositories_root,
                )
                return (
                    "error",
                    f"cannot cleanly stop the previous repository's managers: {errors[0]}",
                    catalog,
                )

            self._active = RepositoryContext(target, new_watcher, new_codex)
            new_catalog = catalog_payload(
                discover_repositories(self.repositories_root, target),
                target,
                self.repositories_root,
            )
            return "selected", f"switched the active repository to {raw_path}", new_catalog

    def reconfigure_watcher(
        self,
        watcher_command_for_repo: Callable[[Path], list[str] | None],
        commit: Callable[[], None],
    ) -> tuple[str, str]:
        """Replace the future watcher command while no managed watcher is active."""

        with self._lock:
            active = self._active
            snapshot = active.watcher.snapshot()
            if snapshot.get("running") or snapshot.get("state") == "stopping":
                return (
                    "conflict",
                    "stop the managed watcher before changing its executor settings",
                )
            try:
                new_watcher = WatcherManager(
                    active.repo,
                    watcher_command_for_repo(active.repo),
                    self.stop_timeout,
                    self.start_grace,
                )
                commit()
            except Exception as error:  # noqa: BLE001 - preserve the active manager
                return "error", f"could not save executor settings: {error}"
            self._watcher_command_for_repo = watcher_command_for_repo
            self._active = RepositoryContext(
                active.repo,
                new_watcher,
                active.codex_session,
            )
            return "updated", "executor settings saved for future watcher starts"

    def shutdown(self) -> None:
        """Idempotently stop whichever managers are currently active.

        Safe to call more than once and safe to call concurrently with a
        request lease or a switch: it takes the same lease/lock as every
        other repo-bound operation, so it never tears down a manager that a
        request or a switch is still using, and both Codex and watcher
        cleanup are attempted even if one raises.
        """
        with self._lock:
            active = self._active
            errors: list[BaseException] = []
            try:
                active.codex_session.shutdown()
            except Exception as error:  # noqa: BLE001 - must still stop the watcher
                errors.append(error)
            try:
                active.watcher.shutdown()
            except Exception as error:  # noqa: BLE001 - report after both attempts
                errors.append(error)
            if errors:
                raise errors[0]


def build_state(
    repo: Path,
    relay_log_lines: int = RELAY_LOG_LINES,
    watcher: WatcherManager | None = None,
    codex_session: CodexSessionManager | None = None,
) -> dict[str, object]:
    coordination = repo / ".coordination"
    now = time.time()
    goal = goal_state(read(coordination / "planner" / "goal.md"))
    task = task_state(read(coordination / "planner" / "current-task.md"))
    progress = goal["progress"]
    accepted = progress["accepted"] if isinstance(progress, dict) else 0
    coder = coder_state(read(coordination / "coder" / "status.md"), task)
    runtime = runtime_state(repo, task, str(goal["id"]), now)
    completion = completion_state(read(coordination / "reviews" / "completion.md"))
    return {
        "repo": str(repo),
        "generated_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
        "coordination_present": (coordination / "README.md").is_file(),
        "goal": goal,
        "roadmap": roadmap_state(
            read(coordination / "planner" / "roadmap.md"), int(accepted), str(task["id"])
        ),
        "task": task,
        "coder": coder,
        "review": review_state(read(coordination / "reviews" / "latest.md")),
        "runtime": runtime,
        "delegations": delegation_state(repo, now),
        "completion": completion,
        "workflow": workflow_state(goal, task, coder, runtime, completion),
        "watchers": watcher_state(repo),
        "managed_watcher": (watcher or WatcherManager(repo)).snapshot(),
        "relay_log": tail(coordination / "runtime" / "relay.log", relay_log_lines),
        "codex_session": (
            codex_session.snapshot()
            if codex_session is not None
            else CodexSessionManager(
                str(repo),
                default_codex_command(repo),
                resume_command=default_codex_resume_command(repo),
            ).snapshot()
        ),
    }


def static_assets(assets: Path) -> dict[str, Path]:
    """Map request paths to a fixed set of readable assets, so no request can select a file."""
    if not assets.is_dir():
        return {}
    routes = {}
    for path in sorted(assets.rglob("*")):
        if path.is_file():
            routes["/" + path.relative_to(assets).as_posix()] = path
    if "/index.html" in routes:
        routes["/"] = routes["/index.html"]
    return routes


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        from .authenticated_web_app import serve_application
    except ModuleNotFoundError as error:
        print(
            f"error: web runtime dependency is missing ({error.name}); install requirements.txt",
            file=sys.stderr,
        )
        return 2
    return serve_application(args)


if __name__ == "__main__":
    raise SystemExit(main())
