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
    "usage",
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
        "delegations-heading",
        "watcher-controls-heading",
        "watchers-heading",
    ],
    "logs": ["relay-log-heading"],
    "activity": ["activity-heading"],
    "usage": ["usage-history-heading"],
    "runs": ["run-history-heading", "run-detail-heading"],
    "settings": [
        "executor-settings-heading",
        "guardrails-heading",
        "preferences-heading",
        "shortcuts-heading",
    ],
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

    def test_review_panel_displays_structured_next_executor(self):
        self.assertIn('id="review-next-executor"', self.html)
        renderer = re.search(r"function renderReview\(state\) \{[\s\S]*?\n\}", self.js)
        self.assertIsNotNone(renderer)
        self.assertIn('review.next_executor', renderer.group(0))


class ProviderUsageHeaderTests(unittest.TestCase):
    def setUp(self):
        self.js = read(WEB_DIR / "app.js")
        self.html = read(WEB_DIR / "index.html")
        self.css = read(WEB_DIR / "app.css")

    def test_header_has_compact_codex_and_claude_usage_values(self):
        for provider in ("codex", "claude"):
            self.assertIn('id="usage-%s"' % provider, self.html)
            self.assertIn('id="usage-%s-value"' % provider, self.html)
            self.assertIn('id="usage-%s-plan"' % provider, self.html)
        self.assertIn('id="usage-refresh"', self.html)

    def test_usage_is_grouped_into_provider_columns(self):
        self.assertIn(
            "grid-template-columns: repeat(2, minmax(18rem, 1fr)) auto",
            self.css,
        )
        self.assertIn(".usage-provider:first-child", self.css)
        self.assertEqual(self.html.count("<span>Projected / pace</span>"), 2)
        self.assertNotIn('id="usage-forecast-list"', self.html)

    def test_provider_limits_are_top_right_and_resets_are_inline(self):
        provider = re.search(r"\.provider-usage\s*\{[^}]*\}", self.css)
        self.assertIsNotNone(provider)
        self.assertIn("grid-column: 3", provider.group(0))
        self.assertIn("grid-row: 1 / span 2", provider.group(0))
        self.assertIn("justify-self: end", provider.group(0))
        self.assertRegex(
            self.css,
            r"\.usage-window-name\s*\{[^}]*flex-direction:\s*row",
        )
        self.assertIn('reset.textContent = "· " + usageResetShort', self.js)

    def test_reset_and_velocity_projection_have_rendering_hooks(self):
        for function in (
            "usageResetShort",
            "usagePaceForecast",
            "usageVelocityLabel",
            "usageForecastDetail",
            "usageWindowChip",
        ):
            self.assertIn(f"function {function}", self.js)
        self.assertIn('projection.className = "usage-window-projection"', self.js)
        self.assertIn('velocity.className = "usage-window-velocity"', self.js)
        self.assertIn("rolling_velocity", self.js)
        self.assertIn("Projected / pace", self.html)

    def test_compact_reset_label_includes_calendar_date(self):
        formatter = re.search(
            r"function usageResetShort\(value\) \{[\s\S]*?\n\}", self.js
        )
        self.assertIsNotNone(formatter)
        self.assertIn('month: "short"', formatter.group(0))
        self.assertIn('day: "numeric"', formatter.group(0))

    def test_stale_watcher_records_are_hidden_from_the_dashboard(self):
        renderer = re.search(r"function renderWatchers\(state\) \{[\s\S]*?\n\}", self.js)
        self.assertIsNotNone(renderer)
        self.assertIn('entry.watcher_state !== "stale"', renderer.group(0))
        self.assertIn('.usage-chip strong[data-tone="ok"]', self.css)
        self.assertIn('.usage-chip strong[data-tone="warn"]', self.css)
        self.assertIn('.usage-chip strong[data-tone="bad"]', self.css)

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

    def test_usage_header_shows_a_live_last_checked_age(self):
        self.assertIn('id="usage-codex-checked"', self.html)
        self.assertIn('id="usage-claude-checked"', self.html)
        self.assertIn("function paintProviderUsageAge()", self.js)
        self.assertIn('node.textContent = "checked " + ago(Date.now() - checkedAt)', self.js)
        self.assertIn("paintProviderUsageAge();", self.js)

    def test_usage_is_rendered_as_remaining_not_consumed(self):
        renderer = re.search(r"function renderProviderUsage\([\s\S]*?\n}\n", self.js)
        self.assertIsNotNone(renderer)
        self.assertIn("remaining_percent", renderer.group(0))
        self.assertIn("remaining", renderer.group(0))
        self.assertIn("windows.forEach", renderer.group(0))
        self.assertIn("usageWindowChip", renderer.group(0))


