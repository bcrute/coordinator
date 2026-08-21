"""Tests for repository discovery/switching in web_app.

Every server here is constructed with injected watcher/codex command seams
(never the real `watch_coordination.py`/`codex` programs), so no test
launches a real watcher, Codex, or Claude process.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "coordinate-claude-work"
INIT = SKILL / "scripts" / "init_project.py"

sys.path.insert(0, str(SKILL / "scripts"))
from web_app import (  # noqa: E402
    ApplicationContext,
    REPOSITORY_SELECT_BODY_BYTES,
    REPOSITORY_SELECT_PATH,
    RepositoryContext,
    default_codex_command,
    discover_repositories,
    is_git_repository,
    parse_args,
)
from tests.asgi_server import authorized_headers, create_server


class FakeManager:
    """Small controllable stand-in for WatcherManager/CodexSessionManager.

    Records shutdown calls and can be told to raise instead of succeeding,
    so tests can prove both old managers are shut down after one raises,
    and both fresh managers are shut down on that failure, without ever
    starting a real watcher or Codex process.
    """

    def __init__(self, *args, fail: bool = False, **kwargs) -> None:
        self.fail = fail
        self.shutdown_calls = 0

    def shutdown(self, *args, **kwargs) -> None:
        self.shutdown_calls += 1
        if self.fail:
            raise RuntimeError("fake shutdown failure")

SLEEP_SCRIPT = "import time\nwhile True:\n    time.sleep(0.05)\n"
EXIT_SCRIPT = "import sys\nsys.exit(0)\n"


class RepositorySwitchingTests(unittest.TestCase):
    """Exercise discovery, the select endpoint, and lifecycle-correct switching."""

    # -- fixtures ---------------------------------------------------------

    def init_repo(self, target: Path, name: str = "Events", git: bool = True) -> None:
        target.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [sys.executable, str(INIT), str(target), "--project-name", name],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        if git:
            (target / ".git").mkdir(exist_ok=True)

    def git_repo(self, target: Path) -> None:
        target.mkdir(parents=True, exist_ok=True)
        (target / ".git").mkdir(exist_ok=True)

    def root(self) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return Path(directory.name)

    def sleep_command(self) -> list[str]:
        return [sys.executable, "-c", SLEEP_SCRIPT]

    def exit_command(self) -> list[str]:
        return [sys.executable, "-c", EXIT_SCRIPT]

    def wait_until(
        self, predicate, timeout: float = 10.0, message: str = "condition was not met in time"
    ) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.05)
        self.assertTrue(predicate(), message)

    # -- HTTP helpers -------------------------------------------------------

    def serve(self, repo: Path, **kwargs) -> str:
        server = create_server(repo, port=0, quiet=True, **kwargs)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        host, port = server.server_address[:2]
        return f"http://{host}:{port}"

    def serve_with_server(self, repo: Path, **kwargs):
        server = create_server(repo, port=0, quiet=True, **kwargs)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        host, port = server.server_address[:2]
        return server, f"http://{host}:{port}"

    def request(
        self,
        url: str,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> tuple[int, dict[str, str], str]:
        req = urllib.request.Request(url, method=method, headers=headers or {}, data=body)
        try:
            with urllib.request.urlopen(req) as response:
                return response.status, dict(response.headers), response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            return error.code, dict(error.headers), error.read().decode("utf-8")

    def post_json(self, url: str, payload, headers=None) -> tuple[int, dict[str, object]]:
        merged = {"Content-Type": "application/json"}
        merged.update(authorized_headers(url, headers))
        body = json.dumps(payload).encode("utf-8") if not isinstance(payload, bytes) else payload
        status, _, text = self.request(url, "POST", merged, body)
        return status, json.loads(text) if text else {}

    def get_json(self, url: str) -> tuple[int, dict[str, object]]:
        status, _, text = self.request(url, "GET")
        return status, json.loads(text) if text else {}

    def select(self, base: str, host: str, path: str) -> tuple[int, dict[str, object]]:
        return self.post_json(
            f"{base}{REPOSITORY_SELECT_PATH}", {"path": path}, {"Origin": f"http://{host}"}
        )

    # -- discover_repositories ----------------------------------------------

    def test_discover_filters_uninitialized_children_and_sorts_case_insensitively(self) -> None:
        root = self.root()
        alpha = root / "Zeta"
        beta = root / "alpha"
        uninitialized = root / "not-a-repo"
        self.init_repo(alpha, "Zeta")
        self.init_repo(beta, "Alpha")
        uninitialized.mkdir()
        (root / "just-a-file.txt").write_text("x", encoding="utf-8")

        entries = discover_repositories(root, alpha.resolve())
        names = [entry["name"] for entry in entries]
        self.assertEqual(names, ["alpha", "Zeta"])
        self.assertTrue(all(Path(entry["path"]).is_absolute() for entry in entries))

    def test_discover_includes_active_repo_outside_root_and_deduplicates(self) -> None:
        root = self.root()
        active_root = self.root()
        active = active_root / "outside"
        self.init_repo(active, "Outside")

        entries = discover_repositories(root, active.resolve())
        paths = [entry["path"] for entry in entries]
        self.assertIn(str(active.resolve()), paths)
        self.assertEqual(len(paths), len(set(paths)))

    # -- CLI defaults/validation ----------------------------------------------

    def test_create_server_defaults_repositories_root_to_repo_parent(self) -> None:
        root = self.root()
        repo = root / "solo"
        self.init_repo(repo, "Solo")
        server = create_server(repo, port=0, quiet=True)
        self.addCleanup(server.server_close)
        self.assertEqual(server.context.repositories_root, root.resolve())

    def test_create_server_rejects_non_directory_repositories_root(self) -> None:
        root = self.root()
        repo = root / "solo"
        self.init_repo(repo, "Solo")
        missing = root / "does-not-exist"
        with self.assertRaises(ValueError):
            create_server(repo, port=0, quiet=True, repositories_root=missing)

    def test_create_server_rejects_uninitialized_repo(self) -> None:
        root = self.root()
        uninitialized = root / "plain"
        uninitialized.mkdir()
        with self.assertRaises(ValueError):
            create_server(uninitialized, port=0, quiet=True)

    # -- /api/state catalog -----------------------------------------------

    def test_state_includes_repository_catalog_with_active_flag(self) -> None:
        root = self.root()
        repo_a = root / "a-repo"
        repo_b = root / "b-repo"
        self.init_repo(repo_a, "A")
        self.init_repo(repo_b, "B")
        base = self.serve(repo_a, repositories_root=root)

        status, payload = self.get_json(f"{base}/api/state")
        self.assertEqual(status, 200)
        catalog = payload["repository_catalog"]
        self.assertEqual(catalog["root"], str(root.resolve()))
        self.assertEqual(catalog["active"], str(repo_a.resolve()))
        entries = {entry["path"]: entry for entry in catalog["entries"]}
        self.assertTrue(entries[str(repo_a.resolve())]["active"])
        self.assertFalse(entries[str(repo_b.resolve())]["active"])

    # -- POST /api/repository/select: validation ---------------------------

    def test_select_requires_exact_bounded_json_body(self) -> None:
        root = self.root()
        repo = root / "solo"
        self.init_repo(repo, "Solo")
        base = self.serve(repo, repositories_root=root)
        host = base.split("//", 1)[1]

        for payload in [{}, {"path": 5}, {"path": "x", "extra": 1}, {"nope": "x"}]:
            status, body = self.post_json(
                f"{base}{REPOSITORY_SELECT_PATH}", payload, {"Origin": f"http://{host}"}
            )
            self.assertEqual(status, 400, payload)
            self.assertEqual(body["outcome"], "validation")

    def test_select_get_is_405(self) -> None:
        root = self.root()
        repo = root / "solo"
        self.init_repo(repo, "Solo")
        base = self.serve(repo, repositories_root=root)

        status, headers, _ = self.request(f"{base}{REPOSITORY_SELECT_PATH}", "GET")
        self.assertEqual(status, 405)
        self.assertEqual(headers.get("Allow") or headers.get("allow"), "POST")

    def test_select_refuses_cross_origin(self) -> None:
        root = self.root()
        repo = root / "solo"
        self.init_repo(repo, "Solo")
        base = self.serve(repo, repositories_root=root)

        status, body = self.post_json(
            f"{base}{REPOSITORY_SELECT_PATH}",
            {"path": str(repo.resolve())},
            {"Origin": "http://evil.example"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["outcome"], "forbidden")

    def test_select_rejects_unknown_path(self) -> None:
        root = self.root()
        repo = root / "solo"
        other = root / "other-not-catalogued"
        self.init_repo(repo, "Solo")
        other.mkdir()
        base = self.serve(repo, repositories_root=root)
        host = base.split("//", 1)[1]

        status, body = self.select(base, host, str(other))
        self.assertEqual(status, 400)
        self.assertEqual(body["outcome"], "validation")
        self.assertTrue(
            any(entry["active"] for entry in body["repository_catalog"]["entries"])
        )

        status, active_payload = self.get_json(f"{base}/api/state")
        self.assertEqual(
            active_payload["repository_catalog"]["active"], str(repo.resolve())
        )

    def test_select_same_repo_is_unchanged(self) -> None:
        root = self.root()
        repo = root / "solo"
        self.init_repo(repo, "Solo")
        base = self.serve(repo, repositories_root=root)
        host = base.split("//", 1)[1]

        status, body = self.select(base, host, str(repo.resolve()))
        self.assertEqual(status, 200)
        self.assertEqual(body["outcome"], "unchanged")

    # -- switching lifecycle -------------------------------------------------

    def test_select_switches_state_and_rebinds_default_codex_command(self) -> None:
        root = self.root()
        repo_a = root / "a-repo"
        repo_b = root / "b-repo"
        self.init_repo(repo_a, "A")
        self.init_repo(repo_b, "B")
        server, base = self.serve_with_server(repo_a, repositories_root=root)
        host = base.split("//", 1)[1]

        status, body = self.select(base, host, str(repo_b.resolve()))
        self.assertEqual(status, 200)
        self.assertEqual(body["outcome"], "selected")
        self.assertTrue(
            any(
                entry["active"] and entry["path"] == str(repo_b.resolve())
                for entry in body["repository_catalog"]["entries"]
            )
        )

        status, state = self.get_json(f"{base}/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(state["repo"], str(repo_b.resolve()))
        self.assertEqual(state["repository_catalog"]["active"], str(repo_b.resolve()))

        expected = default_codex_command(repo_b.resolve())
        self.assertEqual(
            list(server.context.snapshot().codex_session.command), list(expected)
        )
        self.assertEqual(server.context.snapshot().watcher.repo, repo_b.resolve())

    def test_select_stops_running_fake_codex_and_app_owned_watcher(self) -> None:
        root = self.root()
        repo_a = root / "a-repo"
        repo_b = root / "b-repo"
        self.init_repo(repo_a, "A")
        self.init_repo(repo_b, "B")
        server, base = self.serve_with_server(
            repo_a,
            repositories_root=root,
            codex_command=self.sleep_command(),
            watcher_command=self.sleep_command(),
        )
        host = base.split("//", 1)[1]

        old_ctx = server.context.snapshot()
        old_ctx.codex_session.start()
        self.assertTrue(old_ctx.codex_session.snapshot()["running"])
        outcome, _ = old_ctx.watcher.start()
        self.assertEqual(outcome, "started")
        self.wait_until(lambda: old_ctx.codex_session.snapshot()["running"])
        self.wait_until(lambda: old_ctx.watcher.snapshot()["running"])

        status, body = self.select(base, host, str(repo_b.resolve()))
        self.assertEqual(status, 200, body)
        self.assertEqual(body["outcome"], "selected")

        self.wait_until(lambda: not old_ctx.codex_session.snapshot()["running"])
        self.wait_until(lambda: not old_ctx.watcher.snapshot()["running"])

        new_ctx = server.context.snapshot()
        self.assertIsNot(new_ctx.codex_session, old_ctx.codex_session)
        self.assertIsNot(new_ctx.watcher, old_ctx.watcher)
        self.assertFalse(new_ctx.codex_session.snapshot()["running"])

    def test_select_does_not_disturb_external_watcher_lock(self) -> None:
        root = self.root()
        repo_a = root / "a-repo"
        repo_b = root / "b-repo"
        self.init_repo(repo_a, "A")
        self.init_repo(repo_b, "B")
        lock = repo_a / ".coordination" / "runtime" / "watcher-both.lock"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("held externally\n", encoding="utf-8")

        base = self.serve(repo_a, repositories_root=root)
        host = base.split("//", 1)[1]

        status, body = self.select(base, host, str(repo_b.resolve()))
        self.assertEqual(status, 200, body)
        self.assertEqual(body["outcome"], "selected")
        self.assertTrue(lock.is_file())

    def test_server_close_cleans_up_context_active_after_switch(self) -> None:
        root = self.root()
        repo_a = root / "a-repo"
        repo_b = root / "b-repo"
        self.init_repo(repo_a, "A")
        self.init_repo(repo_b, "B")
        server, base = self.serve_with_server(
            repo_a,
            repositories_root=root,
            codex_command=self.sleep_command(),
            watcher_command=self.sleep_command(),
        )
        host = base.split("//", 1)[1]

        status, body = self.select(base, host, str(repo_b.resolve()))
        self.assertEqual(status, 200, body)

        new_ctx = server.context.snapshot()
        new_ctx.codex_session.start()
        self.assertTrue(new_ctx.codex_session.snapshot()["running"])
        outcome, _ = new_ctx.watcher.start()
        self.assertEqual(outcome, "started")
        self.wait_until(lambda: new_ctx.codex_session.snapshot()["running"])
        self.wait_until(lambda: new_ctx.watcher.snapshot()["running"])

        server.server_close()

        self.assertFalse(new_ctx.codex_session.snapshot()["running"])
        self.assertFalse(new_ctx.watcher.snapshot()["running"])

    # -- concurrency: request lease vs. select -------------------------------

    def test_switch_waits_for_an_in_flight_request_lease_before_publishing(self) -> None:
        root = self.root()
        repo_a = root / "a-repo"
        repo_b = root / "b-repo"
        self.init_repo(repo_a, "A")
        self.init_repo(repo_b, "B")
        context = ApplicationContext(
            repo_a.resolve(),
            root.resolve(),
            watcher_command_for_repo=lambda r: None,
            codex_command_for_repo=default_codex_command,
        )

        release = threading.Event()
        entered = threading.Event()
        observed_repo: list[Path] = []

        def hold_lease() -> None:
            with context.lease() as ctx:
                observed_repo.append(ctx.repo)
                entered.set()
                release.wait(timeout=5.0)
                # The repo observed at the start of the lease must still be
                # coherent at the end of the lease: nothing published a new
                # context underneath this in-flight operation.
                observed_repo.append(ctx.repo)

        holder = threading.Thread(target=hold_lease)
        holder.start()
        self.assertTrue(entered.wait(timeout=5.0))

        switch_done = threading.Event()
        switch_result: list[tuple[str, str]] = []

        def do_switch() -> None:
            outcome, message, _ = context.select(str(repo_b.resolve()))
            switch_result.append((outcome, message))
            switch_done.set()

        switcher = threading.Thread(target=do_switch)
        switcher.start()

        # The switch must not be able to publish while the lease is held.
        self.assertFalse(switch_done.wait(timeout=0.3))

        release.set()
        holder.join(timeout=5.0)
        switcher.join(timeout=5.0)

        self.assertTrue(switch_done.is_set())
        self.assertEqual(switch_result[0][0], "selected")
        self.assertEqual(observed_repo, [repo_a.resolve(), repo_a.resolve()])
        self.assertEqual(context.snapshot().repo, repo_b.resolve())
        self.addCleanup(context.shutdown)

    # -- POST /api/repository/select: malformed/oversized body --------------

    def test_select_rejects_malformed_json_body(self) -> None:
        root = self.root()
        repo = root / "solo"
        self.init_repo(repo, "Solo")
        base = self.serve(repo, repositories_root=root)
        host = base.split("//", 1)[1]

        status, body = self.post_json(
            f"{base}{REPOSITORY_SELECT_PATH}", b"not-json", {"Origin": f"http://{host}"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["outcome"], "bad_request")

    def test_select_rejects_body_larger_than_limit(self) -> None:
        root = self.root()
        repo = root / "solo"
        self.init_repo(repo, "Solo")
        base = self.serve(repo, repositories_root=root)
        host = base.split("//", 1)[1]

        oversized = json.dumps(
            {"path": "x" * (REPOSITORY_SELECT_BODY_BYTES + 1)}
        ).encode("utf-8")
        self.assertGreater(len(oversized), REPOSITORY_SELECT_BODY_BYTES)
        status, body = self.post_json(
            f"{base}{REPOSITORY_SELECT_PATH}", oversized, {"Origin": f"http://{host}"}
        )
        self.assertEqual(status, 413)
        self.assertEqual(body["outcome"], "too_large")

    # -- CLI parsing ----------------------------------------------------------

    def test_parse_args_defaults_repositories_root_to_none(self) -> None:
        args = parse_args([])
        self.assertIsNone(args.repositories_root)

    def test_parse_args_accepts_explicit_repositories_root_flag(self) -> None:
        args = parse_args(["--repositories-root", "/tmp/somewhere"])
        self.assertEqual(args.repositories_root, Path("/tmp/somewhere"))

    # -- select(): command-factory failure -------------------------------

    def test_select_returns_error_when_command_factory_raises(self) -> None:
        root = self.root()
        repo_a = root / "a-repo"
        repo_b = root / "b-repo"
        self.init_repo(repo_a, "A")
        self.init_repo(repo_b, "B")

        def watcher_command_for_repo(target: Path) -> list[str] | None:
            if target == repo_b.resolve():
                raise RuntimeError("cannot compute watcher command")
            return None

        context = ApplicationContext(
            repo_a.resolve(),
            root.resolve(),
            watcher_command_for_repo=watcher_command_for_repo,
            codex_command_for_repo=default_codex_command,
        )
        self.addCleanup(context.shutdown)

        outcome, message, _ = context.select(str(repo_b.resolve()))
        self.assertEqual(outcome, "error")
        self.assertIn("cannot construct managers", message)
        self.assertEqual(context.snapshot().repo, repo_a.resolve())

    # -- select(): old/fresh manager shutdown ordering on failure ------------

    def test_select_attempts_all_shutdowns_and_reports_error_on_failure(self) -> None:
        root = self.root()
        repo_a = root / "a-repo"
        repo_b = root / "b-repo"
        self.init_repo(repo_a, "A")
        self.init_repo(repo_b, "B")

        old_codex = FakeManager(fail=True)
        old_watcher = FakeManager(fail=False)
        fresh_watcher = FakeManager()
        fresh_codex = FakeManager()

        with mock.patch(
            "coordinator.web_app.WatcherManager", side_effect=[old_watcher, fresh_watcher]
        ), mock.patch(
            "coordinator.web_app.CodexSessionManager", side_effect=[old_codex, fresh_codex]
        ):
            context = ApplicationContext(
                repo_a.resolve(),
                root.resolve(),
                watcher_command_for_repo=lambda r: None,
                codex_command_for_repo=default_codex_command,
            )

            outcome, message, _ = context.select(str(repo_b.resolve()))

        self.assertEqual(outcome, "error")
        self.assertIn("cannot cleanly stop the previous repository's managers", message)
        self.assertEqual(old_codex.shutdown_calls, 1)
        self.assertEqual(old_watcher.shutdown_calls, 1)
        self.assertEqual(fresh_watcher.shutdown_calls, 1)
        self.assertEqual(fresh_codex.shutdown_calls, 1)
        self.assertEqual(context.snapshot().repo, repo_a.resolve())
        self.assertIs(context.snapshot().codex_session, old_codex)
        self.assertIs(context.snapshot().watcher, old_watcher)


    # -- is_git_repository ----------------------------------------------------

    def test_is_git_repository_accepts_directory_marker(self) -> None:
        root = self.root()
        repo = root / "dir-marker"
        self.git_repo(repo)
        self.assertTrue(is_git_repository(repo))

    def test_is_git_repository_accepts_file_marker(self) -> None:
        root = self.root()
        repo = root / "file-marker"
        repo.mkdir()
        (repo / ".git").write_text("gitdir: ../elsewhere\n", encoding="utf-8")
        self.assertTrue(is_git_repository(repo))

    def test_is_git_repository_rejects_absent_marker(self) -> None:
        root = self.root()
        plain = root / "plain"
        plain.mkdir()
        self.assertFalse(is_git_repository(plain))

    # -- discovery: uninitialized Git children and direct-child scoping -------

    def test_discover_includes_uninitialized_git_children_as_uninitialized(self) -> None:
        root = self.root()
        active = root / "active"
        self.init_repo(active, "Active")
        uninitialized_git = root / "fresh-clone"
        self.git_repo(uninitialized_git)

        entries = discover_repositories(root, active.resolve())
        by_path = {entry["path"]: entry for entry in entries}
        self.assertIn(str(uninitialized_git.resolve()), by_path)
        self.assertFalse(by_path[str(uninitialized_git.resolve())]["initialized"])

    def test_discover_reports_initialized_git_children_as_initialized(self) -> None:
        root = self.root()
        active = root / "active"
        self.init_repo(active, "Active")
        other = root / "other"
        self.init_repo(other, "Other")

        entries = discover_repositories(root, active.resolve())
        by_path = {entry["path"]: entry for entry in entries}
        self.assertTrue(by_path[str(other.resolve())]["initialized"])

    def test_discover_excludes_initialized_non_git_children_unless_active(self) -> None:
        root = self.root()
        active = root / "active"
        self.init_repo(active, "Active")
        non_git_initialized = root / "non-git-initialized"
        self.init_repo(non_git_initialized, "NonGit", git=False)

        entries = discover_repositories(root, active.resolve())
        paths = {entry["path"] for entry in entries}
        self.assertNotIn(str(non_git_initialized.resolve()), paths)

    def test_discover_excludes_nested_non_direct_repositories(self) -> None:
        root = self.root()
        active = root / "active"
        self.init_repo(active, "Active")
        nested = root / "container" / "nested-repo"
        self.git_repo(nested)

        entries = discover_repositories(root, active.resolve())
        paths = {entry["path"] for entry in entries}
        self.assertNotIn(str(nested.resolve()), paths)

    def test_discover_stable_sort_with_mixed_initialization(self) -> None:
        root = self.root()
        active = root / "Active"
        self.init_repo(active, "Active")
        uninitialized_git = root / "beta"
        self.git_repo(uninitialized_git)
        other = root / "alpha"
        self.init_repo(other, "Alpha")

        entries = discover_repositories(root, active.resolve())
        names = [entry["name"] for entry in entries]
        self.assertEqual(names, sorted(names, key=str.lower))

    # -- create_server: uninitialized Git initial repo -------------------------

    def test_create_server_accepts_uninitialized_git_repo(self) -> None:
        root = self.root()
        repo = root / "fresh"
        self.git_repo(repo)
        server = create_server(repo, port=0, quiet=True)
        self.addCleanup(server.server_close)
        self.assertEqual(server.context.snapshot().repo, repo.resolve())

    def test_uninitialized_git_repo_state_reports_coherent_details(self) -> None:
        root = self.root()
        repo = root / "fresh"
        self.git_repo(repo)
        base = self.serve(repo, repositories_root=root)

        status, state = self.get_json(f"{base}/api/state")
        self.assertEqual(status, 200)
        self.assertFalse(state["coordination_present"])
        watcher = state.get("managed_watcher") or {}
        self.assertFalse(watcher.get("can_start", True))
        self.assertTrue(state["codex_session"]["can_start"])

    def test_create_server_accepts_initialized_non_git_repo(self) -> None:
        root = self.root()
        repo = root / "plain-initialized"
        self.init_repo(repo, "Plain", git=False)
        server, base = self.serve_with_server(repo)
        self.assertEqual(server.context.snapshot().repo, repo.resolve())

        status, state = self.get_json(base + "/api/state")
        self.assertEqual(status, 200)
        entries = {
            entry["path"]: entry for entry in state["repository_catalog"]["entries"]
        }
        self.assertIn(str(repo.resolve()), entries)
        self.assertTrue(entries[str(repo.resolve())]["initialized"])

    # -- select(): uninitialized Git target ------------------------------------

    def test_select_uninitialized_git_target_succeeds_and_rebinds_codex(self) -> None:
        root = self.root()
        repo_a = root / "a-repo"
        repo_b = root / "b-repo-fresh"
        self.init_repo(repo_a, "A")
        self.git_repo(repo_b)
        server, base = self.serve_with_server(repo_a, repositories_root=root)
        host = base.split("//", 1)[1]

        status, body = self.select(base, host, str(repo_b.resolve()))
        self.assertEqual(status, 200, body)
        self.assertEqual(body["outcome"], "selected")
        entries = {entry["path"]: entry for entry in body["repository_catalog"]["entries"]}
        self.assertFalse(entries[str(repo_b.resolve())]["initialized"])

        status, state = self.get_json(f"{base}/api/state")
        self.assertEqual(status, 200)
        self.assertFalse(state["coordination_present"])
        expected = default_codex_command(repo_b.resolve())
        self.assertEqual(
            list(server.context.snapshot().codex_session.command), list(expected)
        )

    # -- watcher: rejection before setup and dynamic readiness -----------------

    def test_watcher_start_returns_400_validation_without_spawning_before_setup(
        self,
    ) -> None:
        root = self.root()
        repo = root / "fresh"
        self.git_repo(repo)
        server, base = self.serve_with_server(repo, repositories_root=root)

        status, body = self.post_json(f"{base}/api/watcher/start", {})
        self.assertEqual(status, 400)
        self.assertEqual(body["outcome"], "validation")
        watcher = server.context.snapshot().watcher
        self.assertIsNone(watcher._process)  # noqa: SLF001 - proves no process spawned
        self.assertFalse((repo / ".coordination" / "runtime").exists())

    def test_watcher_becomes_startable_after_marker_appears_without_reconstruction(
        self,
    ) -> None:
        root = self.root()
        repo = root / "fresh"
        self.git_repo(repo)
        server, base = self.serve_with_server(repo, repositories_root=root)

        watcher_before = server.context.snapshot().watcher
        status, state = self.get_json(f"{base}/api/state")
        self.assertFalse(state["managed_watcher"]["can_start"])

        coordination = repo / ".coordination"
        coordination.mkdir(parents=True, exist_ok=True)
        (coordination / "README.md").write_text("# Coordination\n", encoding="utf-8")

        status, state = self.get_json(f"{base}/api/state")
        self.assertEqual(status, 200)
        self.assertTrue(state["managed_watcher"]["can_start"])
        watcher_after = server.context.snapshot().watcher
        self.assertIs(watcher_before, watcher_after)


if __name__ == "__main__":
    unittest.main()
