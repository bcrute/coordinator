from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "coordinate-claude-work"
sys.path.insert(0, str(SKILL / "scripts"))

from codex_session import CodexSessionManager  # noqa: E402

MARKER = "codex-session-test-fake-marker-4f2c9d"


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _fake_command(*parts: str) -> list[str]:
    # Every injected fake process carries MARKER as argv[0]'s tail via -c
    # script name is not observable in ps, so embed the marker in the
    # script text itself (visible via `ps -f` argument listing on some
    # platforms) and, reliably, keep the marker in the child's own
    # environment-independent argv for our own bookkeeping/tests.
    return [sys.executable, "-c", *parts]


class CodexSessionManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._managers: list[CodexSessionManager] = []

    def tearDown(self) -> None:
        for manager in self._managers:
            manager.shutdown()
        self._managers = []
        # Ensure no fake process bearing our marker remains.
        result = subprocess.run(
            ["pgrep", "-f", MARKER], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        self.assertEqual(
            result.stdout.decode().strip(),
            "",
            f"leaked fake process(es) matching marker: {result.stdout!r}",
        )

    def _make(self, script: str, **kwargs) -> CodexSessionManager:
        manager = CodexSessionManager(
            repo_path=str(ROOT), command=[sys.executable, "-c", script], **kwargs
        )
        self._managers.append(manager)
        return manager

    def test_refuses_construction_without_command(self) -> None:
        with self.assertRaises(ValueError):
            CodexSessionManager(repo_path=str(ROOT), command=[])

    def test_tty_detection_and_output(self) -> None:
        script = (
            f"import sys, os; sys.stderr.write('{MARKER}\\n'); "
            "print('is_tty=' + str(sys.stdout.isatty())); "
            "import time; time.sleep(2)"
        )
        manager = self._make(script)
        manager.start()
        self.assertTrue(_wait_until(lambda: "is_tty=True" in manager.read()["text"]))
        manager.stop()

    def test_input_echo(self) -> None:
        script = (
            f"import sys; sys.stderr.write('{MARKER}\\n'); "
            "line = sys.stdin.readline(); sys.stdout.write('got:' + line); "
            "sys.stdout.flush(); "
            "import time; time.sleep(2)"
        )
        manager = self._make(script)
        manager.start()
        _wait_until(lambda: manager.snapshot()["running"])
        manager.write("hello\n")
        self.assertTrue(_wait_until(lambda: "got:hello" in manager.read()["text"]))
        manager.stop()

    def test_resize_is_observed(self) -> None:
        script = (
            f"import sys, fcntl, termios, struct, signal, time\n"
            f"sys.stderr.write('{MARKER}\\n')\n"
            "sizes = []\n"
            "def handler(signum, frame):\n"
            "    packed = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ, b'\\0' * 8)\n"
            "    rows, cols = struct.unpack('HHHH', packed)[:2]\n"
            "    sys.stdout.write('resized:%d,%d\\n' % (rows, cols))\n"
            "    sys.stdout.flush()\n"
            "signal.signal(signal.SIGWINCH, handler)\n"
            "time.sleep(3)\n"
        )
        manager = self._make(script)
        manager.start()
        _wait_until(lambda: manager.snapshot()["running"])
        time.sleep(0.2)
        manager.resize(30, 100)
        self.assertTrue(_wait_until(lambda: "resized:30,100" in manager.read()["text"]))
        self.assertEqual(manager.snapshot()["rows"], 30)
        self.assertEqual(manager.snapshot()["cols"], 100)
        manager.stop()

    def test_duplicate_start_refused(self) -> None:
        script = f"import sys, time; sys.stderr.write('{MARKER}\\n'); time.sleep(2)"
        manager = self._make(script)
        manager.start()
        _wait_until(lambda: manager.snapshot()["running"])
        with self.assertRaises(RuntimeError):
            manager.start()

    def test_each_start_has_a_distinct_process_activity_scope(self) -> None:
        script = f"import sys, time; sys.stderr.write('{MARKER}\\n'); time.sleep(2)"
        manager = self._make(script)

        manager.start()
        first = manager.snapshot()
        manager.stop()
        manager.start()
        second = manager.snapshot()

        self.assertIsInstance(first["session_id"], str)
        self.assertIsInstance(second["session_id"], str)
        self.assertNotEqual(first["session_id"], second["session_id"])
        self.assertEqual(first["process_activity"]["session_id"], first["session_id"])
        self.assertEqual(second["process_activity"]["session_id"], second["session_id"])
        self.assertEqual(second["process_activity"]["root_pid"], second["pid"])
        manager.stop()

    def test_natural_exit_is_reported_truthfully(self) -> None:
        script = f"import sys; sys.stderr.write('{MARKER}\\n'); sys.exit(7)"
        manager = self._make(script)
        manager.start()
        self.assertTrue(_wait_until(lambda: not manager.snapshot()["running"]))
        snap = manager.snapshot()
        self.assertEqual(snap["state"], "exited")
        self.assertEqual(snap["exit_code"], 7)
        self.assertFalse(snap["can_stop"])
        self.assertTrue(snap["can_start"])

    def test_stale_cursor_reset_and_buffer_bound(self) -> None:
        script = (
            f"import sys, time; sys.stderr.write('{MARKER}\\n')\n"
            "for i in range(50):\n"
            "    sys.stdout.write('x' * 20 + '\\n')\n"
            "    sys.stdout.flush()\n"
            "time.sleep(2)\n"
        )
        manager = self._make(script, max_buffer_chars=200)
        manager.start()
        self.assertTrue(_wait_until(lambda: manager.read()["base_cursor"] > 0))
        result = manager.read(cursor=0)
        self.assertTrue(result["reset"])
        self.assertLessEqual(len(result["text"]), 200)
        current = manager.read()
        result2 = manager.read(cursor=current["next_cursor"])
        self.assertFalse(result2["reset"])
        self.assertEqual(result2["text"], "")
        manager.stop()

    def test_clear_output_discards_history_without_stopping_process(self) -> None:
        script = (
            f"import sys; sys.stderr.write('{MARKER}\\n'); sys.stderr.flush(); "
            "line = sys.stdin.readline(); print('after-clear:' + line, flush=True); "
            "import time; time.sleep(2)"
        )
        manager = self._make(script)
        manager.start()
        self.assertTrue(_wait_until(lambda: MARKER in manager.read()["text"]))

        boundary = manager.clear_output()

        cleared = manager.read()
        self.assertEqual(cleared["text"], "")
        self.assertEqual(cleared["base_cursor"], boundary)
        self.assertEqual(cleared["next_cursor"], boundary)
        self.assertTrue(manager.snapshot()["running"])

        manager.write("new-output\n")
        self.assertTrue(
            _wait_until(lambda: "after-clear:new-output" in manager.read(boundary)["text"])
        )
        replay = manager.read()
        self.assertNotIn(MARKER, replay["text"])
        self.assertIn("after-clear:new-output", replay["text"])
        manager.stop()

    def test_invalid_writes_and_sizes(self) -> None:
        script = f"import sys, time; sys.stderr.write('{MARKER}\\n'); time.sleep(2)"
        manager = self._make(script)
        with self.assertRaises(RuntimeError):
            manager.write("no session yet")
        with self.assertRaises(ValueError):
            manager.write(12345)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            manager.write("x" * (manager._max_write_bytes + 1))
        with self.assertRaises(ValueError):
            manager.resize(0, 10)
        with self.assertRaises(ValueError):
            manager.resize(10, -1)
        with self.assertRaises(ValueError):
            manager.resize(1.5, 10)  # type: ignore[arg-type]
        manager.start()
        _wait_until(lambda: manager.snapshot()["running"])
        with self.assertRaises(ValueError):
            manager.resize(0, 0)
        manager.stop()

    def test_oversize_write_rejected(self) -> None:
        script = f"import sys, time; sys.stderr.write('{MARKER}\\n'); time.sleep(2)"
        manager = self._make(script, max_write_bytes=8)
        manager.start()
        _wait_until(lambda: manager.snapshot()["running"])
        with self.assertRaises(ValueError):
            manager.write("this text is definitely longer than eight bytes")
        manager.stop()

    def test_split_utf8_sequence_is_reassembled(self) -> None:
        # Multi-byte UTF-8 written in two separate stdout flushes must be
        # reassembled by the incremental decoder rather than replaced.
        snowman = "☃"  # E2 98 83
        encoded = snowman.encode("utf-8")
        script = (
            f"import sys, time; sys.stderr.write('{MARKER}\\n')\n"
            f"sys.stdout.buffer.write(bytes([{encoded[0]}, {encoded[1]}]))\n"
            "sys.stdout.buffer.flush()\n"
            "time.sleep(0.3)\n"
            f"sys.stdout.buffer.write(bytes([{encoded[2]}]))\n"
            "sys.stdout.buffer.flush()\n"
            "time.sleep(2)\n"
        )
        manager = self._make(script)
        manager.start()
        self.assertTrue(
            _wait_until(lambda: snowman in manager.read()["text"], timeout=5)
        )
        manager.stop()

    def test_truncated_invalid_utf8_replaced_at_eof(self) -> None:
        # A dangling, never-completed multi-byte prefix must be flushed as a
        # replacement character when the process exits, not silently lost.
        script = (
            f"import sys; sys.stderr.write('{MARKER}\\n')\n"
            "sys.stdout.buffer.write(bytes([0xE2, 0x98]))\n"
            "sys.stdout.buffer.flush()\n"
        )
        manager = self._make(script)
        manager.start()
        self.assertTrue(_wait_until(lambda: not manager.snapshot()["running"]))
        text = manager.read()["text"]
        self.assertIn("�", text)

    def test_process_group_cleanup_on_stop(self) -> None:
        script = (
            f"import sys, os, time, subprocess\n"
            f"sys.stderr.write('{MARKER}\\n')\n"
            f"subprocess.Popen([sys.executable, '-c', "
            f"'import time,sys; sys.stderr.write(\"{MARKER}-child\\\\n\"); time.sleep(30)'])\n"
            "time.sleep(30)\n"
        )
        manager = self._make(script)
        manager.start()
        _wait_until(lambda: manager.snapshot()["running"])
        self.assertTrue(_wait_until(lambda: _pgrep_count() >= 2))
        manager.stop()
        self.assertTrue(_wait_until(lambda: _pgrep_count() == 0))
        snap = manager.snapshot()
        # An explicit stop() always concludes as exactly "stopped".
        self.assertEqual(snap["state"], "stopped")
        self.assertFalse(snap["running"])

    def test_shutdown_is_idempotent_and_disables_start(self) -> None:
        script = f"import sys, time; sys.stderr.write('{MARKER}\\n'); time.sleep(2)"
        manager = self._make(script)
        manager.start()
        _wait_until(lambda: manager.snapshot()["running"])
        manager.shutdown()
        manager.shutdown()
        manager.shutdown()
        self.assertEqual(manager.snapshot()["state"], "shutdown")
        with self.assertRaises(RuntimeError):
            manager.start()

    def test_no_reader_thread_leak_after_stop(self) -> None:
        script = f"import sys, time; sys.stderr.write('{MARKER}\\n'); time.sleep(2)"
        manager = self._make(script)
        manager.start()
        _wait_until(lambda: manager.snapshot()["running"])
        thread = manager._reader_thread
        manager.stop()
        self.assertTrue(
            _wait_until(lambda: thread is not None and not thread.is_alive())
        )

    def test_wait_for_output_wakes_immediately_when_reader_appends(self) -> None:
        script = (
            f"import sys, time; sys.stderr.write('{MARKER}\\n'); "
            "time.sleep(0.2); print('streamed-output', flush=True); time.sleep(2)"
        )
        manager = self._make(script)
        manager.start()
        self.assertTrue(_wait_until(lambda: MARKER in manager.read()["text"]))
        cursor = manager.read()["next_cursor"]
        started = time.monotonic()
        result = manager.wait_for_output(cursor, timeout=2)
        elapsed = time.monotonic() - started
        self.assertIn("streamed-output", result["text"])
        self.assertLess(elapsed, 1.5)
        manager.stop()

    def test_wait_for_output_wakes_when_session_stops(self) -> None:
        script = f"import sys, time; sys.stderr.write('{MARKER}\\n'); time.sleep(30)"
        manager = self._make(script)
        manager.start()
        self.assertTrue(_wait_until(lambda: manager.snapshot()["running"]))
        cursor = manager.read()["next_cursor"]
        result: dict[str, object] = {}

        def wait() -> None:
            result.update(manager.wait_for_output(cursor, timeout=5))

        waiter = threading.Thread(target=wait)
        waiter.start()
        manager.stop()
        waiter.join(timeout=2)
        self.assertFalse(waiter.is_alive())


def _pgrep_count() -> int:
    result = subprocess.run(
        ["pgrep", "-f", MARKER], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    text = result.stdout.decode().strip()
    return 0 if not text else len(text.splitlines())


if __name__ == "__main__":
    unittest.main()