class UsageHistoryViewTests(unittest.TestCase):
    def setUp(self):
        self.js = read(WEB_DIR / "app.js")
        self.html = read(WEB_DIR / "index.html")

    def test_dedicated_view_has_dynamic_provider_tabs_and_range_controls(self):
        self.assertIn('id="usage-history-tabs"', self.html)
        self.assertIn('id="usage-history-range"', self.html)
        self.assertIn('id="usage-history-chart"', self.html)
        self.assertIn('id="usage-history-refresh"', self.html)
        renderer = re.search(r"function renderUsageHistory\([\s\S]*?\n}\n", self.js)
        self.assertIsNotNone(renderer)
        self.assertIn("providers.map", renderer.group(0))
        self.assertIn('button.setAttribute("role", "tab")', renderer.group(0))
        self.assertNotIn('["codex", "claude"]', renderer.group(0))

    def test_history_import_is_csrf_protected_and_costs_are_labeled_as_estimates(self):
        self.assertIn('var USAGE_HISTORY_URL = "/api/usage-history"', self.js)
        self.assertIn('var USAGE_HISTORY_REFRESH_URL = "/api/usage-history/refresh"', self.js)
        loader = re.search(r"function loadUsageHistory\([\s\S]*?\n}\n", self.js)
        self.assertIsNotNone(loader)
        self.assertIn('options.method = "POST"', loader.group(0))
        self.assertIn('options.headers["X-CSRF-Token"] = csrfToken', loader.group(0))
        self.assertIn("API-equivalent estimates, not subscription charges", self.js)


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


class DelegationViewTests(unittest.TestCase):
    def setUp(self):
        self.js = read(WEB_DIR / "app.js")
        self.html = read(WEB_DIR / "index.html")

    def test_settings_exposes_role_pipeline_and_agents_render_decision_evidence(self):
        self.assertIn('name="codex_model"', self.html)
        self.assertIn('name="execution_strategy"', self.html)
        self.assertIn('value="claude-local"', self.html)
        self.assertIn('data-role="reviewer"', self.html)
        self.assertIn('data-role="supervisor"', self.html)
        self.assertIn('data-role="executor"', self.html)
        self.assertNotIn('disabled aria-label="Reviewer runtime"', self.html)
        self.assertIn('Primary runtime<select name="primary_adapter"', self.html)
        self.assertIn('value="codex">Codex CLI', self.html)
        self.assertIn('value="claude">Claude Code', self.html)
        self.assertIn('value="mini-swe-agent">Local / API via mini-swe-agent', self.html)
        self.assertIn('Primary model<select name="codex_model"', self.html)
        self.assertIn('Primary effort<select name="codex_effort"', self.html)
        self.assertIn('select name="primary_claude_model"', self.html)
        self.assertIn('select name="primary_claude_effort"', self.html)
        self.assertIn('select name="primary_local_model"', self.html)
        self.assertIn('select name="primary_local_effort"', self.html)
        self.assertIn('input name="primary_local_step_limit"', self.html)
        self.assertIn("Starting permissions", self.html)
        self.assertIn('Supervisor effort<select name="claude_effort"', self.html)
        self.assertIn('Native subagent effort<select name="claude_subagent_effort"', self.html)
        self.assertIn('Reasoning effort<select name="mini_swe_effort"', self.html)
        self.assertGreaterEqual(
            self.html.count('<option value="none">None (disable thinking)</option>'),
            2,
        )
        self.assertIn('Direct execution profile<select name="mini_swe_profile"', self.html)
        self.assertIn('value="bounded">Bounded assignment', self.html)
        self.assertIn('value="exploratory">Exploratory repository work', self.html)
        self.assertIn('id="handoff-budget-summary"', self.html)
        self.assertIn("function updateHandoffBudgetSummary", self.js)
        self.assertIn('" reserved for verification and recovery."', self.js)
        self.assertNotIn('input name="codex_model"', self.html)
        self.assertIn('/api/executor-settings/models?source=codex', self.js)
        self.assertNotIn("Unsaved role changes.", self.js)
        self.assertNotIn("function saveCodexPermission", self.js)
        self.assertIn('scheduleExecutorSettingsSave(executorForm', self.js)
        self.assertIn('persistExecutorSettings(form)', self.js)
        self.assertIn('Selections save automatically', self.html)
        self.assertIn("strategy === \"claude-local\"", self.js)
        self.assertIn('id="delegations"', self.html)
        self.assertIn("function renderDelegations", self.js)
        self.assertIn('text(entry.routing_score, "—") + "/10"', self.js)
        self.assertIn("entry.routing_rationale", self.js)
        self.assertIn("renderDelegations(list(state.delegations))", self.js)


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

    def test_monitor_makes_a_blocked_inactive_goal_explicit(self):
        self.assertIn("No executor is active — this goal is blocked.", self.js)

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
        for literal in (
            '"/api/codex/start"',
            '"/api/codex/stop"',
            '"/api/codex/clear"',
            '"/ws/terminal"',
        ):
            self.assertIn(literal, self.js, "missing endpoint literal: " + literal)
        self.assertIn("new WebSocket", self.js)
        self.assertNotIn("fetch(CODEX_INPUT_URL", self.js)


if __name__ == "__main__":
    unittest.main()
