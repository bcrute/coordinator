#!/usr/bin/env python3
"""Serve the coordination dashboard in local or authenticated OIDC mode."""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import tomllib
import urllib.parse
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .coordination_dashboard import TOKEN_FIELDS, duration, field, load_json
from .coordination_dashboard import one_line, read, section
from .codex_session import CodexSessionManager

ASSETS = Path(__file__).resolve().parent / "assets" / "web"
WATCHER_ROLES = ("claude", "codex", "both")
MANAGED_ROLE = "both"
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
CONFIG_KEYS = {
    "repo",
    "repositories_root",
    "host",
    "port",
    "relay_log_lines",
    "quiet",
    "auth_mode",
    "oidc_issuer",
    "oidc_client_id",
    "oidc_client_secret_env",
    "external_url",
    "allowed_subjects",
    "allowed_groups",
    "groups_claim",
    "state_dir",
    "session_idle_seconds",
    "session_absolute_seconds",
    "trusted_hosts",
    "forwarded_allow_ips",
    "insecure_oidc_http",
}


def default_codex_command(root: Path) -> list[str]:
    """Build the one fixed, non-configurable codex launch command for `root`.

    No request can change the program, its arguments, or the working
    directory that this command encodes; it is resolved once at server
    construction time only.
    """
    executable = shutil.which("codex") or "codex"
    return [executable, "-C", str(root)]


def default_watcher_command(root: Path) -> list[str]:
    """Build the fixed automatic both-watcher command for `root`."""

    return [
        sys.executable,
        "-m",
        "coordinator.watch_coordination",
        "--repo",
        str(root),
        "--role",
        MANAGED_ROLE,
        "--no-dashboard",
    ]


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


def bullets(text: str, heading: str) -> list[str]:
    content = section(text, heading)
    if not content:
        return []
    items: list[str] = []
    wrapping = False
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            wrapping = False
            continue
        if re.match(r"^[-*](?:\s|$)", stripped):
            items.append(stripped[1:].strip())
        elif wrapping and items:
            items[-1] = f"{items[-1]} {stripped}"
        else:
            items.append(stripped)
        wrapping = True
    return [re.sub(r"\s+", " ", item).strip() for item in items]


def number(value: object) -> float | None:
    numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
    return float(value) if numeric else None


def elapsed(started: object, ended: object) -> dict[str, object]:
    start = number(started)
    end = number(ended)
    seconds = end - start if start is not None and end is not None else 0.0
    seconds = max(0.0, seconds)
    return {"seconds": round(seconds, 3), "display": duration(seconds)}


def tail(path: Path, limit: int) -> dict[str, object]:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - RELAY_LOG_BYTES))
            data = handle.read()
    except (FileNotFoundError, NotADirectoryError, IsADirectoryError, OSError):
        return {"path": str(path), "available": False, "lines": [], "truncated": False}
    text = data.decode("utf-8", errors="replace")
    if size > RELAY_LOG_BYTES:
        text = text.split("\n", 1)[-1]
    lines = text.splitlines()
    return {
        "path": str(path),
        "available": True,
        "lines": lines[-limit:] if limit > 0 else [],
        "truncated": size > RELAY_LOG_BYTES or len(lines) > max(0, limit),
    }


def goal_state(text: str) -> dict[str, object]:
    match = re.search(r"-\s+(\d+) of (\d+) (?:planned )?(?:micro-)?subgoals accepted", text)
    accepted = int(match.group(1)) if match else 0
    planned = int(match.group(2)) if match else 0
    return {
        "id": field(text, "Goal ID") or "none",
        "state": field(text, "State") or "unknown",
        "starting_ref": field(text, "Starting ref") or "not recorded",
        "target_branch": field(text, "Target branch") or "not recorded",
        "objective": one_line(section(text, "Objective")),
        "completion_criteria": bullets(text, "Completion criteria"),
        "durable_constraints": bullets(text, "Durable constraints"),
        "owner_decisions": bullets(text, "Owner decisions"),
        "progress": {
            "accepted": accepted,
            "planned": planned,
            "label": f"{accepted}/{planned}" if planned else "not recorded",
        },
    }


def roadmap_state(text: str, accepted: int, task_id: str) -> list[dict[str, object]]:
    task_match = re.search(r"(?:^|-)(\d+)(?:-|$)", task_id)
    current = int(task_match.group(1)) if task_match else None
    entries = []
    for value, title in re.findall(r"^## Turn (\d+)\s+[—-]\s+(.+)$", text, re.MULTILINE):
        turn = int(value)
        status = "accepted" if turn <= accepted else "current" if turn == current else "planned"
        entries.append({"turn": turn, "title": one_line(title), "status": status})
    return entries


def task_state(text: str) -> dict[str, object]:
    return {
        "id": field(text, "Task ID") or "none",
        "state": field(text, "State") or "unknown",
        "review_round": field(text, "Review round") or "0",
        "starting_ref": field(text, "Starting ref") or "not recorded",
        "objective": one_line(section(text, "Objective")),
        "in_scope": bullets(text, "In scope"),
        "out_of_scope": bullets(text, "Out of scope"),
        "acceptance_criteria": bullets(text, "Acceptance criteria"),
        "required_evidence": bullets(text, "Required evidence"),
        "allowed_external_actions": bullets(text, "Allowed external actions"),
        "review_corrections": bullets(text, "Review corrections"),
    }


def coder_state(text: str, task: dict[str, object]) -> dict[str, object]:
    task_id = field(text, "Task ID") or "none"
    review_round = field(text, "Review round") or "0"
    return {
        "task_id": task_id,
        "state": field(text, "State") or "unknown",
        "review_round": review_round,
        "starting_ref": field(text, "Starting ref") or "not recorded",
        "current_ref": field(text, "Current ref") or "not recorded",
        "blocker": field(text, "Blocker") or "none",
        "current_activity": one_line(section(text, "Current activity")),
        "matches_current_task": task_id == task["id"] and review_round == task["review_round"],
    }


def review_state(text: str) -> dict[str, object]:
    return {
        "task_id": field(text, "Task ID") or "none",
        "verdict": field(text, "Verdict") or "not_reviewed",
        "review_round": field(text, "Review round") or "0",
        "examined_ref": field(text, "Examined ref") or "not recorded",
        "findings": bullets(text, "Findings"),
        "next_action": bullets(text, "Next action"),
    }


