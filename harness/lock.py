"""Exclusive dataset lock.

Every run mutates the shared `dataset/Evaluate/projects` tree in place and sweeps stale
`*.portbench-bak` files on startup. Two runners against one dataset therefore corrupt each
other: the second one's sweep restores (and deletes) the first one's live backup, so the first
ends up testing reference code, or crashes on restore.

The lock is dataset-wide rather than per project, because `sweep_backups` is dataset-wide too.
It is a plain O_EXCL file holding pid + run id; a lock whose pid is gone is treated as stale
and reclaimed, so a killed run does not need manual cleanup.
"""

from __future__ import annotations

import json
import os
import platform
import time
from contextlib import contextmanager
from pathlib import Path

LOCK_NAME = ".portbench-lock"


class LockError(RuntimeError):
    pass


def lock_path(dataset: Path) -> Path:
    return Path(dataset) / LOCK_NAME


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True          # someone else's process: alive, just not ours to signal
    except OSError:
        return True
    return True


def read_lock(dataset: Path) -> dict:
    try:
        return json.loads(lock_path(dataset).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def acquire(dataset: Path, run_id: str) -> Path:
    """Take the lock, reclaiming it once if the holder's pid is gone. Raises LockError."""
    path = lock_path(dataset)
    for attempt in (0, 1):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            holder = read_lock(dataset)
            pid = holder.get("pid")
            alive = isinstance(pid, int) and _pid_alive(pid)
            if alive or attempt == 1:
                raise LockError(
                    f"dataset is locked by pid={pid} run_id={holder.get('run_id')!r} "
                    f"since {holder.get('created_at')} ({path}). "
                    "Another PortBench run is using this tree; wait for it to finish, or "
                    "delete the lock file if you are sure it is stale."
                )
            # Stale: the holder died without releasing. Reclaim once.
            path.unlink(missing_ok=True)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({
                "pid": os.getpid(),
                "run_id": run_id,
                "host": platform.node(),
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }, fh)
        return path
    raise LockError(f"could not acquire {path}")


def release(dataset: Path) -> None:
    """Drop the lock, but only if we are still the holder."""
    holder = read_lock(dataset)
    if holder.get("pid") in (os.getpid(), None):
        lock_path(dataset).unlink(missing_ok=True)


@contextmanager
def held(dataset: Path, run_id: str):
    acquire(dataset, run_id)
    try:
        yield
    finally:
        release(dataset)
