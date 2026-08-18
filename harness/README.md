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

## ⚠️ Security: this harness compiles and runs model-generated code

**PortBench writes model output into a real Rust project and runs its test suite on your
machine, as you, with your privileges.** That is the whole point of the benchmark — you cannot
tell whether a translation works without executing it — but it means a hostile or
prompt-injected model response can run arbitrary code: `build.rs` scripts, proc macros and test
bodies all execute during `cargo test`. Treat every run as executing untrusted code.

What the harness does do:

- **The test subprocess gets a scrubbed environment.** Only an allowlist is forwarded (`PATH`,
  `HOME`, `TMPDIR`, `TERM`, `USER`, `LOGNAME`, `SHELL`, `LANG`, `LC_ALL`, `CARGO_HOME`,
  `RUSTUP_HOME`, `RUSTUP_TOOLCHAIN`, `RUST_BACKTRACE`, `SDKROOT`, `DEVELOPER_DIR`), so API keys,
  cloud credentials and tokens sitting in the parent environment are **not** inherited by
  generated code. Add more with `PORTBENCH_EXTRA_ENV=NAME1,NAME2` if a project needs them.
- Prompts are passed to model CLIs via a temp file on stdin, never interpolated into a shell
  command line, and no shell is ever spawned.
- The injected file is always restored, including after a crash.

What it does **not** do: filesystem, network or process isolation. Full sandboxing is out of
scope for a local benchmark harness — the environment scrub is a credential-exposure mitigation,
not a containment boundary.

**Run this on a dedicated or otherwise trusted machine, or inside a container/VM**, especially
when benchmarking models you do not control. Do not run it on a workstation holding production
credentials or with an authenticated cloud CLI session.

## Isolation of model calls (the answer key problem)

`claude` and `codex` are **agentic** CLIs: out of the box they can read files. This repository
contains `dataset/`, which holds the reference Rust implementation of every one of the 108
tasks. A model call made from the repository root is therefore a model call that can look up
the answer, and any score collected that way is meaningless.

Every model call now runs:

- **with `cwd` set to a fresh empty temp directory**, created per call and deleted afterwards.
  Nothing of the repository, the dataset or the user's projects is reachable by a relative path.
- **with tools switched off**, as far as each CLI allows:

  | | flags |
  |---|---|
  | `claude` | `--tools ""` (the documented "disable all built-in tools" form), `--safe-mode` (no CLAUDE.md, skills, plugins, hooks, custom agents), `--strict-mcp-config --mcp-config '{"mcpServers":{}}'` (no MCP server can reintroduce a tool), `--disable-slash-commands`, `--no-session-persistence` |
  | `codex` | `-s read-only` sandbox, plus `--disable shell_tool --disable unified_exec --disable view_image --disable browser_use --disable browser_use_external --disable computer_use --disable multi_agent --disable plugins --disable skill_search --disable apps --disable image_generation`, `-c tools.web_search=false`, `--ignore-user-config`, `--ignore-rules`, `--ephemeral` |

The exact argv is recorded in `<run_id>.meta.json` (`model_argv`) and the concrete model id the
CLI resolved the alias to is recorded per attempt (`resolved_model`).

**Residual risk, stated plainly.** These are configuration flags, not a sandbox. A `codex` turn
runs under a read-only OS sandbox that still permits reads outside the workspace, so a codex
process that *chose* to read an absolute path such as `/Users/you/…/portbench/dataset/…` could
do so — the tool it would need has been disabled, but disabling a tool is a CLI-level decision,
not a kernel-level one. Claude's `--tools ""` likewise removes the tools rather than confining
the process. What makes tool use effectively absent in practice is the combination: an empty
working directory, a single-turn translation prompt that asks only for a code block, and no
tool exposed to act with. It is not a containment boundary, and this harness does not claim one.
Benchmarking a model you actively distrust warrants a container or VM, as above.

## Usage

