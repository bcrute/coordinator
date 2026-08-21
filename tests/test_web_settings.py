"""Tests for the portable `--config` TOML settings support in `web_app.py`.

These tests only exercise `parse_args`/`load_config`; they never start a real
server, watcher, or Codex process.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "coordinate-claude-work"

sys.path.insert(0, str(SKILL / "scripts"))
from web_app import RELAY_LOG_LINES, load_config, main, parse_args  # noqa: E402


def write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


class DefaultsTests(unittest.TestCase):
    def test_defaults_without_config_or_cli(self) -> None:
        args = parse_args([])
        self.assertEqual(args.repo, Path.cwd())
        self.assertIsNone(args.repositories_root)
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8765)
        self.assertEqual(args.relay_log_lines, RELAY_LOG_LINES)
        self.assertEqual(args.usage_refresh_seconds, 3600)
        self.assertFalse(args.quiet)

    def test_local_mode_refuses_a_routable_bind(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".git").mkdir()
            self.assertEqual(main(["--repo", str(repo), "--host", "0.0.0.0"]), 2)


class ConfigValuesTests(unittest.TestCase):
    def test_all_supported_settings_are_applied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "repo_dir").mkdir()
            (base / "root_dir").mkdir()
            config = write(
                base / "workflow.toml",
                """
                repo = "repo_dir"
                repositories_root = "root_dir"
                host = "0.0.0.0"
                port = 9090
                relay_log_lines = 50
                usage_refresh_seconds = 1800
                quiet = true
                """,
            )
            args = parse_args(["--config", str(config)])
            self.assertEqual(args.repo, base / "repo_dir")
            self.assertEqual(args.repositories_root, base / "root_dir")
            self.assertEqual(args.host, "0.0.0.0")
            self.assertEqual(args.port, 9090)
            self.assertEqual(args.relay_log_lines, 50)
            self.assertEqual(args.usage_refresh_seconds, 1800)
            self.assertTrue(args.quiet)

    def test_absolute_config_paths_are_kept_as_is(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with tempfile.TemporaryDirectory() as other_tmp:
                other = Path(other_tmp)
                config = write(
                    base / "workflow.toml",
                    f'repo = "{other.as_posix()}"\n',
                )
                args = parse_args(["--config", str(config)])
                self.assertEqual(args.repo, other)

    def test_partial_config_falls_back_to_defaults_for_missing_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = write(base / "workflow.toml", 'port = 4242\n')
            args = parse_args(["--config", str(config)])
            self.assertEqual(args.port, 4242)
            self.assertEqual(args.host, "127.0.0.1")
            self.assertIsNone(args.repositories_root)
            self.assertEqual(args.repo, Path.cwd())
            self.assertTrue(args.terminal_enabled)

    def test_oidc_settings_arrays_lifetimes_and_state_path_are_applied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = write(
                base / "workflow.toml",
                """
                auth_mode = "oidc"
                oidc_issuer = "https://auth.example/application/o/coordinator"
                oidc_client_id = "coordinator"
                oidc_client_secret_env = "COORDINATOR_OIDC_CLIENT_SECRET"
                external_url = "https://coordinator.example"
                allowed_subjects = ["owner-sub"]
                allowed_groups = ["coordinator-users"]
                groups_claim = "groups"
                state_dir = "state"
                session_idle_seconds = 900
                session_absolute_seconds = 7200
                rate_limit_window_seconds = 90
                rate_limit_auth_attempts = 12
                rate_limit_control_attempts = 80
                rate_limit_terminal_connections = 8
                trusted_hosts = ["coordinator.example"]
                forwarded_allow_ips = "127.0.0.1"
                insecure_oidc_http = false
                terminal_enabled = true
                """,
            )
            args = parse_args(["--config", str(config)])
            self.assertEqual(args.auth_mode, "oidc")
            self.assertEqual(args.allowed_subject, ["owner-sub"])
            self.assertEqual(args.allowed_group, ["coordinator-users"])
            self.assertEqual(args.state_dir, base / "state")
            self.assertEqual(args.session_idle_seconds, 900)
            self.assertEqual(args.session_absolute_seconds, 7200)
            self.assertEqual(args.rate_limit_window_seconds, 90)
            self.assertEqual(args.rate_limit_auth_attempts, 12)
            self.assertEqual(args.rate_limit_control_attempts, 80)
            self.assertEqual(args.rate_limit_terminal_connections, 8)
            self.assertEqual(args.trusted_host, ["coordinator.example"])
            self.assertTrue(args.terminal_enabled)


class ConfigRelativeResolutionTests(unittest.TestCase):
    def test_relative_paths_resolve_against_config_directory_not_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as config_home, tempfile.TemporaryDirectory() as elsewhere:
            base = Path(config_home)
            (base / "myrepo").mkdir()
            config = write(base / "workflow.toml", 'repo = "myrepo"\n')
            settings = load_config_from_elsewhere(config, elsewhere)
            self.assertEqual(settings, base / "myrepo")

    def test_relative_cli_override_resolves_against_launch_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as config_home, tempfile.TemporaryDirectory() as launch_cwd:
            base = Path(config_home)
            (base / "myrepo").mkdir()
            cwd = Path(launch_cwd)
            (cwd / "cli_repo").mkdir()
            config = write(base / "workflow.toml", 'repo = "myrepo"\n')

            import os

            previous = os.getcwd()
            os.chdir(cwd)
            try:
                args = parse_args(
                    ["--config", str(config), "--repo", "cli_repo"]
                )
                self.assertEqual(args.repo, Path("cli_repo"))
                self.assertEqual(args.repo.resolve(), (cwd / "cli_repo").resolve())
            finally:
                os.chdir(previous)


def load_config_from_elsewhere(config: Path, cwd: str) -> Path:
    import os

    previous = os.getcwd()
    os.chdir(cwd)
    try:
        args = parse_args(["--config", str(config)])
        return args.repo
    finally:
        os.chdir(previous)


class CliPrecedenceTests(unittest.TestCase):
    def config_with_everything(self, base: Path) -> Path:
        (base / "repo_dir").mkdir()
        (base / "root_dir").mkdir()
        return write(
            base / "workflow.toml",
            """
            repo = "repo_dir"
            repositories_root = "root_dir"
            host = "0.0.0.0"
            port = 9090
            relay_log_lines = 50
            quiet = true
            """,
        )

    def test_cli_repo_overrides_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = self.config_with_everything(base)
            args = parse_args(["--config", str(config), "--repo", "/tmp"])
            self.assertEqual(args.repo, Path("/tmp"))

    def test_cli_repositories_root_overrides_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = self.config_with_everything(base)
            args = parse_args(
                ["--config", str(config), "--repositories-root", "/tmp"]
            )
            self.assertEqual(args.repositories_root, Path("/tmp"))

    def test_cli_host_overrides_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = self.config_with_everything(base)
            args = parse_args(["--config", str(config), "--host", "192.168.1.1"])
            self.assertEqual(args.host, "192.168.1.1")

    def test_cli_port_overrides_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = self.config_with_everything(base)
            args = parse_args(["--config", str(config), "--port", "1234"])
            self.assertEqual(args.port, 1234)

    def test_cli_relay_log_lines_overrides_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = self.config_with_everything(base)
            args = parse_args(
                ["--config", str(config), "--relay-log-lines", "5"]
            )
            self.assertEqual(args.relay_log_lines, 5)

    def test_cli_quiet_overrides_config_false_to_true(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "repo_dir").mkdir()
            config = write(
                base / "workflow.toml",
                'repo = "repo_dir"\nquiet = false\n',
            )
            args = parse_args(["--config", str(config), "--quiet"])
            self.assertTrue(args.quiet)

    def test_cli_no_quiet_overrides_config_true_to_false(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = self.config_with_everything(base)
            args = parse_args(["--config", str(config), "--no-quiet"])
            self.assertFalse(args.quiet)

    def test_help_includes_no_quiet_flag(self) -> None:
        import contextlib
        import io

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with self.assertRaises(SystemExit):
                parse_args(["--help"])
        self.assertIn("--no-quiet", buf.getvalue())

    def test_omitted_cli_leaves_config_quiet_in_effect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            config = self.config_with_everything(base)
            args = parse_args(["--config", str(config)])
            self.assertTrue(args.quiet)


class MalformedConfigTests(unittest.TestCase):
    def assert_fails(self, argv: list[str]) -> None:
        with self.assertRaises(SystemExit) as ctx:
            parse_args(argv)
        self.assertEqual(ctx.exception.code, 2)

    def test_malformed_toml_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = write(Path(tmp) / "workflow.toml", "this is not [ valid toml")
            self.assert_fails(["--config", str(config)])

    def test_missing_file_fails(self) -> None:
        self.assert_fails(["--config", "/nonexistent/workflow.toml"])

    def test_unknown_key_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = write(Path(tmp) / "workflow.toml", "bogus = 1\n")
            self.assert_fails(["--config", str(config)])

    def test_unknown_section_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = write(Path(tmp) / "workflow.toml", "[server]\nhost = \"x\"\n")
            self.assert_fails(["--config", str(config)])

    def test_wrong_type_port_string_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = write(Path(tmp) / "workflow.toml", 'port = "8765"\n')
            self.assert_fails(["--config", str(config)])

    def test_bool_as_int_port_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = write(Path(tmp) / "workflow.toml", "port = true\n")
            self.assert_fails(["--config", str(config)])

    def test_bool_as_int_relay_log_lines_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = write(Path(tmp) / "workflow.toml", "relay_log_lines = false\n")
            self.assert_fails(["--config", str(config)])

    def test_non_positive_usage_refresh_seconds_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = write(Path(tmp) / "workflow.toml", "usage_refresh_seconds = 0\n")
            self.assert_fails(["--config", str(config)])

    def test_quiet_wrong_type_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = write(Path(tmp) / "workflow.toml", 'quiet = "yes"\n')
            self.assert_fails(["--config", str(config)])

    def test_host_wrong_type_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = write(Path(tmp) / "workflow.toml", "host = 1\n")
            self.assert_fails(["--config", str(config)])

    def test_repo_wrong_type_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = write(Path(tmp) / "workflow.toml", "repo = 1\n")
            self.assert_fails(["--config", str(config)])

    def test_empty_host_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = write(Path(tmp) / "workflow.toml", 'host = "   "\n')
            self.assert_fails(["--config", str(config)])

    def test_empty_repo_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = write(Path(tmp) / "workflow.toml", 'repo = ""\n')
            self.assert_fails(["--config", str(config)])

    def test_empty_repositories_root_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = write(Path(tmp) / "workflow.toml", 'repositories_root = ""\n')
            self.assert_fails(["--config", str(config)])

    def test_port_out_of_range_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = write(Path(tmp) / "workflow.toml", "port = 70000\n")
            self.assert_fails(["--config", str(config)])

    def test_negative_port_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = write(Path(tmp) / "workflow.toml", "port = -1\n")
            self.assert_fails(["--config", str(config)])

    def test_negative_relay_log_lines_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = write(Path(tmp) / "workflow.toml", "relay_log_lines = -5\n")
            self.assert_fails(["--config", str(config)])

    def test_non_table_root_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # A bare array-of-tables document is valid TOML but not a flat
            # settings table; its keys are therefore unknown/unsupported.
            config = write(Path(tmp) / "workflow.toml", "[[repo]]\nname = \"x\"\n")
            self.assert_fails(["--config", str(config)])


class LoadConfigDirectTests(unittest.TestCase):
    """Directly exercise `load_config` for the return shape used by `parse_args`."""

    def test_returns_only_specified_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            config = write(Path(tmp) / "workflow.toml", "port = 1\n")
            settings = load_config(config)
            self.assertEqual(settings, {"port": 1})

    def test_missing_file_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            load_config(Path("/definitely/not/a/real/path/workflow.toml"))


if __name__ == "__main__":
    unittest.main()
