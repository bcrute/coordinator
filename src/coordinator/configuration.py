"""Command-line and TOML configuration for the Coordinator web application."""

from __future__ import annotations

import argparse
import os
import tomllib
from pathlib import Path

RELAY_LOG_LINES = 200
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
    "rate_limit_window_seconds",
    "rate_limit_auth_attempts",
    "rate_limit_control_attempts",
    "rate_limit_terminal_connections",
    "terminal_enabled",
    "trusted_hosts",
    "forwarded_allow_ips",
    "insecure_oidc_http",
}


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

    for key in (
        "session_idle_seconds",
        "session_absolute_seconds",
        "rate_limit_window_seconds",
        "rate_limit_auth_attempts",
        "rate_limit_control_attempts",
        "rate_limit_terminal_connections",
    ):
        if key not in data:
            continue
        value = data[key]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(f"--config {key} must be a positive integer")
        settings[key] = value

    for key in ("insecure_oidc_http", "terminal_enabled"):
        if key in data:
            value = data[key]
            if not isinstance(value, bool):
                raise ValueError(f"--config {key} must be a boolean")
            settings[key] = value

    return settings


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Serve the coordination dashboard in local or authenticated OIDC mode."
    )
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
    parser.add_argument("--rate-limit-window-seconds", type=int, default=None)
    parser.add_argument("--rate-limit-auth-attempts", type=int, default=None)
    parser.add_argument("--rate-limit-control-attempts", type=int, default=None)
    parser.add_argument("--rate-limit-terminal-connections", type=int, default=None)
    parser.add_argument(
        "--terminal-enabled",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="enable the high-risk interactive browser terminal capability",
    )
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
    args.rate_limit_window_seconds = resolved(
        "rate_limit_window_seconds", args.rate_limit_window_seconds, 60
    )
    args.rate_limit_auth_attempts = resolved(
        "rate_limit_auth_attempts", args.rate_limit_auth_attempts, 30
    )
    args.rate_limit_control_attempts = resolved(
        "rate_limit_control_attempts", args.rate_limit_control_attempts, 120
    )
    args.rate_limit_terminal_connections = resolved(
        "rate_limit_terminal_connections", args.rate_limit_terminal_connections, 30
    )
    args.terminal_enabled = bool(
        resolved("terminal_enabled", args.terminal_enabled, args.auth_mode == "local")
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
    if any(
        value <= 0
        for value in (
            args.rate_limit_window_seconds,
            args.rate_limit_auth_attempts,
            args.rate_limit_control_attempts,
            args.rate_limit_terminal_connections,
        )
    ):
        parser.error("rate-limit settings must be positive")
    return args
