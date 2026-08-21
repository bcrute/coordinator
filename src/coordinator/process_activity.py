"""Privacy-bounded process activity for one managed terminal session.

The observer walks only descendants of the exact PID launched by
``CodexSessionManager``.  It does not search globally by process name, and it
never returns arbitrary command arguments or environment values.  This keeps
unrelated Codex and Claude instances on the same workstation out of the
dashboard while still making background terminals and nested agents visible.
"""

from __future__ import annotations

import copy
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import quote

DEFAULT_CACHE_SECONDS = 0.75
MAX_CMDLINE_BYTES = 64 * 1024
MAX_ENVIRON_BYTES = 128 * 1024
MAX_PROCESS_LABEL_CHARS = 80
MAX_OBSERVED_PROCESSES = 256
MAX_AGENT_FDS = 512
STRUCTURED_MODEL_CACHE_SECONDS = 5.0

MODEL_ENVIRONMENT_KEYS = frozenset(
    {"ANTHROPIC_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL", "CODEX_MODEL"}
)
MODEL_FLAGS = frozenset({"--model", "-m"})
MODEL_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,127}$")
CODEX_STATE_DATABASE = re.compile(r"^state_[0-9]+\.sqlite$")

PROVIDER_EXECUTABLES = {
    "openai": frozenset({"codex", "codex-cli"}),
    "anthropic": frozenset({"claude", "claude-code"}),
}
PROVIDER_LABELS = {"openai": "Codex", "anthropic": "Claude"}
NODE_EXECUTABLES = frozenset({"node", "nodejs"})
TRANSPARENT_HELPERS = frozenset({"codex-code-mode", "codex-code-mode-host"})
PROCESS_STATE_LABELS = {
    "R": "running",
    "S": "sleeping",
    "D": "blocked",
    "T": "stopped",
    "t": "tracing",
    "Z": "zombie",
    "X": "exited",
    "I": "idle",
}


@dataclass(frozen=True)
class ProcessRecord:
    """The bounded process metadata needed to classify one descendant."""

    pid: int
    ppid: int
    name: str
    state: str
    start_ticks: int
    argv: tuple[str, ...] = ()
    model_environment: Mapping[str, str] = field(default_factory=dict)
    structured_model: str | None = None


def _basename(value: str) -> str:
    return Path(value).name.lower() if value else ""


def _safe_label(value: str, fallback: str) -> str:
    cleaned = "".join(
        character if character.isprintable() else "?" for character in value
    )
    cleaned = cleaned.strip()
    return (cleaned or fallback)[:MAX_PROCESS_LABEL_CHARS]


def _agent_provider(record: ProcessRecord) -> str | None:
    candidates = {_basename(record.name)}
    if record.argv:
        candidates.add(_basename(record.argv[0]))
        if _basename(record.argv[0]) in NODE_EXECUTABLES and len(record.argv) > 1:
            candidates.add(_basename(record.argv[1]))
    for provider, executables in PROVIDER_EXECUTABLES.items():
        if candidates & executables:
            return provider
    return None


def _is_node_wrapper(record: ProcessRecord, provider: str) -> bool:
    if not record.argv or _basename(record.argv[0]) not in NODE_EXECUTABLES:
        return False
    if len(record.argv) < 2:
        return False
    return _basename(record.argv[1]) in PROVIDER_EXECUTABLES[provider]


