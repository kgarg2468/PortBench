# analysis — harness JSONL → site data

One command turns the raw run files under `results/` into the four JSON files
`site/js/app.js` reads. No hand-editing, no spreadsheet, no step where a number is
typed twice.

```sh
python3 -m analysis.aggregate                       # all defaults
python3 -m analysis.aggregate \
    --results results/ \
    --exclusions results/exclusions.json \
    --out site/data/ \
    --models fable,opus,gpt-5.6-sol,gpt-5.6-terra \
    --runs 'full-*.jsonl'
```

| flag | default | meaning |
|---|---|---|
| `--results` | `results` | directory of `results/<model>/*.jsonl` |
| `--exclusions` | `results/exclusions.json` | the uncovered-task list |
| `--out` | `site/data` | files land in `<out>/real/`, manifest at `<out>/manifest.json` |
| `--models` | all found | comma-separated model keys |
| `--runs` | `full-*.jsonl` | glob inside each model directory |
| `--dataset` | `real` | subdirectory name and manifest key |

Writes `<out>/real/{leaderboard,taxonomy,trend,gallery}.json` and rewrites
`<out>/manifest.json` to `"dataset": "real"`. `<out>/sample/` is never touched, so
flipping the manifest back to `sample` restores the placeholder set.

**The generated files are build output and are not committed.** Run the aggregator
before deploying; `site/data/real/` is regenerated, not maintained.

Two source files:

* `aggregate.py` — mechanics: read, join, count, emit.
* `buckets.py` — policy: the rustc-code taxonomy, the pricing table, model labels.
  A taxonomy or pricing revision is a one-file diff that cannot touch the counting.

Tests: `python3 -m analysis.tests` (stdlib `unittest`, synthetic fixtures under
`analysis/fixtures/`, no network, no dataset checkout needed).

---

## Scoring rules

### Three denominators, and why

| population | size | used for |
|---|---|---|
| raw | every task in the dataset (108) | provenance only |
| effective | raw minus the 8 uncovered tasks (100) | the published task count |
| scored | effective minus this model's no-data and never-reached tasks | **every rate** |

`results/exclusions.json` lists 8 tasks whose suites still pass with the target function
replaced by `unimplemented!()` — they cannot distinguish a solution from a stub, so they
are dropped from every rate. Both raw and effective counts are recorded
(`dataset.tasks_raw`, `dataset.tasks_excluded`, `dataset.tasks_total`) and each model
carries `excluded`, `no_data`, `not_attempted` alongside `tasks_attempted`, which is the
scored denominator the site divides by.

Task ids are normalised across the `.txt` suffix: `exclusions.json` carries it, the
harness records do not.

### Pass

* **one-shot PASS** — attempt 0 verdict is `PASS`.
* **pass with repair** — attempt 0 *or* attempt 1 verdict is `PASS`. It is a superset of
  one-shot, which is why the site can draw one bar with two segments.

### No-data — `TRANSPORT_ERROR` only

A task whose **attempt 0** came back `TRANSPORT_ERROR` **and** which has no attempt 1
record never produced any model output: the CLI call itself failed, so the model never got
to be wrong. Scoring that as a failure charges a model for an API outage.

Such tasks are:

* removed from that model's denominator,
* counted in `no_data` and listed in `no_data_tasks` on the leaderboard model record, so
  the site can print an asterisk next to the rate.

A transport error on **attempt 1** is *not* no-data: attempt 0 already produced a real
verdict, so the one-shot measurement stands and the repair simply did not land.

A task present for one model and absent for another (a run still in flight) is counted
as `not_attempted` and also leaves the denominator — reported separately from `no_data`
because the causes are different.

### `TIMEOUT` is a failure, not no-data

`TIMEOUT` stays in the denominator, keeps its tokens, and is bucketed like any other
failure. In `harness/score.py` it is what the **test run** returns when it blows
`--test-timeout`: the model produced code, the code was injected, and the suite then hung
or ran forever. That is a genuine failure of the port. Treating it as no-data would delete
a real failure from the denominator and inflate the pass rate.

Because `harness/run.py: REPAIR_ELIGIBLE` is `(COMPILE_ERROR, TEST_FAIL, SUITE_ERROR)`,
a `TIMEOUT` is **terminal** — the harness never owed it a repair round, so a lone attempt-0
`TIMEOUT` is a complete task, not an interrupted one.

