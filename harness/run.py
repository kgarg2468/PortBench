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

from . import inject, models, score, tasks
from .tasks import Task

REPO_ROOT = Path(__file__).resolve().parent.parent


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
    transport_retried: bool = False
    verdict_reason: str = ""
    note: str = ""


class ResultsWriter:
    def __init__(self, out_root: Path, model: str, run_id: str):
        self.dir = Path(out_root) / model
        self.dir.mkdir(parents=True, exist_ok=True)
        self.jsonl = self.dir / f"{run_id}.jsonl"
        self.meta = self.dir / f"{run_id}.meta.json"

    def write(self, record: AttemptRecord) -> None:
        with self.jsonl.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(record), sort_keys=True) + "\n")

    def write_meta(self, meta: dict) -> None:
        self.meta.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def evaluate_output(task: Task, model_text: str, dataset: Path,
                    test_timeout: int) -> tuple[score.Verdict, dict, float]:
    """Extract -> inject -> test -> score. Always restores the tree."""
    proj_dir = tasks.project_dir(dataset, task.project)
    try:
        extraction = inject.extract(model_text)
        functions = inject.dedup_against_dependencies(extraction, task)
    except inject.ExtractionError as exc:
        return score.Verdict(score.EXTRACTION_ERROR, error_text=str(exc)), {}, 0.0

    try:
        with inject.injected(task, functions, extraction.uses) as info:
            run = score.run_tests(proj_dir, score.TEST_COMMANDS[task.project], timeout=test_timeout)
    except inject.ExtractionError as exc:
        return score.Verdict(score.EXTRACTION_ERROR, error_text=str(exc)), {}, 0.0

    verdict = score.score(task.project, proj_dir, run, timeout=test_timeout)
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
            transport_retried=result.retried,
            verdict_reason=verdict.reason,
        ))
        print(f"  attempt={attempt} {verdict.verdict} "
              f"{','.join(verdict.failing_tests[:3])}"
              f"{' | ' + verdict.reason if verdict.reason else ''}", flush=True)

        if attempt == 1 or not repair:
            break
        if verdict.verdict not in (score.COMPILE_ERROR, score.TEST_FAIL, score.SUITE_ERROR):
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


def cmd_run(args) -> int:
    dataset = tasks.ensure_dataset(Path(args.dataset))
    restored = inject.sweep_backups(tasks.evaluate_root(dataset) / "projects")
    for path in restored:
        print(f"restored stale backup: {path}", file=sys.stderr)

    all_tasks = tasks.discover_tasks(dataset)
    selected = tasks.select(all_tasks, args.project, args.tasks, args.limit)
    if not selected:
        print("no tasks selected", file=sys.stderr)
        return 2

    run_id = args.run_id or f"run-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    writer = ResultsWriter(Path(args.out), args.model, run_id)
    meta = {
        "run_id": run_id, "model": args.model, "started_at": _now(),
        "dataset_commit": tasks.dataset_commit(dataset), "dataset_path": str(dataset),
        "cli_versions": models.cli_versions(),
        "settings": {
            "reasoning": "cli default (out of the box)",
            "model_timeout_s": args.model_timeout, "test_timeout_s": args.test_timeout,
            "repair_round": not args.no_repair,
            "test_commands": score.TEST_COMMANDS,
        },
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
    if args.self_test:
        from .selftest import run_self_test
        return run_self_test(args)
    if args.baseline:
        return cmd_baseline(args)
    if not args.model:
        build_parser().error("--model is required (or use --list / --self-test / --baseline)")
    return cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