def _model_value(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = value.strip()
    return candidate if MODEL_VALUE.fullmatch(candidate) else None


def _agent_models(
    record: ProcessRecord, provider: str
) -> tuple[str | None, str, str | None]:
    model: str | None = None
    source = "not_reported"
    for index, argument in enumerate(record.argv):
        if argument in MODEL_FLAGS and index + 1 < len(record.argv):
            model = _model_value(record.argv[index + 1])
            if model is not None:
                source = "argument"
                break
        if argument.startswith("--model="):
            model = _model_value(argument.split("=", 1)[1])
            if model is not None:
                source = "argument"
                break
    if model is None:
        model = _model_value(record.structured_model)
        if model is not None:
            source = "session_index"
    if model is None:
        environment_key = "CODEX_MODEL" if provider == "openai" else "ANTHROPIC_MODEL"
        model = _model_value(record.model_environment.get(environment_key))
        if model is not None:
            source = "environment"
    subagent_model = _model_value(
        record.model_environment.get("CLAUDE_CODE_SUBAGENT_MODEL")
    )
    return model, source, subagent_model


def _elapsed(start_ticks: int, uptime: float, clock_ticks: int) -> dict[str, object]:
    seconds = max(0, int(uptime - (start_ticks / max(1, clock_ticks))))
    hours, remainder = divmod(seconds, 3600)
    minutes, remaining_seconds = divmod(remainder, 60)
    return {
        "seconds": seconds,
        "display": f"{hours:02d}:{minutes:02d}:{remaining_seconds:02d}",
    }


def _identity(session_id: str, kind: str, record: ProcessRecord) -> str:
    return f"{session_id}:{kind}:{record.pid}:{record.start_ticks}"


def _scoped_pids(records: Mapping[int, ProcessRecord], root_pid: int) -> set[int]:
    children: dict[int, list[int]] = {}
    for record in records.values():
        children.setdefault(record.ppid, []).append(record.pid)
    scoped: set[int] = set()
    pending = [root_pid]
    while pending:
        pid = pending.pop()
        if pid in scoped or pid not in records:
            continue
        scoped.add(pid)
        pending.extend(children.get(pid, ()))
    return scoped


def _ancestor_chain(
    records: Mapping[int, ProcessRecord], scoped: set[int], pid: int
) -> list[int]:
    chain: list[int] = []
    seen: set[int] = set()
    current = records[pid].ppid
    while current in scoped and current not in seen:
        chain.append(current)
        seen.add(current)
        current = records[current].ppid
    return chain


def build_process_activity(
    records: Mapping[int, ProcessRecord],
    *,
    root_pid: int,
    session_id: str,
    observed_at: float,
    uptime: float,
    clock_ticks: int,
) -> dict[str, object]:
    """Classify one managed PID tree without considering unrelated records."""

    scoped = _scoped_pids(records, root_pid)
    root = records.get(root_pid)
    if root is None or root_pid not in scoped:
        return {
            "supported": True,
            "state": "unavailable",
            "detail": "The managed terminal process is no longer observable.",
            "session_id": session_id,
            "root_pid": root_pid,
            "root_start_ticks": None,
            "observed_at_epoch": observed_at,
            "truncated": False,
            "agents": [],
            "background_terminals": [],
        }

    children: dict[int, list[int]] = {}
    depth: dict[int, int] = {root_pid: 0}
    for pid in scoped:
        record = records[pid]
        children.setdefault(record.ppid, []).append(pid)
    pending = [root_pid]
    while pending:
        parent = pending.pop()
        for child in children.get(parent, ()):  # pragma: no branch - bounded tree walk
            depth[child] = depth[parent] + 1
            pending.append(child)

    candidates = {
        pid: provider
        for pid in scoped
        if (provider := _agent_provider(records[pid])) is not None
    }
    included_agents = set(candidates)
    wrapper_sources: dict[int, ProcessRecord] = {}
    collapsed_wrappers: set[int] = set()
    for pid, provider in candidates.items():
        record = records[pid]
        if not _is_node_wrapper(record, provider):
            continue
        matching_children = [
            child
            for child in children.get(pid, ())
            if candidates.get(child) == provider
        ]
        if len(matching_children) == 1:
            child = matching_children[0]
            included_agents.discard(pid)
            collapsed_wrappers.add(pid)
            wrapper_sources[child] = record

    agent_ids = {
        pid: _identity(session_id, "agent", records[pid]) for pid in included_agents
    }

    def nearest_agent_ancestor(pid: int) -> int | None:
        for ancestor in _ancestor_chain(records, scoped, pid):
            if ancestor in included_agents:
                return ancestor
        return None

    transparent = collapsed_wrappers | {
        pid
        for pid in scoped
        if _basename(records[pid].name) in TRANSPARENT_HELPERS
        or (
            records[pid].argv and _basename(records[pid].argv[0]) in TRANSPARENT_HELPERS
        )
    }
    background_roots: set[int] = set()
    for pid in scoped:
        owner = nearest_agent_ancestor(pid)
        if owner is None:
            continue
        path = list(reversed(_ancestor_chain(records, scoped, pid))) + [pid]
        try:
            owner_index = path.index(owner)
        except (
            ValueError
        ):  # pragma: no cover - defensive against a racing process table
            continue
        for candidate in path[owner_index + 1 :]:
            if candidate not in transparent:
                background_roots.add(candidate)
                break

    background_ids = {
        pid: _identity(session_id, "terminal", records[pid]) for pid in background_roots
    }

    def is_descendant_or_self(pid: int, ancestor: int) -> bool:
        return pid == ancestor or ancestor in _ancestor_chain(records, scoped, pid)

    background_terminals: list[dict[str, object]] = []
    for pid in sorted(
        background_roots, key=lambda value: (records[value].start_ticks, value)
    ):
        record = records[pid]
        owner = nearest_agent_ancestor(pid)
        nested_agent_pids = [
            agent_pid
            for agent_pid in included_agents
            if is_descendant_or_self(agent_pid, pid)
        ]
        process_count = sum(
            1 for candidate in scoped if is_descendant_or_self(candidate, pid)
        )
        provider = candidates.get(pid)
        title = (
            PROVIDER_LABELS[provider]
            if provider is not None
            else _safe_label(record.name, f"process {pid}")
        )
        background_terminals.append(
            {
                "id": background_ids[pid],
                "pid": pid,
                "parent_pid": record.ppid,
                "owner_agent_id": agent_ids.get(owner),
                "title": title,
                "kind": "agent" if pid in included_agents else "terminal",
                "state": "active" if record.state not in {"X", "Z"} else "exiting",
                "os_state": PROCESS_STATE_LABELS.get(record.state, "unknown"),
                "elapsed": _elapsed(record.start_ticks, uptime, clock_ticks),
                "process_count": process_count,
                "agent_ids": [agent_ids[value] for value in nested_agent_pids],
                "agent_count": len(nested_agent_pids),
                "depth": depth.get(pid, 0),
            }
        )

    agents: list[dict[str, object]] = []
    for pid in sorted(
        included_agents,
        key=lambda value: (
            0 if nearest_agent_ancestor(value) is None else 1,
            depth.get(value, 0),
            records[value].start_ticks,
            value,
        ),
    ):
        record = records[pid]
        provider = candidates[pid]
        model, source, subagent_model = _agent_models(record, provider)
        wrapper = wrapper_sources.get(pid)
        if wrapper is not None:
            wrapper_model, wrapper_source, wrapper_subagent_model = _agent_models(
                wrapper, provider
            )
            if model is None:
                model, source = wrapper_model, wrapper_source
            if subagent_model is None:
                subagent_model = wrapper_subagent_model
        parent_agent = nearest_agent_ancestor(pid)
        containing_background = [
            root_pid_value
            for root_pid_value in background_roots
            if is_descendant_or_self(pid, root_pid_value)
        ]
        containing_background.sort(key=lambda value: depth.get(value, 0), reverse=True)
        agents.append(
            {
                "id": agent_ids[pid],
                "pid": pid,
                "parent_pid": record.ppid,
                "parent_agent_id": agent_ids.get(parent_agent),
                "background_terminal_id": (
                    background_ids[containing_background[0]]
                    if containing_background
                    else None
                ),
                "provider": provider,
                "label": PROVIDER_LABELS[provider],
                "role": "lead" if parent_agent is None else "nested",
                "state": "active" if record.state not in {"X", "Z"} else "exiting",
                "os_state": PROCESS_STATE_LABELS.get(record.state, "unknown"),
                "model": model,
                "model_source": source,
                "subagent_model": subagent_model,
                "elapsed": _elapsed(record.start_ticks, uptime, clock_ticks),
                "depth": depth.get(pid, 0),
            }
        )

    return {
        "supported": True,
        "state": (
            "background_work"
            if background_terminals
            else "agent_active"
            if agents
            else "process_running"
        ),
        "detail": (
            "Scoped to descendants of this managed terminal session; "
            "unrelated workstation agents are excluded."
        ),
        "session_id": session_id,
        "root_pid": root_pid,
        "root_start_ticks": root.start_ticks,
        "observed_at_epoch": observed_at,
        "truncated": False,
        "agents": agents,
        "background_terminals": background_terminals,
    }


class ProcessActivityObserver:
    """Read and briefly cache one Linux procfs descendant tree."""

    def __init__(
        self,
        *,
        proc_root: Path = Path("/proc"),
        cache_seconds: float = DEFAULT_CACHE_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self.proc_root = proc_root
        self.cache_seconds = max(0.0, float(cache_seconds))
        self.monotonic = monotonic
        self.wall_clock = wall_clock
        self._lock = threading.Lock()
        self._cache_key: tuple[int | None, str | None] | None = None
        self._cache_at = 0.0
        self._cache: dict[str, object] | None = None
        self._structured_model_cache: dict[
            tuple[int, int], tuple[float, str | None]
        ] = {}

    def snapshot(
        self, root_pid: int | None, session_id: str | None
    ) -> dict[str, object]:
        key = (root_pid, session_id)
        current = self.monotonic()
        with self._lock:
            if (
                self._cache_key == key
                and self._cache is not None
                and current - self._cache_at < self.cache_seconds
            ):
                return copy.deepcopy(self._cache)
            payload = self._observe(root_pid, session_id)
            self._cache_key = key
            self._cache_at = current
            self._cache = payload
            return copy.deepcopy(payload)

    def _observe(
        self, root_pid: int | None, session_id: str | None
    ) -> dict[str, object]:
        observed_at = self.wall_clock()
        if root_pid is None or session_id is None:
            return {
                "supported": self.proc_root.is_dir(),
                "state": "idle",
                "detail": "No managed terminal session is running.",
                "session_id": session_id,
                "root_pid": root_pid,
                "root_start_ticks": None,
                "observed_at_epoch": observed_at,
                "truncated": False,
                "agents": [],
                "background_terminals": [],
            }
        if not self.proc_root.is_dir():
            return {
                "supported": False,
                "state": "unsupported",
                "detail": "Session process activity requires Linux procfs.",
                "session_id": session_id,
                "root_pid": root_pid,
                "root_start_ticks": None,
                "observed_at_epoch": observed_at,
                "truncated": False,
                "agents": [],
                "background_terminals": [],
            }
        records, truncated = self._read_tree(root_pid)
        try:
            uptime = float((self.proc_root / "uptime").read_text().split()[0])
        except FileNotFoundError, OSError, ValueError, IndexError:
            uptime = 0.0
        try:
            clock_ticks = int(os.sysconf("SC_CLK_TCK"))
        except OSError, ValueError:  # pragma: no cover - unusual libc failure
            clock_ticks = 100
        payload = build_process_activity(
            records,
            root_pid=root_pid,
            session_id=session_id,
            observed_at=observed_at,
            uptime=uptime,
            clock_ticks=clock_ticks,
        )
        payload["truncated"] = truncated
        if truncated:
            payload["detail"] = (
                f"Showing the first {MAX_OBSERVED_PROCESSES} descendants in this "
                "managed terminal session; unrelated workstation agents remain excluded."
            )
        return payload

    def _read_tree(self, root_pid: int) -> tuple[dict[int, ProcessRecord], bool]:
        records: dict[int, ProcessRecord] = {}
        pending: list[tuple[int, int | None]] = [(root_pid, None)]
        seen: set[int] = set()
        while pending:
            if len(records) >= MAX_OBSERVED_PROCESSES:
                return records, True
            pid, expected_parent = pending.pop()
            if pid in seen:
                continue
            seen.add(pid)
            record = self._read_record(pid)
            if record is None or (
                expected_parent is not None and record.ppid != expected_parent
            ):
                continue
            records[pid] = record
            children_path = self.proc_root / str(pid) / "task" / str(pid) / "children"
            try:
                child_pids = [int(value) for value in children_path.read_text().split()]
            except FileNotFoundError, OSError, ValueError:
                child_pids = []
            pending.extend((child_pid, pid) for child_pid in child_pids)
        return records, False

    def _read_record(self, pid: int) -> ProcessRecord | None:
        base = self.proc_root / str(pid)
        try:
            stat_text = (base / "stat").read_text()
            closing = stat_text.rfind(")")
            if closing < 0:
                return None
            name = stat_text[stat_text.find("(") + 1 : closing]
            fields = stat_text[closing + 2 :].split()
            state = fields[0]
            ppid = int(fields[1])
            start_ticks = int(fields[19])
        except FileNotFoundError, OSError, ValueError, IndexError:
            return None
        argv = self._read_null_fields(base / "cmdline", MAX_CMDLINE_BYTES)
        provisional = ProcessRecord(
            pid=pid,
            ppid=ppid,
            name=_safe_label(name, f"process {pid}"),
            state=state,
            start_ticks=start_ticks,
            argv=argv,
        )
        environment: dict[str, str] = {}
        provider = _agent_provider(provisional)
        if provider is not None:
            for value in self._read_null_fields(base / "environ", MAX_ENVIRON_BYTES):
                key, separator, content = value.partition("=")
                if separator and key in MODEL_ENVIRONMENT_KEYS:
                    environment[key] = content
        structured_model = (
            self._codex_index_model(base, provisional) if provider == "openai" else None
        )
        return ProcessRecord(
            pid=pid,
            ppid=ppid,
            name=provisional.name,
            state=state,
            start_ticks=start_ticks,
            argv=argv,
            model_environment=environment,
            structured_model=structured_model,
        )

    def _codex_index_model(self, base: Path, record: ProcessRecord) -> str | None:
        key = (record.pid, record.start_ticks)
        current = self.monotonic()
        cached = self._structured_model_cache.get(key)
        if cached is not None and current - cached[0] < STRUCTURED_MODEL_CACHE_SECONDS:
            return cached[1]

        model: str | None = None
        rollouts: list[str] = []
        databases: list[str] = []
        fd_root = base / "fd"
        try:
            descriptors = sorted(fd_root.iterdir(), key=lambda path: path.name)[
                :MAX_AGENT_FDS
            ]
        except FileNotFoundError, OSError:
            descriptors = []
        for descriptor in descriptors:
            try:
                target = os.readlink(descriptor)
            except OSError:
                continue
            target_path = Path(target)
            target_parts = target_path.parts
            if (
                target_path.suffix == ".jsonl"
                and target_path.name.startswith("rollout-")
                and ".codex" in target_parts
                and "sessions" in target_parts
            ):
                rollouts.append(target)
            elif (
                CODEX_STATE_DATABASE.fullmatch(target_path.name)
                and ".codex" in target_parts
            ):
                databases.append(target)
            if len(rollouts) >= 8 and len(databases) >= 4:
                break
        if rollouts and databases:
            model = self._model_from_session_index(databases, rollouts)
        if model is None and cached is not None:
            model = cached[1]
        self._structured_model_cache[key] = (current, model)
        if len(self._structured_model_cache) > 32:
            oldest = min(
                self._structured_model_cache,
                key=lambda cache_key: self._structured_model_cache[cache_key][0],
            )
            self._structured_model_cache.pop(oldest, None)
        return model

    @staticmethod
    def _model_from_session_index(
        databases: list[str], rollouts: list[str]
    ) -> str | None:
        placeholders = ",".join("?" for _ in rollouts)
        query = (
            "SELECT model FROM threads WHERE rollout_path IN ("
            f"{placeholders}) ORDER BY updated_at_ms DESC LIMIT 1"
        )
        for database in dict.fromkeys(databases):
            try:
                uri = f"file:{quote(database, safe='/')}?mode=ro"
                connection = sqlite3.connect(uri, uri=True, timeout=0.02)
                try:
                    row = connection.execute(query, rollouts).fetchone()
                finally:
                    connection.close()
            except sqlite3.Error:
                continue
            if row and isinstance(row[0], str):
                return _model_value(row[0])
        return None

    @staticmethod
    def _read_null_fields(path: Path, limit: int) -> tuple[str, ...]:
        try:
            with path.open("rb") as stream:
                raw = stream.read(limit)
        except FileNotFoundError, OSError:
            return ()
        return tuple(
            value.decode("utf-8", errors="replace")
            for value in raw.split(b"\0")
            if value
        )
