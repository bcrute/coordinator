"""Focused contract tests for the browser repository picker source.

These tests only read the checked-in HTML/JS source under
``skills/coordinate-claude-work/assets/web``; they never launch a real
browser, watcher, Codex, or Claude process.
"""

import re
import unittest
from pathlib import Path

WEB_DIR = (
    Path(__file__).resolve().parent.parent
    / "skills"
    / "coordinate-claude-work"
    / "assets"
    / "web"
)

ROUTES = [
    "monitor",
    "terminal",
    "work",
    "agents",
    "logs",
    "activity",
    "setup",
    "sessions",
    "diagnostics",
]


def read(path):
    return path.read_text(encoding="utf-8")


class MarkupTests(unittest.TestCase):
    """The picker must be a labeled select with feedback and a root hint."""

    def setUp(self):
        self.html = read(WEB_DIR / "index.html")

    def test_select_present_with_label(self):
        self.assertIn('<select id="repository-select"', self.html)
        self.assertIn('for="repository-select"', self.html)

    def test_select_starts_disabled(self):
        match = re.search(r'<select id="repository-select"[^>]*>', self.html)
        self.assertIsNotNone(match)
        self.assertIn("disabled", match.group(0))

    def test_live_feedback_element_present(self):
        self.assertIn('id="repository-select-feedback"', self.html)
        self.assertIn('aria-live="polite"', self.html)

    def test_catalog_root_hint_present(self):
        self.assertIn('id="repository-catalog-root"', self.html)

    def test_navigation_routes_still_present(self):
        # The picker must not remove any existing view/route.
        for route in ROUTES:
            self.assertIn('id="view-' + route + '"', self.html)
            self.assertIn('id="nav-' + route + '"', self.html)


class EndpointTests(unittest.TestCase):
    """The select request must target the fixed endpoint with an exact body."""

    def setUp(self):
        self.js = read(WEB_DIR / "app.js")

    def test_fixed_endpoint_literal_present(self):
        self.assertIn('"/api/repository/select"', self.js)

    def test_request_body_is_exact_path_object(self):
        self.assertIn("JSON.stringify({ path: path })", self.js)

    def test_request_uses_json_headers_no_store_and_same_origin(self):
        function_match = re.search(
            r"function selectRepository\(path\) \{.*?\n\}\n", self.js, re.S
        )
        self.assertIsNotNone(function_match)
        body = function_match.group(0)
        self.assertIn('"Content-Type": "application/json"', body)
        self.assertIn('Accept: "application/json"', body)
        self.assertIn('cache: "no-store"', body)
        self.assertIn('credentials: "same-origin"', body)
        self.assertIn('method: "POST"', body)

    def test_no_dynamic_endpoint_construction_from_untrusted_input(self):
        self.assertNotRegex(self.js, r"payload\.(command|args|cwd)")


class CatalogOptionTests(unittest.TestCase):
    """Options must be built only from server-provided catalog entries."""

    def setUp(self):
        self.js = read(WEB_DIR / "app.js")

    def test_entries_read_from_repository_catalog_field(self):
        self.assertIn("record(state.repository_catalog)", self.js)
        self.assertIn("list(catalog.entries)", self.js)

    def test_option_value_and_label_use_entry_path_and_name(self):
        function_match = re.search(
            r"function applyRepositoryCatalog\(catalog\) \{.*?\n\}\n", self.js, re.S
        )
        self.assertIsNotNone(function_match)
        body = function_match.group(0)
        self.assertIn("opt.value = path", body)
        self.assertIn("opt.textContent = text(entry.name, path)", body)
        self.assertIn("opt.title = path", body)

    def test_select_value_synced_to_active(self):
        self.assertIn("select.value = active", self.js)


