"""Focused contract tests for the browser Codex terminal source.

These tests only read the checked-in HTML/JS/CSS/vendor source under
``src/coordinator/assets/web``; they never launch a real
browser, a real Codex session, or a watcher process.
"""

import re
import unittest
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parent.parent / "src" / "coordinator" / "assets" / "web"
VENDOR_DIR = WEB_DIR / "vendor"


def read(path):
    return path.read_text(encoding="utf-8")


class VendorAssetTests(unittest.TestCase):
    """The terminal library must be a local, pinned vendor asset."""

    def test_vendor_files_exist(self):
        for name in ("xterm.js", "xterm.css", "addon-fit.js"):
            self.assertTrue(
                (VENDOR_DIR / name).is_file(), "missing vendor file: " + name
            )

    def test_html_references_local_vendor_assets_only(self):
        html = read(WEB_DIR / "index.html")
        self.assertIn("/vendor/xterm.css", html)
        self.assertIn("/vendor/xterm.js", html)
        self.assertIn("/vendor/addon-fit.js", html)
        # No remote CDN references for the terminal library.
        self.assertNotRegex(html.lower(), r"https?://[^\"'\s]*xterm")
        self.assertNotRegex(html.lower(), r"https?://[^\"'\s]*cdn")

    def test_app_js_does_not_fetch_terminal_library_remotely(self):
        js = read(WEB_DIR / "app.js")
        self.assertNotRegex(js.lower(), r"https?://[^\"'\s]*xterm")
        self.assertNotRegex(js.lower(), r"https?://[^\"'\s]*cdn")


class CodexEndpointTests(unittest.TestCase):
    """Codex control routes and terminal socket must be fixed literals."""

    def setUp(self):
        self.js = read(WEB_DIR / "app.js")

    def test_fixed_endpoint_literals_present(self):
        for literal in (
            '"/api/codex/start"',
            '"/api/codex/stop"',
            '"/api/codex/clear"',
            '"/ws/terminal"',
        ):
            self.assertIn(literal, self.js, "missing endpoint literal: " + literal)

    def test_no_dynamic_endpoint_construction_from_untrusted_input(self):
        # Endpoint URLs should not be built by string concatenation from
        # server payload fields (e.g. command/path); only the output cursor
        # Check this within the functions that issue terminal/control traffic.
        forbidden = r"payload\.(command|path|cwd|repo|args)"
        for name in (
            "codexControl",
            "connectCodexSocket",
            "sendCodexResize",
        ):
            match = re.search(
                r"function " + name + r"\([^)]*\)\s*\{[\s\S]*?\n\}\n", self.js
            )
            self.assertIsNotNone(match, name + " function not found")
            self.assertNotRegex(match.group(0), forbidden)


class OutputHandlingTests(unittest.TestCase):
    """Socket output and reset/full-replay handling."""

    def setUp(self):
        self.js = read(WEB_DIR / "app.js")

    def test_applies_output_writes_chunk_to_terminal(self):
        self.assertIn("codexTerminal.write(chunk)", self.js)

    def test_reset_flag_resets_terminal_before_replay(self):
        match = re.search(r"function applyCodexOutput\([\s\S]*?\n}\n", self.js)
        self.assertIsNotNone(match, "applyCodexOutput function not found")
        body = match.group(0)
        reset_index = body.index("codexTerminal.reset()")
        write_index = body.index("codexTerminal.write(chunk)")
        self.assertLess(
            reset_index, write_index, "reset must happen before replay write"
        )

    def test_start_success_resets_terminal_for_fresh_attachment(self):
        match = re.search(r"function codexControl\([\s\S]*?\n}\n", self.js)
        self.assertIsNotNone(match, "codexControl function not found")
        body = match.group(0)
        self.assertIn("codexTerminal.reset()", body)
        self.assertIn("connectCodexSocket()", body)

    def test_clear_uses_server_cursor_before_reconnecting(self):
        control = re.search(r"function codexControl\([\s\S]*?\n}\n", self.js)
        clear = re.search(r"function codexClear\([\s\S]*?\n}\n", self.js)
        self.assertIsNotNone(control, "codexControl function not found")
        self.assertIsNotNone(clear, "codexClear function not found")
        body = control.group(0)
        self.assertIn("payload.cleared_through_cursor", body)
        self.assertIn("closeCodexSocket()", body)
        self.assertIn("codexTerminal.reset()", body)
        self.assertIn("connectCodexSocket()", body)
        self.assertIn('codexControl("clear")', clear.group(0))