def completion_state(text: str) -> dict[str, object]:
    return {
        "goal_id": field(text, "Goal ID") or "none",
        "state": field(text, "State") or "unknown",
        "accepted_ref": field(text, "Accepted ref") or "not recorded",
        "result": bullets(text, "Result"),
        "evidence": bullets(text, "Evidence"),
        "limitations": bullets(text, "Limitations and optional follow-up"),
        "present": bool(text.strip()),
    }


def workflow_state(
    goal: dict[str, object],
    task: dict[str, object],
    coder: dict[str, object],
    runtime: dict[str, object],
    completion: dict[str, object],
) -> dict[str, object]:
    """Derive one owner-facing lifecycle phase from the raw coordination records.

    Raw coder/runtime/completion records are left untouched by the caller; this
    only reads them to pick a phase and a truthful current label/detail.
    """
    completion_matches = (
        completion["present"]
        and completion["goal_id"] == goal["id"]
        and completion["state"] == "done"
        and goal["state"] == "done"
    )
    coder_current = bool(coder["matches_current_task"])
    runtime_current = bool(runtime["matches_current_task"])
    coder_state = coder["state"] if coder_current else None
    task_state = task["state"]

    goal_blocked = goal["state"] == "blocked"
    task_blocked = task_state == "blocked"
    coder_blocked = coder_current and coder_state == "blocked"

    if completion_matches:
        phase = "done"
        active = False
        label = "Goal complete"
        detail = one_line(" ".join(completion["result"])) if completion["result"] else (
            "The goal is complete."
        )
    elif goal_blocked or task_blocked or coder_blocked:
        phase = "blocked"
        active = False
        label = "Blocked"
        detail = (
            coder["current_activity"]
            or (f"Blocked: {coder['blocker']}" if coder_blocked and coder["blocker"] != "none" else None)
            or "The goal or current task is blocked."
        )
    elif coder_current and coder_state == "implementing":
        phase = "implementing"
        active = True
        label = "Claude is implementing"
        detail = coder["current_activity"] or "Claude is working on the current task."
    elif coder_current and coder_state == "review":
        phase = "waiting_for_codex"
        active = False
        label = "Waiting for Codex review"
        detail = "The current task is awaiting Codex review."
    elif task_state in ("ready", "changes_requested"):
        phase = "waiting_for_claude"
        active = False
        label = "Waiting for Claude"
        detail = f"The current task is {task_state} and has not been picked up yet."
    elif task_state == "review":
        phase = "waiting_for_codex"
        active = False
        label = "Waiting for Codex review"
        detail = "The current task is awaiting Codex review."
    else:
        phase = "inactive"
        active = False
        label = "Inactive"
        detail = "No active phase could be determined from the coordination records."

    return {
        "phase": phase,
        "active": active,
        "label": label,
        "detail": detail,
        "coder_current": coder_current,
        "runtime_current": runtime_current,
        "completion_current": completion_matches,
    }


def subagent_state(metrics: dict[str, object], now: float) -> list[dict[str, object]]:
    values = metrics.get("subagents")
    if not isinstance(values, list):
        return []
    agents = []
    for value in values:
        if not isinstance(value, dict):
            continue
        state = str(value.get("state") or "unknown")
        usage = value.get("usage") if isinstance(value.get("usage"), dict) else {}
        ended = now if state == "running" else value.get("completed_at_epoch")
        agents.append(
            {
                "state": state,
                "model": str(value.get("model") or metrics.get("subagent_model") or "inherited"),
                "description": one_line(str(value.get("description") or "Claude subagent")),
                "started_at_epoch": number(value.get("started_at_epoch")),
                "completed_at_epoch": number(value.get("completed_at_epoch")),
                "elapsed": elapsed(value.get("started_at_epoch"), ended),
                "usage": {
                    name: usage[name] if isinstance(usage.get(name), int) else 0
                    for name in TOKEN_FIELDS
                },
            }
        )
    return agents


def runtime_state(
    repo: Path, task: dict[str, object], goal_id: str, now: float
) -> dict[str, object]:
    metrics = load_json(repo / ".coordination" / "runtime" / "claude-progress.json")
    timing = load_json(repo / ".coordination" / "runtime" / "goal-timing.json")
    same_task = metrics.get("task_id") == task["id"]
    current = metrics if same_task else {}
    finished = metrics.get("completed_at_epoch") if metrics.get("state") == "completed" else None
    metric_end = number(finished) if same_task else None
    metric_end = metric_end if metric_end is not None else now
    usage = current.get("usage") if isinstance(current.get("usage"), dict) else {}
    same_goal = timing.get("goal_id") == goal_id
    goal_started = number(timing.get("started_at_epoch")) if same_goal else None
    return {
        "task_id": metrics.get("task_id") if isinstance(metrics.get("task_id"), str) else None,
        "state": str(metrics.get("state") or "unknown"),
        "matches_current_task": bool(same_task),
        "primary_model": str(metrics.get("primary_model") or "Claude"),
        "subagent_model": str(metrics.get("subagent_model") or "provider-selected"),
        "orchestration_mode": str(metrics.get("orchestration_mode") or "not recorded"),
        "tokens": {
            name: usage[name] if isinstance(usage.get(name), int) else 0 for name in TOKEN_FIELDS
        },
        "timing": {
            "activity": elapsed(current.get("objective_started_epoch"), metric_end),
            "turn": elapsed(current.get("turn_started_epoch"), metric_end),
            "overall": elapsed(goal_started, now if goal_started is not None else None),
            "goal_started_at_epoch": goal_started,
        },
        "subagents": subagent_state(current, now),
    }


def watcher_state(repo: Path) -> list[dict[str, object]]:
    runtime = repo / ".coordination" / "runtime"
    watchers = []
    for role in WATCHER_ROLES:
        payload = load_json(runtime / f"watcher-{role}-status.json")
        if not payload:
            continue
        coordination = payload.get("coordination")
        watchers.append(
            {
                "role": str(payload.get("role") or role),
                "watcher_state": str(payload.get("watcher_state") or "unknown"),
                "detail": one_line(str(payload.get("detail") or "")),
                "updated_at": str(payload.get("updated_at") or "not recorded"),
                "coordination": coordination if isinstance(coordination, dict) else {},
                "lock_present": (runtime / f"watcher-{role}.lock").is_file(),
            }
        )
    return watchers


