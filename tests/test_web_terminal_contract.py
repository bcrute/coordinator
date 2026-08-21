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
