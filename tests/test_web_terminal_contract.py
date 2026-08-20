"""Focused contract tests for the browser Codex terminal source.

These tests only read the checked-in HTML/JS/CSS/vendor source under
``skills/coordinate-claude-work/assets/web``; they never launch a real
browser, a real Codex session, or a watcher process.
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
    """Codex control/output/input/resize endpoints must be fixed literals."""

    def setUp(self):
        self.js = read(WEB_DIR / "app.js")

    def test_fixed_endpoint_literals_present(self):
        for literal in (
            '"/api/codex/start"',
            '"/api/codex/stop"',
            '"/api/codex/output"',
            '"/api/codex/input"',
            '"/api/codex/resize"',
        ):
            self.assertIn(literal, self.js, "missing endpoint literal: " + literal)

    def test_no_dynamic_endpoint_construction_from_untrusted_input(self):
        # Endpoint URLs should not be built by string concatenation from
        # server payload fields (e.g. command/path); only the output cursor
        # query parameter is appended to a fixed base URL. Check this within
        # the bodies of the functions that actually issue codex endpoint
        # requests, rather than across the entire app.js source.
        forbidden = r"payload\.(command|path|cwd|repo|args)"
        for name in (
            "codexControl",
            "pollCodexOutput",
            "drainCodexInputQueue",
            "sendCodexResize",
        ):
            match = re.search(
                r"function " + name + r"\([^)]*\)\s*\{[\s\S]*?\n\}\n", self.js
            )
            self.assertIsNotNone(match, name + " function not found")
            self.assertNotRegex(match.group(0), forbidden)


class OutputHandlingTests(unittest.TestCase):
    """Cursor advancement and reset/full-replay handling."""

    def setUp(self):
        self.js = read(WEB_DIR / "app.js")

    def test_applies_output_writes_chunk_to_terminal(self):
        self.assertIn("codexTerminal.write(chunk)", self.js)

    def test_reset_flag_resets_terminal_before_replay(self):
        match = re.search(
            r"function applyCodexOutput\([\s\S]*?\n}\n", self.js
        )
        self.assertIsNotNone(match, "applyCodexOutput function not found")
        body = match.group(0)
        reset_index = body.index("codexTerminal.reset()")
        write_index = body.index("codexTerminal.write(chunk)")
        self.assertLess(
            reset_index, write_index, "reset must happen before replay write"
        )

    def test_cursor_advances_from_next_cursor_field(self):
        self.assertIn("codexOutputCursor = nextCursor", self.js)

    def test_start_success_clears_cursor_for_fresh_attachment(self):
        match = re.search(r"function codexControl\([\s\S]*?\n}\n", self.js)
        self.assertIsNotNone(match, "codexControl function not found")
        body = match.group(0)
        self.assertIn("codexOutputCursor = null", body)


class InputSerializationTests(unittest.TestCase):
    """Input must be serialized, queued, and chunked to a 16-KiB bound."""

    def setUp(self):
        self.js = read(WEB_DIR / "app.js")

    def test_input_chunk_size_is_16_kib(self):
        self.assertIn("CODEX_INPUT_CHUNK_CHARS = 16 * 1024", self.js)

    def test_input_is_chunked_before_queueing(self):
        match = re.search(r"function queueCodexInput\([\s\S]*?\n}\n", self.js)
        self.assertIsNotNone(match, "queueCodexInput function not found")
        body = match.group(0)
        self.assertIn("CODEX_INPUT_CHUNK_CHARS", body)
        self.assertIn("codexInputQueue.push", body)

    def test_input_is_drained_serially_one_request_at_a_time(self):
        match = re.search(r"function drainCodexInputQueue\([\s\S]*?\n}\n", self.js)
        self.assertIsNotNone(match, "drainCodexInputQueue function not found")
        body = match.group(0)
        self.assertIn("codexInputInFlight", body)
        # Guards re-entrancy while a request is outstanding.
        self.assertRegex(body, r"if\s*\(\s*codexInputInFlight")


class ResizeTests(unittest.TestCase):
    """Resize requests must be debounced and deduplicated."""

    def setUp(self):
        self.js = read(WEB_DIR / "app.js")

    def test_resize_is_debounced_with_a_timer(self):
        self.assertIn("CODEX_RESIZE_DEBOUNCE_MS", self.js)
        match = re.search(
            r"function scheduleCodexFitAndResize\([\s\S]*?\n}\n", self.js
        )
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

    def test_successful_start_resets_terminal_and_restarts_polling(self):
        match = re.search(r"function codexControl\([\s\S]*?\n}\n", self.js)
        self.assertIsNotNone(match, "codexControl function not found")
        body = match.group(0)
        self.assertIn('kind === "start"', body)
        self.assertIn("codexOutputCursor = null", body)
        self.assertIn("codexTerminal.reset()", body)
        self.assertIn("stopCodexOutputPolling()", body)
        self.assertIn("startCodexOutputPolling()", body)


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
        match = re.search(
            r"function teardownCodexTerminal\([\s\S]*?\n}\n", self.js
        )
        self.assertIsNotNone(match, "teardownCodexTerminal function not found")
        body = match.group(0)
        self.assertIn("window.clearTimeout(codexOutputTimer)", body)
        self.assertIn("window.clearTimeout(codexResizeTimer)", body)
        self.assertIn("codexOutputPollActive = false", body)
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
        options_match = re.search(
            r"var options = \{([\s\S]*?)\};", body
        )
        self.assertIsNotNone(options_match, "control fetch options not found")
        options_body = options_match.group(1)
        self.assertNotIn("body", options_body)

    def test_no_command_args_executable_repo_cwd_or_path_field_sent(self):
        # Scoped to the four Codex request functions only (not the whole
        # source file), since other parts of app.js legitimately reference
        # a "path" field for the unrelated repository picker feature.
        request_functions = [
            "codexControl",
            "pollCodexOutput",
            "drainCodexInputQueue",
            "sendCodexResize",
        ]
        forbidden = re.compile(
            r'["\'](command|args|executable|repo|cwd|path)["\']\s*:'
        )
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