class SelectionOutcomeTests(unittest.TestCase):
    """selected/unchanged/error outcomes must be handled distinctly."""

    def setUp(self):
        self.js = read(WEB_DIR / "app.js")

    def test_selected_outcome_resets_terminal_client_state(self):
        self.assertIn("resetTerminalClientStateForSwitch()", self.js)
        function_match = re.search(
            r"function resetTerminalClientStateForSwitch\(\) \{.*?\n\}\n", self.js, re.S
        )
        self.assertIsNotNone(function_match)
        body = function_match.group(0)
        self.assertIn("closeCodexSocket()", body)
        self.assertIn("codexTerminal.reset()", body)
        self.assertIn("codexLastSentRows = null", body)
        self.assertIn("codexLastSentCols = null", body)
        self.assertIn("renderedLog = null", body)

    def test_unchanged_outcome_does_not_reset_terminal(self):
        function_match = re.search(
            r"function selectRepository\(path\) \{.*?\n\}\n", self.js, re.S
        )
        self.assertIsNotNone(function_match)
        body = function_match.group(0)
        unchanged_branch = re.search(
            r'outcome === "unchanged"\) \{(.*?)\} else', body, re.S
        )
        self.assertIsNotNone(unchanged_branch)
        self.assertNotIn("resetTerminalClientStateForSwitch", unchanged_branch.group(1))

    def test_failure_restores_previous_active_selection(self):
        function_match = re.search(
            r"function selectRepository\(path\) \{.*?\n\}\n", self.js, re.S
        )
        self.assertIsNotNone(function_match)
        body = function_match.group(0)
        self.assertIn("var previousActive = repositoryCatalog.active", body)
        self.assertIn("select.value = previousActive", body)

    def test_change_handler_ignores_unchanged_or_empty_value(self):
        self.assertIn(
            'if (value === "" || value === repositoryCatalog.active) {', self.js
        )


class StaleResponseInvalidationTests(unittest.TestCase):
    """A repository switch must replace the prior state stream."""

    def setUp(self):
        self.js = read(WEB_DIR / "app.js")

    def test_state_epoch_variable_declared(self):
        self.assertIn("var stateEpoch = 0;", self.js)

    def test_selection_advances_epoch_before_request(self):
        function_match = re.search(
            r"function selectRepository\(path\) \{.*?\n\}\n", self.js, re.S
        )
        self.assertIsNotNone(function_match)
        body = function_match.group(0)
        self.assertIn("stateEpoch += 1", body)
        self.assertIn("var myEpoch = stateEpoch", body)

    def test_switch_stops_old_feed_before_request(self):
        function_match = re.search(
            r"function selectRepository\(path\) \{.*?\n\}\n", self.js, re.S
        )
        self.assertIsNotNone(function_match)
        body = function_match.group(0)
        self.assertLess(body.index("stopStateFeed()"), body.index("fetch("))


class ControlDisablingTests(unittest.TestCase):
    """Watcher and Codex controls plus the selector must disable while switching."""

    def setUp(self):
        self.js = read(WEB_DIR / "app.js")

    def test_repository_switching_variable_declared(self):
        self.assertIn("var repositorySwitching = false;", self.js)

    def test_watcher_controls_disabled_while_switching(self):
        function_match = re.search(
            r"function paintControls\(\) \{.*?\n\}\n", self.js, re.S
        )
        self.assertIsNotNone(function_match)
        self.assertIn("repositorySwitching", function_match.group(0))

    def test_codex_controls_disabled_while_switching(self):
        function_match = re.search(
            r"function paintCodexControls\(\) \{.*?\n\}\n", self.js, re.S
        )
        self.assertIsNotNone(function_match)
        self.assertIn("repositorySwitching", function_match.group(0))

    def test_selector_disabled_while_switching(self):
        function_match = re.search(
            r"function paintRepositorySelector\(\) \{.*?\n\}\n", self.js, re.S
        )
        self.assertIsNotNone(function_match)
        self.assertIn("repositorySwitching", function_match.group(0))

    def test_selector_disabled_considers_both_pending_control_flags(self):
        function_match = re.search(
            r"function paintRepositorySelector\(\) \{.*?\n\}\n", self.js, re.S
        )
        self.assertIsNotNone(function_match)
        body = function_match.group(0)
        self.assertIn('pendingControl !== ""', body)
        self.assertIn('codexPendingControl !== ""', body)


class TerminalResetGenerationTests(unittest.TestCase):
    """Reset must close the old repository's terminal socket."""

    def setUp(self):
        self.js = read(WEB_DIR / "app.js")

    def test_reset_closes_socket_and_has_no_http_input_queue(self):
        function_match = re.search(
            r"function resetTerminalClientStateForSwitch\(\) \{.*?\n\}\n", self.js, re.S
        )
        self.assertIsNotNone(function_match)
        body = function_match.group(0)
        self.assertIn("closeCodexSocket()", body)
        self.assertNotIn("codexInputQueue", body)


