"""HTTP contract tests for the /api/codex/* Codex-session endpoints in web_app.

Every server here is constructed with an injected `codex_command` seam (never
the real `codex` CLI), so no test launches real Codex, Claude, or a watcher.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "coordinate-claude-work"
INIT = SKILL / "scripts" / "init_project.py"

sys.path.insert(0, str(SKILL / "scripts"))
from web_app import create_server, default_codex_command  # noqa: E402


ECHO_SCRIPT = (
    "import sys, time\n"
    "sys.stdout.write('ready\\n')\n"
    "sys.stdout.flush()\n"
    "while True:\n"
    "    line = sys.stdin.readline()\n"
    "    if not line:\n"
    "        break\n"
    "    sys.stdout.write('echo:' + line)\n"
    "    sys.stdout.flush()\n"
)

SLEEP_SCRIPT = "import time\nwhile True:\n    time.sleep(0.05)\n"

EXIT_SCRIPT = "import sys\nsys.exit(0)\n"


class CodexSessionHTTPTests(unittest.TestCase):
    """Exercise CodexSessionManager wiring through the web app's HTTP routes."""

    def project(self) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        target = Path(directory.name)
        result = subprocess.run(
            [sys.executable, str(INIT), str(target), "--project-name", "Events"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return target

    def echo_command(self) -> list[str]:
        return [sys.executable, "-c", ECHO_SCRIPT]

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

    # -- server / HTTP helpers ------------------------------------------

    def serve(self, target: Path, **kwargs) -> str:
        server = create_server(target, port=0, quiet=True, **kwargs)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        host, port = server.server_address[:2]
        return f"http://{host}:{port}"

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

    def post(self, url: str, headers=None, body=None) -> tuple[int, dict[str, object]]:
        status, _, text = self.request(url, "POST", headers, body)
        return status, json.loads(text) if text else {}

    def post_json(self, url: str, payload, headers=None) -> tuple[int, dict[str, object]]:
        merged = {"Content-Type": "application/json"}
        merged.update(headers or {})
        body = json.dumps(payload).encode("utf-8") if not isinstance(payload, bytes) else payload
        return self.post(url, merged, body)

    def get_json(self, url: str) -> tuple[int, dict[str, object]]:
        status, _, text = self.request(url, "GET")
        return status, json.loads(text) if text else {}

    # -- default command wiring -------------------------------------------

    def test_default_command_uses_fixed_repo_root_without_starting(self) -> None:
        target = self.project()
        command = default_codex_command(target)
        self.assertIn("-C", command)
        self.assertEqual(command[command.index("-C") + 1], str(target))
        self.assertTrue(command[0].endswith("codex") or command[0] == "codex")

        base = self.serve(target)
        status, payload = self.get_json(f"{base}/api/codex/output")
        self.assertEqual(status, 200)
        self.assertEqual(payload["codex_session"]["state"], "not_started")
        self.assertFalse(payload["codex_session"]["running"])
        self.assertEqual(payload["codex_session"]["command"], command)
        self.assertEqual(payload["codex_session"]["repo_path"], str(target.resolve()))

    def test_injected_command_is_immutable_and_reported(self) -> None:
        target = self.project()
        command = self.echo_command()
        base = self.serve(target, codex_command=command)
        status, payload = self.get_json(f"{base}/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(payload["codex_session"]["command"], command)

    # -- state snapshot -----------------------------------------------------

    def test_state_endpoint_includes_codex_session(self) -> None:
        target = self.project()
        base = self.serve(target, codex_command=self.sleep_command())
        status, payload = self.get_json(f"{base}/api/state")
        self.assertEqual(status, 200)
        self.assertIn("codex_session", payload)
        self.assertEqual(payload["codex_session"]["state"], "not_started")

    # -- start / duplicate start -------------------------------------------

    def test_start_then_duplicate_start_conflicts(self) -> None:
        target = self.project()
        base = self.serve(target, codex_command=self.sleep_command())

        status, payload = self.post(f"{base}/api/codex/start")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["outcome"], "started")
        self.assertTrue(payload["codex_session"]["running"])

        status2, payload2 = self.post(f"{base}/api/codex/start")
        self.assertEqual(status2, 409, payload2)
        self.assertEqual(payload2["outcome"], "conflict")

    # -- output cursor / reset -----------------------------------------------

    def test_output_cursor_advances_and_reset_reports_loss(self) -> None:
        target = self.project()
        base = self.serve(target, codex_command=self.echo_command())
        status, _ = self.post(f"{base}/api/codex/start")
        self.assertEqual(status, 200)

        def has_ready() -> bool:
            _, payload = self.get_json(f"{base}/api/codex/output")
            return "ready" in payload["output"]["text"]

        self.wait_until(has_ready, message="the fake codex process never printed 'ready'")

        status, payload = self.get_json(f"{base}/api/codex/output")
        self.assertEqual(status, 200)
        self.assertTrue(payload["output"]["reset"])
        cursor = payload["output"]["next_cursor"]

        status2, payload2 = self.get_json(f"{base}/api/codex/output?cursor={cursor}")
        self.assertEqual(status2, 200)
        self.assertEqual(payload2["output"]["text"], "")
        self.assertFalse(payload2["output"]["reset"])

        status3, payload3 = self.get_json(f"{base}/api/codex/output?cursor=999999999")
        self.assertEqual(status3, 200)
        self.assertTrue(payload3["output"]["reset"])

    # -- input echo -----------------------------------------------------------

    def test_input_is_written_and_echoed_back(self) -> None:
        target = self.project()
        base = self.serve(target, codex_command=self.echo_command())
        self.post(f"{base}/api/codex/start")

        self.wait_until(
            lambda: "ready" in self.get_json(f"{base}/api/codex/output")[1]["output"]["text"]
        )
        status, payload = self.post_json(f"{base}/api/codex/input", {"data": "hello\n"})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["outcome"], "accepted")

        self.wait_until(
            lambda: "echo:hello" in self.get_json(f"{base}/api/codex/output")[1]["output"]["text"]
        )

    # -- resize ---------------------------------------------------------------

    def test_resize_accepted_and_reflected_in_snapshot(self) -> None:
        target = self.project()
        base = self.serve(target, codex_command=self.sleep_command())
        self.post(f"{base}/api/codex/start")
        status, payload = self.post_json(f"{base}/api/codex/resize", {"rows": 40, "cols": 100})
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["codex_session"]["rows"], 40)
        self.assertEqual(payload["codex_session"]["cols"], 100)

    # -- stop / stop-when-idle -------------------------------------------------

    def test_stop_and_stop_when_idle(self) -> None:
        target = self.project()
        base = self.serve(target, codex_command=self.sleep_command())
        self.post(f"{base}/api/codex/start")
        self.wait_until(
            lambda: self.get_json(f"{base}/api/state")[1]["codex_session"]["running"]
        )
        status, payload = self.post(f"{base}/api/codex/stop")
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["outcome"], "stopped")

        status2, payload2 = self.post(f"{base}/api/codex/stop")
        self.assertEqual(status2, 409, payload2)
        self.assertEqual(payload2["outcome"], "conflict")

    # -- GET 405 on POST-only routes -------------------------------------------

    def test_get_is_405_on_every_post_only_session_route(self) -> None:
        target = self.project()
        base = self.serve(target, codex_command=self.sleep_command())
        for path in (
            "/api/codex/start",
            "/api/codex/stop",
            "/api/codex/input",
            "/api/codex/resize",
        ):
            status, headers, _ = self.request(f"{base}{path}", "GET")
            self.assertEqual(status, 405, path)
            self.assertEqual(headers.get("Allow"), "POST", path)

    # -- 404 inventory ----------------------------------------------------------

    def test_unknown_codex_routes_are_404(self) -> None:
        target = self.project()
        base = self.serve(target, codex_command=self.sleep_command())
        for path in ("/api/codex/unknown", "/api/codex", "/api/codex/"):
            status, _, _ = self.request(f"{base}{path}", "GET")
            self.assertEqual(status, 404, path)
        status, _, _ = self.request(f"{base}/api/codex/unknown", "POST")
        self.assertEqual(status, 404)

    # -- malformed / oversized / wrong-shape input bodies ----------------------

    def test_input_rejects_malformed_and_wrong_shape_bodies(self) -> None:
        target = self.project()
        base = self.serve(target, codex_command=self.sleep_command())
        self.post(f"{base}/api/codex/start")

        status, payload = self.post_json(f"{base}/api/codex/input", b"{not json")
        self.assertEqual(status, 400, payload)

        status, payload = self.post(
            f"{base}/api/codex/input",
            headers={"Content-Type": "application/json"},
            body=b"",
        )
        self.assertEqual(status, 400, payload)

        status, payload = self.post_json(f"{base}/api/codex/input", {"data": 5})
        self.assertEqual(status, 400, payload)

        status, payload = self.post_json(f"{base}/api/codex/input", {"data": "hi", "extra": 1})
        self.assertEqual(status, 400, payload)

        status, payload = self.post_json(f"{base}/api/codex/input", {})
        self.assertEqual(status, 400, payload)

    def test_input_rejects_oversized_body(self) -> None:
        target = self.project()
        base = self.serve(target, codex_command=self.sleep_command())
        self.post(f"{base}/api/codex/start")
        huge = json.dumps({"data": "x" * (200 * 1024)}).encode("utf-8")
        status, payload = self.post_json(f"{base}/api/codex/input", huge)
        self.assertEqual(status, 413, payload)

    def test_resize_rejects_bool_nonpositive_and_huge_values(self) -> None:
        target = self.project()
        base = self.serve(target, codex_command=self.sleep_command())
        self.post(f"{base}/api/codex/start")

        for payload_body in (
            {"rows": True, "cols": 10},
            {"rows": 10, "cols": True},
            {"rows": 0, "cols": 10},
            {"rows": 10, "cols": -1},
            {"rows": 999999, "cols": 10},
            {"rows": 10},
            {"rows": 10, "cols": 10, "extra": 1},
        ):
            status, payload = self.post_json(f"{base}/api/codex/resize", payload_body)
            self.assertEqual(status, 400, (payload_body, payload))

    def test_output_rejects_invalid_repeated_unexpected_and_negative_queries(self) -> None:
        target = self.project()
        base = self.serve(target, codex_command=self.sleep_command())
        for query in (
            "cursor=abc",
            "cursor=-1",
            "cursor=1&cursor=2",
            "cursor=1&other=2",
            "other=1",
        ):
            status, _, _ = self.request(f"{base}/api/codex/output?{query}", "GET")
            self.assertEqual(status, 400, query)

    # -- same-origin / cross-origin -------------------------------------------

    def test_same_origin_accepted_cross_origin_refused(self) -> None:
        target = self.project()
        base = self.serve(target, codex_command=self.sleep_command())
        host = base.split("//", 1)[1]

        status, payload = self.post(
            f"{base}/api/codex/start", headers={"Origin": f"http://{host}"}
        )
        self.assertEqual(status, 200, payload)

        status2, payload2 = self.post_json(
            f"{base}/api/codex/input",
            {"data": "should not land\n"},
            headers={"Origin": "http://evil.example"},
        )
        self.assertEqual(status2, 403, payload2)

        status3, payload3 = self.get_json(f"{base}/api/codex/output")
        self.assertEqual(status3, 200)
        self.assertNotIn("should not land", payload3["output"]["text"])

        status4, payload4 = self.post(
            f"{base}/api/codex/stop", headers={"Sec-Fetch-Site": "cross-site"}
        )
        self.assertEqual(status4, 403, payload4)
        status5, payload5 = self.get_json(f"{base}/api/state")
        self.assertTrue(payload5["codex_session"]["running"])

    def test_start_and_stop_reject_nonempty_bodies(self) -> None:
        target = self.project()
        base = self.serve(target, codex_command=self.sleep_command())
        status, payload = self.post(
            f"{base}/api/codex/start",
            headers={"Content-Type": "application/json"},
            body=b'{"unexpected": true}',
        )
        self.assertEqual(status, 413, payload)

        self.post(f"{base}/api/codex/start")
        status2, payload2 = self.post(
            f"{base}/api/codex/stop",
            headers={"Content-Type": "application/json"},
            body=b'{"unexpected": true}',
        )
        self.assertEqual(status2, 413, payload2)

    # -- server_close cleanup -------------------------------------------------

    def test_server_close_stops_running_session_and_attempts_both_cleanups(self) -> None:
        target = self.project()
        server = create_server(target, port=0, quiet=True, codex_command=self.sleep_command())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)

        server.codex_session.start()
        self.wait_until(lambda: server.codex_session.snapshot()["running"])
        pid = server.codex_session.snapshot()["pid"]

        server.server_close()
        thread.join(5)

        self.assertFalse(server.codex_session.snapshot()["running"])

        def dead() -> bool:
            try:
                os.kill(pid, 0)
            except OSError:
                return True
            return False

        self.wait_until(dead, message="the codex child survived server_close")

    def test_server_close_attempts_both_cleanups_even_when_one_raises(self) -> None:
        target = self.project()
        server = create_server(target, port=0, quiet=True, codex_command=self.sleep_command())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)

        class ExplodingWatcher:
            def shutdown(self, timeout=None):
                raise RuntimeError("boom")

        server.codex_session.start()
        self.wait_until(lambda: server.codex_session.snapshot()["running"])
        server.watcher = ExplodingWatcher()

        with self.assertRaises(RuntimeError):
            server.server_close()
        thread.join(5)

        self.assertFalse(server.codex_session.snapshot()["running"])


if __name__ == "__main__":
    unittest.main()
