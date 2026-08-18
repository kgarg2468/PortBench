# PortBench harness

Runs the 108 python→rust tasks of [RustRepoTrans](https://github.com/SYSUSELab/RustRepoTrans)
against a model CLI: build a prompt → call the model → inject its Rust into the real project
tree → run the project's test suite → record a verdict. One repair round on failure.

Design rationale, baseline failures and task anatomy live in [`../SPIKE.md`](../SPIKE.md).

## Setup

The dataset is **never vendored**. It lives at `./dataset` (gitignored) and is treated as
read-only input; `--dataset` points elsewhere if you want. If the directory is missing the
harness clones the repo at the pinned commit `7026a8a9c8d4a524cbb554ff9b8100df4020114c`.

> Never `cargo clean`, `cargo update`, or delete a `Cargo.lock` under `dataset/`. The ~12 GB of
> prebuilt `target/` artifacts are what make a run take minutes instead of hours, and the
> committed lockfiles are the only reason these projects still build.

Python deps are `tree-sitter` + `tree-sitter-rust`. Keep the virtualenv **outside** this repo:

```bash
uv venv --python 3.12 /tmp/portbench-venv
VIRTUAL_ENV=/tmp/portbench-venv uv pip install -r harness/requirements.txt
/tmp/portbench-venv/bin/python -m harness.run --list
```

or, without a persistent venv:

```bash
uv run --with-requirements harness/requirements.txt python -m harness.run --list
```

Also required on PATH: `cargo`/`rustc` (stable 1.95), `make` (for iceberg), and whichever model
CLI you are benchmarking (`claude`, `codex`).

## Usage

```bash
python -m harness.run --list                       # index tasks, print the 108 count
python -m harness.run --self-test                  # validate the machinery, zero model calls
python -m harness.run --baseline libp2p            # snapshot pre-existing test failures
python -m harness.run --model opus --run-id r1     # full sweep, 108 tasks
python -m harness.run --model gpt-5.6-sol --project iceberg --limit 5 --run-id smoke
python -m harness.run --model fable --tasks 'projects__libp2p__*ecdsa*' --run-id ecdsa
```

| flag | meaning |
|---|---|
| `--model` | `fable`, `opus`, `sonnet`, `haiku` (Claude CLI) or `gpt-5.6-sol`, `gpt-5.6-terra`, `gpt-5.6-luna`, `gpt-5.5` (Codex CLI) |
| `--tasks` | task ids or fnmatch globs |
| `--project` | `charset-normalizer`, `iceberg`, `libp2p` |
| `--limit N` | first N of the selection |
| `--run-id` | caller-supplied id; defaults to a UTC timestamp |
| `--no-repair` | one-shot only, skip the repair round |
| `--model-timeout` / `--test-timeout` | seconds (default 600 / 1800) |
| `--out` | results root (default `./results`) |

Execution is **sequential by design**. Every task in a project mutates the same working tree and
shares one warm `target/`; running two at once corrupts both.

## How a task is scored

1. **Prompt** — the paper's four slots (python function, rust signature, dependency declarations,
   dependency `use` lines) with its instruction text verbatim, so numbers stay comparable. We add
   one sentence asking for a single ` ```rust ` block.
2. **Extract** — tree-sitter-rust harvest of every `function_item` and `use_declaration`, trying
   fenced/tagged blocks in order and falling back to the raw response.
3. **Inject** — back up the target file, replace the reference function body, insert any missing
   top-level `use` lines, run tests, **restore in a `finally` block**. A killed run leaves
   `*.portbench-bak` files; the next start sweeps and restores them.
4. **Test** — `cargo test --workspace --no-fail-fast` (charset-normalizer, libp2p) or
   `make unit-test` (iceberg). Never fail-fast, never `-p <crate>`: workspace feature unification
   means a single-package build does not compile.
5. **Score** — parse *every* `test result:` summary and every `test <name> ... FAILED` line, then
   subtract the per-project baseline allowlist (`harness/baseline.py`). A failure inside the
   allowlist is this host's problem, not the model's.

### Verdicts

| verdict | meaning |
|---|---|
| `PASS` | no failing test outside the baseline allowlist |
| `COMPILE_ERROR` | cargo reported `could not compile`; rustc codes captured |
| `TEST_FAIL` | at least one non-baseline test failed |
| `EXTRACTION_ERROR` | no Rust function in the response, or the anchor was not found |
| `TRANSPORT_ERROR` | model CLI crashed or returned unparseable output (retried once first) |
| `TIMEOUT` | model call or test run exceeded its timeout |

### Repair round

On `COMPILE_ERROR` or `TEST_FAIL` the same model gets one follow-up: the original prompt, its own
previous answer, and the compiler/test output truncated to the last ~8k chars (rustc puts the
summary at the end). Re-inject, re-score. Both attempts are recorded — `attempt=0` is one-shot,
`attempt=1` is post-repair — so pass@1 and pass@1-with-repair are both derivable.

## Results

`results/<model>/<run_id>.jsonl`, one line per attempt:

```
run_id  model  task_id  project  attempt  verdict  failing_tests  error_class_hint
tokens_in  tokens_out  tokens_total  duration_s  model_duration_s  test_duration_s
prompt_chars  task_content_hash  baselined_failures  flaky_recovered
ambiguous_anchor  transport_retried  note
```

`error_class_hint` is the raw list of rustc codes seen (`["E0502", "E0308"]`) — collection only;
the error taxonomy is a later step. `task_content_hash` is sha256 over the two benchmark source
files, so a result is reproducible without us ever copying task text into this repo.

`results/<model>/<run_id>.meta.json` carries CLI versions, dataset commit, toolchain, start/end
time, the exact test commands, and the settings used.

**Reasoning settings are each CLI's out-of-the-box default.** Fairness here means "what you get
when you type the command", not a tuned configuration. The exact argv is recorded in the meta file.

## Deviations from the paper's `auto_test_rust.py`

Both of the paper's known bugs are reimplemented rather than ported:

- **`index != -1` on a `None`.** When a file has no top-level `use ...;` line, the paper computes
  `index = next(..., None)`, and `None != -1` is `True`, so `list.insert(None, ...)` raises
  `TypeError` and the task is silently lost. We fall back to inserting at the top of the file.
- **The `output.split("\n")[-3].startswith("test result: ok")` success predicate.** Under
  `--no-fail-fast` this only inspects the *last* test binary's summary, so an early failure still
  scores as a success. We parse all summaries and all `FAILED` lines instead.

Three further deliberate differences:

- **Ambiguous anchor** — the paper's unanchored `str.replace` hits every occurrence. SPIKE (f)
  asked for `assert count == 1`, but exactly one task
  (`...iceberg...io__.rs__function__12`, `pub fn location`) has two byte-identical occurrences
  (`InputFile` and `OutputFile`). Hard-asserting would permanently lose that task, so we replace
  all occurrences (identical replacement text — the paper's behaviour) and set
  `ambiguous_anchor: true` on the record. `count == 0` is still a hard `EXTRACTION_ERROR`.
- **Dedup never drops the target function.** The paper discards any extracted function whose text
  appears in the dependency block. For 2 of the 108 tasks the dependency block quotes the target
  function itself, so a correct answer would be thrown away. We keep a function whose name matches
  the requested signature regardless.
- **Newline normalisation.** The benchmark `.txt` files are CRLF; the `.rs` sources are read with
  universal newlines. Without normalising, *zero* of the 108 anchors match.

## Self-test

`--self-test` feeds each task's own reference Rust function back through the complete
extract → inject → test → score path as if a model had emitted it, for two small tasks per
project. All six must return `PASS`; it makes no model calls. It also asserts no backup files
survive, i.e. the tree was restored byte-for-byte.

If the self-test fails, the harness is broken — not the model.

## Known limitations

- The full suite runs per task (~2 min warm for libp2p). Per-test-name narrowing is possible but
  needs a task→test mapping we do not have yet.
- The baseline allowlist is static, taken from SPIKE (b) on this host. `--baseline` re-snapshots
  it but nothing auto-updates `harness/baseline.py`.
- iceberg's docker-compose stacks (glue/hms/s3/datafusion/rest catalogs) are never started;
  `make unit-test` is doc+lib only and does not need them.
- Codex token accounting depends on the CLI's JSONL event shape and degrades to `null` rather
  than failing if that shape changes.
