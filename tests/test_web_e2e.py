"""Opt-in browser checks for the local dashboard runtime."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tests.asgi_server import create_server

ROOT = Path(__file__).resolve().parents[1]
WEB_APP = ROOT / "skills" / "coordinate-claude-work" / "scripts" / "web_app.py"


@unittest.skipUnless(
    os.environ.get("COORDINATOR_E2E") == "1",
    "set COORDINATOR_E2E=1 after installing the Playwright browsers",
)
class DashboardBrowserTests(unittest.TestCase):
    def test_firefox_terminal_socket_reconnects_after_unexpected_close(
        self,
    ) -> None:
        from playwright.sync_api import sync_playwright

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            (repo / ".git").mkdir()
            server = create_server(repo, quiet=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://{server.server_address[0]}:{server.server_address[1]}"
            try:
                with sync_playwright() as playwright:
                    browser = playwright.firefox.launch(headless=True)
                    page = browser.new_page()
                    page.route(
                        "**/api/provider-usage",
                        lambda route: route.fulfill(
                            status=200,
                            content_type="application/json",
                            body=json.dumps({"providers": []}),
                        ),
                    )
                    page.goto(url + "/#terminal", wait_until="domcontentloaded")
                    page.locator("#codex-session-feedback").filter(
                        has_text="input ownership"
                    ).wait_for(timeout=10_000)
                    page.evaluate(
                        "globalThis.__closedTerminalSocket = codexSocket; "
                        "codexSocket.close()"
                    )
                    page.wait_for_function(
                        "() => codexSocket && "
                        "codexSocket !== globalThis.__closedTerminalSocket && "
                        "codexSocket.readyState === WebSocket.OPEN",
                        timeout=10_000,
                    )
                    page.locator("#codex-session-feedback").filter(
                        has_text="input ownership"
                    ).wait_for(timeout=10_000)
                    with urllib.request.urlopen(url + "/healthz", timeout=2) as response:
                        self.assertEqual(response.status, 200)
                    browser.close()
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_cleared_terminal_history_stays_cleared_after_refresh(self) -> None:
        from playwright.sync_api import sync_playwright

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            (repo / ".git").mkdir()
            command = [
                sys.executable,
                "-u",
                "-c",
                "import os; exec('while True:\\n data=os.read(0,1024)\\n "
                "if not data: break\\n os.write(1,data)')",
            ]
            server = create_server(repo, quiet=True, codex_command=command)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            url = f"http://{server.server_address[0]}:{server.server_address[1]}"
            try:
                deadline = time.monotonic() + 10
                while True:
                    try:
                        with urllib.request.urlopen(url + "/healthz", timeout=1):
                            break
                    except OSError:
                        if time.monotonic() >= deadline:
                            self.fail("dashboard did not become healthy")
                        time.sleep(0.05)

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    page = browser.new_page()
                    page.route(
                        "**/api/provider-usage",
                        lambda route: route.fulfill(
                            status=200,
                            content_type="application/json",
                            body=json.dumps({"providers": []}),
                        ),
                    )
                    page.goto(url + "/#terminal", wait_until="domcontentloaded")
                    page.locator("#codex-session-start").click()
                    page.locator("#codex-session-state").filter(has_text="running").wait_for()
                    page.locator("#codex-session-feedback").filter(
                        has_text="input ownership"
                    ).wait_for()

                    marker = "clear-refresh-browser-marker"
                    page.evaluate(
                        """(marker) => {
                          const send = () => {
                            if (codexTerminalWritable && codexSocket &&
                                codexSocket.readyState === WebSocket.OPEN) {
                              codexSocket.send(JSON.stringify({
                                type: 'input', protocol: 'terminal.v1',
                                data: marker + '\\n'
                              }));
                            }
                          };
                          send();
                          globalThis.__terminalMarkerRetry = setInterval(send, 250);
                        }""",
                        marker,
                    )

                    def terminal_text() -> str:
                        return page.evaluate(
                            """() => Array.from(
                              {length: codexTerminal.buffer.active.length},
                              (_, index) => {
                                const line = codexTerminal.buffer.active.getLine(index);
                                return line ? line.translateToString(true) : '';
                              }
                            ).join('\\n')"""
                        )

                    page.wait_for_function(
                        "marker => Array.from({length: codexTerminal.buffer.active.length}, "
                        "(_, index) => codexTerminal.buffer.active.getLine(index)"
                        "?.translateToString(true) || '').join('\\n').includes(marker)",
                        arg=marker,
                    )
                    page.evaluate("clearInterval(globalThis.__terminalMarkerRetry)")

                    page.locator("#codex-terminal-clear").click()
                    page.wait_for_function("() => codexPendingControl === ''")
                    page.locator("#codex-session-feedback").filter(
                        has_text="input ownership"
                    ).wait_for()
                    page.wait_for_function(
                        "marker => !Array.from({length: codexTerminal.buffer.active.length}, "
                        "(_, index) => codexTerminal.buffer.active.getLine(index)"
                        "?.translateToString(true) || '').join('\\n').includes(marker)",
                        arg=marker,
                    )

                    page.reload(wait_until="domcontentloaded")
                    page.locator("#codex-session-state").filter(has_text="running").wait_for()
                    page.locator("#codex-session-feedback").filter(
                        has_text="input ownership"
                    ).wait_for()
                    self.assertNotIn(marker, terminal_text())
                    browser.close()
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()

    def test_live_dashboard_setup_and_administration_pages(self) -> None:
        from playwright.sync_api import sync_playwright

        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            repo = base / "initial"
            repo.mkdir()
            (repo / ".git").mkdir()
            state_dir = base / "state"
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = int(probe.getsockname()[1])
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(WEB_APP),
                    "--repo",
                    str(repo),
                    "--repositories-root",
                    str(base),
                    "--state-dir",
                    str(state_dir),
                    "--port",
                    str(port),
                    "--quiet",
                ],
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            url = f"http://127.0.0.1:{port}"
            try:
                self._wait_for_server(url, process)
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = browser.new_context(
                        permissions=["clipboard-read", "clipboard-write"]
                    )
                    page = context.new_page()
                    errors: list[str] = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.on(
                        "console",
                        lambda message: (
                            errors.append(message.text)
                            if message.type == "error"
                            else None
                        ),
                    )
                    now = datetime.now(timezone.utc)
                    weekly = timedelta(days=7)
                    session = timedelta(hours=5)

                    def reset_after(duration: timedelta, elapsed: timedelta) -> str:
                        return (now + duration - elapsed).isoformat()

                    usage_payload = {
                        "ok": True,
                        "generated_at": now.isoformat(),
                        "next_refresh_at": (now + timedelta(hours=1)).isoformat(),
                        "refresh_interval_seconds": 3600,
                        "refreshing": False,
                        "providers": [
                            {
                                "id": "codex",
                                "name": "Codex",
                                "status": "stale",
                                "stale": True,
                                "plan": "pro",
                                "message": "Codex usage request timed out.",
                                "last_success_at": now.isoformat(),
                                "remaining_percent": 70,
                                "windows": [
                                    {
                                        "id": "codex:primary",
                                        "label": "Weekly (7d)",
                                        "remaining_percent": 70,
                                        "used_percent": 30,
                                        "duration_minutes": 10080,
                                        "resets_at": reset_after(weekly, timedelta(days=1)),
                                    },
                                ],
                            },
                            {
                                "id": "claude",
                                "name": "Claude",
                                "status": "available",
                                "plan": "max",
                                "remaining_percent": 55,
                                "windows": [
                                    {
                                        "id": "session:0",
                                        "label": "Session",
                                        "remaining_percent": 80,
                                        "used_percent": 20,
                                        "duration_minutes": 300,
                                        "resets_at": reset_after(session, timedelta(hours=2)),
                                        "forecast": {
                                            "method": "rolling_velocity",
                                            "projected_remaining": 32,
                                            "burn_rate_percent_per_hour": 6.25,
                                            "sustainable_rate_percent_per_hour": 8,
                                            "velocity_ratio": 0.78,
                                            "sample_count": 6,
                                            "basis_hours": 5,
                                            "confidence": "high",
                                        },
                                    },
                                    {
                                        "id": "weekly:1",
                                        "label": "Weekly",
                                        "group": "weekly",
                                        "remaining_percent": 55,
                                        "used_percent": 45,
                                        "duration_minutes": 10080,
                                        "resets_at": reset_after(
                                            weekly, timedelta(days=3, hours=12)
                                        ),
                                    },
                                    {
                                        "id": "weekly:2",
                                        "label": "Fable",
                                        "group": "weekly",
                                        "scope": {
                                            "model": {"display_name": "Fable"}
                                        },
                                        "remaining_percent": 100,
                                        "used_percent": 0,
                                        "duration_minutes": 10080,
                                        "resets_at": None,
                                    },
                                ],
                            },
                        ],
                    }
                    history_payload = {
                        "ok": True,
                        "generated_at": now.isoformat(),
                        "refreshing": False,
                        "range": "7d",
                        "from": (now - weekly).isoformat(),
                        "to": now.isoformat(),
                        "bucket_seconds": 3600,
                        "providers": [
                            {
                                "id": "codex",
                                "name": "Codex",
                                "status": "available",
                                "metric": "cost",
                                "cost_label": "Estimated API value",
                                "coverage_percent": 100.0,
                                "totals": {"total_tokens": 125000, "cost_usd": 4.25},
                                "series": [
                                    {"timestamp": (now - timedelta(days=1)).isoformat(), "tokens": 50000, "cost_usd": 1.25},
                                    {"timestamp": now.isoformat(), "tokens": 75000, "cost_usd": 3.0},
                                ],
                                "models": [{"model": "gpt-test", "tokens": 125000, "valued_tokens": 125000, "cost_usd": 4.25}],
                            },
                            {
                                "id": "local-qwen",
                                "name": "Local Qwen",
                                "status": "available",
                                "metric": "tokens",
                                "cost_label": None,
                                "coverage_percent": None,
                                "totals": {"total_tokens": 42000, "cost_usd": 0},
                                "series": [{"timestamp": now.isoformat(), "tokens": 42000, "cost_usd": 0}],
                                "models": [{"model": "qwen-27b", "tokens": 42000, "valued_tokens": 0, "cost_usd": 0}],
                            },
                        ],
                    }
                    page.route(
                        "**/api/provider-usage",
                        lambda route: route.fulfill(
                            status=200,
                            content_type="application/json",
                            body=json.dumps(usage_payload),
                        ),
                    )
                    page.route(
                        "**/api/usage-history?*",
                        lambda route: route.fulfill(
                            status=200,
                            content_type="application/json",
                            body=json.dumps(history_payload),
                        ),
                    )
                    def model_catalog(route):
                        codex = "source=codex" in route.request.url
                        identifiers = ["gpt-test"] if codex else ["opus", "sonnet"]
                        models = [
                            {
                                "id": identifier,
                                "label": identifier,
                                "default": index == 0,
                                "default_effort": "medium",
                                "efforts": [
                                    {"id": effort}
                                    for effort in ("low", "medium", "high")
                                ],
                            }
                            for index, identifier in enumerate(identifiers)
                        ]
                        route.fulfill(
                            status=200,
                            content_type="application/json",
                            body=json.dumps({"ok": True, "models": models}),
                        )

                    page.route("**/api/executor-settings/models?*", model_catalog)
                    page.goto(url, wait_until="domcontentloaded")
                    page.locator("#usage-codex-value").filter(has_text="70%").wait_for(
                        timeout=10_000
                    )
                    self.assertIn(
                        "stale",
                        page.locator("#usage-codex-plan").inner_text().casefold(),
                    )
                    self.assertIn(
                        "Codex usage request timed out",
                        page.locator("#usage-codex").get_attribute("title"),
                    )
                    page.locator("#usage-claude-value").filter(has_text="55%").wait_for(
                        timeout=10_000
                    )
                    self.assertNotIn(
                        "Spark", page.locator("#usage-codex").inner_text()
                    )
                    page.locator("#usage-claude-value").filter(has_text="Weekly").wait_for(
                        timeout=10_000
                    )
                    page.locator("#usage-claude-value .usage-chip").filter(
                        has_text="Fable"
                    ).filter(has_text="100%").wait_for(timeout=10_000)
                    projection_expectations = (
                        ("#usage-codex-value .usage-chip", 0, "-110%", "bad"),
                        ("#usage-claude-value .usage-chip", 0, "32%", "ok"),
                        ("#usage-claude-value .usage-chip", 1, "10%", "warn"),
                        ("#usage-claude-value .usage-chip", 2, "—", "neutral"),
                    )
                    for selector, index, expected, tone in projection_expectations:
                        projection = page.locator(selector).nth(index).locator(
                            ".usage-window-projection"
                        )
                        self.assertEqual(projection.inner_text(), expected)
                        self.assertEqual(projection.get_attribute("data-tone"), tone)
                    self.assertIn(
                        "projected remaining at reset",
                        page.locator("#usage-codex-value .usage-chip").first.get_attribute(
                            "title"
                        ),
                    )
                    self.assertEqual(
                        page.locator("#usage-claude-value .usage-window-velocity")
                        .first.inner_text(),
                        "6.3%/h",
                    )
                    self.assertIn(
                        "0.78× sustainable pace",
                        page.locator("#usage-claude-value .usage-chip")
                        .first.get_attribute("title"),
                    )
                    page.locator("#nav-usage").click()
                    page.locator("#usage-history-tabs .provider-tab").filter(
                        has_text="Codex"
                    ).wait_for(timeout=10_000)
                    self.assertEqual(page.locator("#usage-value-total").inner_text(), "$4.25")
                    self.assertGreater(
                        page.locator("#usage-history-chart .usage-value-line").count(), 0
                    )
                    page.locator("#usage-history-tabs .provider-tab").filter(
                        has_text="Local Qwen"
                    ).click()
                    self.assertEqual(
                        page.locator("#usage-value-total").inner_text(), "42,000 tokens"
                    )
                    self.assertEqual(
                        page.locator("#usage-price-coverage").inner_text(), "not applicable"
                    )
                    page.locator("#connection-label").filter(
                        has_text="state feed"
                    ).wait_for(timeout=10_000)
                    page.locator("#nav-terminal").click()
                    page.locator("#codex-session-feedback").filter(
                        has_text="input ownership"
                    ).wait_for(timeout=10_000)
                    page.locator("#terminal-activity-heading").filter(
                        has_text="Session activity"
                    ).wait_for(timeout=10_000)
                    self.assertTrue(page.locator("#terminal-agents").is_visible())
                    self.assertTrue(
                        page.locator("#background-terminals").is_visible()
                    )
                    page.evaluate(
                        """() => new Promise((resolve) => {
                          codexTerminal.write('clipboard-marker', resolve);
                        })"""
                    )
                    page.evaluate("codexTerminal.selectAll(); codexTerminal.focus()")
                    page.locator("#codex-terminal-copy").wait_for(state="visible")
                    self.assertTrue(page.locator("#codex-terminal-copy").is_enabled())
                    page.keyboard.press("Control+Shift+C")
                    page.locator("#codex-session-feedback").filter(
                        has_text="Copied"
                    ).wait_for(timeout=10_000)
                    self.assertIn(
                        "clipboard-marker", page.evaluate("navigator.clipboard.readText()")
                    )

                    page.locator("#nav-setup").click()
                    page.locator("#repository-create-name").fill("browser-created")
                    page.locator("#repository-create-form button").click()
                    page.locator("#repository-create-feedback").filter(
                        has_text="Created and selected"
                    ).wait_for(timeout=10_000)
                    self.assertTrue((base / "browser-created" / ".git").is_dir())
                    existing_ci = (
                        base
                        / "browser-created"
                        / ".github"
                        / "workflows"
                        / "tests.yml"
                    )
                    existing_ci.parent.mkdir(parents=True)
                    existing_ci.write_text("name: Existing tests\n", encoding="utf-8")

                    page.locator("#repository-project-name").fill("Browser project")
                    page.locator(
                        '#repository-initialize-form button[type="submit"]'
                    ).click()
                    page.locator("#repository-initialize-feedback").filter(
                        has_text="Coordination files are ready"
                    ).wait_for(timeout=10_000)
                    self.assertTrue(
                        (base / "browser-created" / ".coordination").is_dir()
                    )
                    page.locator("#repository-ci-confirmation").wait_for(
                        state="visible", timeout=10_000
                    )
                    page.locator("#repository-ci-workflows").filter(
                        has_text=".github/workflows/tests.yml"
                    ).wait_for(timeout=10_000)
                    self.assertFalse(
                        (existing_ci.parent / "coordinator.yml").exists()
                    )
                    page.locator("#repository-ci-skip").click()
                    page.locator("#repository-initialize-feedback").filter(
                        has_text="Kept the repository's current CI configuration"
                    ).wait_for(timeout=10_000)
                    page.locator("#repository-ci-confirmation").wait_for(state="hidden")
                    self.assertFalse(
                        (existing_ci.parent / "coordinator.yml").exists()
                    )
                    page.locator(
                        '#repository-initialize-form button[type="submit"]'
                    ).click()
                    page.locator("#repository-ci-confirmation").wait_for(
                        state="visible", timeout=10_000
                    )
                    page.locator("#repository-ci-add").click()
                    page.locator("#repository-initialize-feedback").filter(
                        has_text="Added .github/workflows/coordinator.yml"
                    ).wait_for(timeout=10_000)
                    self.assertTrue(
                        (existing_ci.parent / "coordinator.yml").is_file()
                    )
                    self.assertEqual(
                        existing_ci.read_text(encoding="utf-8"),
                        "name: Existing tests\n",
                    )

                    page.locator("#nav-monitor").click()
                    page.locator("#workspace-repositories").filter(
                        has_text="browser-created"
                    ).wait_for(timeout=10_000)

                    page.locator("#nav-runs").click()
                    page.locator("#run-records").filter(
                        has_text="browser-created"
                    ).wait_for(timeout=10_000)
                    page.locator("#run-records .record-select").first.click()
                    page.locator("#run-timeline").filter(
                        has_text="run_discovered"
                    ).wait_for(timeout=10_000)

                    page.locator("#nav-settings").click()
                    reviewer_model = page.locator(
                        '#executor-settings-form select[name="codex_model"]'
                    )
                    page.wait_for_function(
                        "() => document.querySelector("
                        "'#executor-settings-form select[name=\"codex_model\"]'"
                        ").options.length > 1",
                        timeout=10_000,
                    )
                    reviewer_model.select_option(index=1)
                    self.assertNotEqual(reviewer_model.input_value(), "")
                    reviewer_effort = page.locator(
                        '#executor-settings-form select[name="codex_effort"]'
                    )
                    reviewer_effort.select_option("high")
                    self.assertEqual(reviewer_effort.input_value(), "high")
                    page.locator("#executor-settings-feedback").filter(
                        has_text="Unsaved role changes"
                    ).wait_for(timeout=10_000)
                    page.locator('[data-role-profile="local-heavy"]').click()
                    self.assertEqual(
                        page.locator(
                            '#executor-settings-form select[name="execution_strategy"]'
                        ).input_value(),
                        "claude-local",
                    )
                    self.assertTrue(page.locator("#local-model-settings").is_visible())
                    page.locator("#executor-fallback").filter(
                        has_text="Claude keeps rejected"
                    ).wait_for(timeout=10_000)
                    page.locator('[data-role-profile="frontier"]').click()
                    self.assertFalse(page.locator("#local-model-settings").is_visible())
                    self.assertEqual(
                        page.locator(
                            '#executor-settings-form select[name="claude_effort"]'
                        ).input_value(),
                        "high",
                    )
                    page.locator('#preferences-form select[name="theme"]').select_option("dark")
                    page.locator("#preferences-form button").click()
                    page.locator("#preferences-feedback").filter(
                        has_text="Preferences saved"
                    ).wait_for(timeout=10_000)
                    self.assertEqual(
                        page.locator("html").get_attribute("data-theme"), "dark"
                    )
                    page.locator('#guardrails-form input[name="generated_tokens"]').fill("1000")
                    page.locator("#guardrails-form button").click()
                    page.locator("#guardrails-feedback").filter(
                        has_text="Guardrails saved"
                    ).wait_for(timeout=10_000)

                    page.locator("#nav-activity").click()
                    page.locator("#activity-events").filter(
                        has_text="repository_initialize"
                    ).wait_for(timeout=10_000)
                    page.locator("#nav-sessions").click()
                    page.locator("#session-records").filter(
                        has_text="local browser"
                    ).wait_for(timeout=10_000)
                    page.locator("#nav-diagnostics").click()
                    page.locator("#diagnostics-feedback").filter(
                        has_text="Mode local"
                    ).wait_for(timeout=10_000)
                    accessibility = page.evaluate(
                        """() => ({
                          duplicateIds: [...document.querySelectorAll('[id]')]
                            .map((node) => node.id)
                            .filter((id, index, values) => values.indexOf(id) !== index),
                          unlabeledControls: [...document.querySelectorAll('input, select, textarea')]
                            .filter((node) => node.labels.length === 0 && !node.getAttribute('aria-label'))
                            .map((node) => node.id || node.name),
                          unnamedButtons: [...document.querySelectorAll('button')]
                            .filter((node) => !node.textContent.trim() && !node.getAttribute('aria-label'))
                            .length,
                        })"""
                    )
                    self.assertEqual(accessibility["duplicateIds"], [])
                    self.assertEqual(accessibility["unlabeledControls"], [])
                    self.assertEqual(accessibility["unnamedButtons"], 0)
                    self.assertEqual(errors, [])
                    browser.close()
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                if process.stdout is not None:
                    process.stdout.close()

    @staticmethod
    def _wait_for_server(url: str, process: subprocess.Popen[str]) -> None:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                output = process.stdout.read() if process.stdout else ""
                raise AssertionError(f"dashboard exited before startup:\n{output}")
            try:
                with urllib.request.urlopen(url + "/healthz", timeout=1) as response:
                    if response.status == 200:
                        return
            except OSError:
                time.sleep(0.1)
        raise AssertionError("dashboard did not become healthy")


if __name__ == "__main__":
    unittest.main()
