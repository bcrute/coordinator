#!/usr/bin/env python3.14
"""Thread-safe PTY manager for one trusted interactive Codex CLI process.

`CodexSessionManager` owns a single Unix pseudo-terminal bound to one fixed
repository path. Its new-session command and optional resume command are supplied
by trusted application configuration and can be replaced for a future launch.
HTTP wiring is deliberately out of scope; this module only manages the process
lifecycle, PTY I/O, and a bounded output buffer addressed by absolute character
cursors.
"""

from __future__ import annotations

import codecs
import errno
import fcntl
import os
import pty
import secrets
import signal
import struct
import subprocess
import termios
import threading
import time
from typing import Sequence

from .process_activity import ProcessActivityObserver
from .process_guard import guarded_command

DEFAULT_MAX_BUFFER_CHARS = 1_000_000
DEFAULT_MAX_WRITE_BYTES = 64 * 1024
DEFAULT_ROWS = 24
DEFAULT_COLS = 80
READ_CHUNK_BYTES = 4096
TERMINATE_GRACE_SECONDS = 2.0
JOIN_TIMEOUT_SECONDS = 5.0

STATE_NOT_STARTED = "not_started"
STATE_RUNNING = "running"
STATE_EXITED = "exited"
STATE_STOPPED = "stopped"
STATE_ERROR = "error"
STATE_SHUTDOWN = "shutdown"