```bash
python -m harness.run --list                       # index tasks, print the 108 count
python -m harness.run --unit-test                  # parser/verdict tests, no cargo, no dataset
python -m harness.run --self-test                  # validate the machinery, zero model calls
python -m harness.run --baseline libp2p            # snapshot pre-existing test failures
python -m harness.run --snapshot-targets           # snapshot each project's test-target count
python -m harness.run --calibrate --tasks 'projects__iceberg__*'   # mutation probe, no model calls
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
| `--run-id` | caller-supplied id; defaults to a UTC timestamp. Refused if results already exist |
| `--resume` | append to an existing run id, skipping (task_id, attempt) pairs already recorded |
| `--no-repair` | one-shot only, skip the repair round |
| `--calibrate` | inject `unimplemented!()` into the selected tasks and report whether the suite notices; no model calls |
| `--snapshot-targets` | record each project's test-target summary count on the unmodified tree |
| `--force-snapshot` | allow `--snapshot-targets` to *lower* an already recorded count |
| `--model-timeout` / `--test-timeout` | seconds (default 600 / 1800) |
| `--out` | results root (default `./results`) |

Execution is **sequential by design**. Every task in a project mutates the same working tree and
shares one warm `target/`; running two at once corrupts both.

### Runs cannot collide

Three rails, because each of these produced real corruption during development:

- **A dataset lock.** `dataset/.portbench-lock` is taken before any injection and released in a
  `finally`. A second runner exits immediately and names the pid and run id holding it, instead
  of sweeping away the first runner's live `*.portbench-bak` (which would leave the first one
  silently testing reference code). A lock whose pid is gone is reclaimed automatically, so a
  killed run needs no manual cleanup.

  Creation is **atomic**: the metadata is written to a temp file and `os.link`ed into place, so
  the lock is never observable without a pid in it. Creating an empty file and then writing it
  leaves a window in which a second process reads `{}`, concludes the holder is dead, and
  unlinks a live lock. For the same reason a lock with no readable metadata is treated as
  **held**, and only becomes reclaimable once it is provably older than 120s.
- **A dataset preflight.** Before the first model call: `HEAD` must equal the pinned commit, no
  `*.portbench-bak` may be left over, no tracked file may be modified, and all 108 anchors must
  resolve. Drift or an in-flight injection would otherwise be recorded as a *model* failure.
- **Run-id exclusivity.** `results/<model>/<run_id>.jsonl` and `.meta.json` are refused if they
  exist. Reusing a run id used to append new attempts to the old JSONL while overwriting the
  metadata, so the meta described a different set of tasks than the file held and every
  leaderboard denominator drawn from the pair was wrong. `--resume` opts in explicitly and
  dedupes by `(task_id, attempt)`.

  A resumed task counts as finished only if attempt 1 exists, or attempt 0 landed on a verdict
  the repair round would not have acted on (or `--no-repair` is set). A lone repair-eligible
  attempt 0 is a half-done task: treating it as complete would leave the run permanently owing
  a repair attempt, and pass@1-with-repair would be computed over a denominator that quietly
  lost it.

## How a task is scored

1. **Prompt** — the paper's four slots (python function, rust signature, dependency declarations,
   dependency `use` lines) with its instruction text verbatim, so numbers stay comparable. We add
   one sentence asking for a single ` ```rust ` block. The dependency block is checked for the
   answer first — see *Reference leakage* below.
2. **Extract** — tree-sitter-rust harvest of every `function_item` and `use_declaration`, trying
   fenced/tagged blocks in order and falling back to the raw response. The harvest must contain
   a function with the **requested name**, or the attempt is `EXTRACTION_ERROR`: a response
   consisting only of a novel `fn helper()` would otherwise be injected over the target, and if
   nothing in the crate calls the target the suite goes green for a translation never written.
   Extra helper functions the model emitted are injected alongside.
3. **Inject** — back up the target file, replace the target's **AST node** (resolved by byte
   range, see *Ambiguous anchors*), insert any missing top-level `use` lines, run tests, **score
   while the model's code is still in place**, and **restore in a `finally` block**. A killed run
   leaves `*.portbench-bak` files; the next start sweeps and restores them.
