"""How many `test result:` summaries a project's suite prints when nothing is broken.

A generated function is free to call `std::process::exit(0)`. That kills its test binary
*before* libtest prints the binary's summary, cargo sees a zero exit for that target, and the
remaining targets' green summaries are all the scorer would ever see -- a silent PASS for code
that ran no tests. Counting summaries is the cheap, exit-code-independent way to notice: a
target that vanished is a target that printed no summary.

The count is snapshotted once per project from the unmodified tree (`--snapshot-targets`, or
opportunistically during `--self-test`, which already runs each suite on reference code) and
cached in `expected_summaries.json` next to this file, committed as a constant. If a project
has no snapshot yet the check is skipped rather than guessed.
"""

from __future__ import annotations

import json
from pathlib import Path

CACHE_PATH = Path(__file__).resolve().parent / "expected_summaries.json"


def load() -> dict[str, int]:
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {k: int(v) for k, v in data.items() if isinstance(v, int) or str(v).isdigit()}


def expected(project: str) -> int | None:
    return load().get(project)


class SnapshotRefused(RuntimeError):
    """The proposed snapshot would weaken or disable the missing-target check."""


def record(project: str, count: int, force: bool = False) -> bool:
    """Store a snapshot for `project`. Returns True if the cache changed.

    Two refusals, because a bad snapshot is worse than none: it silently disables the check it
    is supposed to drive. A count of zero would make "fewer summaries than expected" impossible
    to trigger. A count *lower* than one already recorded is almost always a truncated run, and
    accepting it lowers the bar for every future run -- `--force-snapshot` is required to say
    that a project genuinely lost test targets.
    """
    if count <= 0:
        raise SnapshotRefused(
            f"refusing to record {count} test-target summaries for {project}: "
            "a zero snapshot disables the missing-target check entirely"
        )
    data = load()
    previous = data.get(project)
    if previous is not None and count < previous and not force:
        raise SnapshotRefused(
            f"refusing to lower {project} from {previous} to {count} test-target summaries: "
            "this is usually a truncated run. Pass --force-snapshot if the project really "
            "has fewer test targets now."
        )
    if previous == count:
        return False
    data[project] = int(count)
    CACHE_PATH.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return True
