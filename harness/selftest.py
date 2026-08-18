"""Machinery self-test: no model calls.

Feeds each task's REFERENCE Rust function back through the full extract -> inject -> test ->
score path as if a model had emitted it. All six must come back PASS; anything else means the
harness itself is broken (bad path decoding, bad anchor, bad output parsing, leaky restore),
not that a model is bad.

Two small tasks per project, chosen for short reference bodies so the fixture is cheap to read.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from . import expected, inject, lock, score, tasks
from .run import (AttemptRecord, ResultsWriter, RunIdCollision, evaluate_output, preflight,
                  _now, _print_problems)

SELF_TEST_TASKS = [
    # charset-normalizer
    "projects__charset-normalizer__rust__src__entity__.rs__function__18",
    "projects__charset-normalizer__rust__src__entity__.rs__function__10",
    # iceberg
    "projects__iceberg__rust__crates__iceberg__src__transform__truncate__.rs__function__1",
    "projects__iceberg__rust__crates__iceberg__src__spec__schema__.rs__function__18",
    # libp2p
    "projects__libp2p__rust__identity__src__ecdsa__.rs__function__3",
    "projects__libp2p__rust__identity__src__ed25519__.rs__function__16",
]


def run_self_test(args) -> int:
    dataset = tasks.ensure_dataset(Path(args.dataset))
    run_id = args.run_id or f"selftest-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    try:
        writer = ResultsWriter(Path(args.out), "reference", run_id, resume=bool(args.resume))
    except RunIdCollision as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return 2
    try:
        lock.acquire(dataset, run_id)
    except lock.LockError as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return 2
    try:
        return _run_self_test(args, dataset, run_id, writer)
    finally:
        lock.release(dataset)


def _run_self_test(args, dataset: Path, run_id: str, writer: ResultsWriter) -> int:
    restored = inject.sweep_backups(tasks.evaluate_root(dataset) / "projects")
    for path in restored:
        print(f"restored stale backup: {path}", file=sys.stderr)

    discovered = tasks.discover_tasks(dataset)
    all_tasks = {t.task_id: t for t in discovered}
    missing = [tid for tid in SELF_TEST_TASKS if tid not in all_tasks]
    if missing:
        print(f"self-test fixtures missing from dataset: {missing}", file=sys.stderr)
        return 1

    problems = preflight(dataset, discovered)
    if problems:
        _print_problems(problems, "self-test aborted: dataset preflight failed")
        return 1

    writer.write_meta({
        "run_id": run_id, "model": "reference", "started_at": _now(),
        "dataset_commit": tasks.dataset_commit(dataset), "dataset_path": str(dataset),
        "settings": {"mode": "self-test", "test_timeout_s": args.test_timeout},
        "task_ids": SELF_TEST_TASKS,
    })

    results = []
    observed: dict[str, int] = {}
    for i, task_id in enumerate(SELF_TEST_TASKS, 1):
        task = all_tasks[task_id]
        # The fixture is exactly what a well-behaved model would return.
        fake_output = f"```rust\n{task.reference_rust_fn}\n```\n"
        started = time.monotonic()
        verdict, info, test_s = evaluate_output(task, fake_output, dataset, args.test_timeout)
        elapsed = time.monotonic() - started
        results.append((task_id, verdict))
        writer.write(AttemptRecord(
            run_id=run_id, model="reference", task_id=task_id, project=task.project,
            attempt=0, verdict=verdict.verdict, failing_tests=verdict.failing_tests,
            error_class_hint=verdict.error_class_hint, duration_s=elapsed,
            test_duration_s=test_s, prompt_chars=len(tasks.build_prompt(task)),
            task_content_hash=task.content_hash, baselined_failures=verdict.baselined,
            flaky_recovered=verdict.flaky_recovered,
            ambiguous_anchor=bool(info.get("ambiguous_anchor")),
            anchor_resolved_by=str(info.get("anchor_resolved_by") or ""),
            leak_stripped=task.leak_stripped,
            verdict_reason=verdict.reason,
            note="reference function injected as model output",
        ))
        # A green reference run is by definition a full run of that project's suite, so it is
        # also a valid snapshot of how many test targets the project has (see harness/expected).
        if verdict.verdict == score.PASS and verdict.summaries:
            observed[task.project] = max(observed.get(task.project, 0), len(verdict.summaries))
        status = "PASS" if verdict.verdict == score.PASS else verdict.verdict
        detail = ""
        if verdict.failing_tests:
            detail = " failing=" + ",".join(verdict.failing_tests[:5])
        elif verdict.verdict != score.PASS:
            detail = " " + verdict.error_text.strip().splitlines()[-1][:160] if verdict.error_text else ""
        print(f"[{i}/{len(SELF_TEST_TASKS)}] {status:16s} {task.project:20s} "
              f"{task_id} ({elapsed:.1f}s){detail}", flush=True)

    passed = sum(1 for _, v in results if v.verdict == score.PASS)
    print(f"\nself-test: {passed}/{len(results)} PASS  ->  {writer.jsonl}")

    for project, count in sorted(observed.items()):
        known = expected.expected(project)
        if known is None:
            expected.record(project, count)
            print(f"snapshotted {project}: {count} test-target summaries "
                  f"-> {expected.CACHE_PATH.name}")
        elif known != count:
            print(f"WARNING: {project} produced {count} test-target summaries, "
                  f"snapshot says {known}", file=sys.stderr)
        else:
            print(f"{project}: {count} test-target summaries (matches snapshot)")

    # Belt and braces: the working tree must be byte-identical to how we found it.
    leftovers = list((tasks.evaluate_root(dataset) / "projects").rglob("*" + inject.BACKUP_SUFFIX))
    if leftovers:
        print(f"ERROR: backups left behind: {leftovers}", file=sys.stderr)
        return 1
    return 0 if passed == len(results) else 1