Interruptions are tracked separately: a scored task whose attempt-0 verdict *is* in
`REPAIR_ELIGIBLE` but which has no attempt 1 record means the process died mid-run. Attempt
0 still stands (the one-shot measurement is real) but the +repair figure is a floor, so the
task is counted in `repair_missing` and listed in `repair_missing_tasks`. The eligibility
set is mirrored in `buckets.REPAIR_ELIGIBLE` with a test asserting the two agree.

**One residual ambiguity**, worth a harness fix rather than a heuristic here:
`harness/models.py` also raises `TransportError(timed_out=True)` when the *model CLI*
times out, and `run.py` writes that as `TIMEOUT` too. Those records are distinguishable in
practice — null tokens and a populated `note` — but a single verdict string covers both
causes. Splitting the verdict (`MODEL_TIMEOUT` vs `TEST_TIMEOUT`) is the clean fix. Until
then the test-run reading wins, because it is the only one that has actually occurred in
any sweep on disk: every `TIMEOUT` in `results/` carries real token counts and sits at
attempt 1, and every attempt-0 no-data is a `TRANSPORT_ERROR`.

### Backfill: later runs override earlier ones

If several run files for one model match `--runs`, they are applied **oldest first** and a
later run overrides an earlier one for the same `(task_id, attempt)`. Ordering key:
`(meta.started_at, run_id, filename)` — the metadata timestamp first because run ids are
not lexicographically ordered, the filename only as a final tie-break.

This makes re-running the tasks that transport-errored a matter of dropping
`full-opus-02.jsonl` next to `full-opus-01.jsonl`. The original file is never edited, the
provenance stays on disk, and `run_ids` on the model record lists every run that
contributed.

### Cost

`tokens_in` / `tokens_out` are summed per model over every scored attempt that reported
them. Attempts that reported nothing are counted rather than silently treated as zero.
Three states:

| state | `cost_usd` | `cost_available` | `cost_partial` |
|---|---|---|---|
| every scored attempt reported usage | exact | `true` | `false` |
| some attempts reported usage | a **floor** over the priced attempts | `true` | `true` |
| no attempt reported usage | `null` — **never `0`** | `false` | `false` |

`cost_priced_attempts` and `cost_unpriced_attempts` give the split, so a partial figure can
never be mistaken for a total.

The null case matters: an early codex smoke run reports no usage at all, and charging it
`$0.00` would put it top of the "cheapest per solved task" column, which is exactly
backwards. `buckets.cost_usd` returns `None` — never `0.0` — whenever either the price or
the token counts are missing.

Note for whoever wires the frontend: `site/js/app.js` currently computes cost inline as
`m.tokens.in / 1e6 * price.in + ...`, and in JavaScript `null / 1e6` is `0`, so a null-cost
model would still render `$0.00` there. The data side is correct and self-describing —
`cost_available: false` is the flag to branch on — but the guard belongs in `app.js`, which
is outside this directory's scope.

