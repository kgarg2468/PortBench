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
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

LOCK_NAME = ".portbench-lock"

# How long a lock file with no readable metadata is respected before it can be reclaimed.
# A lock is created with its metadata already inside it (see `_create_lock`), so an unreadable
# one is either corruption or a foreign writer -- either way it is treated as HELD first and
# reclaimed only once it is provably old.
UNREADABLE_LOCK_GRACE_S = 120


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


def _age_seconds(path: Path) -> float | None:
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None


def _create_lock(path: Path, run_id: str) -> None:
    """Create the lock file with its metadata already in it, or raise FileExistsError.

    Creating the file and then writing it is two steps, and between them the lock exists with
    no pid in it. A second process arriving in that window reads `{}`, concludes there is no
    live holder, and unlinks a lock that is very much alive -- after which both runners inject
    into the same tree. So the payload is written to a temp file first and `os.link`ed into
    place: the link is atomic and fails if the target exists, so the lock is never observable
    without its metadata.
    """
    payload = {
        "pid": os.getpid(),
        "run_id": run_id,
        "host": platform.node(),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    fd, staging = tempfile.mkstemp(dir=str(path.parent), prefix=".portbench-lock-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.link(staging, str(path))      # atomic; FileExistsError if someone beat us to it
    finally:
        os.unlink(staging)


def acquire(dataset: Path, run_id: str) -> Path:
    """Take the lock, reclaiming it once if the holder's pid is gone. Raises LockError."""
    path = lock_path(dataset)
    for attempt in (0, 1):
        try:
            _create_lock(path, run_id)
        except FileExistsError:
            holder = read_lock(dataset)
            if not holder:
                # No readable metadata. Never assume this means "free": see `_create_lock`.
                age = _age_seconds(path)
                if age is None or age < UNREADABLE_LOCK_GRACE_S or attempt == 1:
                    raise LockError(
                        f"dataset lock {path} exists but carries no readable metadata "
                        f"(age {age if age is None else round(age)}s). Treating it as held. "
                        f"If it is genuinely stale it becomes reclaimable after "
                        f"{UNREADABLE_LOCK_GRACE_S}s, or delete it yourself."
                    )
                path.unlink(missing_ok=True)
                continue
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
        return path
    raise LockError(f"could not acquire {path}")


def release(dataset: Path) -> None:
    """Drop the lock, but only if we are still the holder.

    A lock we cannot read is left alone rather than removed: it is not ours to delete, and
    `acquire` already has a grace-period path for genuinely abandoned ones.
    """
    if read_lock(dataset).get("pid") == os.getpid():
        lock_path(dataset).unlink(missing_ok=True)


@contextmanager
def held(dataset: Path, run_id: str):
    acquire(dataset, run_id)
    try:
        yield
    finally:
        release(dataset)
