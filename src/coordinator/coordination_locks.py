"""Small, process-aware lock files for coordination relays and turns."""

from __future__ import annotations

import errno
import os
import re
from pathlib import Path


def lock_pid(path: Path) -> int | None:
    """Return a lock owner's PID when the bounded lock payload is valid."""

    try:
        payload = path.read_text(encoding="utf-8")[:4096]
    except (OSError, UnicodeError):
        return None
    match = re.search(r"(?:^|\s)pid=(\d+)(?:\s|$)", payload)
    return int(match.group(1)) if match else None


def process_alive(pid: int) -> bool:
    """Check a PID without signalling it, treating denied access as alive."""

    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as error:
        return error.errno == errno.EPERM
    return True


def active_lock(path: Path, *, reclaim_stale: bool = False) -> bool:
    """Report whether a lock has a live owner and optionally remove stale locks."""

    try:
        before = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    pid = lock_pid(path)
    # An unparseable lock may belong to an older or external implementation;
    # never delete it automatically because ownership cannot be disproved.
    if pid is None or process_alive(pid):
        return True
    if not reclaim_stale:
        return False
    try:
        after = path.stat(follow_symlinks=False)
        if (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino):
            path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        return True
    return False


def acquire_lock(path: Path, payload: str) -> int:
    """Acquire a lock, reclaiming one whose recorded process no longer exists."""

    for _ in range(2):
        try:
            descriptor = os.open(
                path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600
            )
        except FileExistsError:
            if active_lock(path, reclaim_stale=True):
                raise
            continue
        os.write(descriptor, payload.encode())
        return descriptor
    raise FileExistsError(path)
