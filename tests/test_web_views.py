"""Focused contract tests for the multi-view dashboard navigation layout.

These tests only read the checked-in HTML/JS source under
``src/coordinator/assets/web``; they never launch a real
browser, session, or watcher process.
"""

import re
import unittest
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent / "src" / "coordinator" / "assets" / "web"

ROUTES = [
    "monitor",
    "terminal",
    "work",
    "agents",
    "logs",
    "activity",
    "runs",
    "settings",
    "setup",
    "sessions",
    "diagnostics",
]

# Panel/element ids expected to live inside each view section, keyed by
# route name. These are used to check panel-to-view grouping.
VIEW_PANEL_IDS = {
    "monitor": ["workspace-repositories-heading", "workflow-heading", "metrics-heading"],
    "terminal": ["codex-session-heading"],
    "work": ["goal-heading", "roadmap-heading", "task-heading", "review-heading"],
    "agents": [
        "coder-heading",
        "subagents-heading",
        "watcher-controls-heading",
        "watchers-heading",
    ],
    "logs": ["relay-log-heading"],
    "activity": ["activity-heading"],
    "runs": ["run-history-heading", "run-detail-heading"],
    "settings": ["guardrails-heading", "preferences-heading", "shortcuts-heading"],
    "setup": ["create-repository-heading", "initialize-heading"],
    "sessions": ["sessions-heading"],
    "diagnostics": ["diagnostics-heading"],
}


def read(path):
    return path.read_text(encoding="utf-8")


class RouteAndHashTests(unittest.TestCase):
    def setUp(self):
        self.js = read(WEB_DIR / "app.js")
        self.html = read(WEB_DIR / "index.html")

    def test_routes_array_has_expected_routes_in_order(self):
        match = re.search(r"var ROUTES\s*=\s*\[([^\]]*)\];", self.js)
        self.assertIsNotNone(match, "ROUTES array not found")
        found = re.findall(r'["\']([a-z]+)["\']', match.group(1))
        self.assertEqual(found, ROUTES)

    def test_nav_links_use_exact_hashes(self):
        for route in ROUTES:
            self.assertRegex(
                self.html,
                r'href="#%s"[^>]*id="nav-%s"' % (re.escape(route), re.escape(route)),
                "missing nav link with exact hash for route: " + route,
            )

    def test_view_sections_have_unique_ids(self):
        ids = re.findall(r'id="(view-[a-z]+)"', self.html)
        expected = ["view-" + route for route in ROUTES]
        self.assertEqual(sorted(ids), sorted(expected))
        self.assertEqual(len(ids), len(set(ids)), "view ids must be unique")

    def test_nav_link_ids_are_unique(self):
        ids = re.findall(r'id="(nav-[a-z]+)"', self.html)
        expected = ["nav-" + route for route in ROUTES]
        self.assertEqual(sorted(ids), sorted(expected))
        self.assertEqual(len(ids), len(set(ids)), "nav ids must be unique")

    def test_default_route_is_monitor(self):
        self.assertRegex(self.js, r'var DEFAULT_ROUTE\s*=\s*"monitor"\s*;')

    def test_route_from_hash_falls_back_to_default_for_unknown_hash(self):
        match = re.search(r"function routeFromHash\([\s\S]*?\n}\n", self.js)
        self.assertIsNotNone(match, "routeFromHash function not found")
        body = match.group(0)
        self.assertIn("ROUTES.indexOf(raw)", body)
        self.assertIn("DEFAULT_ROUTE", body)
        self.assertRegex(body, r"===\s*-1\s*\?\s*DEFAULT_ROUTE")

    def test_hashchange_listener_wired_to_apply_route(self):
        match = re.search(r"function wireNavigation\([\s\S]*?\n}\n", self.js)
        self.assertIsNotNone(match, "wireNavigation function not found")
        body = match.group(0)
        self.assertIn('addEventListener("hashchange", applyRoute)', body)
        self.assertIn("applyRoute()", body)


class ProviderUsageHeaderTests(unittest.TestCase):
    def setUp(self):
        self.js = read(WEB_DIR / "app.js")
        self.html = read(WEB_DIR / "index.html")

    def test_header_has_compact_codex_and_claude_usage_values(self):
        for provider in ("codex", "claude"):
            self.assertIn('id="usage-%s"' % provider, self.html)
            self.assertIn('id="usage-%s-value"' % provider, self.html)
        self.assertIn('id="usage-refresh"', self.html)

    def test_usage_reads_cache_and_manual_refresh_uses_post(self):
        self.assertIn('var PROVIDER_USAGE_URL = "/api/provider-usage"', self.js)
        self.assertIn(
            'var PROVIDER_USAGE_REFRESH_URL = "/api/provider-usage/refresh"',
            self.js,
        )
        refresh = re.search(r"function refreshProviderUsage\([\s\S]*?\n}\n", self.js)
        self.assertIsNotNone(refresh)
        self.assertIn('method: "POST"', refresh.group(0))
        self.assertIn('"X-CSRF-Token": csrfToken', refresh.group(0))
        self.assertIn("result.status < 200 || result.status >= 300", refresh.group(0))
        self.assertNotIn("result.response", refresh.group(0))

    def test_usage_is_rendered_as_remaining_not_consumed(self):
        renderer = re.search(r"function renderProviderUsage\([\s\S]*?\n}\n", self.js)
        self.assertIsNotNone(renderer)
        self.assertIn("remaining_percent", renderer.group(0))
        self.assertIn("remaining", renderer.group(0))


