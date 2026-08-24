"""Read provider usage limits without creating Codex or Claude model turns."""

from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import sqlite3
import stat
import subprocess
import threading
import time
import urllib.error
import urllib.request
from contextlib import closing, suppress
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from . import __version__

DEFAULT_REFRESH_SECONDS = 3600
PROVIDER_TIMEOUT_SECONDS = 10.0
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
VELOCITY_WINDOW_SECONDS = 6 * 60 * 60
VELOCITY_HALF_LIFE_SECONDS = 3 * 60 * 60
MIN_VELOCITY_SPAN_SECONDS = 30 * 60
VELOCITY_RETENTION_SECONDS = 30 * 24 * 60 * 60


class ProviderUsageError(RuntimeError):
    """A bounded, user-safe provider usage collection failure."""


def _iso_timestamp(value: float | int | str | None) -> str | None:
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.astimezone(timezone.utc).isoformat()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _epoch_timestamp(value: object) -> float | None:
    normalized = _iso_timestamp(value if isinstance(value, (float, int, str)) else None)
    if normalized is None:
        return None
    return datetime.fromisoformat(normalized).timestamp()


def _reset_observation_key(value: object) -> str:
    """Canonicalize sub-second provider jitter without changing displayed resets."""

    normalized = _iso_timestamp(value if isinstance(value, (float, int, str)) else None)
    if normalized is None:
        return value if isinstance(value, str) else ""
    return datetime.fromisoformat(normalized).replace(microsecond=0).isoformat()


def _remaining_percent(used: object) -> float | None:
    if not isinstance(used, (int, float)) or isinstance(used, bool):
        return None
    return round(max(0.0, min(100.0, 100.0 - float(used))), 1)


def _window_label(minutes: object) -> str:
    if minutes == 300:
        return "Session (5h)"
    if minutes == 10080:
        return "Weekly (7d)"
    if isinstance(minutes, int) and minutes > 0:
        if minutes % 1440 == 0:
            return f"{minutes // 1440}-day"
        if minutes % 60 == 0:
            return f"{minutes // 60}-hour"
        return f"{minutes}-minute"
    return "rolling"


def _codex_windows(result: Mapping[str, object]) -> list[dict[str, object]]:
    """Flatten every default and named Codex rate-limit snapshot."""

    default = result.get("rateLimits")
    snapshots: list[Mapping[str, object]] = []
    seen_ids: set[str] = set()
    if isinstance(default, Mapping):
        snapshots.append(default)
        default_id = default.get("limitId")
        if isinstance(default_id, str):
            seen_ids.add(default_id)
    by_limit_id = result.get("rateLimitsByLimitId")
    if isinstance(by_limit_id, Mapping):
        for limit_id, candidate in by_limit_id.items():
            if not isinstance(candidate, Mapping) or limit_id in seen_ids:
                continue
            snapshots.append(candidate)
            if isinstance(limit_id, str):
                seen_ids.add(limit_id)

    windows: list[dict[str, object]] = []
    for snapshot_index, snapshot in enumerate(snapshots):
        limit_id = snapshot.get("limitId")
        stable_id = limit_id if isinstance(limit_id, str) else f"limit-{snapshot_index}"
        limit_name = snapshot.get("limitName")
        scope = limit_name.strip() if isinstance(limit_name, str) else ""
        if "spark" in f"{stable_id} {scope}".casefold():
            continue
        for key in ("primary", "secondary"):
            raw_window = snapshot.get(key)
            if not isinstance(raw_window, Mapping):
                continue
            remaining = _remaining_percent(raw_window.get("usedPercent"))
            if remaining is None:
                continue
            period = _window_label(raw_window.get("windowDurationMins"))
            duration = raw_window.get("windowDurationMins")
            windows.append(
                {
                    "id": f"{stable_id}:{key}",
                    "label": f"{scope} · {period}" if scope else period,
                    "scope": scope or None,
                    "kind": key,
                    "duration_minutes": duration if isinstance(duration, int) else None,
                    "remaining_percent": remaining,
                    "used_percent": round(100.0 - remaining, 1),
                    "resets_at": _iso_timestamp(raw_window.get("resetsAt")),
                }
            )
        individual = snapshot.get("individualLimit")
        if isinstance(individual, Mapping):
            remaining = individual.get("remainingPercent")
            if isinstance(remaining, (int, float)) and not isinstance(remaining, bool):
                bounded = round(max(0.0, min(100.0, float(remaining))), 1)
                label = f"{scope} · Spend" if scope else "Spend allowance"
                windows.append(
                    {
                        "id": f"{stable_id}:spend",
                        "label": label,
                        "scope": scope or None,
                        "kind": "spend",
                        "duration_minutes": None,
                        "remaining_percent": bounded,
                        "used_percent": round(100.0 - bounded, 1),
                        "resets_at": _iso_timestamp(individual.get("resetsAt")),
                    }
                )
    return windows


