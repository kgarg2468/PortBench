"""PortBench results aggregator: raw harness JSONL -> the four JSON files the site reads.

    python -m analysis.aggregate \
        --results results/ \
        --exclusions results/exclusions.json \
        --out site/data/ \
        [--models fable,opus] [--runs 'full-*.jsonl']

Writes `<out>/real/{leaderboard,taxonomy,trend,gallery}.json` and rewrites `<out>/manifest.json`
to point the site at that directory. Nothing under `<out>/sample/` is touched.

Design notes that are easy to get wrong and expensive to get wrong quietly:

  Determinism.  Every dict is built in a sorted order and every list is sorted before it is
  emitted, so two runs over the same inputs produce byte-identical files. The only timestamps
  in the output are lifted out of the run `.meta.json` files (`started_at` / `ended_at`); the
  aggregator never reads the wall clock. `git diff` on the output therefore means the data
  changed, not that the script ran again.

  Denominators.  Three separate populations, and conflating them is the classic way to publish
  a wrong leaderboard:
    * raw           — every task in the dataset (108).
    * effective     — raw minus the 8 uncovered tasks from exclusions.json (100). All published
                      rates use this.
    * scored        — effective minus this model's `no-data` tasks, and minus any task the run
                      never reached. This is the per-model denominator, and it is emitted as
                      `tasks_attempted` because that is the field the site divides by.
  Both raw and effective counts are recorded at the top of leaderboard.json, and every model
  record carries `no_data` and `not_attempted` so the site can print an asterisk.

  No-data.  A task whose attempt 0 came back TRANSPORT_ERROR or TIMEOUT *and* which has no
  attempt 1 record never produced any model output. Scoring it as a failure would punish a
  model for an API outage. It is dropped from the denominator and surfaced separately. A
  transport error on attempt 1 is not no-data: attempt 0 already produced a real verdict.

  Backfill.  If several run files for one model match `--runs`, they are applied oldest to
  newest and a later run overrides an earlier one for the same (task_id, attempt). "Later" is
  `(meta.started_at, run_id, filename)` — meta first because run ids are not ordered, filename
  last only as a tie-break. This makes re-running the tasks that transport-errored a matter of
  dropping a second JSONL next to the first, with no editing of the original.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import statistics
import sys
from pathlib import Path

try:                                  # `python -m analysis.aggregate` from the repo root
    from . import buckets
except ImportError:                   # direct execution / odd sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from analysis import buckets


DATASET = {
    "name": "RustRepoTrans (python -> rust slice)",
    "source": "https://github.com/WinterSunset95/RustRepoTrans",
}

GALLERY_PER_BUCKET = 4      # entries kept per bucket, so no bucket drowns the others
GALLERY_MAX = 24


# ------------------------------------------------------------------------------ inputs

def normalize_task_id(task_id: str) -> str:
    """Harness records drop the `.txt` the dataset filenames (and exclusions.json) carry."""
    return task_id[:-4] if task_id.endswith(".txt") else task_id


def load_exclusions(path: Path) -> tuple[set[str], int | None]:
    """-> (excluded task ids, declared effective task count or None)."""
    if not path or not Path(path).exists():
        return set(), None
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    excluded = {normalize_task_id(t) for t in data.get("uncovered", {}).get("tasks", [])}
    return excluded, data.get("effective_task_count")


def load_run_files(results_dir: Path, models_filter, runs_glob: str) -> list[dict]:
    """Every JSONL under results/<model>/ matching the glob, oldest run first.

    The model key comes from the records themselves, not the directory name, so a
    misfiled run still lands under the right model.
    """
    runs = []
    for model_dir in sorted(p for p in results_dir.iterdir() if p.is_dir()):
        for path in sorted(model_dir.iterdir()):
            if path.suffix != ".jsonl" or not fnmatch.fnmatch(path.name, runs_glob):
                continue
            records = []
            for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue                      # a half-written last line on a killed run
            if not records:
                continue
            model = str(records[0].get("model") or model_dir.name)
            if models_filter and model not in models_filter:
                continue
            meta_path = path.with_suffix(".meta.json")
            meta = {}
            if meta_path.exists():
                try:
                    meta = json.loads(meta_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    meta = {}
            runs.append({
                "model": model,
                "path": path,
                "records": records,
                "meta": meta,
                "run_id": str(records[0].get("run_id") or path.stem),
                "started_at": str(meta.get("started_at") or ""),
                "ended_at": str(meta.get("ended_at") or ""),
            })
    runs.sort(key=lambda r: (r["model"], r["started_at"], r["run_id"], r["path"].name))
    return runs


def index_attempts(runs: list[dict]) -> dict[str, dict[str, dict[int, dict]]]:
    """{model: {task_id: {attempt: record}}}, later runs overriding earlier ones.

    `runs` must already be in oldest-first order per model; `load_run_files` guarantees it.
    """
    index: dict[str, dict[str, dict[int, dict]]] = {}
    for run in runs:
        per_model = index.setdefault(run["model"], {})
        for record in run["records"]:
            task_id = normalize_task_id(str(record.get("task_id") or ""))
            attempt = record.get("attempt")
            if not task_id or not isinstance(attempt, int):
                continue
            per_model.setdefault(task_id, {})[attempt] = record
    return index


# ------------------------------------------------------------------------------ scoring

class ModelStats:
    """Everything derived for one model, computed once and read by all four emitters."""

    def __init__(self, model: str, tasks: dict[str, dict[int, dict]],
                 excluded: set[str], universe: set[str], runs: list[dict]):
        self.model = model
        meta = buckets.model_meta(model)
        self.label = meta["label"]
        self.family = meta["family"]
        self.runs = runs
        self.run_ids = sorted({r["run_id"] for r in runs})

        self.raw_tasks = len(tasks)
        self.excluded_present = sorted(t for t in tasks if t in excluded)

        in_scope = {t: a for t, a in tasks.items() if t not in excluded}
        self.effective_tasks = len(in_scope)

        # A task the run never reached at all (still in flight, or the process died).
        self.not_attempted = sorted(t for t in (universe - excluded) if t not in in_scope)

        self.no_data: list[str] = []
        self.scored: dict[str, dict[int, dict]] = {}
        for task_id in sorted(in_scope):
            attempts = in_scope[task_id]
            first = attempts.get(0)
            if first is None:
                # Only a repair record survived; without attempt 0 there is no one-shot
                # measurement, so it cannot enter the denominator either.
                self.not_attempted.append(task_id)
                continue
            if first.get("verdict") in buckets.NO_DATA_VERDICTS and 1 not in attempts:
                self.no_data.append(task_id)
                continue
            self.scored[task_id] = attempts
        self.not_attempted.sort()

        self.n = len(self.scored)

        self.passed_oneshot = 0
        self.passed_after_repair = 0
        self.verdicts = {"attempt_0": {}, "attempt_1": {}}
        self.by_project: dict[str, dict] = {}
        self.tax = {"attempt_0": {b: 0 for b in buckets.BUCKET_IDS},
                    "attempt_1": {b: 0 for b in buckets.BUCKET_IDS}}
        self.code_counts: dict[str, int] = {}

        self.tokens_in = 0
        self.tokens_out = 0
        self.token_attempts = 0
        self.token_missing_attempts = 0
        durations: list[float] = []
        self.duration_total = 0.0

        self.repairs_attempted = 0
        self.repairs_passed = 0

        for task_id in sorted(self.scored):
            attempts = self.scored[task_id]
            a0 = attempts.get(0)
            a1 = attempts.get(1)
            project = str(a0.get("project") or "unknown")
            proj = self.by_project.setdefault(
                project, {"n": 0, "passed_oneshot": 0, "passed_after_repair": 0})
            proj["n"] += 1

            one = a0.get("verdict") == buckets.PASS
            rep = one or (a1 is not None and a1.get("verdict") == buckets.PASS)
            self.passed_oneshot += int(one)
            self.passed_after_repair += int(rep)
            proj["passed_oneshot"] += int(one)
            proj["passed_after_repair"] += int(rep)

            if a1 is not None:
                self.repairs_attempted += 1
                self.repairs_passed += int(a1.get("verdict") == buckets.PASS)

            for key, record in (("attempt_0", a0), ("attempt_1", a1)):
                if record is None:
                    continue
                verdict = str(record.get("verdict") or "UNKNOWN")
                self.verdicts[key][verdict] = self.verdicts[key].get(verdict, 0) + 1

                tin, tout = record.get("tokens_in"), record.get("tokens_out")
                if tin is None or tout is None:
                    self.token_missing_attempts += 1
                else:
                    self.tokens_in += int(tin)
                    self.tokens_out += int(tout)
                    self.token_attempts += 1

                seconds = record.get("duration_s")
                if isinstance(seconds, (int, float)):
                    durations.append(float(seconds))
                    self.duration_total += float(seconds)

                if verdict == buckets.PASS:
                    continue
                self.tax[key][buckets.bucket_for_attempt(
                    verdict, record.get("error_class_hint"))] += 1
                if key == "attempt_0":
                    for code in record.get("error_class_hint") or []:
                        self.code_counts[code] = self.code_counts.get(code, 0) + 1

        self.duration_median = statistics.median(durations) if durations else 0.0
        self.failed_oneshot = self.n - self.passed_oneshot

        # Tokens: sum of the attempts that reported them. If *no* attempt reported any, we
        # have no usage data at all and the cost must be null rather than $0.00.
        self.has_tokens = self.token_attempts > 0
        self.cost = buckets.cost_usd(
            model,
            self.tokens_in if self.has_tokens else None,
            self.tokens_out if self.has_tokens else None,
        )
        self.price = buckets.price_for(model)

    # -- derived rates, all over the scored denominator ---------------------------------

    def _rate(self, num: int):
        return (num / self.n) if self.n else None

    @property
    def oneshot_rate(self):
        return self._rate(self.passed_oneshot)

    @property
    def repair_rate(self):
        return self._rate(self.passed_after_repair)

    @property
    def recovery_rate(self):
        """Share of one-shot failures the repair round converts to a PASS."""
        if not self.failed_oneshot:
            return None
        return (self.passed_after_repair - self.passed_oneshot) / self.failed_oneshot

    @property
    def recovery_rate_of_attempted(self):
        """Same, over the failures that actually got a repair round.

        Not every failure is repair-eligible: `harness/run.py` only re-prompts on
        COMPILE_ERROR / TEST_FAIL / SUITE_ERROR, so an EXTRACTION_ERROR ends the task at
        attempt 0 and drags the headline recovery rate down through no fault of the repair.
        """
        if not self.repairs_attempted:
            return None
        return self.repairs_passed / self.repairs_attempted

    def leaderboard_record(self, verdict_order: list[str]) -> dict:
        return {
            "model": self.model,
            "label": self.label,
            "family": self.family,
            "run_ids": self.run_ids,
            "tasks_attempted": self.n,
            "tasks_raw": self.raw_tasks,
            "tasks_effective": self.effective_tasks,
            "excluded": len(self.excluded_present),
            "no_data": len(self.no_data),
            "no_data_tasks": self.no_data,
            "not_attempted": len(self.not_attempted),
            "passed_oneshot": self.passed_oneshot,
            "passed_after_repair": self.passed_after_repair,
            "verdicts": {
                key: {v: self.verdicts[key].get(v, 0) for v in verdict_order}
                for key in ("attempt_0", "attempt_1")
            },
            "by_project": {
                p: self.by_project[p] for p in sorted(self.by_project)
            },
            "tokens": {
                "in": self.tokens_in if self.has_tokens else None,
                "out": self.tokens_out if self.has_tokens else None,
                "attempts_with_usage": self.token_attempts,
                "attempts_without_usage": self.token_missing_attempts,
            },
            "price_per_mtok_usd": (
                {"in": self.price["in"], "out": self.price["out"]} if self.price else None
            ),
            "price_confidence": self.price["confidence"] if self.price else "unknown",
            "cost_usd": None if self.cost is None else round(self.cost, 4),
            "duration_s_total": round(self.duration_total, 1),
            "duration_s_median": round(self.duration_median, 1),
        }

    def repair_record(self) -> dict:
        return {
            "model": self.model,
            "label": self.label,
            "failed_oneshot": self.failed_oneshot,
            "repairs_attempted": self.repairs_attempted,
            "repairs_passed": self.repairs_passed,
            "recovery_rate": _round(self.recovery_rate),
            "recovery_rate_of_attempted": _round(self.recovery_rate_of_attempted),
        }

    def taxonomy_record(self) -> dict:
        return {
            "model": self.model,
            "label": self.label,
            "family": self.family,
            "attempt_0": dict(self.tax["attempt_0"]),
            "attempt_1": dict(self.tax["attempt_1"]),
        }


def _round(value, digits: int = 4):
    return None if value is None else round(value, digits)


# ------------------------------------------------------------------------------ emitters

def build_leaderboard(stats: list[ModelStats], excluded: set[str], universe: set[str],
                      declared_effective, sweep_id: str, generated: str,
                      dataset_commit: str, verdict_order: list[str]) -> dict:
    scoreable = sorted(universe - excluded)

    # Project sizes are a property of the dataset, not of any one model, so count each
    # scoreable task once using whichever model happened to observe it.
    project_of: dict[str, str] = {}
    for s in sorted(stats, key=lambda x: buckets.model_sort_key(x.model)):
        for task_id in sorted(s.scored):
            attempts = s.scored[task_id]
            record = attempts.get(0) or next(iter(attempts.values()))
            project_of.setdefault(task_id, str(record.get("project") or "unknown"))
    counts: dict[str, int] = {}
    for task_id in sorted(project_of):
        counts[project_of[task_id]] = counts.get(project_of[task_id], 0) + 1
    projects = {p: counts[p] for p in sorted(counts)}

    ordered = sorted(stats, key=lambda s: (-s.passed_oneshot, buckets.model_sort_key(s.model)))
    pooled_failed = sum(s.failed_oneshot for s in stats)
    pooled_attempted = sum(s.repairs_attempted for s in stats)
    pooled_passed = sum(s.repairs_passed for s in stats)

    return {
        "schema": "portbench.leaderboard/1",
        "comment": (
            "Generated by analysis/aggregate.py from the harness JSONL. Counts only — the site "
            "derives every rate, lift and cost, so there is exactly one source of truth. "
            "tasks_attempted is the per-model scored denominator: the effective task set minus "
            "that model's no-data tasks. Dollar figures are a list-price approximation; see "
            "pricing_basis."
        ),
        "run_id": sweep_id,
        "generated": generated,
        "dataset": {
            "name": DATASET["name"],
            "source": DATASET["source"],
            "commit": dataset_commit,
            "tasks_total": len(scoreable) or (declared_effective or 0),
            "tasks_raw": len(universe),
            "tasks_excluded": len(excluded & universe),
            "declared_effective_task_count": declared_effective,
            "projects": projects,
        },
        "pricing_basis": buckets.PRICING_BASIS,
        "verdict_order": verdict_order,
        "models": [s.leaderboard_record(verdict_order) for s in ordered],
        "repair": {
            "comment": (
                "recovery_rate is over every one-shot failure; recovery_rate_of_attempted is "
                "over the failures the harness actually re-prompted (COMPILE_ERROR, TEST_FAIL, "
                "SUITE_ERROR only)."
            ),
            "pooled": {
                "models": len(stats),
                "failed_oneshot": pooled_failed,
                "repairs_attempted": pooled_attempted,
                "repairs_passed": pooled_passed,
                "recovery_rate": _round(pooled_passed / pooled_failed) if pooled_failed else None,
                "recovery_rate_of_attempted": (
                    _round(pooled_passed / pooled_attempted) if pooled_attempted else None
                ),
            },
            "models": [s.repair_record() for s in ordered],
        },
    }


def build_taxonomy(stats: list[ModelStats], sweep_id: str, generated: str) -> dict:
    ordered = sorted(stats, key=lambda s: (-s.passed_oneshot, buckets.model_sort_key(s.model)))

    pooled: dict[str, int] = {}
    for s in stats:
        for code, count in s.code_counts.items():
            pooled[code] = pooled.get(code, 0) + count
    top = sorted(pooled.items(), key=lambda kv: (-kv[1], kv[0]))[:12]

    return {
        "schema": "portbench.taxonomy/1",
        "comment": (
            "Every non-PASS attempt bucketed exactly once, so per model the buckets for an "
            "attempt sum to that attempt's failure count in leaderboard.json. Compile failures "
            "are keyed off the first rustc code in error_class_hint (the harness stores that "
            "list sorted, so 'first' is deterministic); test_fail and harness come straight off "
            "the verdict. top_codes counts every code in an attempt-0 failure, not just the one "
            "that decided the bucket."
        ),
        "run_id": sweep_id,
        "generated": generated,
        "buckets": [
            {"id": b["id"], "label": b["label"], "short": b["short"],
             "codes": list(b["codes"]), "blurb": b["blurb"]}
            for b in buckets.BUCKETS
        ],
        "models": [s.taxonomy_record() for s in ordered],
        "top_codes": [
            {"code": code, "bucket": buckets.bucket_for_code(code), "count": count,
             "message": buckets.message_for_code(code)}
            for code, count in top
        ],
    }


def build_trend(stats: list[ModelStats], tasks_total: int, sweep_id: str,
                generated: str, date: str) -> dict:
    ordered = sorted(stats, key=lambda s: (-s.passed_oneshot, buckets.model_sort_key(s.model)))
    best = ordered[0] if ordered else None
    one = sorted(s.passed_oneshot for s in stats)
    rep = sorted(s.passed_after_repair for s in stats)

    denominators = {s.model: s.n for s in sorted(stats, key=lambda x: x.model)}
    uneven = len(set(denominators.values())) > 1

    note = f"First published sweep: {len(stats)} models over {tasks_total} scoreable tasks."
    if uneven:
        note += (
            " Per-model denominators differ (no-data tasks and in-flight runs are dropped per "
            "model), so counts here are not all out of the same n — see leaderboard.json."
        )

    point = {
        "date": date,
        "run_id": sweep_id,
        "models_run": len(stats),
        "best_model": best.model if best else None,
        "best_model_label": best.label if best else None,
        "best_passed_oneshot": best.passed_oneshot if best else 0,
        "best_passed_after_repair": best.passed_after_repair if best else 0,
        "median_passed_oneshot": statistics.median(one) if one else 0,
        "median_passed_after_repair": statistics.median(rep) if rep else 0,
        "note": note,
        "denominators": denominators,
    }
    return {
        "schema": "portbench.trend/1",
        "comment": (
            "One point per full sweep, counts out of tasks_total; the site derives rates. Dates "
            "come from the run metadata, never from the clock at aggregation time. Medians are "
            "across the models run on that date, so the model set is not constant."
        ),
        "generated": generated,
        "tasks_total": tasks_total,
        "points": [point],
    }


GALLERY_CAPTURE_NOTE = (
    "The harness does not persist model output or raw compiler text: harness/run.py builds an "
    "AttemptRecord from the verdict only, and models.ModelResult.text plus Verdict.error_text "
    "are dropped once the repair prompt has been built. These entries are therefore assembled "
    "from what the JSONL does keep — task, model, attempt, verdict, rustc codes, failing tests. "
    "Nothing here is reconstructed or invented. Populating the side-by-side panes needs an "
    "artifact-capture flag in the harness; see analysis/README.md."
)

_NO_CODE = (
    "// Model output was not captured for this run.\n"
    "// The harness records the verdict, not the code that produced it.\n"
    "// Re-run with artifact capture enabled to fill this pane."
)


def _no_error_text(record: dict) -> str:
    codes = ", ".join(record.get("error_class_hint") or []) or "none recorded"
    failing = ", ".join(record.get("failing_tests") or []) or "none recorded"
    lines = [
        "Raw compiler / test output was not captured for this run.",
        "",
        f"verdict         {record.get('verdict')}",
        f"rustc codes     {codes}",
        f"failing tests   {failing}",
    ]
    reason = str(record.get("verdict_reason") or "").strip()
    if reason:
        lines += ["", "harness reason  " + reason[:400]]
    note = str(record.get("note") or "").strip()
    if note:
        lines += ["", "harness note    " + note[:400]]
    return "\n".join(lines)


def _title(record: dict, bucket: str) -> str:
    verdict = str(record.get("verdict") or "")
    codes = record.get("error_class_hint") or []
    if codes:
        return f"{codes[0]} · {buckets.message_for_code(codes[0])}"
    if verdict == buckets.TEST_FAIL:
        failing = record.get("failing_tests") or []
        if failing:
            return f"Compiled, then failed {failing[0]}"
        return "Compiled, then failed the project's own suite"
    return verdict.replace("_", " ").title() + " · no rustc code recorded"


def build_gallery(stats: list[ModelStats], sweep_id: str, generated: str) -> dict:
    candidates: dict[str, list[dict]] = {b: [] for b in buckets.BUCKET_IDS}
    for s in sorted(stats, key=lambda x: buckets.model_sort_key(x.model)):
        for task_id in sorted(s.scored):
            for attempt in (0, 1):
                record = s.scored[task_id].get(attempt)
                if record is None or record.get("verdict") == buckets.PASS:
                    continue
                bucket = buckets.bucket_for_attempt(
                    record.get("verdict"), record.get("error_class_hint"))
                candidates[bucket].append({
                    "id": f"{s.model}--{task_id}--a{attempt}",
                    "model": s.model,
                    "model_label": s.label,
                    "task_id": task_id,
                    "project": str(record.get("project") or "unknown"),
                    "attempt": attempt,
                    "verdict": str(record.get("verdict") or ""),
                    "bucket": bucket,
                    "error_class_hint": list(record.get("error_class_hint") or []),
                    "failing_tests": list(record.get("failing_tests") or [])[:3],
                    "title": _title(record, bucket),
                    "takeaway": (
                        f"{s.label} on attempt {attempt}"
                        f"{' (after repair)' if attempt else ' (one-shot)'}: "
                        f"{str(record.get('verdict') or '').replace('_', ' ').lower()}. "
                        "Observed record only — the model's code was not retained by this run."
                    ),
                    "model_code": _NO_CODE,
                    "model_code_available": False,
                    "compiler_error": _no_error_text(record),
                    "compiler_error_available": False,
                })

    entries: list[dict] = []
    for bucket in buckets.BUCKET_IDS:
        picked = sorted(candidates[bucket],
                        key=lambda e: (e["attempt"], e["model"], e["task_id"]))
        entries.extend(picked[:GALLERY_PER_BUCKET])
    entries = entries[:GALLERY_MAX]

    return {
        "schema": "portbench.gallery/1",
        "comment": GALLERY_CAPTURE_NOTE,
        "run_id": sweep_id,
        "generated": generated,
        "code_capture": "unavailable",
        "entries": entries,
    }


def build_manifest(dataset: str) -> dict:
    return {
        "schema": "portbench.manifest/1",
        "comment": ("Points the site at a dataset directory. Written by "
                    "analysis/aggregate.py; flip \"dataset\" back to \"sample\" to restore "
                    "the placeholder set."),
        "dataset": dataset,
        "files": {
            "leaderboard": f"{dataset}/leaderboard.json",
            "taxonomy": f"{dataset}/taxonomy.json",
            "trend": f"{dataset}/trend.json",
            "gallery": f"{dataset}/gallery.json",
        },
    }


# ------------------------------------------------------------------------------ driver

def aggregate(results_dir: Path, exclusions_path: Path, models_filter, runs_glob: str,
              dataset: str = "real") -> dict:
    """-> {"manifest": ..., "leaderboard": ..., "taxonomy": ..., "trend": ..., "gallery": ...}"""
    excluded, declared_effective = load_exclusions(exclusions_path)
    runs = load_run_files(results_dir, models_filter, runs_glob)
    if not runs:
        raise SystemExit(
            f"no run files under {results_dir} matching {runs_glob!r}"
            + (f" for models {sorted(models_filter)}" if models_filter else "")
        )
    index = index_attempts(runs)

    universe: set[str] = set()
    for tasks in index.values():
        universe |= set(tasks)

    stats = []
    for model in sorted(index, key=buckets.model_sort_key):
        model_runs = [r for r in runs if r["model"] == model]
        stats.append(ModelStats(model, index[model], excluded, universe, model_runs))

    verdict_order = list(buckets.VERDICT_ORDER)
    seen = {v for s in stats for key in s.verdicts for v in s.verdicts[key]}
    verdict_order += sorted(seen - set(verdict_order))

    ended = sorted(r["ended_at"] for r in runs if r["ended_at"])
    started = sorted(r["started_at"] for r in runs if r["started_at"])
    generated = (ended[-1] if ended else (started[-1] if started else ""))
    date = (generated or "0000-00-00")[:10]
    sweep_id = f"sweep-{date}"
    commits = sorted({str(r["meta"].get("dataset_commit") or "") for r in runs} - {""})
    dataset_commit = commits[0] if len(commits) == 1 else ",".join(commits)

    tasks_total = len(universe - excluded) or (declared_effective or 0)

    return {
        "manifest": build_manifest(dataset),
        "leaderboard": build_leaderboard(stats, excluded, universe, declared_effective,
                                         sweep_id, generated, dataset_commit, verdict_order),
        "taxonomy": build_taxonomy(stats, sweep_id, generated),
        "trend": build_trend(stats, tasks_total, sweep_id, generated, date),
        "gallery": build_gallery(stats, sweep_id, generated),
        "_stats": stats,
    }


def write_output(out_dir: Path, built: dict, dataset: str = "real") -> list[Path]:
    target = out_dir / dataset
    target.mkdir(parents=True, exist_ok=True)
    written = []
    for name in ("leaderboard", "taxonomy", "trend", "gallery"):
        path = target / f"{name}.json"
        path.write_text(json.dumps(built[name], indent=2, sort_keys=False) + "\n",
                        encoding="utf-8")
        written.append(path)
    manifest = out_dir / "manifest.json"
    manifest.write_text(json.dumps(built["manifest"], indent=2, sort_keys=False) + "\n",
                        encoding="utf-8")
    written.append(manifest)
    return written


def summarize(stats: list[ModelStats]) -> str:
    rows = sorted(stats, key=lambda s: (-s.passed_oneshot, buckets.model_sort_key(s.model)))
    out = [f"{'model':<16}{'n':>5}{'one-shot':>12}{'+repair':>12}{'no-data':>9}"
           f"{'not-run':>9}{'cost':>10}"]
    for s in rows:
        one = f"{s.passed_oneshot} ({s.oneshot_rate * 100:.1f}%)" if s.n else "-"
        rep = f"{s.passed_after_repair} ({s.repair_rate * 100:.1f}%)" if s.n else "-"
        cost = "n/a" if s.cost is None else f"${s.cost:,.2f}"
        out.append(f"{s.model:<16}{s.n:>5}{one:>12}{rep:>12}"
                   f"{len(s.no_data):>9}{len(s.not_attempted):>9}{cost:>10}")
    return "\n".join(out)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m analysis.aggregate",
        description="Aggregate PortBench harness JSONL into the site's data files.")
    parser.add_argument("--results", type=Path, default=Path("results"),
                        help="directory of results/<model>/*.jsonl (default: results)")
    parser.add_argument("--exclusions", type=Path, default=Path("results/exclusions.json"),
                        help="exclusions.json (default: results/exclusions.json)")
    parser.add_argument("--out", type=Path, default=Path("site/data"),
                        help="site data directory; files land in <out>/real (default: site/data)")
    parser.add_argument("--models", default="",
                        help="comma-separated model keys to include (default: all found)")
    parser.add_argument("--runs", default="full-*.jsonl",
                        help="glob for run files inside each model directory "
                             "(default: full-*.jsonl, which skips smoke and self-test runs)")
    parser.add_argument("--dataset", default="real",
                        help="subdirectory of --out to write, and the manifest's dataset "
                             "key (default: real)")
    args = parser.parse_args(argv)

    models_filter = {m.strip() for m in args.models.split(",") if m.strip()} or None
    built = aggregate(args.results, args.exclusions, models_filter, args.runs, args.dataset)
    written = write_output(args.out, built, args.dataset)

    print(summarize(built["_stats"]))
    print()
    for path in written:
        print("wrote", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