class InputSerializationTests(unittest.TestCase):
    """Input must be chunked and sent directly through the socket."""

    def setUp(self):
        self.js = read(WEB_DIR / "app.js")

    def test_input_chunk_size_is_16_kib(self):
        self.assertIn("CODEX_INPUT_CHUNK_CHARS = 16 * 1024", self.js)

    def test_input_is_chunked_before_socket_send(self):
        match = re.search(r"codexTerminal\.onData\([\s\S]*?\n    \}\);", self.js)
        self.assertIsNotNone(match, "terminal input handler not found")
        body = match.group(0)
        self.assertIn("CODEX_INPUT_CHUNK_CHARS", body)
        self.assertIn("codexSocket.send", body)

    def test_input_does_not_use_per_keystroke_http_requests(self):
        self.assertNotIn("fetch(CODEX_INPUT_URL", self.js)
        self.assertNotIn("codexInputInFlight", self.js)


class ClipboardTests(unittest.TestCase):
    """Selected terminal output can be copied without breaking shell interrupts."""

    def setUp(self):
        self.js = read(WEB_DIR / "app.js")
        self.html = read(WEB_DIR / "index.html")

    def test_copy_selection_control_is_available(self):
        self.assertIn('id="codex-terminal-copy"', self.html)
        self.assertIn("copyNode.addEventListener(\"click\", copyCodexSelection)", self.js)

    def test_copy_shortcuts_use_selected_terminal_text(self):
        handler = re.search(
            r"function handleCodexCopyShortcut\([\s\S]*?\n}\n", self.js
        )
        self.assertIsNotNone(handler)
        body = handler.group(0)
        self.assertIn("codexTerminal.hasSelection()", body)
        self.assertIn("event.ctrlKey && event.shiftKey", body)
        self.assertIn("event.metaKey", body)
        self.assertIn("selectedControlC", body)
        self.assertIn("return true", body)
        self.assertIn("return false", body)

    def test_clipboard_api_has_a_legacy_fallback(self):
        self.assertIn("navigator.clipboard.writeText(selection)", self.js)
        self.assertIn('document.execCommand("copy")', self.js)
        self.assertIn(
            "codexTerminal.attachCustomKeyEventHandler(handleCodexCopyShortcut)",
            self.js,
        )


class SessionActivityTests(unittest.TestCase):
    """Managed-session agents and background terminals stay visible and scoped."""

    def setUp(self):
        self.js = read(WEB_DIR / "app.js")
        self.html = read(WEB_DIR / "index.html")

    def test_activity_section_is_immediately_above_terminal(self):
        for element_id in (
            "terminal-activity-summary",
            "terminal-activity-detail",
            "terminal-agents",
            "background-terminals",
        ):
            self.assertIn(f'id="{element_id}"', self.html)
        self.assertLess(
            self.html.index('class="terminal-activity"'),
            self.html.index('id="codex-terminal"'),
        )

    def test_renderer_shows_agent_models_and_background_process_counts(self):
        match = re.search(
            r"function renderTerminalProcessActivity\([\s\S]*?\n}\n", self.js
        )
        self.assertIsNotNone(match, "process activity renderer not found")
        body = match.group(0)
        for field in (
            "process_activity",
            "background_terminals",
            "entry.model",
            "entry.subagent_model",
            "entry.agent_count",
            "entry.process_count",
        ):
            self.assertIn(field, body)
        self.assertNotIn("entry.argv", body)
        self.assertNotIn("entry.environment", body)
        self.assertNotIn("entry.command", body)

    def test_session_updates_always_refresh_process_activity(self):
        match = re.search(r"function applyCodexSession\([\s\S]*?\n}\n", self.js)
        self.assertIsNotNone(match, "session renderer not found")
        self.assertIn("renderTerminalProcessActivity(session)", match.group(0))


