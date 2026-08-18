#!/usr/bin/env python3
"""PortBench CLI: translate -> inject -> test -> score, one task at a time.

Execution is strictly sequential. Tasks in the same project share one working tree and one
warm 12 GB target/ directory, so parallelism there would corrupt both.

    python -m harness.run --list
    python -m harness.run --self-test
    python -m harness.run --model opus --project libp2p --limit 5 --run-id smoke-01
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import expected, inject, lock, models, score, tasks
from .tasks import Task

REPO_ROOT = Path(__file__).resolve().parent.parent


class RunIdCollision(RuntimeError):
    pass


# Verdicts the repair round acts on. Anything else ends the task at attempt 0.
REPAIR_ELIGIBLE = (score.COMPILE_ERROR, score.TEST_FAIL, score.SUITE_ERROR)


@dataclass
class AttemptRecord:
    run_id: str
    model: str
    task_id: str
    project: str
    attempt: int
    verdict: str
    failing_tests: list[str] = field(default_factory=list)
    error_class_hint: list[str] = field(default_factory=list)
    tokens_in: int | None = None
    tokens_out: int | None = None
    tokens_total: int | None = None
    duration_s: float = 0.0
    model_duration_s: float = 0.0
    test_duration_s: float = 0.0
    prompt_chars: int = 0
    task_content_hash: str = ""
    baselined_failures: list[str] = field(default_factory=list)
    flaky_recovered: list[str] = field(default_factory=list)
    ambiguous_anchor: bool = False
    anchor_resolved_by: str = ""
    transport_retried: bool = False
    resolved_model: str = ""
    leak_stripped: bool = False
    verdict_reason: str = ""
    note: str = ""


class ResultsWriter:
    """One run id, one pair of result files.

    Reusing a run id used to append fresh attempts to the old JSONL while *overwriting* the
    metadata, so the meta said "2 tasks" over a file holding records from an unrelated earlier
    run -- and every leaderboard denominator computed from that pair was wrong. A run id that
    already exists is now refused outright; `--resume` opts into appending, and then only the
    (task_id, attempt) pairs that are actually missing.
    """

    def __init__(self, out_root: Path, model: str, run_id: str, resume: bool = False):
        self.dir = Path(out_root) / model
        self.dir.mkdir(parents=True, exist_ok=True)
        self.jsonl = self.dir / f"{run_id}.jsonl"
        self.meta = self.dir / f"{run_id}.meta.json"
        self.resume = resume
        self.existing: dict[tuple[str, int], str] = {}

        present = [p for p in (self.jsonl, self.meta) if p.exists()]
        if present and not resume:
            raise RunIdCollision(
                f"run id {run_id!r} already exists for model {model!r}: "
                f"{', '.join(str(p) for p in present)}. "
                "Pick a new --run-id, or pass --resume to append only the missing attempts."
            )
        if resume:
            self.existing = self._load_existing()

    def _load_existing(self) -> dict[tuple[str, int], str]:
        seen: dict[tuple[str, int], str] = {}
        if not self.jsonl.exists():
            return seen
        for line in self.jsonl.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            task_id = record.get("task_id")
            attempt = record.get("attempt")
            if isinstance(task_id, str) and isinstance(attempt, int):
                seen[(task_id, attempt)] = str(record.get("verdict") or "")
        return seen

    def done_task_ids(self, repair: bool = True) -> set[str]:
        """Tasks that need no further work.

        A recorded attempt 0 is not the same as a finished task. If its verdict was one the
        repair round would have acted on and the process died before attempt 1, the task is
        half-done: skipping it on resume would leave the run permanently missing a repair
        attempt it owes, and every pass@1-with-repair figure drawn from the file would be
        computed over a denominator that quietly lost it.
        """
        attempts: dict[str, dict[int, str]] = {}
        for (task_id, attempt), verdict in self.existing.items():
            attempts.setdefault(task_id, {})[attempt] = verdict

        done: set[str] = set()
        for task_id, by_attempt in attempts.items():
            if 1 in by_attempt:
                done.add(task_id)
                continue
            if 0 not in by_attempt:
                continue
            if not repair or by_attempt[0] not in REPAIR_ELIGIBLE:
                done.add(task_id)
        return done

    def write(self, record: AttemptRecord) -> None:
        key = (record.task_id, record.attempt)
        if key in self.existing:
            return                                   # resume: already on record, never duplicate
        self.existing[key] = record.verdict
        with self.jsonl.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(record), sort_keys=True) + "\n")

    def write_meta(self, meta: dict) -> None:
        self.meta.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def evaluate_output(task: Task, model_text: str, dataset: Path,
                    test_timeout: int) -> tuple[score.Verdict, dict, float]:
    """Extract -> inject -> test -> score. Always restores the tree.

    Scoring happens *inside* the injection context. It used to run after the `with` block had
    already restored the reference file, so the flaky-retry rerun inside `score.score` was
    re-running the reference implementation rather than the model's.
    """
    proj_dir = tasks.project_dir(dataset, task.project)
    try:
        extraction = inject.extract(model_text)
        functions = inject.dedup_against_dependencies(extraction, task)
        inject.require_target_function(functions, task)
    except inject.ExtractionError as exc:
        return score.Verdict(score.EXTRACTION_ERROR, error_text=str(exc)), {}, 0.0

    try:
        with inject.injected(task, functions, extraction.uses) as info:
            run = score.run_tests(proj_dir, score.TEST_COMMANDS[task.project], timeout=test_timeout)
            verdict = score.score(task.project, proj_dir, run, timeout=test_timeout)
    except inject.ExtractionError as exc:
        return score.Verdict(score.EXTRACTION_ERROR, error_text=str(exc)), {}, 0.0

    return verdict, info, run.duration_s


def run_task(task: Task, model: str, run_id: str, dataset: Path, writer: ResultsWriter,
             model_timeout: int, test_timeout: int, repair: bool) -> str:
    """Run one task through the one-shot attempt and, if needed, one repair attempt."""
    prompt = tasks.build_prompt(task)
    final_verdict = score.TRANSPORT_ERROR

    for attempt in (0, 1):
        started = time.monotonic()
        try:
            result = models.call_model(model, prompt, timeout=model_timeout)
        except models.TransportError as exc:
            writer.write(AttemptRecord(
                run_id=run_id, model=model, task_id=task.task_id, project=task.project,
                attempt=attempt,
                verdict=score.TIMEOUT if exc.timed_out else score.TRANSPORT_ERROR,
                duration_s=time.monotonic() - started, prompt_chars=len(prompt),
                task_content_hash=task.content_hash, note=str(exc)[:500],
            ))
            return score.TIMEOUT if exc.timed_out else score.TRANSPORT_ERROR

        verdict, info, test_s = evaluate_output(task, result.text, dataset, test_timeout)
        final_verdict = verdict.verdict
        writer.write(AttemptRecord(
            run_id=run_id, model=model, task_id=task.task_id, project=task.project,
            attempt=attempt, verdict=verdict.verdict, failing_tests=verdict.failing_tests,
            error_class_hint=verdict.error_class_hint,
            tokens_in=result.tokens_in, tokens_out=result.tokens_out,
            tokens_total=result.tokens_total,
            duration_s=time.monotonic() - started, model_duration_s=result.duration_s,
            test_duration_s=test_s, prompt_chars=len(prompt),
            task_content_hash=task.content_hash,
            baselined_failures=verdict.baselined, flaky_recovered=verdict.flaky_recovered,
            ambiguous_anchor=bool(info.get("ambiguous_anchor")),
            anchor_resolved_by=str(info.get("anchor_resolved_by") or ""),
            transport_retried=result.retried,
            resolved_model=result.resolved_model,
            leak_stripped=task.leak_stripped,
            verdict_reason=verdict.reason,
        ))
        print(f"  attempt={attempt} {verdict.verdict} "
              f"{','.join(verdict.failing_tests[:3])}"
              f"{' | ' + verdict.reason if verdict.reason else ''}", flush=True)

        if attempt == 1 or not repair:
            break
        if verdict.verdict not in REPAIR_ELIGIBLE:
            break
        prompt = tasks.build_repair_prompt(task, result.text, verdict.error_text)

    return final_verdict


def cmd_list(dataset: Path) -> int:
    found = tasks.discover_tasks(dataset)
    counts: dict[str, int] = {}
    for task in found:
        counts[task.project] = counts.get(task.project, 0) + 1
    for task in found:
        print(f"{task.project:20s} {task.task_id}")
    print(f"\ntotal: {len(found)} tasks  " +
          "  ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 0


def anchor_problems(all_tasks: list[Task]) -> list[str]:
    """Every task whose reference function can no longer be located in the tree."""
    problems: list[str] = []
    cache: dict[Path, str] = {}
    for task in all_tasks:
        content = cache.get(task.target_path)
        if content is None:
            try:
                content = task.target_path.read_text(encoding="utf-8", errors="ignore")
            except OSError as exc:
                problems.append(f"{task.task_id}: cannot read {task.target_rel}: {exc}")
                continue
            cache[task.target_path] = content
        try:
            inject.resolve_anchor(content, task)
        except inject.ExtractionError as exc:
            problems.append(f"{task.task_id}: {exc}")
    return problems


def preflight(dataset: Path, all_tasks: list[Task]) -> list[str]:
    """Everything that must be true before the first model call.

    Drift in the dataset checkout, another runner's in-flight injection, or an anchor that no
    longer resolves all show up downstream as *model* failures. They are harness state, so the
    run aborts instead of recording them against a model.
    """
    return tasks.dataset_problems(dataset) + anchor_problems(all_tasks)


def _print_problems(problems: list[str], header: str) -> None:
    print(header, file=sys.stderr)
    for problem in problems:
        print(f"  - {problem}", file=sys.stderr)


def cmd_run(args) -> int:
    dataset = tasks.ensure_dataset(Path(args.dataset))
    run_id = args.run_id or f"run-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"

    # Fail on a run-id collision before taking the lock or touching the tree.
    try:
        writer = ResultsWriter(Path(args.out), args.model, run_id, resume=args.resume)
    except RunIdCollision as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return 2

    try:
        lock.acquire(dataset, run_id)
    except lock.LockError as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return 2

    try:
        restored = inject.sweep_backups(tasks.evaluate_root(dataset) / "projects")
        for path in restored:
            print(f"restored stale backup: {path}", file=sys.stderr)

        all_tasks = tasks.discover_tasks(dataset)
        problems = preflight(dataset, all_tasks)
        if problems:
            _print_problems(problems, "refusing to start: dataset preflight failed")
            return 2

        selected = tasks.select(all_tasks, args.project, args.tasks, args.limit)
        excluded = [t.task_id for t in selected if t.excluded_leak]
        selected = [t for t in selected if not t.excluded_leak]
        stripped = [t.task_id for t in selected if t.leak_stripped]
        if excluded:
            print(f"excluded {len(excluded)} task(s) whose prompt still leaks the reference: "
                  f"{excluded}", file=sys.stderr)
        if args.resume:
            done = writer.done_task_ids(repair=not args.no_repair)
            skipped = [t.task_id for t in selected if t.task_id in done]
            selected = [t for t in selected if t.task_id not in done]
            if skipped:
                print(f"resume: {len(skipped)} task(s) already recorded, skipping", flush=True)
        if not selected:
            print("no tasks selected", file=sys.stderr)
            return 2

        meta = {
            "run_id": run_id, "model": args.model, "started_at": _now(),
            "dataset_commit": tasks.dataset_commit(dataset), "dataset_path": str(dataset),
            "cli_versions": models.cli_versions(),
            "model_argv": models.argv_preview(args.model),
            "settings": {
                "reasoning": "cli default (out of the box)",
                "model_timeout_s": args.model_timeout, "test_timeout_s": args.test_timeout,
                "repair_round": not args.no_repair,
                "resume": bool(args.resume),
                "test_commands": score.TEST_COMMANDS,
                "expected_summaries": expected.load(),
                "model_calls_isolated": True,
            },
            "leak_stripped_task_ids": stripped,
            "excluded_leak_task_ids": excluded,
            "task_ids": [t.task_id for t in selected],
            "n_tasks": len(selected),
        }
        writer.write_meta(meta)

        tally: dict[str, int] = {}
        for i, task in enumerate(selected, 1):
            print(f"[{i}/{len(selected)}] {task.project} {task.task_id}", flush=True)
            verdict = run_task(task, args.model, run_id, dataset, writer,
                               args.model_timeout, args.test_timeout, not args.no_repair)
            tally[verdict] = tally.get(verdict, 0) + 1

        meta["ended_at"] = _now()
        meta["final_verdict_tally"] = tally
        writer.write_meta(meta)
        print(f"\n{run_id}: " + "  ".join(f"{k}={v}" for k, v in sorted(tally.items())))
        print(f"results -> {writer.jsonl}")
        return 0
    finally:
        lock.release(dataset)


def cmd_calibrate(args) -> int:
    """Mutation probe: replace a task's function with `unimplemented!()` and see if tests notice.

    A task only measures translation quality if the project's suite actually exercises the
    function. This mode reports that for one task at a time; deciding what to do with a task
    whose suite stays green is a separate stage, not this harness's job.
    """
    dataset = tasks.ensure_dataset(Path(args.dataset))
    all_tasks = tasks.discover_tasks(dataset)
    selected = tasks.select(all_tasks, args.project, args.tasks, args.limit)
    if not selected:
        print("no tasks selected", file=sys.stderr)
        return 2

    try:
        lock.acquire(dataset, f"calibrate-{datetime.now(timezone.utc).strftime('%H%M%S')}")
    except lock.LockError as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return 2
    try:
        uncovered = []
        for i, task in enumerate(selected, 1):
            mutant = f"```rust\n{task.target_signature.rstrip()} {{\n    unimplemented!()\n}}\n```\n"
            verdict, _info, _test_s = evaluate_output(task, mutant, dataset, args.test_timeout)
            covered = verdict.verdict != score.PASS
            if not covered:
                uncovered.append(task.task_id)
            print(f"[{i}/{len(selected)}] {'COVERED    ' if covered else 'NOT COVERED'} "
                  f"{verdict.verdict:16s} {task.task_id}", flush=True)
        print(f"\ncalibrate: {len(selected) - len(uncovered)}/{len(selected)} covered")
        for task_id in uncovered:
            print(f"  suite still passes with unimplemented!(): {task_id}")
        return 0
    finally:
        lock.release(dataset)


def snapshot_health_problems(run: score.TestRun, count: int) -> list[str]:
    """Why a snapshot run is not trustworthy enough to record a test-target count from.

    A timed-out, non-compiling or otherwise unreconciled run still parses *some* summaries, and
    persisting that truncated number quietly lowers the bar for every later run: fewer targets
    are then "expected", so a generated function that kills a test binary before it reports can
    pass. Only a clean, fully accounted run counts.
    """
    problems: list[str] = []
    if run.timed_out:
        problems.append("the snapshot run timed out")
    if score.is_compile_error(run):
        problems.append("the snapshot run did not compile")
    problems.extend(score.unaccounted_reasons(run))
    if count <= 0:
        problems.append("the snapshot run produced no test result summaries")
    return problems


def cmd_snapshot_targets(args) -> int:
    """Record how many `test result:` summaries each project prints on the unmodified tree."""
    dataset = tasks.ensure_dataset(Path(args.dataset))
    projects = ([args.project] if args.project else sorted(score.TEST_COMMANDS))
    try:
        lock.acquire(dataset, "snapshot-targets")
    except lock.LockError as exc:
        print(f"refusing to start: {exc}", file=sys.stderr)
        return 2
    try:
        failed = False
        for project in projects:
            run = score.run_tests(tasks.project_dir(dataset, project),
                                  score.TEST_COMMANDS[project], timeout=args.test_timeout)
            count = len(score.parse_summaries(run))
            problems = snapshot_health_problems(run, count)
            if problems:
                failed = True
                _print_problems(
                    problems,
                    f"{project}: NOT recorded ({count} summaries, rc={run.returncode})")
                continue
            try:
                expected.record(project, count, force=args.force_snapshot)
            except expected.SnapshotRefused as exc:
                failed = True
                print(f"{project}: {exc}", file=sys.stderr)
                continue
            print(f"{project}: {count} test-target summaries (rc={run.returncode})")
        print(f"recorded -> {expected.CACHE_PATH}")
        return 1 if failed else 0
    finally:
        lock.release(dataset)


def cmd_baseline(args) -> int:
    dataset = tasks.ensure_dataset(Path(args.dataset))
    for project in ([args.baseline] if args.baseline != "all" else sorted(score.TEST_COMMANDS)):
        failures = score.snapshot_baseline(
            project, tasks.project_dir(dataset, project), timeout=args.test_timeout)
        print(f"{project}: {len(failures)} failing")
        for name in failures:
            print(f"  {name}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="harness.run", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dataset", default=str(REPO_ROOT / "dataset"))
    p.add_argument("--model", choices=list(models.ALL_MODELS))
    p.add_argument("--tasks", nargs="*", metavar="GLOB",
                   help="task ids or fnmatch globs to select")
    p.add_argument("--limit", type=int)
    p.add_argument("--project", choices=sorted(tasks.EXPECTED_COUNTS))
    p.add_argument("--run-id")
    p.add_argument("--out", default=str(REPO_ROOT / "results"))
    p.add_argument("--model-timeout", type=int, default=models.DEFAULT_TIMEOUT)
    p.add_argument("--test-timeout", type=int, default=score.DEFAULT_TEST_TIMEOUT)
    p.add_argument("--no-repair", action="store_true", help="skip the repair round")
    p.add_argument("--resume", action="store_true",
                   help="append to an existing run id, skipping attempts already recorded")
    p.add_argument("--calibrate", action="store_true",
                   help="inject unimplemented!() into the selected tasks and report whether "
                        "the suite notices; no model calls")
    p.add_argument("--snapshot-targets", action="store_true",
                   help="record each project's test-target summary count on the unmodified tree")
    p.add_argument("--force-snapshot", action="store_true",
                   help="allow --snapshot-targets to lower an already recorded count")
    p.add_argument("--self-test", action="store_true",
                   help="inject reference functions as if they were model output; no model calls")
    p.add_argument("--unit-test", action="store_true",
                   help="offline parser/verdict tests on synthetic output; no cargo, no dataset")
    p.add_argument("--list", action="store_true", help="index the tasks and print the count")
    p.add_argument("--baseline", metavar="PROJECT",
                   help="snapshot baseline test failures on the unmodified tree ('all' for every project)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.unit_test:
        from .tests import run_unit_tests
        return run_unit_tests()
    if args.list:
        return cmd_list(tasks.ensure_dataset(Path(args.dataset)))
    if args.snapshot_targets:
        return cmd_snapshot_targets(args)
    if args.calibrate:
        return cmd_calibrate(args)
    if args.self_test:
        from .selftest import run_self_test
        return run_self_test(args)
    if args.baseline:
        return cmd_baseline(args)
    if not args.model:
        build_parser().error(
            "--model is required (or use --list / --self-test / --calibrate / "
            "--snapshot-targets / --baseline)")
    return cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