class CodexSessionManager:
    """Owns the lifecycle of one fixed-command PTY session.

    The repository path and command are fixed at construction; no method
    accepts a caller-selected command or path. All public methods are
    thread-safe.
    """

    def __init__(
        self,
        repo_path: str,
        command: Sequence[str],
        *,
        resume_command: Sequence[str] | None = None,
        max_buffer_chars: int = DEFAULT_MAX_BUFFER_CHARS,
        max_write_bytes: int = DEFAULT_MAX_WRITE_BYTES,
        initial_rows: int = DEFAULT_ROWS,
        initial_cols: int = DEFAULT_COLS,
        process_observer: ProcessActivityObserver | None = None,
    ) -> None:
        if not command:
            raise ValueError("command must be a non-empty sequence")
        if resume_command is not None and not resume_command:
            raise ValueError("resume_command must be non-empty when provided")
        if max_buffer_chars <= 0:
            raise ValueError("max_buffer_chars must be positive")
        if max_write_bytes <= 0:
            raise ValueError("max_write_bytes must be positive")
        if initial_rows <= 0 or initial_cols <= 0:
            raise ValueError("initial_rows and initial_cols must be positive")

        self._repo_path = os.path.abspath(repo_path)
        self._command = tuple(command)
        self._resume_command = (
            tuple(resume_command) if resume_command is not None else None
        )
        self._active_command = self._command
        self._max_buffer_chars = max_buffer_chars
        self._max_write_bytes = max_write_bytes

        self._lock = threading.RLock()
        self._output_changed = threading.Condition(self._lock)
        self._state = STATE_NOT_STARTED
        self._pid: int | None = None
        self._process: subprocess.Popen | None = None
        self._master_fd: int | None = None
        self._reader_thread: threading.Thread | None = None
        self._started_at: float | None = None
        self._ended_at: float | None = None
        self._exit_code: int | None = None
        self._detail = "session has not been started"
        self._rows = initial_rows
        self._cols = initial_cols
        self._process_observer = process_observer or ProcessActivityObserver()
        self._session_id: str | None = None

        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._buffer = ""
        self._buffer_base_cursor = 0
        self._buffer_next_cursor = 0

        self._shutdown = False
        self._stop_requested = False

    # -- read-only fixed properties -------------------------------------

    @property
    def repo_path(self) -> str:
        return self._repo_path

    @property
    def command(self) -> tuple[str, ...]:
        return self._command

    @property
    def resume_command(self) -> tuple[str, ...] | None:
        return self._resume_command

    def configure_commands(
        self,
        command: Sequence[str],
        *,
        resume_command: Sequence[str] | None = None,
    ) -> bool:
        """Set trusted commands for the next launch without disturbing a live PTY.

        Returns true when no session is running and the new command is therefore
        immediately visible as the active command. While a session is running its
        observed command remains truthful; the replacement is used after it stops.
        """

        if not command:
            raise ValueError("command must be a non-empty sequence")
        if resume_command is not None and not resume_command:
            raise ValueError("resume_command must be non-empty when provided")
        replacement = tuple(command)
        replacement_resume = (
            tuple(resume_command) if resume_command is not None else None
        )
        with self._lock:
            if self._shutdown:
                raise RuntimeError("session manager has been shut down")
            self._command = replacement
            self._resume_command = replacement_resume
            immediate = self._state != STATE_RUNNING
            if immediate:
                self._active_command = replacement
            return immediate

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        """Launch the fixed new-session command."""
        self._launch(self._command, "process started")

    def resume(self) -> None:
        """Launch the fixed resume command, if one was configured."""
        if self._resume_command is None:
            raise RuntimeError("session resume is not available")
        self._launch(self._resume_command, "previous session resume started")

    def _launch(self, command: tuple[str, ...], detail: str) -> None:
        """Launch one construction-time command in a new PTY and process group.

        Refuses to start twice: if a session is already running, or the
        manager has been shut down, raises RuntimeError with a truthful
        message instead of silently succeeding.
        """
        with self._lock:
            if self._shutdown:
                raise RuntimeError("session manager has been shut down")
            if self._state == STATE_RUNNING:
                raise RuntimeError("session is already running")

            master_fd, slave_fd = pty.openpty()
            try:
                process = subprocess.Popen(
                    guarded_command(command),
                    cwd=self._repo_path,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    start_new_session=True,
                    close_fds=True,
                )
            except Exception:
                os.close(slave_fd)
                os.close(master_fd)
                raise
            # The child has its own duplicated copy of the slave fd; the
            # parent must close its copy so EOF is observable when the
            # child exits.
            os.close(slave_fd)

            self._process = process
            self._pid = process.pid
            self._session_id = secrets.token_urlsafe(12)
            self._active_command = command
            self._master_fd = master_fd
            self._started_at = time.time()
            self._ended_at = None
            self._exit_code = None
            self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            self._buffer = ""
            self._buffer_base_cursor = 0
            self._buffer_next_cursor = 0
            self._stop_requested = False
            self._state = STATE_RUNNING
            self._detail = detail
            self._output_changed.notify_all()

            self._set_winsize_locked(self._rows, self._cols)

            self._reader_thread = threading.Thread(
                target=self._reader_loop, args=(master_fd, process), daemon=True
            )
            self._reader_thread.start()

    def stop(self) -> None:
        """Terminate the running session's process group and reap it.

        Idempotent: stopping when nothing is running is a truthful no-op
        that leaves state unchanged rather than raising. An explicit stop
        always concludes as state `stopped` with the observed exit code
        (as reported by the reader thread, which is the sole waiter on the
        child process); a session that exits on its own without a stop
        request remains `exited`. If the reader thread cannot be reaped
        even after SIGKILL, the final state is truthfully `error`.
        """
        with self._lock:
            if self._state != STATE_RUNNING:
                return
            process = self._process
            thread = self._reader_thread
            self._stop_requested = True

        if process is None or thread is None:
            with self._lock:
                self._stop_requested = False
            return

        self._signal_process_group(process.pid, signal.SIGTERM)
        thread.join(timeout=TERMINATE_GRACE_SECONDS)
        if thread.is_alive():
            self._signal_process_group(process.pid, signal.SIGKILL)
            thread.join(timeout=JOIN_TIMEOUT_SECONDS)

        with self._lock:
            self._stop_requested = False
            if thread.is_alive():
                self._state = STATE_ERROR
                self._detail = "reader thread failed to terminate after SIGKILL"
            # Otherwise the reader thread already recorded the final
            # (stopped/exited) state before it finished.

    def shutdown(self) -> None:
        """Stop any running session and permanently disable further starts.

        Idempotent: calling repeatedly performs no duplicate work and never
        raises.
        """
        with self._lock:
            if self._shutdown:
                return
            self._shutdown = True
            self._output_changed.notify_all()
        self.stop()
        with self._lock:
            if self._state != STATE_ERROR:
                self._state = STATE_SHUTDOWN
                self._detail = "manager shut down"

    # -- I/O ----------------------------------------------------------

    def write(self, text: str) -> int:
        """Write `text` (encoded as UTF-8) to the session's PTY stdin.

        Raises ValueError for non-string input or text that encodes to more
        than `max_write_bytes`. Raises RuntimeError if no session is
        currently running. Returns the number of bytes written.
        """
        if not isinstance(text, str):
            raise ValueError("text must be a str")
        data = text.encode("utf-8", errors="replace")
        if len(data) > self._max_write_bytes:
            raise ValueError(
                f"write of {len(data)} bytes exceeds max_write_bytes "
                f"({self._max_write_bytes})"
            )
        if not data:
            return 0
        with self._lock:
            if self._state != STATE_RUNNING or self._master_fd is None:
                raise RuntimeError("no running session to write to")
            fd = self._master_fd
        total = 0
        try:
            while total < len(data):
                total += os.write(fd, data[total:])
        except OSError as exc:
            raise RuntimeError(f"write failed: {exc}") from exc
        return total

    def resize(self, rows: int, cols: int) -> None:
        """Validate and apply a new terminal size via TIOCSWINSZ."""
        if not isinstance(rows, int) or not isinstance(cols, int):
            raise ValueError("rows and cols must be integers")
        if isinstance(rows, bool) or isinstance(cols, bool):
            raise ValueError("rows and cols must be integers")
        if rows <= 0 or cols <= 0:
            raise ValueError("rows and cols must be positive")
        if rows > 10_000 or cols > 10_000:
            raise ValueError("rows and cols must be reasonable terminal sizes")
        with self._lock:
            self._rows = rows
            self._cols = cols
            if self._state == STATE_RUNNING and self._master_fd is not None:
                self._set_winsize_locked(rows, cols)
                if self._process is not None:
                    self._signal_process_group(self._process.pid, signal.SIGWINCH)

    def read(self, cursor: int | None = None) -> dict[str, object]:
        """Return buffered output at or after `cursor`.

        Returns a dict with keys `text`, `next_cursor`, `base_cursor`, and
        `reset`. If `cursor` is None or falls before the retained buffer's
        base cursor, `reset` is True and the full retained buffer is
        returned starting at `base_cursor` so callers can detect loss.
        """
        with self._lock:
            base = self._buffer_base_cursor
            nxt = self._buffer_next_cursor
            buf = self._buffer
            if cursor is None or cursor < base or cursor > nxt:
                text = buf
                return {
                    "text": text,
                    "next_cursor": nxt,
                    "base_cursor": base,
                    "reset": True,
                }
            offset = cursor - base
            text = buf[offset:]
            return {
                "text": text,
                "next_cursor": nxt,
                "base_cursor": base,
                "reset": False,
            }

    def clear_output(self) -> int:
        """Discard retained terminal output without touching the PTY process.

        The returned absolute cursor identifies the clear boundary. Reconnecting
        clients can resume from it and receive only output produced after the
        clear operation.
        """

        with self._output_changed:
            cursor = self._buffer_next_cursor
            self._buffer = ""
            self._buffer_base_cursor = cursor
            self._output_changed.notify_all()
            return cursor

    def wait_for_output(
        self, cursor: int | None, timeout: float = 1.0
    ) -> dict[str, object]:
        """Wait until output or lifecycle state changes, then return a cursor read.

        This gives streaming transports an event-driven bridge to the reader
        thread without busy-polling the PTY. The bounded timeout lets callers
        notice disconnects and session expiry even while the process is quiet.
        """

        if cursor is not None and (
            not isinstance(cursor, int) or isinstance(cursor, bool)
        ):
            raise ValueError("cursor must be an integer or None")
        if timeout < 0:
            raise ValueError("timeout must not be negative")
        with self._output_changed:
            initial_cursor = self._buffer_next_cursor
            initial_state = self._state
            if cursor is not None and (
                cursor < self._buffer_base_cursor or cursor > self._buffer_next_cursor
            ):
                return self.read(cursor)
            if cursor is None or cursor < self._buffer_next_cursor:
                return self.read(cursor)
            self._output_changed.wait_for(
                lambda: (
                    self._buffer_next_cursor != initial_cursor
                    or self._state != initial_state
                ),
                timeout=timeout,
            )
            return self.read(cursor)

    # -- observability ------------------------------------------------

    def snapshot(self) -> dict[str, object]:
        """Return an observed lifecycle snapshot for status reporting."""
        with self._lock:
            state = self._state
            running = state == STATE_RUNNING
            can_start = (not self._shutdown) and state in (
                STATE_NOT_STARTED,
                STATE_EXITED,
                STATE_STOPPED,
                STATE_ERROR,
            )
            can_stop = running
            can_resume = self._resume_command is not None and can_start
            payload = {
                "state": state,
                "pid": self._pid,
                "session_id": self._session_id,
                "running": running,
                "can_start": can_start,
                "can_resume": can_resume,
                "can_stop": can_stop,
                "command": list(self._active_command),
                "repo_path": self._repo_path,
                "started_at": self._started_at,
                "ended_at": self._ended_at,
                "exit_code": self._exit_code,
                "rows": self._rows,
                "cols": self._cols,
                "buffer_base_cursor": self._buffer_base_cursor,
                "buffer_next_cursor": self._buffer_next_cursor,
                "detail": self._detail,
            }
            observed_pid = self._pid if running else None
            session_id = self._session_id
        payload["process_activity"] = self._process_observer.snapshot(
            observed_pid, session_id
        )
        return payload

    # -- internals ------------------------------------------------------

    def _set_winsize_locked(self, rows: int, cols: int) -> None:
        assert self._master_fd is not None
        packed = struct.pack("HHHH", rows, cols, 0, 0)
        try:
            fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, packed)
        except OSError as exc:
            self._detail = f"resize failed: {exc}"

    def _signal_process_group(self, pid: int, sig: int) -> None:
        """Signal the process group rooted at `pid`, tolerating its absence."""
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError:  # pragma: no cover - defensive
            pass

    def _reader_loop(self, master_fd: int, process: subprocess.Popen) -> None:
        detail = "process exited"
        read_error: Exception | None = None
        try:
            while True:
                try:
                    chunk = os.read(master_fd, READ_CHUNK_BYTES)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        # EIO is the expected PTY EOF signal once the slave
                        # side has closed (child exit).
                        break
                    read_error = exc
                    detail = f"reader error: {exc}"
                    break
                if not chunk:
                    break
                self._append_output(chunk, final=False)
        finally:
            # Flush any pending partial multi-byte sequence with a
            # replacement character rather than silently dropping it.
            self._append_output(b"", final=True)
            try:
                os.close(master_fd)
            except OSError:
                pass

            exit_code = process.wait()

            with self._lock:
                if self._master_fd == master_fd:
                    self._master_fd = None
                if self._state == STATE_RUNNING:
                    if read_error is not None:
                        self._state = STATE_ERROR
                        self._detail = detail
                    elif self._stop_requested:
                        self._state = STATE_STOPPED
                        self._detail = "stopped by request"
                    else:
                        self._state = STATE_EXITED
                        self._detail = detail
                    self._exit_code = exit_code
                    self._ended_at = time.time()
                self._output_changed.notify_all()

    def _append_output(self, chunk: bytes, final: bool = False) -> None:
        text = self._decoder.decode(chunk, final)
        if not text:
            return
        with self._lock:
            self._buffer += text
            self._buffer_next_cursor += len(text)
            overflow = len(self._buffer) - self._max_buffer_chars
            if overflow > 0:
                self._buffer = self._buffer[overflow:]
                self._buffer_base_cursor += overflow
            self._output_changed.notify_all()