class CodexSocketReplacementTests(unittest.TestCase):
    """Events from a replaced terminal socket must be ignored."""

    def setUp(self):
        self.js = read(WEB_DIR / "app.js")

    def test_socket_is_recorded_as_current(self):
        function_match = re.search(
            r"function connectCodexSocket\(\) \{.*?\n\}\n", self.js, re.S
        )
        self.assertIsNotNone(function_match)
        self.assertIn("codexSocket = socket", function_match.group(0))

    def test_open_and_message_handlers_ignore_replaced_socket(self):
        function_match = re.search(
            r"function connectCodexSocket\(\) \{.*?\n\}\n", self.js, re.S
        )
        self.assertIsNotNone(function_match)
        body = function_match.group(0)
        self.assertGreaterEqual(body.count("socket !== codexSocket"), 2)

    def test_close_handler_reconnects_current_socket(self):
        self.assertIn(
            "codexSocketTimer = window.setTimeout(connectCodexSocket, 1000)", self.js
        )


class StateFeedRestartTests(unittest.TestCase):
    """State feed replacement closes the old EventSource first."""

    def setUp(self):
        self.js = read(WEB_DIR / "app.js")

    def test_stop_closes_and_clears_source(self):
        function_match = re.search(
            r"function stopStateFeed\(\) \{.*?\n\}\n", self.js, re.S
        )
        self.assertIsNotNone(function_match)
        body = function_match.group(0)
        self.assertIn("stateSource.close()", body)
        self.assertIn("stateSource = null", body)

    def test_restart_stops_then_starts(self):
        function_match = re.search(
            r"function restartStateFeed\(\) \{.*?\n\}\n", self.js, re.S
        )
        self.assertIsNotNone(function_match)
        body = function_match.group(0)
        self.assertLess(
            body.index("stopStateFeed()"), body.index("\n  startStateFeed()")
        )


class AppJsByteHygieneTests(unittest.TestCase):
    """app.js must not contain literal NUL or SOH control bytes."""

    def test_no_nul_or_soh_bytes(self):
        raw = (WEB_DIR / "app.js").read_bytes()
        self.assertNotIn(b"\x00", raw)
        self.assertNotIn(b"\x01", raw)


class LayoutStickyStaticTests(unittest.TestCase):
    """The topbar stays sticky; the views nav is static with no fixed top."""

    def setUp(self):
        self.css = read(WEB_DIR / "app.css")

    def test_topbar_is_sticky(self):
        match = re.search(r"\.topbar\s*\{[^}]*\}", self.css)
        self.assertIsNotNone(match)
        self.assertIn("position: sticky", match.group(0))

    def test_views_nav_is_static_without_fixed_top(self):
        match = re.search(r"\.views-nav\s*\{[^}]*\}", self.css)
        self.assertIsNotNone(match)
        body = match.group(0)
        self.assertIn("position: static", body)
        self.assertNotRegex(body, r"\btop:\s*0")


class FreshSnapshotAndRoutePreservationTests(unittest.TestCase):
    """A successful switch requests a fresh snapshot without touching the hash."""

    def setUp(self):
        self.js = read(WEB_DIR / "app.js")

    def test_state_feed_restarted_after_switch_completes(self):
        function_match = re.search(
            r"function selectRepository\(path\) \{.*?\n\}\n", self.js, re.S
        )
        self.assertIsNotNone(function_match)
        self.assertIn("restartStateFeed()", function_match.group(0))

    def test_select_function_never_touches_location_hash(self):
        function_match = re.search(
            r"function selectRepository\(path\) \{.*?\n\}\n", self.js, re.S
        )
        self.assertIsNotNone(function_match)
        self.assertNotIn("location.hash", function_match.group(0))

    def test_routes_array_still_has_expected_routes_in_order(self):
        match = re.search(r"var ROUTES\s*=\s*\[([^\]]*)\];", self.js)
        self.assertIsNotNone(match)
        found = re.findall(r'["\']([a-z]+)["\']', match.group(1))
        self.assertEqual(found, ROUTES)


