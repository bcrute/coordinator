"""Opt-in browser checks for the local dashboard runtime."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB_APP = ROOT / "skills" / "coordinate-claude-work" / "scripts" / "web_app.py"


@unittest.skipUnless(
    os.environ.get("COORDINATOR_E2E") == "1",
    "set COORDINATOR_E2E=1 after installing the Playwright Chromium browser",
)
class DashboardBrowserTests(unittest.TestCase):
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
                    page = browser.new_page()
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
                                "status": "available",
                                "plan": "pro",
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
                                    {
                                        "id": "spark:primary",
                                        "label": "Spark · Session (5h)",
                                        "remaining_percent": 100,
                                        "used_percent": 0,
                                        "duration_minutes": 300,
                                        "resets_at": reset_after(session, timedelta(hours=1)),
                                    },
                                    {
                                        "id": "spark:secondary",
                                        "label": "Spark · Weekly (7d)",
                                        "scope": "Spark",
                                        "remaining_percent": 90,
                                        "used_percent": 10,
                                        "duration_minutes": 10080,
                                        "resets_at": reset_after(weekly, timedelta(days=4)),
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
                    page.route(
                        "**/api/provider-usage",
                        lambda route: route.fulfill(
                            status=200,
                            content_type="application/json",
                            body=json.dumps(usage_payload),
                        ),
                    )
                    page.goto(url, wait_until="domcontentloaded")
                    page.locator("#usage-codex-value").filter(has_text="70%").wait_for(
                        timeout=10_000
                    )
                    page.locator("#usage-claude-value").filter(has_text="55%").wait_for(
                        timeout=10_000
                    )
                    page.locator("#usage-codex-value").filter(
                        has_text="Spark · Session (5h)"
                    ).wait_for(timeout=10_000)
                    page.locator("#usage-claude-value").filter(has_text="Weekly").wait_for(
                        timeout=10_000
                    )
                    page.locator("#usage-claude-value .usage-chip").filter(
                        has_text="Fable"
                    ).filter(has_text="100%").wait_for(timeout=10_000)
                    for forecast in ("Run out", "Close", "Nowhere near", "Unknown"):
                        page.locator("#usage-forecast-list").filter(
                            has_text=forecast
                        ).wait_for(timeout=10_000)
                    page.locator("#connection-label").filter(
                        has_text="state feed"
                    ).wait_for(timeout=10_000)
                    page.locator("#nav-terminal").click()
                    page.locator("#codex-session-feedback").filter(
                        has_text="input ownership"
                    ).wait_for(timeout=10_000)

                    page.locator("#nav-setup").click()
                    page.locator("#repository-create-name").fill("browser-created")
                    page.locator("#repository-create-form button").click()
                    page.locator("#repository-create-feedback").filter(
                        has_text="Created and selected"
                    ).wait_for(timeout=10_000)
                    self.assertTrue((base / "browser-created" / ".git").is_dir())

                    page.locator("#repository-project-name").fill("Browser project")
                    page.locator("#repository-initialize-form button").click()
                    page.locator("#repository-initialize-feedback").filter(
                        has_text="Coordination files are ready"
                    ).wait_for(timeout=10_000)
                    self.assertTrue(
                        (base / "browser-created" / ".coordination").is_dir()
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