**Pricing is a list-price approximation.** The table lives in `buckets.py:PRICING`, in USD
per million tokens, and each row carries a `confidence` of `published` (a long-stable
public tier) or `inferred` (our best reading of the model's tier, not a quoted figure).
Cache-read, batch and reasoning-token adjustments are ignored: input is input, output is
output. The basis string is copied into `leaderboard.json` as `pricing_basis` so nobody
reads a dollar figure off the site without it.

**Subscription users pay $0 marginal.** Every run in this repository was made through a
Claude Max and a Codex Max subscription, where the marginal cost of these tokens is zero.
The dollar columns answer "what would this cost an API customer" — the only figure that is
comparable across vendors — and nothing else.

---

## Taxonomy

Six buckets. The ids are load-bearing: `site/js/app.js` keys its colour map off exactly
`borrow`, `types`, `imports`, `other_compile`, `test_fail`, `harness`.

| bucket | source |
|---|---|
| `borrow` | borrow-checker and lifetime codes (E0382, E0499, E0502, E0505–E0510, E0515, E0597, E0716, E0106, E0621, E0623, …) |
| `types` | type and trait codes (E0308, E0277, E0599, E0609, E0061, E0107, E0560, …) |
| `imports` | name-resolution codes (E0432, E0433, E0412, E0425, E0405, E0463, …) |
| `other_compile` | fallback: any compile failure whose code is not in the tables above, or which carries no code at all |
| `test_fail` | verdict `TEST_FAIL` — it compiled, it was wrong |
| `harness` | verdict `EXTRACTION_ERROR`, `TRANSPORT_ERROR`, `TIMEOUT`, `SUITE_ERROR` |

The `harness` bucket lumps four very different outcomes together, so the verdict split is
carried alongside it as `harness_breakdown` per attempt. A `TIMEOUT` (the suite hung on the
model's code) is a much stronger statement than an `EXTRACTION_ERROR` (no function in the
reply), and the totals still sum to the bucket, so nothing double-counts.

Every code is commented individually in `buckets.py`. Each non-PASS attempt is filed under
exactly **one** bucket, so per model the buckets for an attempt sum to that attempt's
failure count in `leaderboard.json` — a test asserts this.

**Verdict beats codes.** A `TEST_FAIL` record can carry a rustc code scraped out of an
unrelated warning stream; it is still test logic, because `harness/score.py` only reaches a
`TEST_FAIL` verdict when the crate compiled. A `TIMEOUT` record can carry codes too; it is
still infrastructure.

**Multi-code attempts are bucketed by the *first* code in `error_class_hint`.** The harness
stores that list sorted and de-duplicated (`harness/score.py: error_codes`), so "first"
means "numerically lowest code present" — a stable, reproducible choice, not a severity
ranking. Two runs that hit the same set of codes always land in the same bucket. The
alternatives are worse: bucketing by "most severe" needs a severity table nobody can
defend, and counting an attempt once per bucket breaks the sum-to-failure-count invariant
that makes the chart readable.

`top_codes` counts **every** code on an attempt-0 failure, not just the one that decided
the bucket, because the question it answers ("what does rustc actually say to these
models") is about frequency, not attribution.

---

## Repair statistics

Per model and pooled, in `leaderboard.json` under `repair`:

* `failed_oneshot` — scored tasks whose attempt 0 was not a PASS
* `repairs_attempted` — scored tasks with an attempt 1 record
* `repairs_passed` — of those, attempt 1 verdict PASS
* `recovery_rate` — `repairs_passed / failed_oneshot`
* `recovery_rate_of_attempted` — `repairs_passed / repairs_attempted`

The two rates differ because not every failure is repair-eligible: `harness/run.py` only
re-prompts on `COMPILE_ERROR`, `TEST_FAIL` and `SUITE_ERROR`, so an `EXTRACTION_ERROR` ends
the task at attempt 0 and drags the headline recovery rate down through no fault of the
repair round. The site derives its own pooled figure from the counts; these fields exist so
a downstream consumer does not have to.

---

## Trend

One point, dated from the run metadata (the latest `ended_at` across the runs that fed the
aggregation). The aggregator **never reads the wall clock** — the only timestamps in the
output come out of the `.meta.json` files, so re-running over unchanged inputs produces
byte-identical files and a `git diff` means the data moved.

Caveat carried in the point's `note` and in a `denominators` map: `trend.json` counts are
out of a single `tasks_total`, but per-model denominators differ once no-data and in-flight
tasks are dropped. The frontier line is exact whenever the leading model has a full
denominator; the median line is approximate when the field is uneven.

---

## Gallery, and what the harness does not keep

**The harness does not persist model output or raw compiler text.** `harness/run.py` builds
an `AttemptRecord` from the verdict alone; `models.ModelResult.text` and
`score.Verdict.error_text` are used to construct the repair prompt and then dropped. There
are no artifact directories under `results/` — only `<run>.jsonl` and `<run>.meta.json`.

So `gallery.json` is assembled from what the JSONL does keep: task, project, model,
attempt, verdict, rustc codes, failing test names, and the harness's own note. Nothing is
reconstructed and nothing is invented. Both code panes carry an explicit placeholder, and
every entry is flagged `model_code_available: false` / `compiler_error_available: false`,
with `code_capture: "unavailable"` at the top of the file.

The harness now has that flag: `--keep-artifacts` (default off) writes
`results/<model>/<run_id>.artifacts/<task_id>/attempt<N>/` with the raw answer, the
injected Rust and the failure text, and the record carries `artifacts_dir` relative to the
results root. Artifact directories are gitignored — raw answers can quote the (unlicensed)
task prompt, so only curated snippets from permissively-licensed projects are ever
published, via a gallery-capture pass that reads `artifacts_dir`. Until that pass runs,
the gallery is an index of observed failures rather than a code exhibit.

Selection is deterministic: up to 4 entries per bucket (attempt 0 first, then model, then
task id), capped at 24 total, so no single bucket drowns the others.

---

## Determinism

Every dict is built in sorted order, every list is sorted before it is emitted, and no
value is derived from the clock, the filesystem order, or a set iteration. Two runs over
the same inputs produce byte-identical files. A test asserts it.
