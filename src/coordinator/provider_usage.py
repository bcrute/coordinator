"""Read provider usage limits without creating Codex or Claude model turns."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from contextlib import suppress
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


def _claude_windows(payload: Mapping[str, object]) -> list[dict[str, object]]:
    """Prefer Claude's complete limit list, including named scoped limits."""

    windows: list[dict[str, object]] = []
    limits = payload.get("limits")
    if isinstance(limits, list):
        for index, candidate in enumerate(limits):
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
                    "id": f"{kind if isinstance(kind, str) else 'limit'}:{index}",
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
    ) -> None:
        if refresh_seconds <= 0:
            raise ValueError("refresh_seconds must be positive")
        self.refresh_seconds = refresh_seconds
        self._collectors = dict(
            collectors
            or {"codex": collect_codex_usage, "claude": collect_claude_usage}
        )
        self._clock = clock
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
                }
                for provider_id in self._collectors
            ],
        }

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return deepcopy(self._snapshot)

    def refresh(self) -> dict[str, object]:
        with self._refresh_lock:
            with self._lock:
                self._snapshot["refreshing"] = True

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
                providers = list(executor.map(collect, self._collectors.items()))
            completed = self._clock()
            with self._lock:
                self._snapshot = {
                    "generated_at": _iso_timestamp(completed),
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