def _claude_limit_label(limit: Mapping[str, object]) -> str:
    scope = limit.get("scope")
    if isinstance(scope, Mapping):
        model = scope.get("model")
        if isinstance(model, Mapping):
            display_name = model.get("display_name")
            if isinstance(display_name, str) and display_name.strip():
                return display_name.strip()
        surface = scope.get("surface")
        if isinstance(surface, str) and surface.strip():
            return surface.replace("_", " ").title()
    kind = limit.get("kind")
    labels = {
        "session": "Session",
        "weekly_all": "Weekly",
        "weekly_scoped": "Weekly scoped",
    }
    if isinstance(kind, str):
        return labels.get(kind, kind.replace("_", " ").title())
    group = limit.get("group")
    return group.replace("_", " ").title() if isinstance(group, str) else "Usage"


def _claude_window_id(limit: Mapping[str, object]) -> str:
    kind = limit.get("kind")
    prefix = kind if isinstance(kind, str) and kind else "limit"
    identity = json.dumps(
        {
            "kind": kind,
            "group": limit.get("group"),
            "scope": limit.get("scope"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}:{digest}"


def _claude_windows(payload: Mapping[str, object]) -> list[dict[str, object]]:
    """Prefer Claude's complete limit list, including named scoped limits."""

    windows: list[dict[str, object]] = []
    limits = payload.get("limits")
    if isinstance(limits, list):
        for candidate in limits:
            if not isinstance(candidate, Mapping):
                continue
            remaining = _remaining_percent(candidate.get("percent"))
            if remaining is None:
                continue
            kind = candidate.get("kind")
            group = candidate.get("group")
            duration = (
                300 if kind == "session" else 10080 if group == "weekly" else None
            )
            windows.append(
                {
                    "id": _claude_window_id(candidate),
                    "label": _claude_limit_label(candidate),
                    "scope": candidate.get("scope"),
                    "kind": kind,
                    "group": group,
                    "active": candidate.get("is_active") is True,
                    "severity": candidate.get("severity"),
                    "duration_minutes": duration,
                    "remaining_percent": remaining,
                    "used_percent": round(100.0 - remaining, 1),
                    "resets_at": _iso_timestamp(candidate.get("resets_at")),
                }
            )
    if windows:
        return windows

    fallback_keys = [("five_hour", "Session"), ("seven_day", "Weekly")]
    fallback_keys.extend(
        (key, key.removeprefix("seven_day_").replace("_", " ").title())
        for key in payload
        if isinstance(key, str) and key.startswith("seven_day_")
    )
    for key, label in fallback_keys:
        raw_window = payload.get(key)
        if not isinstance(raw_window, Mapping):
            continue
        remaining = _remaining_percent(raw_window.get("utilization"))
        if remaining is None:
            continue
        windows.append(
            {
                "id": key,
                "label": label,
                "scope": None,
                "kind": key,
                "duration_minutes": 300 if key == "five_hour" else 10080,
                "remaining_percent": remaining,
                "used_percent": round(100.0 - remaining, 1),
                "resets_at": _iso_timestamp(raw_window.get("resets_at")),
            }
        )
    return windows


def _provider_result(
    provider_id: str,
    name: str,
    *,
    plan: str | None,
    source: str,
    windows: list[dict[str, object]],
) -> dict[str, object]:
    remaining = [
        float(window["remaining_percent"])
        for window in windows
        if isinstance(window.get("remaining_percent"), (int, float))
    ]
    return {
        "id": provider_id,
        "name": name,
        "status": "available" if remaining else "unavailable",
        "plan": plan,
        "source": source,
        "remaining_percent": min(remaining) if remaining else None,
        "windows": windows,
        "message": None if remaining else "No rolling usage windows were returned.",
    }


def _rate_forecast(
    window: Mapping[str, object],
    now: float,
    burn_rate: float,
    *,
    method: str,
    sample_count: int,
    basis_hours: float,
    confidence: str,
) -> dict[str, object]:
    remaining = window.get("remaining_percent")
    reset_at = _epoch_timestamp(window.get("resets_at"))
    if (
        not isinstance(remaining, (int, float))
        or isinstance(remaining, bool)
        or reset_at is None
        or reset_at <= now
    ):
        return _unavailable_forecast(sample_count, basis_hours)
    hours_to_reset = (reset_at - now) / 3600
    sustainable = float(remaining) / hours_to_reset
    projected = float(remaining) - burn_rate * hours_to_reset
    exhausts_at = (
        _iso_timestamp(now + float(remaining) / burn_rate * 3600)
        if burn_rate > 0 and projected <= 0
        else None
    )
    return {
        "method": method,
        "projected_remaining": round(projected, 1),
        "burn_rate_percent_per_hour": round(burn_rate, 3),
        "sustainable_rate_percent_per_hour": round(sustainable, 3),
        "velocity_ratio": round(burn_rate / sustainable, 2) if sustainable > 0 else None,
        "sample_count": sample_count,
        "basis_hours": round(basis_hours, 2),
        "confidence": confidence,
        "exhausts_at": exhausts_at,
    }


def _unavailable_forecast(
    sample_count: int = 0, basis_hours: float = 0.0
) -> dict[str, object]:
    return {
        "method": "unavailable",
        "projected_remaining": None,
        "burn_rate_percent_per_hour": None,
        "sustainable_rate_percent_per_hour": None,
        "velocity_ratio": None,
        "sample_count": sample_count,
        "basis_hours": round(basis_hours, 2),
        "confidence": "unavailable",
        "exhausts_at": None,
    }


def _reset_average_forecast(
    window: Mapping[str, object], now: float
) -> dict[str, object]:
    reset_at = _epoch_timestamp(window.get("resets_at"))
    duration = window.get("duration_minutes")
    remaining = window.get("remaining_percent")
    if (
        reset_at is None
        or not isinstance(duration, (int, float))
        or isinstance(duration, bool)
        or duration <= 0
        or not isinstance(remaining, (int, float))
        or isinstance(remaining, bool)
    ):
        return _unavailable_forecast()
    started_at = reset_at - float(duration) * 60
    elapsed_hours = (now - started_at) / 3600
    if elapsed_hours <= 0:
        return _unavailable_forecast()
    burn_rate = max(0.0, (100.0 - float(remaining)) / elapsed_hours)
    return _rate_forecast(
        window,
        now,
        burn_rate,
        method="reset_average",
        sample_count=1,
        basis_hours=elapsed_hours,
        confidence="fallback",
    )


def _rolling_velocity_forecast(
    window: Mapping[str, object],
    observations: list[tuple[float, float]],
    now: float,
) -> dict[str, object]:
    points = sorted(
        (timestamp, remaining)
        for timestamp, remaining in observations
        if now - VELOCITY_WINDOW_SECONDS <= timestamp <= now
    )
    if len(points) < 2 or points[-1][0] - points[0][0] < MIN_VELOCITY_SPAN_SECONDS:
        return _reset_average_forecast(window, now)
    values = [((timestamp - now) / 3600, remaining) for timestamp, remaining in points]
    weights = [
        0.5 ** ((now - timestamp) / VELOCITY_HALF_LIFE_SECONDS)
        for timestamp, _remaining in points
    ]
    total_weight = sum(weights)
    mean_time = sum(weight * value[0] for weight, value in zip(weights, values)) / total_weight
    mean_remaining = (
        sum(weight * value[1] for weight, value in zip(weights, values)) / total_weight
    )
    denominator = sum(
        weight * (value[0] - mean_time) ** 2
        for weight, value in zip(weights, values)
    )
    if denominator <= 0:
        return _reset_average_forecast(window, now)
    remaining_slope = sum(
        weight * (value[0] - mean_time) * (value[1] - mean_remaining)
        for weight, value in zip(weights, values)
    ) / denominator
    basis_hours = (points[-1][0] - points[0][0]) / 3600
    if len(points) >= 5 and basis_hours >= 4:
        confidence = "high"
    elif len(points) >= 3 and basis_hours >= 2:
        confidence = "medium"
    else:
        confidence = "low"
    return _rate_forecast(
        window,
        now,
        max(0.0, -remaining_slope),
        method="rolling_velocity",
        sample_count=len(points),
        basis_hours=basis_hours,
        confidence=confidence,
    )


class ProviderUsageVelocityStore:
    """Persist reset-scoped limit observations for rolling velocity forecasts."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir.resolve()
        self.path = self.state_dir / "usage.sqlite3"
        self.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.state_dir.chmod(0o700)
        details = self.state_dir.stat()
        if details.st_uid != os.geteuid() or stat.S_IMODE(details.st_mode) & 0o077:
            raise ValueError("state_dir must be owned by the service user and mode 0700")
        if self.path.is_symlink():
            raise ValueError("usage database path must not be a symbolic link")
        with closing(sqlite3.connect(self.path)) as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                CREATE TABLE IF NOT EXISTS provider_limit_observations (
                    provider_id TEXT NOT NULL,
                    window_id TEXT NOT NULL,
                    reset_key TEXT NOT NULL,
                    observed_at REAL NOT NULL,
                    remaining_percent REAL NOT NULL,
                    PRIMARY KEY(provider_id, window_id, reset_key, observed_at)
                );
                CREATE INDEX IF NOT EXISTS provider_limit_observations_recent
                    ON provider_limit_observations(
                        provider_id, window_id, reset_key, observed_at
                    );
                """
            )
            self._normalize_existing_reset_keys(connection)
            connection.commit()
        self.path.chmod(0o600)

    @staticmethod
    def _normalize_existing_reset_keys(connection: sqlite3.Connection) -> None:
        """Merge observations written before reset-key canonicalization."""

        rows = connection.execute(
            """
            SELECT provider_id, window_id, reset_key, observed_at, remaining_percent
            FROM provider_limit_observations
            """
        ).fetchall()
        for provider_id, window_id, reset_key, observed_at, remaining in rows:
            canonical = _reset_observation_key(reset_key)
            if canonical == reset_key:
                continue
            connection.execute(
                """
                INSERT INTO provider_limit_observations(
                    provider_id, window_id, reset_key, observed_at, remaining_percent
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(provider_id, window_id, reset_key, observed_at)
                DO UPDATE SET remaining_percent=excluded.remaining_percent
                """,
                (provider_id, window_id, canonical, observed_at, remaining),
            )
            connection.execute(
                """
                DELETE FROM provider_limit_observations
                WHERE provider_id = ? AND window_id = ? AND reset_key = ?
                  AND observed_at = ?
                """,
                (provider_id, window_id, reset_key, observed_at),
            )

    def enrich(
        self, provider: dict[str, object], observed_at: float
    ) -> dict[str, object]:
        provider_id = str(provider.get("id") or "")
        windows = provider.get("windows")
        if not provider_id or not isinstance(windows, list):
            return provider
        cutoff = observed_at - VELOCITY_RETENTION_SECONDS
        with closing(sqlite3.connect(self.path, timeout=10.0)) as connection:
            connection.execute(
                "DELETE FROM provider_limit_observations WHERE observed_at < ?", (cutoff,)
            )
            for raw_window in windows:
                if not isinstance(raw_window, dict):
                    continue
                window_id = raw_window.get("id")
                remaining = raw_window.get("remaining_percent")
                reset_key = raw_window.get("resets_at")
                if (
                    not isinstance(window_id, str)
                    or not window_id
                    or not isinstance(remaining, (int, float))
                    or isinstance(remaining, bool)
                ):
                    raw_window["forecast"] = _reset_average_forecast(
                        raw_window, observed_at
                    )
                    continue
                normalized_reset = _reset_observation_key(reset_key)
                connection.execute(
                    """
                    INSERT INTO provider_limit_observations(
                        provider_id, window_id, reset_key, observed_at, remaining_percent
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(provider_id, window_id, reset_key, observed_at)
                    DO UPDATE SET remaining_percent=excluded.remaining_percent
                    """,
                    (
                        provider_id,
                        window_id,
                        normalized_reset,
                        observed_at,
                        float(remaining),
                    ),
                )
                rows = connection.execute(
                    """
                    SELECT observed_at, remaining_percent
                    FROM provider_limit_observations
                    WHERE provider_id = ? AND window_id = ? AND reset_key = ?
                      AND observed_at >= ? AND observed_at <= ?
                    ORDER BY observed_at
                    """,
                    (
                        provider_id,
                        window_id,
                        normalized_reset,
                        observed_at - VELOCITY_WINDOW_SECONDS,
                        observed_at,
                    ),
                ).fetchall()
                raw_window["forecast"] = _rolling_velocity_forecast(
                    raw_window,
                    [(float(row[0]), float(row[1])) for row in rows],
                    observed_at,
                )
            connection.commit()
        return provider


def _write_message(stream: TextIO, message: Mapping[str, object]) -> None:
    stream.write(json.dumps(message, separators=(",", ":")) + "\n")
    stream.flush()


def _response_reader(stream: TextIO, output: queue.Queue[object]) -> None:
    try:
        for line in stream:
            try:
                output.put(json.loads(line))
            except json.JSONDecodeError:
                continue
    finally:
        output.put(None)


def _wait_for_response(
    output: queue.Queue[object], request_id: int, deadline: float
) -> dict[str, object]:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderUsageError("Codex usage request timed out.")
        try:
            message = output.get(timeout=remaining)
        except queue.Empty as error:
            raise ProviderUsageError("Codex usage request timed out.") from error
        if message is None:
            raise ProviderUsageError("Codex app server closed before returning usage.")
        if not isinstance(message, dict) or message.get("id") != request_id:
            continue
        if "error" in message:
            raise ProviderUsageError("Codex app server rejected the usage request.")
        result = message.get("result")
        if not isinstance(result, dict):
            raise ProviderUsageError("Codex app server returned invalid usage data.")
        return result


def collect_codex_usage(
    *,
    command: str | None = None,
    timeout: float = PROVIDER_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """Read Codex account limits through app-server JSON-RPC, without a turn."""

    executable = command or shutil.which("codex")
    if not executable:
        raise ProviderUsageError("Codex CLI is not installed or not on PATH.")
    try:
        process = subprocess.Popen(
            [executable, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )
    except OSError as error:
        raise ProviderUsageError("Codex app server could not be started.") from error

    if process.stdin is None or process.stdout is None:
        process.kill()
        raise ProviderUsageError("Codex app server streams are unavailable.")

    output: queue.Queue[object] = queue.Queue()
    reader = threading.Thread(
        target=_response_reader,
        args=(process.stdout, output),
        name="coordinator-codex-usage-reader",
        daemon=True,
    )
    reader.start()
    deadline = time.monotonic() + timeout
    try:
        _write_message(
            process.stdin,
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "coordinator", "version": __version__}
                },
            },
        )
        _wait_for_response(output, 1, deadline)
        _write_message(process.stdin, {"method": "initialized"})
        _write_message(
            process.stdin,
            {"id": 2, "method": "account/rateLimits/read", "params": None},
        )
        result = _wait_for_response(output, 2, deadline)
    except (BrokenPipeError, OSError) as error:
        raise ProviderUsageError("Codex app server closed during the usage request.") from error
    finally:
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                process.kill()
            process.wait(timeout=1.0)

    raw_limits = result.get("rateLimits")
    if not isinstance(raw_limits, dict):
        raise ProviderUsageError("Codex did not return account rate limits.")
    windows = _codex_windows(result)
    plan = raw_limits.get("planType")
    return _provider_result(
        "codex",
        "Codex",
        plan=str(plan) if isinstance(plan, str) else None,
        source="Codex app server",
        windows=windows,
    )


