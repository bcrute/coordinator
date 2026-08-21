"""Opt-in browser checks for the local dashboard runtime."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
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
                    page.goto(url, wait_until="domcontentloaded")
                    page.locator("#connection-label").filter(
                        has_text="state feed"
                    ).wait_for(timeout=10_000)
                    page.locator("#nav-terminal").click()
                    page.locator("#codex-session-feedback").filter(
                        has_text="live socket"
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