class ResizeTests(unittest.TestCase):
    """Resize requests must be debounced and deduplicated."""

    def setUp(self):
        self.js = read(WEB_DIR / "app.js")

    def test_resize_is_debounced_with_a_timer(self):
        self.assertIn("CODEX_RESIZE_DEBOUNCE_MS", self.js)
        match = re.search(r"function scheduleCodexFitAndResize\([\s\S]*?\n}\n", self.js)
        self.assertIsNotNone(match, "scheduleCodexFitAndResize function not found")
        body = match.group(0)
        self.assertIn("window.clearTimeout(codexResizeTimer)", body)
        self.assertIn("window.setTimeout", body)
        self.assertIn("CODEX_RESIZE_DEBOUNCE_MS", body)

    def test_resize_is_deduplicated_against_last_sent_size(self):
        match = re.search(r"function sendCodexResize\([\s\S]*?\n}\n", self.js)
        self.assertIsNotNone(match, "sendCodexResize function not found")
        body = match.group(0)
        self.assertIn("codexLastSentRows", body)
        self.assertIn("codexLastSentCols", body)
        self.assertRegex(
            body,
            r"rows === codexLastSentRows\s*&&\s*cols === codexLastSentCols",
        )


class StartAttachmentTests(unittest.TestCase):
    """A successful Start must produce a fresh output attachment."""

    def setUp(self):
        self.js = read(WEB_DIR / "app.js")

    def test_successful_start_resets_terminal_and_uses_socket(self):
        match = re.search(r"function codexControl\([\s\S]*?\n}\n", self.js)
        self.assertIsNotNone(match, "codexControl function not found")
        body = match.group(0)
        self.assertIn('kind === "start"', body)
        self.assertIn("codexTerminal.reset()", body)
        self.assertIn("connectCodexSocket()", body)
        self.assertNotIn("OutputPolling", body)


class TeardownTests(unittest.TestCase):
    """The page must tear down the terminal session on pagehide."""

    def setUp(self):
        self.js = read(WEB_DIR / "app.js")

    def test_pagehide_listener_invokes_teardown(self):
        match = re.search(r'addEventListener\("pagehide",[\s\S]*?\}\);', self.js)
        self.assertIsNotNone(match, "pagehide listener not found")
        body = match.group(0)
        self.assertIn("teardownCodexTerminal()", body)

    def test_teardown_disposes_timers_observer_and_listener(self):
        match = re.search(r"function teardownCodexTerminal\([\s\S]*?\n}\n", self.js)
        self.assertIsNotNone(match, "teardownCodexTerminal function not found")
        body = match.group(0)
        self.assertIn("closeCodexSocket()", body)
        self.assertIn("window.clearTimeout(codexResizeTimer)", body)
        self.assertIn("codexResizeObserver.disconnect()", body)
        self.assertIn("dispose()", body)


class StartRequestFieldTests(unittest.TestCase):
    """The Start control request must be a fixed, body-less POST."""

    def setUp(self):
        self.js = read(WEB_DIR / "app.js")

    def test_codex_control_request_has_no_body(self):
        match = re.search(r"function codexControl\([\s\S]*?\n}\n", self.js)
        self.assertIsNotNone(match, "codexControl function not found")
        body = match.group(0)
        options_match = re.search(r"var options = \{([\s\S]*?)\};", body)
        self.assertIsNotNone(options_match, "control fetch options not found")
        options_body = options_match.group(1)
        self.assertNotIn("body", options_body)

    def test_no_command_args_executable_repo_cwd_or_path_field_sent(self):
        # Scoped to the Codex transport functions only (not the whole
        # source file), since other parts of app.js legitimately reference
        # a "path" field for the unrelated repository picker feature.
        request_functions = [
            "codexControl",
            "connectCodexSocket",
            "sendCodexResize",
        ]
        forbidden = re.compile(r'["\'](command|args|executable|repo|cwd|path)["\']\s*:')
        for name in request_functions:
            match = re.search(r"function " + name + r"\([\s\S]*?\n}\n", self.js)
            self.assertIsNotNone(match, name + " function not found")
            body = match.group(0)
            self.assertIsNone(
                forbidden.search(body),
                name
                + " request body must not name command/args/executable/repo/cwd/path",
            )


if __name__ == "__main__":
    unittest.main()