4. **Test** — `cargo test --workspace --no-fail-fast` (charset-normalizer, libp2p) or
   `make unit-test` (iceberg). Never fail-fast, never `-p <crate>`: workspace feature unification
   means a single-package build does not compile.
5. **Score** — parse *every* `test result:` summary and every `test <name> ... FAILED` line,
   reconcile them per test binary, check that no test target went missing, then subtract the
   per-project baseline allowlist (`harness/baseline.py`). A failure inside the allowlist is this
   host's problem, not the model's.

### Reference leakage

Two of the 108 tasks ship a dependency block that quotes the **answer**: the complete reference
Rust body sits in the "function dependencies and data type declarations" slot, so the prompt
hands the model the translation it is being asked to produce
(`…iceberg…catalog…glue…catalog.rs__function__11` and
`…iceberg…writer__base_writer__data_file_writer.rs__function__4`). All 108 tasks are scanned at
load time — whitespace-insensitively, because the blocks re-indent what they quote — and the
offending declaration, which is a complete AST node, is excised exactly, leaving every genuine
dependency intact. Both tasks strip cleanly, so nothing is excluded; the affected ids are
recorded in the run metadata as `leak_stripped_task_ids`. If a future leak ever survived
stripping the task would be marked `excluded_leak`, skipped, and disclosed as
`excluded_leak_task_ids` rather than silently scored.

### Ambiguous anchors

The paper injects with an unanchored `str.replace(reference, answer)`, which rewrites *every*
occurrence. Exactly one task (`…iceberg…io.rs__function__12`, `pub fn location`) has two
byte-identical occurrences — `InputFile` and `OutputFile` — so the paper scores the model on two
edited functions instead of the one it asked about. Anchors are resolved through the AST
instead: the target is located as a `function_item` and replaced by byte range. When more than
one node matches, the task id's `__function__N` ordinal picks one (measured across all 108
tasks, that ordinal is a 1-based index into the file's `function_item` list). An anchor that
still cannot be resolved to exactly one node is a hard `EXTRACTION_ERROR` — never a blind
replace. `anchor_count` and `anchor_resolved_by` are on every record.

### Verdicts

| verdict | meaning |
|---|---|
| `PASS` | every test target reported, and no failing test outside the baseline allowlist |
| `COMPILE_ERROR` | nonzero exit **and** a real diagnostic (`could not compile`, or an `error[Exxxx]` code) |
| `TEST_FAIL` | at least one non-baseline test failed |
| `SUITE_ERROR` | the exit code is not explained by the parsed output, or a test target never reported (see below) |
| `EXTRACTION_ERROR` | no Rust function in the response, the requested function is missing from it, or the anchor did not resolve |
| `TRANSPORT_ERROR` | model CLI exited nonzero or produced no well-formed terminal envelope (retried once first) |
| `TIMEOUT` | model call or test run exceeded its timeout |

#### Reconciling nonzero exits

A test binary that segfaults or aborts prints **no** `test result:` summary and **no**
`test <name> ... FAILED` line, yet cargo still exits nonzero. Scoring purely on parsed output
would call that suite `PASS`. So every nonzero exit is reconciled against what was actually
reported — summary failure counts must match the printed failure lines, and cargo's per-target
`error: test failed` count must match the number of `FAILED` summaries. If the exit code is not
fully explained, the verdict is `SUITE_ERROR` and `verdict_reason` says why; `PASS` is never
reachable from a nonzero exit that we cannot account for.

That reconciliation is done **per test binary**, not over a global list of names. Test names are
not unique across binaries: `tests::roundtrip` failing in two crates is two failures and one
name, and comparing a de-duplicated name list against summed summary counts invented a mismatch
that surfaced as a false `SUITE_ERROR`. Output is segmented by each binary's own
`running N tests` … `test result:` bracket, which is the only per-target grouping available —
cargo's `Running <target>` lines go to stderr while the binary's test lines go to stdout, so the
two streams cannot be interleaved back together.

The common nonzero-but-fine case still passes: cargo exits 101 because allowlisted baseline
tests failed, and every one of them was both named and summarised.

#### Reconciling *zero* exits