def moment(value: float | None) -> str | None:
    if value is None:
        return None
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def process_group(process: subprocess.Popen) -> int | None:
    """Read the child's own process group so the whole relay tree can be signalled."""
    try:
        return os.getpgid(process.pid)
    except (AttributeError, OSError):
        return None


class WatcherManager:
    """Own at most one automatic watcher child for the configured repository.

    The command, the repository, and the role are fixed when the server starts, so
    no request can choose a program, a path, or an argument. Every transition is
    taken under one lock, and the state reported back is only ever observed state.
    """

    def __init__(
        self,
        repo: Path,
        command: list[str] | None = None,
        stop_timeout: float = STOP_TIMEOUT_SECONDS,
        start_grace: float = START_GRACE_SECONDS,
    ) -> None:
        self.repo = repo
        self.command = list(command) if command else default_watcher_command(repo)
        self.stop_timeout = max(0.05, float(stop_timeout))
        self.start_grace = max(0.0, float(start_grace))
        self.runtime = repo / ".coordination" / "runtime"
        self.log_path = self.runtime / "relay.log"
        self.lock_path = self.runtime / f"watcher-{MANAGED_ROLE}.lock"
        self._lock = threading.RLock()
        self._process: subprocess.Popen | None = None
        self._log = None
        self._group: int | None = None
        self._state = "idle"
        self._detail = "No watcher has been started from this web app."
        self._pid: int | None = None
        self._started: float | None = None
        self._exited: float | None = None
        self._exit_code: int | None = None
        self._stop_requested = False

    # Transitions ---------------------------------------------------------

    def start(self) -> tuple[str, str]:
        """Launch the one fixed watcher command, or report why no process was created."""
        with self._lock:
            self._refresh()
            if self._state in ACTIVE_STATES:
                return "conflict", f"a managed watcher is already running as pid {self._pid}"
            if self._state == "stopping":
                return "conflict", "the managed watcher is still stopping; retry once it exits"
            if not is_initialized(self.repo):
                return (
                    "validation",
                    "coordination is not initialized in this repository yet; start Codex "
                    "and complete the initial coordination discussion, then retry",
                )
            if self.lock_path.is_file():
                return (
                    "conflict",
                    f"another watcher already holds {self.lock_path}; no process was started",
                )
            try:
                self.runtime.mkdir(parents=True, exist_ok=True)
                log = self.log_path.open("ab")
            except OSError as error:
                return self._failed(f"cannot append to {self.log_path}: {error}")
            try:
                log.write(
                    f"\n[{datetime.now(timezone.utc).isoformat()}] web app starting watcher: "
                    f"{' '.join(self.command)}\n".encode("utf-8")
                )
                log.flush()
                process = subprocess.Popen(
                    self.command,
                    cwd=str(self.repo),
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except OSError as error:
                log.close()
                return self._failed(f"cannot launch the watcher: {error}")
            self._process = process
            self._log = log
            group = process_group(process)
            self._group = process.pid if group is None else group
            self._pid = process.pid
            self._state = "starting"
            self._detail = f"launching {' '.join(self.command)}"
            self._started = time.time()
            self._exited = None
            self._exit_code = None
            self._stop_requested = False
            return "started", f"started the watcher as pid {process.pid}"

    def stop(self, timeout: float | None = None) -> tuple[str, str]:
        """Terminate the whole managed process group within a bounded time."""
        with self._lock:
            self._refresh()
            process = self._process
            if process is None or self._state not in ACTIVE_STATES:
                if self._state == "stopping":
                    return "conflict", "the managed watcher is already stopping"
                return "conflict", "no watcher started by this web app is running"
            pid = self._pid
            group = self._group
            self._state = "stopping"
            self._stop_requested = True
            self._detail = f"terminating the managed watcher process group (pid {pid})"
        self._terminate(process, group, timeout)
        with self._lock:
            if process.poll() is None:
                self._detail = f"the watcher (pid {pid}) survived the bounded stop escalation"
                return "error", self._detail
            self._refresh()
            return "stopped", f"stopped the watcher (pid {pid}, exit status {self._exit_code})"

    def shutdown(self, timeout: float | None = None) -> None:
        """Leave no managed child behind when the server closes."""
        with self._lock:
            running = self._process is not None and self._process.poll() is None
        if running:
            self.stop(timeout)
        with self._lock:
            self._refresh()
            self._close_log()

    # Observation ---------------------------------------------------------

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            self._refresh()
            lock_present = self.lock_path.is_file()
            active = self._state in ACTIVE_STATES
            initialized = is_initialized(self.repo)
            idle = not active and self._state != "stopping"
            detail = self._detail
            if idle and not initialized:
                detail = (
                    "coordination is not initialized in this repository yet; start Codex "
                    "and complete the initial coordination discussion, then retry"
                )
            elif idle and lock_present:
                detail = f"{detail} Another watcher currently holds {self.lock_path.name}."
            return {
                "role": MANAGED_ROLE,
                "state": self._state,
                "detail": detail,
                "running": active,
                "pid": self._pid,
                "started_at": moment(self._started),
                "started_at_epoch": self._started,
                "exited_at": moment(self._exited),
                "exit_code": self._exit_code,
                "command": list(self.command),
                "log_path": str(self.log_path),
                "lock_present": lock_present,
                "can_start": idle and not lock_present and initialized,
                "can_stop": active,
            }

    # Internals -----------------------------------------------------------

    def _refresh(self) -> None:
        self._reap()
        if self._state == "starting" and self._started is not None:
            if time.time() - self._started >= self.start_grace:
                self._state = "running"
                self._detail = f"watching {self.repo} as pid {self._pid}"

    def _reap(self) -> None:
        process = self._process
        if process is None or process.poll() is None:
            return
        code = process.returncode
        self._process = None
        self._close_log()
        self._exit_code = code
        self._exited = time.time()
        if self._stop_requested:
            self._state = "exited"
            self._detail = f"stopped by this web app (exit status {code})"
        elif code == 0:
            self._state = "exited"
            self._detail = "the watcher exited on its own with status 0"
        else:
            self._state = "failed"
            self._detail = f"the watcher exited with status {code}"

    def _terminate(
        self, process: subprocess.Popen, group: int | None, timeout: float | None
    ) -> None:
        limit = self.stop_timeout if timeout is None else max(0.05, float(timeout))
        for number in (signal.SIGTERM, signal.SIGKILL):
            self._signal(process, group, number)
            try:
                process.wait(timeout=limit)
                return
            except subprocess.TimeoutExpired:
                continue

    def _signal(self, process: subprocess.Popen, group: int | None, number: int) -> None:
        if process.poll() is not None:
            return
        try:
            if group is not None and hasattr(os, "killpg"):
                os.killpg(group, number)
            else:
                process.send_signal(number)
        except (OSError, ValueError):
            pass

    def _failed(self, detail: str) -> tuple[str, str]:
        self._state = "failed"
        self._detail = detail
        return "error", detail

    def _close_log(self) -> None:
        if self._log is not None:
            try:
                self._log.close()
            except OSError:
                pass
            self._log = None


def is_initialized(path: Path) -> bool:
    try:
        return (path / ".coordination" / "README.md").is_file()
    except OSError:
        return False


def is_git_repository(path: Path) -> bool:
    """Report whether `path` has a direct `.git` marker (file or directory).

    This intentionally never invokes Git or inspects the marker's contents;
    it is a cheap, local structural check only.
    """
    try:
        marker = path / ".git"
        return marker.is_file() or marker.is_dir()
    except OSError:
        return False


def discover_repositories(root: Path, active_repo: Path) -> list[dict[str, object]]:
    """Discover Git direct children of `root`, plus the active repo.

    A direct child is included only when it is itself a Git repository (has
    a direct `.git` file-or-directory marker); coordination initialization is
    not a discovery filter. The active repository is included even when it is
    not a direct child of `root`, but only when it is either a Git repository
    or already coordination-initialized, so the initial configuration stays
    usable without exposing arbitrary directories. Paths are resolved and
    deduplicated, and every entry carries a dynamically derived `initialized`
    flag. Entries are sorted by case-insensitive display name, then by path.
    """
    candidates: list[Path] = []
    if root.is_dir():
        try:
            children = sorted(root.iterdir())
        except OSError:
            children = []
        for child in children:
            try:
                if child.is_dir() and is_git_repository(child):
                    candidates.append(child)
            except OSError:
                continue

    seen: set[Path] = set()
    entries: list[dict[str, object]] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        entries.append(
            {"name": resolved.name, "path": str(resolved), "initialized": is_initialized(resolved)}
        )

    try:
        active_resolved = active_repo.resolve()
    except OSError:
        active_resolved = None
    if active_resolved is not None and active_resolved not in seen:
        if is_git_repository(active_resolved) or is_initialized(active_resolved):
            entries.append(
                {
                    "name": active_resolved.name,
                    "path": str(active_resolved),
                    "initialized": is_initialized(active_resolved),
                }
            )

    entries.sort(key=lambda entry: (str(entry["name"]).lower(), str(entry["path"])))
    return entries


def catalog_payload(
    entries: list[dict[str, object]], active_repo: Path, root: Path
) -> dict[str, object]:
    active_str = str(active_repo)
    return {
        "root": str(root),
        "active": active_str,
        "entries": [
            {
                "name": entry["name"],
                "path": entry["path"],
                "active": entry["path"] == active_str,
                "initialized": entry["initialized"],
            }
            for entry in entries
        ],
    }


class RepositoryContext:
    """One coherent set of a repository and its bound managers.

    Instances are treated as immutable by `ApplicationContext.select()`, which
    always publishes a brand-new `RepositoryContext` rather than mutating one
    in place. The `watcher`/`codex_session` setters below exist only for
    legacy `CoordinationServer.watcher =` / `.codex_session =` compatibility
    and do mutate the currently active instance in place.
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
        stop_timeout: float = STOP_TIMEOUT_SECONDS,
        start_grace: float = START_GRACE_SECONDS,
    ) -> None:
        self.repositories_root = repositories_root
        self._watcher_command_for_repo = watcher_command_for_repo
        self._codex_command_for_repo = codex_command_for_repo
        self.stop_timeout = stop_timeout
        self.start_grace = start_grace
        self._lock = threading.RLock()
        self._active = RepositoryContext(
            repo,
            WatcherManager(repo, watcher_command_for_repo(repo), stop_timeout, start_grace),
            CodexSessionManager(str(repo), codex_command_for_repo(repo)),
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
                new_watcher = WatcherManager(
                    target,
                    self._watcher_command_for_repo(target),
                    self.stop_timeout,
                    self.start_grace,
                )
                new_codex = CodexSessionManager(str(target), self._codex_command_for_repo(target))
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
        "completion": completion,
        "workflow": workflow_state(goal, task, coder, runtime, completion),
        "watchers": watcher_state(repo),
        "managed_watcher": (watcher or WatcherManager(repo)).snapshot(),
        "relay_log": tail(coordination / "runtime" / "relay.log", relay_log_lines),
        "codex_session": (
            codex_session.snapshot()
            if codex_session is not None
            else CodexSessionManager(str(repo), default_codex_command(repo)).snapshot()
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


def make_handler(
    context: ApplicationContext,
    assets: dict[str, Path],
    relay_log_lines: int,
    quiet: bool,
):
    class CoordinationHandler(BaseHTTPRequestHandler):
        server_version = "CoordinationWebApp/1.0"
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            if not quiet:
                super().log_message(format, *args)

        def respond(
            self,
            status: int,
            content_type: str,
            body: bytes,
            headers: dict[str, str] | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def respond_json(self, status: int, payload: dict[str, object]) -> None:
            body = json.dumps(payload, indent=2).encode("utf-8")
            self.respond(status, "application/json; charset=utf-8", body)

        def refuse(self, status: int, outcome: str, message: str, action: str) -> None:
            self.respond_json(
                status, {"ok": False, "action": action, "outcome": outcome, "message": message}
            )

        def path_only(self) -> str:
            return self.path.split("?", 1)[0].split("#", 1)[0]

        def discard_body(self, action: str) -> bool:
            """Consume and ignore any request body: control actions take no input."""
            if self.headers.get("Transfer-Encoding"):
                self.close_connection = True
                self.refuse(411, "length_required", "send a body-less request", action)
                return False
            raw = (self.headers.get("Content-Length") or "0").strip()
            try:
                length = int(raw or "0")
            except ValueError:
                length = -1
            if length < 0:
                self.close_connection = True
                self.refuse(400, "bad_request", "the Content-Length header is unusable", action)
                return False
            if length > CONTROL_BODY_BYTES:
                self.close_connection = True
                self.refuse(413, "too_large", "this control ignores and refuses bodies", action)
                return False
            if length:
                self.rfile.read(length)
            return True

        def same_origin(self) -> bool:
            """Refuse a browser request that another site sent to this server."""
            site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
            if site and site not in ("same-origin", "none"):
                return False
            origin = (self.headers.get("Origin") or "").strip()
            if not origin:
                return True
            host = (self.headers.get("Host") or "").strip()
            return bool(host) and origin.lower() == f"http://{host}".lower()

        def read_body(self, action: str, limit: int) -> bytes | None:
            """Read a bounded request body, refusing chunked or oversized input."""
            if self.headers.get("Transfer-Encoding"):
                self.close_connection = True
                self.refuse(411, "length_required", "send a request with Content-Length", action)
                return None
            raw = (self.headers.get("Content-Length") or "0").strip()
            try:
                length = int(raw or "0")
            except ValueError:
                length = -1
            if length < 0:
                self.close_connection = True
                self.refuse(400, "bad_request", "the Content-Length header is unusable", action)
                return None
            if length > limit:
                self.close_connection = True
                self.refuse(413, "too_large", "the request body is too large", action)
                return None
            return self.rfile.read(length) if length else b""

        def read_json_body(self, action: str, limit: int) -> tuple[bool, object]:
            """Read and parse a bounded JSON body; returns (ok, value_or_None)."""
            raw = self.read_body(action, limit)
            if raw is None:
                return False, None
            try:
                value = json.loads(raw.decode("utf-8")) if raw else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.refuse(400, "bad_request", "the request body is not valid JSON", action)
                return False, None
            return True, value

        def codex_empty_body(self, action: str) -> bool:
            """Read a bounded body for a Codex start/stop request and require it be empty.

            Unlike `discard_body` (used by watcher controls, which silently ignore any
            body), Codex start/stop must reject a nonempty body outright. The body is
            consumed exactly once, within the same bound as other control endpoints, and
            exactly one JSON error response is sent on any failure path.
            """
            raw = self.read_body(action, CONTROL_BODY_BYTES)
            if raw is None:
                return False
            if raw:
                self.refuse(
                    413, "too_large", "codex start/stop requests must not include a body", action
                )
                return False
            return True

        def codex_status(self, outcome: str) -> int:
            return {
                "started": 200,
                "stopped": 200,
                "accepted": 200,
                "conflict": 409,
                "validation": 400,
                "error": 500,
            }[outcome]

        def respond_codex(
            self, action: str, outcome: str, message: str, codex_session: CodexSessionManager
        ) -> None:
            status = self.codex_status(outcome)
            self.respond_json(
                status,
                {
                    "ok": status == 200,
                    "action": action,
                    "outcome": outcome,
                    "message": message,
                    "codex_session": codex_session.snapshot(),
                },
            )

        def do_codex_start(self, action: str, codex_session: CodexSessionManager) -> None:
            if not self.codex_empty_body(action):
                return
            if not self.same_origin():
                self.refuse(403, "forbidden", "cross-origin control requests are refused", action)
                return
            try:
                codex_session.start()
            except RuntimeError as error:
                self.respond_codex(action, "conflict", str(error), codex_session)
                return
            except OSError as error:
                self.respond_codex(action, "error", f"cannot launch codex: {error}", codex_session)
                return
            self.respond_codex(action, "started", "started the codex session", codex_session)

        def do_codex_stop(self, action: str, codex_session: CodexSessionManager) -> None:
            if not self.codex_empty_body(action):
                return
            if not self.same_origin():
                self.refuse(403, "forbidden", "cross-origin control requests are refused", action)
                return
            snapshot_before = codex_session.snapshot()
            codex_session.stop()
            if snapshot_before.get("running"):
                self.respond_codex(action, "stopped", "stopped the codex session", codex_session)
            else:
                self.respond_codex(action, "conflict", "no codex session is running", codex_session)

        def do_codex_input(self, action: str, codex_session: CodexSessionManager) -> None:
            if not self.same_origin():
                if not self.discard_body(action):
                    return
                self.refuse(403, "forbidden", "cross-origin control requests are refused", action)
                return
            ok, value = self.read_json_body(action, CODEX_INPUT_BODY_BYTES)
            if not ok:
                return
            if not isinstance(value, dict) or set(value.keys()) != {"data"} or not isinstance(
                value.get("data"), str
            ):
                self.refuse(
                    400, "validation", 'body must be exactly JSON {"data": string}', action
                )
                return
            try:
                codex_session.write(value["data"])
            except ValueError as error:
                self.respond_codex(action, "validation", str(error), codex_session)
                return
            except RuntimeError as error:
                self.respond_codex(action, "conflict", str(error), codex_session)
                return
            self.respond_codex(action, "accepted", "wrote input to the codex session", codex_session)

        def do_codex_resize(self, action: str, codex_session: CodexSessionManager) -> None:
            if not self.same_origin():
                if not self.discard_body(action):
                    return
                self.refuse(403, "forbidden", "cross-origin control requests are refused", action)
                return
            ok, value = self.read_json_body(action, CODEX_INPUT_BODY_BYTES)
            if not ok:
                return
            if (
                not isinstance(value, dict)
                or set(value.keys()) != {"rows", "cols"}
                or not isinstance(value.get("rows"), int)
                or not isinstance(value.get("cols"), int)
                or isinstance(value.get("rows"), bool)
                or isinstance(value.get("cols"), bool)
            ):
                self.refuse(
                    400,
                    "validation",
                    'body must be exactly JSON {"rows": int, "cols": int}',
                    action,
                )
                return
            try:
                codex_session.resize(value["rows"], value["cols"])
            except ValueError as error:
                self.respond_codex(action, "validation", str(error), codex_session)
                return
            self.respond_codex(action, "accepted", "resized the codex session", codex_session)

        def do_repository_select(self, action: str) -> None:
            if not self.same_origin():
                if not self.discard_body(action):
                    return
                self.refuse(403, "forbidden", "cross-origin control requests are refused", action)
                return
            ok, value = self.read_json_body(action, REPOSITORY_SELECT_BODY_BYTES)
            if not ok:
                return
            if not isinstance(value, dict) or set(value.keys()) != {"path"} or not isinstance(
                value.get("path"), str
            ):
                self.refuse(
                    400, "validation", 'body must be exactly JSON {"path": string}', action
                )
                return
            outcome, message, catalog = context.select(value["path"])
            status = {"selected": 200, "unchanged": 200, "validation": 400, "error": 500}[outcome]
            self.respond_json(
                status,
                {
                    "ok": status == 200,
                    "action": action,
                    "outcome": outcome,
                    "message": message,
                    "repository_catalog": catalog,
                },
            )

        def do_POST(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
            path = self.path_only()
            action = WATCHER_ACTIONS.get(path)
            if action is not None:
                if not self.discard_body(action):
                    return
                if not self.same_origin():
                    self.refuse(
                        403, "forbidden", "cross-origin control requests are refused", action
                    )
                    return
                with context.lease() as ctx:
                    outcome, message = (
                        ctx.watcher.start() if action == "start" else ctx.watcher.stop()
                    )
                    status = {
                        "started": 200,
                        "stopped": 200,
                        "conflict": 409,
                        "validation": 400,
                        "error": 500,
                    }[outcome]
                    self.respond_json(
                        status,
                        {
                            "ok": status == 200,
                            "action": action,
                            "outcome": outcome,
                            "message": message,
                            "managed_watcher": ctx.watcher.snapshot(),
                        },
                    )
                return
            if path == "/api/codex/start":
                with context.lease() as ctx:
                    self.do_codex_start("codex_start", ctx.codex_session)
                return
            if path == "/api/codex/stop":
                with context.lease() as ctx:
                    self.do_codex_stop("codex_stop", ctx.codex_session)
                return
            if path == "/api/codex/input":
                with context.lease() as ctx:
                    self.do_codex_input("codex_input", ctx.codex_session)
                return
            if path == "/api/codex/resize":
                with context.lease() as ctx:
                    self.do_codex_resize("codex_resize", ctx.codex_session)
                return
            if path == REPOSITORY_SELECT_PATH:
                self.do_repository_select("repository_select")
                return
            self.refuse(404, "not_found", f"no control endpoint at {path}", "none")

        def do_codex_output(self, codex_session: CodexSessionManager) -> None:
            query = self.path.split("?", 1)[1] if "?" in self.path else ""
            params = urllib.parse.parse_qsl(query, keep_blank_values=True, strict_parsing=False)
            names = [name for name, _ in params]
            if names and (names != ["cursor"]):
                self.refuse(
                    400, "validation", "only a single cursor query parameter is accepted", "codex_output"
                )
                return
            cursor: int | None = None
            if names == ["cursor"]:
                raw_cursor = params[0][1]
                if not re.fullmatch(r"\d+", raw_cursor):
                    self.refuse(
                        400,
                        "validation",
                        "cursor must be a nonnegative integer",
                        "codex_output",
                    )
                    return
                cursor = int(raw_cursor)
            record = codex_session.read(cursor)
            self.respond_json(
                200,
                {
                    "ok": True,
                    "action": "codex_output",
                    "outcome": "read",
                    "output": record,
                    "codex_session": codex_session.snapshot(),
                },
            )

        def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
            path = self.path_only()
            if path == "/api/state":
                with context.lease() as ctx:
                    state = build_state(ctx.repo, relay_log_lines, ctx.watcher, ctx.codex_session)
                    entries = discover_repositories(context.repositories_root, ctx.repo)
                    state["repository_catalog"] = catalog_payload(
                        entries, ctx.repo, context.repositories_root
                    )
                body = json.dumps(state, indent=2).encode("utf-8")
                self.respond(200, "application/json; charset=utf-8", body)
                return
            if path == "/api/codex/output":
                with context.lease() as ctx:
                    self.do_codex_output(ctx.codex_session)
                return
            if path in WATCHER_ACTIONS or path in CODEX_SESSION_ACTIONS or path == REPOSITORY_SELECT_PATH:
                message = f"use POST for {path}\n"
                self.respond(
                    405, "text/plain; charset=utf-8", message.encode("utf-8"), {"Allow": "POST"}
                )
                return
            if path in ("/api/codex/input", "/api/codex/resize"):
                message = f"use POST for {path}\n"
                self.respond(
                    405, "text/plain; charset=utf-8", message.encode("utf-8"), {"Allow": "POST"}
                )
                return
            asset = assets.get(path)
            if asset is not None:
                try:
                    body = asset.read_bytes()
                except OSError:
                    self.respond(500, "text/plain; charset=utf-8", b"asset is unreadable\n")
                    return
                content_type = CONTENT_TYPES.get(asset.suffix, "application/octet-stream")
                self.respond(200, content_type, body)
                return
            message = (
                f"not found: {path}\n"
                "Available: /, /api/state, POST /api/watcher/start, POST /api/watcher/stop, "
                "POST /api/codex/start, POST /api/codex/stop, POST /api/codex/input, "
                "POST /api/codex/resize, GET /api/codex/output, "
                "POST /api/repository/select\n"
            )
            self.respond(404, "text/plain; charset=utf-8", message.encode("utf-8"))

        do_HEAD = do_GET

    return CoordinationHandler


class CoordinationServer(ThreadingHTTPServer):
    """A loopback server that stops whichever watcher/Codex context is active when it closes."""

    daemon_threads = True
    context: ApplicationContext | None = None

    @property
    def watcher(self) -> WatcherManager | None:
        return self.context.watcher if self.context is not None else None

    @watcher.setter
    def watcher(self, value: WatcherManager) -> None:
        if self.context is not None:
            self.context.watcher = value

    @property
    def codex_session(self) -> CodexSessionManager | None:
        return self.context.codex_session if self.context is not None else None

    @codex_session.setter
    def codex_session(self, value: CodexSessionManager) -> None:
        if self.context is not None:
            self.context.codex_session = value

    def server_close(self) -> None:
        try:
            super().server_close()
        finally:
            if self.context is not None:
                self.context.shutdown()


def create_server(
    repo: Path,
    host: str = "127.0.0.1",
    port: int = 0,
    relay_log_lines: int = RELAY_LOG_LINES,
    quiet: bool = False,
    assets: Path = ASSETS,
    watcher_command: list[str] | None = None,
    stop_timeout: float = STOP_TIMEOUT_SECONDS,
    start_grace: float = START_GRACE_SECONDS,
    codex_command: list[str] | None = None,
    repositories_root: Path | None = None,
    watcher_command_for_repo: Callable[[Path], list[str] | None] | None = None,
    codex_command_for_repo: Callable[[Path], list[str]] | None = None,
) -> CoordinationServer:
    root = repo.resolve()
    if not (is_git_repository(root) or is_initialized(root)):
        raise ValueError(
            f"{root} is neither a Git repository nor already coordination-initialized"
        )
    root_dir = repositories_root.resolve() if repositories_root is not None else root.parent
    if not root_dir.is_dir():
        raise ValueError(f"--repositories-root must be a directory: {root_dir}")

    if watcher_command_for_repo is not None:
        watcher_factory = watcher_command_for_repo
    elif watcher_command is not None:
        fixed_watcher_command = list(watcher_command)
        watcher_factory = lambda r: fixed_watcher_command  # noqa: E731
    else:
        watcher_factory = lambda r: None  # noqa: E731

    if codex_command_for_repo is not None:
        codex_factory = codex_command_for_repo
    elif codex_command is not None:
        fixed_codex_command = list(codex_command)
        codex_factory = lambda r: fixed_codex_command  # noqa: E731
    else:
        codex_factory = default_codex_command

    context = ApplicationContext(
        root,
        root_dir,
        watcher_command_for_repo=watcher_factory,
        codex_command_for_repo=codex_factory,
        stop_timeout=stop_timeout,
        start_grace=start_grace,
    )
    handler = make_handler(context, static_assets(assets), relay_log_lines, quiet)
    server = CoordinationServer((host, port), handler)
    server.context = context
    return server


def serve(args: argparse.Namespace) -> int:
    repo = args.repo.resolve()
    if not is_loopback_host(str(args.host)):
        print(
            "error: unauthenticated mode refuses a non-loopback bind; configure "
            "auth_mode = 'oidc' for a reverse-proxied deployment",
            file=sys.stderr,
        )
        return 2
    try:
        server = create_server(
            repo,
            args.host,
            args.port,
            args.relay_log_lines,
            args.quiet,
            repositories_root=args.repositories_root,
        )
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except OSError as error:
        print(f"error: cannot bind {args.host}:{args.port}: {error}", file=sys.stderr)
        return 2
    host, port = server.server_address[:2]
    print(f"Serving {repo} at http://{host}:{port}/ (Ctrl-C to stop)", flush=True)
    print("Browser controls start and stop one automatic both watcher.", flush=True)
    stop_on_signal(server)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Web app stopped.")
        return 130
    finally:
        server.server_close()
    return 0


def stop_on_signal(server: CoordinationServer) -> None:
    """Let a termination signal reach the shutdown path that reaps the watcher."""

    def handle(number: int, frame: object) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    for number in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(number, handle)
        except (AttributeError, OSError, ValueError):
            continue


def load_config(path: Path) -> dict[str, object]:
    """Load and validate a portable settings file for one of the supported keys.

    Relative `repo`/`repositories_root` values are resolved against `path`'s
    own directory (not the launch working directory), so the same config file
    behaves the same regardless of where it is invoked from. Only the flat,
    documented keys in `CONFIG_KEYS` are accepted; anything else -- an unknown
    key, an unknown section, a wrong TOML type (including bool-as-int), an
    out-of-range port/log length, or an empty path/host -- raises `ValueError`
    with a message meant to be shown via `argparse`-style usage errors.
    """
    resolved = path.resolve()
    if not resolved.is_file():
        raise ValueError(f"--config file not found: {path}")
    try:
        with resolved.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as error:
        raise ValueError(f"--config file is not valid TOML: {error}") from error
    except OSError as error:
        raise ValueError(f"cannot read --config file: {error}") from error
    if not isinstance(data, dict):
        raise ValueError("--config file must contain a table at the top level")

    unknown_keys = set(data) - CONFIG_KEYS
    if unknown_keys:
        raise ValueError(
            "--config file has unknown key(s): " + ", ".join(sorted(unknown_keys))
        )

    config_dir = resolved.parent
    settings: dict[str, object] = {}

    for key in ("repo", "repositories_root", "state_dir"):
        if key not in data:
            continue
        value = data[key]
        if not isinstance(value, str):
            raise ValueError(f"--config {key} must be a string")
        if not value.strip():
            raise ValueError(f"--config {key} must not be empty")
        candidate = Path(value)
        settings[key] = candidate if candidate.is_absolute() else (config_dir / candidate)

    if "host" in data:
        value = data["host"]
        if not isinstance(value, str):
            raise ValueError("--config host must be a string")
        if not value.strip():
            raise ValueError("--config host must not be empty")
        settings["host"] = value

    if "port" in data:
        value = data["port"]
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("--config port must be an integer")
        if not 0 <= value <= 65535:
            raise ValueError("--config port must be between 0 and 65535")
        settings["port"] = value

    if "relay_log_lines" in data:
        value = data["relay_log_lines"]
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("--config relay_log_lines must be an integer")
        if value < 0:
            raise ValueError("--config relay_log_lines must not be negative")
        settings["relay_log_lines"] = value

    if "quiet" in data:
        value = data["quiet"]
        if not isinstance(value, bool):
            raise ValueError("--config quiet must be a boolean")
        settings["quiet"] = value

    string_keys = (
        "auth_mode",
        "oidc_issuer",
        "oidc_client_id",
        "oidc_client_secret_env",
        "external_url",
        "groups_claim",
        "forwarded_allow_ips",
    )
    for key in string_keys:
        if key not in data:
            continue
        value = data[key]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"--config {key} must be a non-empty string")
        settings[key] = value

    for key in ("allowed_subjects", "allowed_groups", "trusted_hosts"):
        if key not in data:
            continue
        value = data[key]
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item.strip() for item in value
        ):
            raise ValueError(f"--config {key} must be an array of non-empty strings")
        settings[key] = list(value)

    for key in ("session_idle_seconds", "session_absolute_seconds"):
        if key not in data:
            continue
        value = data[key]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"--config {key} must be a positive integer")
        settings[key] = value

    if "insecure_oidc_http" in data:
        value = data["insecure_oidc_http"]
        if not isinstance(value, bool):
            raise ValueError("--config insecure_oidc_http must be a boolean")
        settings["insecure_oidc_http"] = value

    return settings


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="portable TOML settings file (see workflow.example.toml); explicit "
        "command-line flags override its base-server and OIDC values",
    )
    parser.add_argument(
        "--repo",
        type=Path,
        default=None,
        help="project root to serve; must be a Git repository or already coordination-"
        "initialized; a relative path resolves against the current working directory "
        "(default: the current working directory, unless --config supplies repo)",
    )
    parser.add_argument(
        "--repositories-root",
        type=Path,
        default=None,
        help="directory whose Git direct children can be switched to; defaults to "
        "the resolved --repo's parent directory, unless --config supplies "
        "repositories_root",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="bind address; the localhost default keeps the unauthenticated app off the "
        "LAN (default: 127.0.0.1, unless --config supplies host)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="TCP port, 0 picks a free port (default: 8765, unless --config supplies port)",
    )
    parser.add_argument(
        "--relay-log-lines",
        type=int,
        default=None,
        help="relay-log tail length returned by /api/state (default: "
        f"{RELAY_LOG_LINES}, unless --config supplies relay_log_lines)",
    )
    parser.add_argument(
        "--quiet",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="suppress per-request logging; use --no-quiet to force logging on "
        "(default: off, unless --config supplies quiet)",
    )
    parser.add_argument(
        "--auth-mode",
        choices=("local", "oidc"),
        default=None,
        help="local uses the loopback-only ASGI runtime; oidc enables OpenID Connect",
    )
    parser.add_argument("--oidc-issuer", default=None, help="exact OpenID Provider issuer URL")
    parser.add_argument("--oidc-client-id", default=None, help="OIDC confidential client id")
    parser.add_argument(
        "--oidc-client-secret-env",
        default=None,
        help="name of the environment variable containing the OIDC client secret",
    )
    parser.add_argument("--external-url", default=None, help="canonical external HTTPS origin")
    parser.add_argument(
        "--allowed-subject", action="append", default=None, help="allowed OIDC sub; repeatable"
    )
    parser.add_argument(
        "--allowed-group", action="append", default=None, help="allowed OIDC group; repeatable"
    )
    parser.add_argument("--groups-claim", default=None, help="OIDC claim containing group names")
    parser.add_argument(
        "--state-dir", type=Path, default=None, help="owner-only directory for sessions and audit data"
    )
    parser.add_argument("--session-idle-seconds", type=int, default=None)
    parser.add_argument("--session-absolute-seconds", type=int, default=None)
    parser.add_argument(
        "--trusted-host", action="append", default=None, help="accepted Host value; repeatable"
    )
    parser.add_argument(
        "--forwarded-allow-ips",
        default=None,
        help="Uvicorn trusted proxy IP/CIDR list; never use * on an exposed socket",
    )
    parser.add_argument(
        "--insecure-oidc-http",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="development-only: permit HTTP callback and a non-Secure cookie",
    )
    args = parser.parse_args(argv)

    config_values: dict[str, object] = {}
    if args.config is not None:
        try:
            config_values = load_config(args.config)
        except ValueError as error:
            parser.error(str(error))

    def resolved(name: str, cli_value: object, default: object) -> object:
        if cli_value is not None:
            return cli_value
        if name in config_values:
            return config_values[name]
        return default

    args.repo = resolved("repo", args.repo, Path.cwd())
    args.repositories_root = resolved("repositories_root", args.repositories_root, None)
    args.host = resolved("host", args.host, "127.0.0.1")
    args.port = resolved("port", args.port, 8765)
    args.relay_log_lines = resolved("relay_log_lines", args.relay_log_lines, RELAY_LOG_LINES)
    args.quiet = bool(resolved("quiet", args.quiet, False))
    args.auth_mode = resolved("auth_mode", args.auth_mode, "local")
    args.oidc_issuer = resolved("oidc_issuer", args.oidc_issuer, "")
    args.oidc_client_id = resolved("oidc_client_id", args.oidc_client_id, "")
    args.oidc_client_secret_env = resolved(
        "oidc_client_secret_env", args.oidc_client_secret_env, "COORDINATOR_OIDC_CLIENT_SECRET"
    )
    args.external_url = resolved("external_url", args.external_url, "")
    args.allowed_subject = resolved("allowed_subjects", args.allowed_subject, [])
    args.allowed_group = resolved("allowed_groups", args.allowed_group, [])
    args.groups_claim = resolved("groups_claim", args.groups_claim, "groups")
    default_state_home = Path(
        os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local" / "state"))
    )
    args.state_dir = resolved("state_dir", args.state_dir, default_state_home / "coordinator")
    args.session_idle_seconds = resolved(
        "session_idle_seconds", args.session_idle_seconds, 3600
    )
    args.session_absolute_seconds = resolved(
        "session_absolute_seconds", args.session_absolute_seconds, 43200
    )
    args.trusted_host = resolved("trusted_hosts", args.trusted_host, [])
    args.forwarded_allow_ips = resolved(
        "forwarded_allow_ips", args.forwarded_allow_ips, "127.0.0.1"
    )
    args.insecure_oidc_http = bool(
        resolved("insecure_oidc_http", args.insecure_oidc_http, False)
    )

    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    if args.relay_log_lines < 0:
        parser.error("--relay-log-lines must not be negative")
    if not str(args.host).strip():
        parser.error("--host must not be empty")
    if args.auth_mode not in {"local", "oidc"}:
        parser.error("--auth-mode must be local or oidc")
    if args.session_idle_seconds <= 0 or args.session_absolute_seconds <= 0:
        parser.error("session lifetimes must be positive")
    return args


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