class SetupNeededLabelTests(unittest.TestCase):
    """Uninitialized picker entries must be visibly distinct but keep the exact path."""

    def setUp(self):
        self.js = read(WEB_DIR / "app.js")

    def test_signature_includes_initialized_flag(self):
        # The option-rebuild signature must include entry.initialized so labels
        # update live on a later poll without requiring a reload or switch.
        signature_block = re.search(
            r"var signature = entries.*?\.join\(\"\\u0001\"\);",
            self.js,
            re.S,
        )
        self.assertIsNotNone(signature_block)
        self.assertIn("entry.initialized === true", signature_block.group(0))

    def test_setup_needed_suffix_appended_only_when_uninitialized(self):
        self.assertIn("if (entry.initialized !== true) {", self.js)
        self.assertIn("setup needed", self.js)

    def test_option_value_and_title_remain_exact_path_regardless_of_label(self):
        option_block = re.search(
            r"entries\.map\(function \(entry\) \{\s*var path = text\(entry\.path.*?return opt;\s*\}\)",
            self.js,
            re.S,
        )
        self.assertIsNotNone(option_block)
        block = option_block.group(0)
        self.assertIn("opt.value = path;", block)
        self.assertIn("opt.title = path;", block)


class OnboardingWorkflowTests(unittest.TestCase):
    """When coordination is absent, Monitor must show setup guidance, not stale data."""

    def setUp(self):
        self.js = read(WEB_DIR / "app.js")
        match = re.search(
            r"function renderWorkflow\(state\) \{.*?\n\}\n", self.js, re.S
        )
        self.assertIsNotNone(match)
        self.function_text = match.group(0)

    def test_checks_coordination_present_before_normal_rendering(self):
        self.assertIn("state.coordination_present !== true", self.function_text)

    def test_onboarding_branch_mentions_terminal_and_codex_and_goal(self):
        # Only inspect the early-return branch, not the whole function.
        branch = self.function_text.split("state.coordination_present !== true")[1]
        branch = branch.split("return;")[0]
        self.assertIn("Terminal", branch)
        self.assertIn("Codex", branch)
        self.assertIn("goal", branch.lower())
        self.assertIn('coordinated " +\n        "work', branch)

    def test_onboarding_branch_hides_stale_completion(self):
        branch = self.function_text.split("state.coordination_present !== true")[1]
        branch = branch.split("return;")[0]
        self.assertIn("onboardingNode.hidden = true", branch)

    def test_onboarding_branch_returns_early(self):
        segments = self.function_text.split("state.coordination_present !== true")
        self.assertEqual(len(segments), 2)
        self.assertIn("return;", segments[1].split('setText(\n    "workflow-phase"')[0])

    def test_normal_branch_still_unhides_current_completion(self):
        # After the early-return guard, the normal path must still control
        # visibility from workflow.completion_current on later initialized polls.
        after_guard = self.function_text.split("return;\n  }")[-1]
        self.assertIn("node.hidden = !current;", after_guard)


class ActiveRepositoryReadinessTests(unittest.TestCase):
    """After a normal render, header feedback must state active-repo readiness."""

    def setUp(self):
        self.js = read(WEB_DIR / "app.js")

    def test_render_calls_readiness_report(self):
        render_match = re.search(r"function render\(state\) \{.*?\n\}\n", self.js, re.S)
        self.assertIsNotNone(render_match)
        self.assertIn("reportActiveRepositoryReadiness(state)", render_match.group(0))

    def test_readiness_function_skips_while_switching(self):
        function_match = re.search(
            r"function reportActiveRepositoryReadiness\(state\) \{.*?\n\}\n",
            self.js,
            re.S,
        )
        self.assertIsNotNone(function_match)
        body = function_match.group(0)
        self.assertIn("if (repositorySwitching) {", body)
        self.assertIn("return;", body)

    def test_readiness_function_uses_only_server_coordination_present_field(self):
        function_match = re.search(
            r"function reportActiveRepositoryReadiness\(state\) \{.*?\n\}\n",
            self.js,
            re.S,
        )
        self.assertIsNotNone(function_match)
        body = function_match.group(0)
        self.assertIn("state.coordination_present === true", body)
        self.assertIn("repositoryReport(", body)


class SyntaxTests(unittest.TestCase):
    def test_app_js_is_valid_javascript_via_node(self):
        import shutil
        import subprocess

        node = shutil.which("node")
        if node is None:
            self.skipTest("node is not available in this environment")
        result = subprocess.run(
            [node, "--check", str(WEB_DIR / "app.js")],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