Exit zero is not evidence that the tests ran. A generated function may call
`std::process::exit(0)`: its test binary dies before libtest prints that binary's summary, cargo
sees success for the target, and the other targets' green summaries are all that is left to
score — a `PASS` for code that ran no tests at all. The number of `test result:` lines a project
prints is fixed, so a target that has gone missing is directly visible. That count is
snapshotted once per project from the unmodified tree (`--snapshot-targets`, or opportunistically
during `--self-test`, which already runs each suite on reference code) into
`harness/expected_summaries.json`, and a run with fewer summaries than the snapshot is
`SUITE_ERROR` regardless of exit code. Projects with no snapshot yet skip the check rather than
guess.

A snapshot is only persisted from a **healthy** run: not timed out, compiling, exit code fully
reconciled, and more than zero summaries. Recording a zero would disable the check outright, and
recording a truncated count would lower the bar for every later run, so a count below one
already stored needs `--force-snapshot` to say the project genuinely lost test targets.

### Baseline vs. flake

`harness/baseline.py` distinguishes two things the paper conflates:

- **Environment failures** (`BASELINE_FAILURES`) can never pass on this host no matter what the
  model writes — no IPv6 egress, no IPv6 multicast, a live-DNS lookup. Three libp2p names.
  Subtracted unconditionally.
- **Flakes** (`FLAKY_TESTS`) are *not* allowlisted. Allowlisting one would subtract it even when
  the model's own code is what broke it, and would make the retry path unreachable — the name
  would never reach the retry loop. Instead the test is re-run by exact name, up to
  `FLAKY_RETRIES` times, and only a rerun that **exits 0, executed at least one test, and
  reported no failures** counts as a recovery. "The failure name is absent" is not evidence: a
  rerun that fails to compile, or that matches no test, also prints no `FAILED` line.
  Recoveries are recorded in `flaky_recovered`.

Retries happen while the model's code is still injected. They used to run after the `with` block
had already restored the file, i.e. against the reference implementation.

### Mutation calibration

A task only measures translation quality if the project's suite actually executes the function.
`--calibrate` injects `unimplemented!()` in place of the selected tasks' target and reports
whether the suite noticed, reusing the normal inject → test → score path and making no model
calls. Deciding what to do with a task whose suite stays green (exclude it, supplement it) is a
separate stage; this mode only measures.

### Repair round

On `COMPILE_ERROR`, `TEST_FAIL` or `SUITE_ERROR` the same model gets one follow-up: the original prompt, its own
previous answer, and the compiler/test output truncated to the last ~8k chars (rustc puts the
summary at the end). Re-inject, re-score. Both attempts are recorded — `attempt=0` is one-shot,
`attempt=1` is post-repair — so pass@1 and pass@1-with-repair are both derivable.

## Results

`results/<model>/<run_id>.jsonl`, one line per attempt:

```
run_id  model  resolved_model  task_id  project  attempt  verdict  failing_tests
error_class_hint  tokens_in  tokens_out  tokens_total  duration_s  model_duration_s
test_duration_s  prompt_chars  task_content_hash  baselined_failures  flaky_recovered
ambiguous_anchor  anchor_resolved_by  leak_stripped  transport_retried
verdict_reason  note
```

`error_class_hint` is the raw list of rustc codes seen (`["E0502", "E0308"]`) — collection only;
the error taxonomy is a later step. `task_content_hash` is sha256 over the two benchmark source
files, so a result is reproducible without us ever copying task text into this repo.

`results/<model>/<run_id>.meta.json` carries CLI versions, dataset commit, toolchain, start/end
time, the exact test commands, the exact model argv, the recorded test-target counts, and the
leak-stripped / leak-excluded task ids.

**Reasoning settings are each CLI's out-of-the-box default.** Fairness here means "what you get
when you type the command", not a tuned configuration. The exact argv is recorded in the meta
file, and the concrete model id the CLI resolved the alias to is on every attempt record.

### Accepting an answer

