"""Behavioral coverage for subprocess ownership and forced cleanup."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from coordinator.process_guard import guarded_command


def process_running(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[2]
    except (FileNotFoundError, OSError):
        return False
    return state != "Z"


class ProcessGuardTests(unittest.TestCase):
    def wait_until_stopped(self, pid: int, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and process_running(pid):
            time.sleep(0.05)
        self.assertFalse(process_running(pid), f"process {pid} survived its owner")

    def test_guard_returns_the_child_exit_status(self) -> None:
        completed = subprocess.run(
            guarded_command([sys.executable, "-c", "raise SystemExit(7)"]),
            check=False,
        )
        self.assertEqual(completed.returncode, 7)

    @unittest.skipUnless(sys.platform.startswith("linux"), "uses Linux process inspection")
    def test_child_tree_terminates_when_guard_parent_is_killed(self) -> None:
        child_source = (
            "import os, pathlib, subprocess, sys, time; "
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
            "grandchild = subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(300)']); "
            "pathlib.Path(sys.argv[2]).write_text(str(grandchild.pid)); "
            "time.sleep(300)"
        )
        parent_source = (
            "import subprocess, sys, time; "
            "from coordinator.process_guard import guarded_command; "
            "subprocess.Popen(guarded_command([sys.executable, '-c', sys.argv[2], "
            "sys.argv[1], sys.argv[3]])); time.sleep(300)"
        )
        with tempfile.TemporaryDirectory() as directory:
            pid_path = Path(directory) / "child.pid"
            grandchild_path = Path(directory) / "grandchild.pid"
            parent = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    parent_source,
                    str(pid_path),
                    child_source,
                    str(grandchild_path),
                ]
            )
            child_pid = 0
            grandchild_pid = 0
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not grandchild_path.exists():
                    time.sleep(0.05)
                self.assertTrue(pid_path.exists(), "guarded child never started")
                self.assertTrue(grandchild_path.exists(), "guarded grandchild never started")
                child_pid = int(pid_path.read_text(encoding="utf-8"))
                grandchild_pid = int(grandchild_path.read_text(encoding="utf-8"))
                self.assertTrue(process_running(child_pid))
                self.assertTrue(process_running(grandchild_pid))
                parent.kill()
                parent.wait(timeout=5)
                self.wait_until_stopped(child_pid)
                self.wait_until_stopped(grandchild_pid)
            finally:
                if parent.poll() is None:
                    parent.kill()
                    parent.wait(timeout=5)
                if child_pid and process_running(child_pid):
                    os.kill(child_pid, signal.SIGKILL)
                if grandchild_pid and process_running(grandchild_pid):
                    os.kill(grandchild_pid, signal.SIGKILL)

    @unittest.skipUnless(sys.platform.startswith("linux"), "uses Linux process inspection")
    def test_session_escaping_descendant_terminates_with_guard_owner(self) -> None:
        child_source = (
            "import pathlib, subprocess, sys, time; "
            "escaped = subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(300)'], start_new_session=True); "
            "pathlib.Path(sys.argv[1]).write_text(str(escaped.pid)); time.sleep(300)"
        )
        parent_source = (
            "import subprocess, sys, time; "
            "from coordinator.process_guard import guarded_command; "
            "subprocess.Popen(guarded_command([sys.executable, '-c', sys.argv[2], "
            "sys.argv[1]])); time.sleep(300)"
        )
        with tempfile.TemporaryDirectory() as directory:
            pid_path = Path(directory) / "escaped.pid"
            parent = subprocess.Popen(
                [sys.executable, "-c", parent_source, str(pid_path), child_source]
            )
            escaped_pid = 0
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not pid_path.exists():
                    time.sleep(0.05)
                self.assertTrue(pid_path.exists(), "escaped descendant never started")
                escaped_pid = int(pid_path.read_text(encoding="utf-8"))
                self.assertTrue(process_running(escaped_pid))
                parent.kill()
                parent.wait(timeout=5)
                self.wait_until_stopped(escaped_pid)
            finally:
                if parent.poll() is None:
                    parent.kill()
                    parent.wait(timeout=5)
                if escaped_pid and process_running(escaped_pid):
                    os.kill(escaped_pid, signal.SIGKILL)

    @unittest.skipUnless(sys.platform.startswith("linux"), "uses Linux process inspection")
    def test_session_escaping_descendant_gets_term_cleanup_grace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cleanup = root / "cleanup"
            pid_path = root / "escaped.pid"
            escaped_script = root / "escaped.py"
            escaped_script.write_text(
                "import os, signal, sys, time\n"
                "from pathlib import Path\n"
                "def stop(*_):\n"
                "    time.sleep(0.3)\n"
                "    Path(sys.argv[1]).write_text('clean')\n"
                "    os._exit(0)\n"
                "signal.signal(signal.SIGTERM, stop)\n"
                "time.sleep(300)\n",
                encoding="utf-8",
            )
            child_script = root / "child.py"
            child_script.write_text(
                "import subprocess, sys, time\n"
                "from pathlib import Path\n"
                "child = subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]], "
                "start_new_session=True)\n"
                "Path(sys.argv[3]).write_text(str(child.pid))\n"
                "time.sleep(300)\n",
                encoding="utf-8",
            )
            parent_source = (
                "import subprocess, sys, time; "
                "from coordinator.process_guard import guarded_command; "
                "subprocess.Popen(guarded_command([sys.executable, sys.argv[1], "
                "sys.argv[2], sys.argv[3], sys.argv[4]])); time.sleep(300)"
            )
            parent = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    parent_source,
                    str(child_script),
                    str(escaped_script),
                    str(cleanup),
                    str(pid_path),
                ]
            )
            escaped_pid = 0
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not pid_path.exists():
                    time.sleep(0.05)
                self.assertTrue(pid_path.exists(), "escaped descendant never started")
                escaped_pid = int(pid_path.read_text(encoding="utf-8"))
                parent.kill()
                parent.wait(timeout=5)
                self.wait_until_stopped(escaped_pid)
                self.assertEqual(cleanup.read_text(encoding="utf-8"), "clean")
            finally:
                if parent.poll() is None:
                    parent.kill()
                    parent.wait(timeout=5)
                if escaped_pid and process_running(escaped_pid):
                    os.kill(escaped_pid, signal.SIGKILL)


if __name__ == "__main__":
    unittest.main()