class PanelGroupingTests(unittest.TestCase):
    def setUp(self):
        self.html = read(WEB_DIR / "index.html")

    def test_each_view_section_contains_its_expected_panels(self):
        for route in ROUTES:
            section_match = re.search(
                r'<section id="view-%s"[\s\S]*?(?=<section id="view-|</main>)'
                % re.escape(route),
                self.html,
            )
            self.assertIsNotNone(section_match, "view section not found for: " + route)
            section_body = section_match.group(0)
            for panel_id in VIEW_PANEL_IDS[route]:
                self.assertIn(
                    'id="%s"' % panel_id,
                    section_body,
                    "panel %s expected inside view-%s but not found"
                    % (panel_id, route),
                )

    def test_panels_are_not_duplicated_across_views(self):
        seen = {}
        for route in ROUTES:
            for panel_id in VIEW_PANEL_IDS[route]:
                self.assertNotIn(
                    panel_id, seen, "panel id duplicated across views: " + panel_id
                )
                seen[panel_id] = route


class VisibilityAndActiveStateTests(unittest.TestCase):
    def setUp(self):
        self.js = read(WEB_DIR / "app.js")
        self.html = read(WEB_DIR / "index.html")

    def test_only_monitor_view_visible_by_default_in_markup(self):
        # Monitor view must not carry a hidden attribute; all other views must.
        monitor_match = re.search(r'<section id="view-monitor"[^>]*>', self.html)
        self.assertIsNotNone(monitor_match)
        self.assertNotIn("hidden", monitor_match.group(0))
        for route in ROUTES:
            if route == "monitor":
                continue
            match = re.search(
                r'<section id="view-%s"[^>]*>' % re.escape(route), self.html
            )
            self.assertIsNotNone(match, "view section not found for: " + route)
            self.assertIn("hidden", match.group(0))

    def test_apply_route_toggles_hidden_for_exactly_one_view(self):
        match = re.search(r"function applyRoute\([\s\S]*?\n}\n", self.js)
        self.assertIsNotNone(match, "applyRoute function not found")
        body = match.group(0)
        self.assertIn("ROUTES.forEach", body)
        self.assertIn("view.hidden = name !== route", body)

    def test_apply_route_sets_aria_current_on_active_nav_link_only(self):
        match = re.search(r"function applyRoute\([\s\S]*?\n}\n", self.js)
        self.assertIsNotNone(match, "applyRoute function not found")
        body = match.group(0)
        self.assertIn('link.setAttribute("aria-current", "page")', body)
        self.assertIn('link.removeAttribute("aria-current")', body)


class TerminalFitDeferralTests(unittest.TestCase):
    def setUp(self):
        self.js = read(WEB_DIR / "app.js")

    def test_init_codex_terminal_skips_initial_fit_call(self):
        match = re.search(r"function initCodexTerminal\([\s\S]*?\n}\n", self.js)
        self.assertIsNotNone(match, "initCodexTerminal function not found")
        body = match.group(0)
        # Must not unconditionally fit at init time; fitting only happens
        # when the terminal route is already the current route.
        self.assertIn('if (currentRoute === "terminal")', body)
        self.assertIn("scheduleCodexFitAndResize()", body)
        # No bare unconditional call to fitAddon.fit() in this function.
        self.assertNotRegex(body, r"(?<!\.)\bfitAddon\.fit\(\)")

    def test_apply_route_fits_and_resizes_when_terminal_becomes_visible(self):
        match = re.search(r"function applyRoute\([\s\S]*?\n}\n", self.js)
        self.assertIsNotNone(match, "applyRoute function not found")
        body = match.group(0)
        self.assertIn('if (route === "terminal")', body)
        self.assertIn("terminalEverVisible = true", body)
        self.assertIn("scheduleCodexFitAndResize()", body)
        self.assertIn("codexTerminalReady", body)


class CompletionDetailsTests(unittest.TestCase):
    def setUp(self):
        self.js = read(WEB_DIR / "app.js")
        self.html = read(WEB_DIR / "index.html")

    def test_completion_details_element_is_closed_by_default_in_markup(self):
        match = re.search(r'<details id="workflow-completion-details"[^>]*>', self.html)
        self.assertIsNotNone(match, "completion details element not found")
        self.assertNotIn("open", match.group(0))

    def test_completion_sentence_references_current_goal(self):
        self.assertIn(
            "A completion report matches the current goal. See the conclusion below and expand for full detail.",
            self.js,
        )

    def test_conclusion_uses_first_result_entry(self):
        match = re.search(r"function renderWorkflow\([\s\S]*?\n}\n", self.js)
        self.assertIsNotNone(match, "renderWorkflow function not found")
        body = match.group(0)
        self.assertIn("results.length > 0 ? results[0].trim()", body)


class CodexTransportTests(unittest.TestCase):
    def setUp(self):
        self.js = read(WEB_DIR / "app.js")

    def test_controls_remain_http_and_interactive_io_uses_websocket(self):
        for literal in ('"/api/codex/start"', '"/api/codex/stop"', '"/ws/terminal"'):
            self.assertIn(literal, self.js, "missing endpoint literal: " + literal)
        self.assertIn("new WebSocket", self.js)
        self.assertNotIn("fetch(CODEX_INPUT_URL", self.js)


if __name__ == "__main__":
    unittest.main()