def _claude_credentials_path() -> Path:
    config_dir = Path.home() / ".claude"
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    if configured:
        config_dir = Path(configured).expanduser()
    return config_dir / ".credentials.json"


def _read_claude_usage(token: str, timeout: float) -> dict[str, object]:
    request = urllib.request.Request(
        CLAUDE_USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": f"coordinator/{__version__}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read(1024 * 1024 + 1)
    except urllib.error.HTTPError as error:
        if error.code in {401, 403}:
            message = "Claude login is expired or cannot read usage. Run `claude auth login`."
        elif error.code == 429:
            message = "Claude's usage endpoint is temporarily rate limited."
        else:
            message = f"Claude's usage endpoint returned HTTP {error.code}."
        raise ProviderUsageError(message) from error
    except (OSError, urllib.error.URLError) as error:
        raise ProviderUsageError("Claude's usage endpoint could not be reached.") from error
    if len(body) > 1024 * 1024:
        raise ProviderUsageError("Claude returned an unexpectedly large usage response.")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProviderUsageError("Claude returned invalid usage data.") from error
    if not isinstance(payload, dict):
        raise ProviderUsageError("Claude returned invalid usage data.")
    return payload


def collect_claude_usage(
    *,
    command: str | None = None,
    credentials_path: Path | None = None,
    timeout: float = PROVIDER_TIMEOUT_SECONDS,
) -> dict[str, object]:
    """Read Claude subscription limits without opening an interactive session."""

    executable = command or shutil.which("claude")
    if not executable:
        raise ProviderUsageError("Claude CLI is not installed or not on PATH.")
    try:
        status = subprocess.run(
            [executable, "auth", "status", "--json"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ProviderUsageError("Claude authentication status could not be read.") from error
    try:
        auth = json.loads(status.stdout)
    except json.JSONDecodeError as error:
        raise ProviderUsageError("Claude authentication status was invalid.") from error
    if status.returncode != 0 or not isinstance(auth, dict) or auth.get("loggedIn") is not True:
        raise ProviderUsageError("Claude CLI is not logged in.")
    if auth.get("authMethod") != "claude.ai":
        raise ProviderUsageError("Claude usage is available only for claude.ai subscriptions.")

    credential_file = credentials_path or _claude_credentials_path()
    try:
        credentials = json.loads(credential_file.read_text(encoding="utf-8"))
        oauth = credentials["claudeAiOauth"]
        token = oauth["accessToken"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ProviderUsageError(
            "Claude's local OAuth credentials are unavailable; run `claude auth login`."
        ) from error
    if not isinstance(token, str) or not token:
        raise ProviderUsageError("Claude's local OAuth access token is unavailable.")

    payload = _read_claude_usage(token, timeout)
    windows = _claude_windows(payload)
    plan = auth.get("subscriptionType")
    return _provider_result(
        "claude",
        "Claude",
        plan=str(plan) if isinstance(plan, str) else None,
        source="Claude Code account",
        windows=windows,
    )


class ProviderUsageService:
    """Own one process-wide, hourly-refreshed provider usage snapshot."""

    def __init__(
        self,
        refresh_seconds: int = DEFAULT_REFRESH_SECONDS,
        *,
        collectors: Mapping[str, Callable[[], dict[str, object]]] | None = None,
        clock: Callable[[], float] = time.time,
        state_dir: Path | None = None,
    ) -> None:
        if refresh_seconds <= 0:
            raise ValueError("refresh_seconds must be positive")
        self.refresh_seconds = refresh_seconds
        self._collectors = dict(
            collectors
            or {"codex": collect_codex_usage, "claude": collect_claude_usage}
        )
        self._clock = clock
        self._velocity_store = (
            ProviderUsageVelocityStore(state_dir) if state_dir is not None else None
        )
        self._lock = threading.RLock()
        self._refresh_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._snapshot: dict[str, object] = {
            "generated_at": None,
            "next_refresh_at": None,
            "refresh_interval_seconds": refresh_seconds,
            "refreshing": False,
            "providers": [
                {
                    "id": provider_id,
                    "name": provider_id.title(),
                    "status": "checking",
                    "plan": None,
                    "source": None,
                    "remaining_percent": None,
                    "windows": [],
                    "message": "Waiting for the first usage check.",
                    "stale": False,
                    "last_success_at": None,
                    "last_error_at": None,
                }
                for provider_id in self._collectors
            ],
        }

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return deepcopy(self._snapshot)

    def _enrich_forecasts(
        self, provider: dict[str, object], observed_at: float
    ) -> None:
        if self._velocity_store is not None:
            try:
                self._velocity_store.enrich(provider, observed_at)
                return
            except (OSError, sqlite3.Error):
                provider["forecast_history_status"] = "unavailable"
        windows = provider.get("windows")
        if isinstance(windows, list):
            for window in windows:
                if isinstance(window, dict):
                    window["forecast"] = _reset_average_forecast(window, observed_at)

    def refresh(self) -> dict[str, object]:
        with self._refresh_lock:
            with self._lock:
                self._snapshot["refreshing"] = True
                previous = {
                    str(provider.get("id")): deepcopy(provider)
                    for provider in self._snapshot.get("providers", [])
                    if isinstance(provider, Mapping) and provider.get("id")
                }

            def collect(item: tuple[str, Callable[[], dict[str, object]]]):
                provider_id, collector = item
                try:
                    return collector()
                except ProviderUsageError as error:
                    message = str(error)
                except Exception:  # noqa: BLE001 - never expose provider internals
                    message = "Usage could not be read due to an unexpected provider error."
                return {
                    "id": provider_id,
                    "name": provider_id.title(),
                    "status": "unavailable",
                    "plan": None,
                    "source": None,
                    "remaining_percent": None,
                    "windows": [],
                    "message": message,
                }

            with ThreadPoolExecutor(
                max_workers=max(1, len(self._collectors)),
                thread_name_prefix="coordinator-provider-usage",
            ) as executor:
                collected = list(executor.map(collect, self._collectors.items()))
            completed = self._clock()
            completed_at = _iso_timestamp(completed)
            providers: list[dict[str, object]] = []
            for result in collected:
                provider_id = str(result.get("id") or "")
                if result.get("status") == "available":
                    self._enrich_forecasts(result, completed)
                    result.update(
                        {
                            "stale": False,
                            "last_success_at": completed_at,
                            "last_error_at": None,
                        }
                    )
                    providers.append(result)
                    continue
                prior = previous.get(provider_id)
                prior_has_usage = bool(
                    prior
                    and (
                        prior.get("windows")
                        or isinstance(prior.get("remaining_percent"), (int, float))
                    )
                    and prior.get("status") in {"available", "stale"}
                )
                if prior_has_usage and prior is not None:
                    prior.update(
                        {
                            "status": "stale",
                            "stale": True,
                            "message": result.get("message"),
                            "last_error_at": completed_at,
                        }
                    )
                    providers.append(prior)
                    continue
                result.update(
                    {
                        "stale": False,
                        "last_success_at": None,
                        "last_error_at": completed_at,
                    }
                )
                providers.append(result)
            with self._lock:
                self._snapshot = {
                    "generated_at": completed_at,
                    "next_refresh_at": _iso_timestamp(completed + self.refresh_seconds),
                    "refresh_interval_seconds": self.refresh_seconds,
                    "refreshing": False,
                    "providers": providers,
                }
                return deepcopy(self._snapshot)

    def _run(self) -> None:
        if self.snapshot().get("generated_at") is not None:
            if self._stop.wait(self.refresh_seconds):
                return
        while not self._stop.is_set():
            self.refresh()
            self._stop.wait(self.refresh_seconds)

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            if self._snapshot.get("generated_at") is None:
                self._snapshot["refreshing"] = True
            self._thread = threading.Thread(
                target=self._run,
                name="coordinator-provider-usage",
                daemon=True,
            )
            self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=PROVIDER_TIMEOUT_SECONDS + 2.0)
