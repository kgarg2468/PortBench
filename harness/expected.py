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


def record(project: str, count: int) -> bool:
    """Store a snapshot for `project`. Returns True if the cache changed."""
    data = load()
    if data.get(project) == count:
        return False
    data[project] = int(count)
    CACHE_PATH.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return True