A model answer is used only when the CLI **exited 0** *and* produced a well-formed terminal
envelope — for `claude`, JSON carrying a `result` field with no `is_error`; for `codex`, a
non-empty `-o` last-message file or a terminal `agent_message` event in the JSONL stream.
Anything else takes the transport retry path (one retry, then `TRANSPORT_ERROR`). A residual or
partial final message left behind by a failed process is debris, not an answer, and scoring it
would charge the model for the CLI's crash.

## Deviations from the paper's `auto_test_rust.py`

Both of the paper's known bugs are reimplemented rather than ported:

- **`index != -1` on a `None`.** When a file has no top-level `use ...;` line, the paper computes
  `index = next(..., None)`, and `None != -1` is `True`, so `list.insert(None, ...)` raises
  `TypeError` and the task is silently lost. We fall back to inserting at the top of the file.
- **The `output.split("\n")[-3].startswith("test result: ok")` success predicate.** Under
  `--no-fail-fast` this only inspects the *last* test binary's summary, so an early failure still
  scores as a success. We parse all summaries and all `FAILED` lines instead.

Three further deliberate differences:

- **Ambiguous anchor** — the paper's unanchored `str.replace` hits every occurrence, so the one
  task with two byte-identical `pub fn location` bodies is scored on two edited functions. We
  resolve the anchor through the AST and replace exactly one node, picking by the task's
  `__function__N` ordinal when several match. See *Ambiguous anchors* above.
- **Dedup never drops the target function.** The paper discards any extracted function whose text
  appears in the dependency block. For 2 of the 108 tasks the dependency block quotes the target
  function itself, so a correct answer would be thrown away. We keep a function whose name matches
  the requested signature regardless — and strip that quoted reference out of the prompt in the
  first place, since it is the answer.
- **Newline normalisation.** The benchmark `.txt` files are CRLF; the `.rs` sources are read with
  universal newlines. Without normalising, *zero* of the 108 anchors match.

## Tests

`--unit-test` (`harness/tests.py`) exercises the output parser, the verdict function and the run
safety rails against synthetic cargo output and temp directories — no cargo, no dataset, no
model, runs in well under a second. It covers each verdict class plus every regression that has
bitten: a nonzero exit whose failures are all allowlisted must still `PASS`; a binary that
crashes without printing a `FAILED` line must never `PASS`; a missing test-target summary on
exit 0 must never `PASS`; the same test name failing in two binaries must not read as a
mismatch; an answer without the requested function must be rejected; a dependency block quoting
the answer must be stripped exactly; a duplicated anchor must resolve by ordinal; an existing
run id must be refused; a second runner must be locked out (and a dead holder's lock reclaimed);
and each codex JSONL token/message shape must parse.

`--self-test` feeds each task's own reference Rust function back through the complete
extract → inject → test → score path as if a model had emitted it, for two small tasks per
project. All six must return `PASS`; it makes no model calls. It also asserts no backup files
survive, i.e. the tree was restored byte-for-byte, and records each project's test-target
summary count if no snapshot exists yet (warning if one exists and disagrees).

If the self-test fails, the harness is broken — not the model.

## Known limitations

- The full suite runs per task (~2 min warm for libp2p). Per-test-name narrowing is possible but
  needs a task→test mapping we do not have yet.
- The baseline allowlist is static, taken from SPIKE (b) on this host. `--baseline` re-snapshots
  it but nothing auto-updates `harness/baseline.py`.
- iceberg's docker-compose stacks (glue/hms/s3/datafusion/rest catalogs) are never started;
  `make unit-test` is doc+lib only and does not need them.
- Codex token accounting searches the JSONL event for a usage object rather than a fixed path,
  which covers every shape shipped so far, but it still degrades to `null` rather than failing
  if the CLI stops emitting one.
- Failure *names* are reported unqualified. Reconciliation is per test binary, but the record's
  `failing_tests` list is de-duplicated by name, so a name failing in two crates appears once.
  Attributing names to crates needs cargo's stderr `Running <target>` lines to be interleaved
  with stdout, which the two-stream capture does not preserve.
- `--calibrate` measures whether a suite notices `unimplemented!()`; it does not act on the
  answer. Excluding or supplementing uncovered tasks is a separate stage.
