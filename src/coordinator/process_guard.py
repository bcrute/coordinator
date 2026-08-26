"""Supervise a subprocess so it cannot outlive its Coordinator parent."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path


CHILD_STOP_TIMEOUT_SECONDS = 2.0
OWNER_CHECK_SECONDS = 0.05


def guarded_command(command: Sequence[str]) -> list[str]:
    """Return an argv that runs ``command`` beneath the process guard."""

    if not command:
        raise ValueError("guarded command must not be empty")
    return [
        sys.executable,
        "-m",
        "coordinator.process_guard",
        "--owner-pid",
        str(os.getpid()),
        "--",
        *command,
    ]


def _signal_group(child: subprocess.Popen[object], number: int) -> None:
    if child.poll() is not None:
        return
    try:
        os.killpg(child.pid, number)
    except (AttributeError, ProcessLookupError, PermissionError):
        child.send_signal(number)


def _linux_descendant_groups(root_pid: int) -> set[int]:
    """Return process groups below ``root_pid``, including escaped sessions."""

    if not sys.platform.startswith("linux"):
        return set()
    processes: dict[int, tuple[int, int]] = {}
    for stat_path in Path("/proc").glob("[0-9]*/stat"):
        try:
            pid = int(stat_path.parent.name)
            suffix = stat_path.read_text(encoding="utf-8").rsplit(") ", 1)[1].split()
            processes[pid] = (int(suffix[1]), int(suffix[2]))
        except (FileNotFoundError, IndexError, OSError, ValueError):
            continue
    descendants = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, (parent, _group) in processes.items():
            if parent in descendants and pid not in descendants:
                descendants.add(pid)
                changed = True
    return {
        group
        for pid in descendants
        if (record := processes.get(pid)) is not None
        for group in (record[1],)
        if group > 1
    }


def _signal_groups(groups: set[int], number: int) -> None:
    own_group = os.getpgrp()
    for group in groups:
        if group == own_group:
            continue
        try:
            os.killpg(group, number)
        except (ProcessLookupError, PermissionError):
            continue


def _stop_child(child: subprocess.Popen[object], number: int) -> None:
    groups = _linux_descendant_groups(child.pid)
    groups.add(child.pid)
    _signal_groups(groups, number)
    try:
        child.wait(timeout=CHILD_STOP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _signal_groups(groups | _linux_descendant_groups(child.pid), signal.SIGKILL)
        child.wait()
    else:
        # A descendant can create its own session and be reparented as soon as
        # the direct child exits. The pre-signal group snapshot still owns it.
        _signal_groups(groups, signal.SIGKILL)


def run(command: Sequence[str], *, owner_pid: int | None = None) -> int:
    """Run one command and relay termination to its complete process group."""

    if not command:
        print("error: process guard requires a command after --", file=sys.stderr)
        return 2
    if owner_pid is not None and os.getppid() != owner_pid:
        return 128 + signal.SIGTERM
    requested_signal: list[int] = []
    child_holder: list[subprocess.Popen[object]] = []

    def request_stop(number: int, _frame: object) -> None:
        if not requested_signal:
            requested_signal.append(number)

    def forward_signal(number: int, _frame: object) -> None:
        if child_holder:
            _signal_group(child_holder[0], number)

    prior_handlers = {
        number: signal.signal(number, request_stop)
        for number in (signal.SIGINT, signal.SIGTERM)
    }
    if hasattr(signal, "SIGWINCH"):
        prior_handlers[signal.SIGWINCH] = signal.signal(signal.SIGWINCH, forward_signal)
    child: subprocess.Popen[object] | None = None
    try:
        child = subprocess.Popen(list(command), start_new_session=True)
        child_holder.append(child)
        while child.poll() is None and not requested_signal:
            if owner_pid is not None and os.getppid() != owner_pid:
                requested_signal.append(signal.SIGTERM)
                break
            time.sleep(OWNER_CHECK_SECONDS)
        if requested_signal and child.poll() is None:
            _stop_child(child, requested_signal[0])
            return 128 + requested_signal[0]
        return child.wait()
    finally:
        if child is not None and child.poll() is None:
            _stop_child(child, signal.SIGTERM)
        for number, handler in prior_handlers.items():
            signal.signal(number, handler)


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    owner_pid: int | None = None
    if arguments[:1] == ["--owner-pid"]:
        if len(arguments) < 2:
            print("error: --owner-pid requires an integer", file=sys.stderr)
            return 2
        try:
            owner_pid = int(arguments[1])
        except ValueError:
            print("error: --owner-pid requires an integer", file=sys.stderr)
            return 2
        arguments = arguments[2:]
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    return run(arguments, owner_pid=owner_pid)


if __name__ == "__main__":
    raise SystemExit(main())
