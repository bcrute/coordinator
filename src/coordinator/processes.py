"""Lifecycle supervision for the repository coordination watcher."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .coordination_locks import active_lock
from .executor_adapters import ExecutorAdapter
from .repositories import is_initialized

MANAGED_ROLE = "executor"
STOP_TIMEOUT_SECONDS = 10.0
START_GRACE_SECONDS = 1.0
ACTIVE_STATES = ("starting", "running")


def default_watcher_command(
    root: Path,
    executor: ExecutorAdapter | None = None,
    reviewer_model: str = "",
    reviewer_effort: str = "",
) -> list[str]:
    """Build the fixed app-owned executor watcher command for `root`."""

    command = [
        sys.executable,
        "-m",
        "coordinator.watch_coordination",
        "--repo",
        str(root),
        "--role",
        MANAGED_ROLE,
        "--no-dashboard",
    ]
    if executor is not None:
        command.extend(executor.watcher_arguments())
    if reviewer_model:
        command.extend(("--codex-model", reviewer_model))
    if reviewer_effort:
        command.extend(("--codex-effort", reviewer_effort))
    return command

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
            if active_lock(self.lock_path, reclaim_stale=True):
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
            lock_present = active_lock(self.lock_path, reclaim_stale=True)
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
