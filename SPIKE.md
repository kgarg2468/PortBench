# PortBench Dataset Spike — RustRepoTrans (python→rust slice)

Date: 2026-08-18
Dataset under test: `dataset/` = clone of `https://github.com/SYSUSELab/RustRepoTrans`
(paper: *RustRepoTrans: Repository-level Code Translation Benchmark Targeting Rust*).
Scope of this spike: the **108 python→rust tasks** only (the benchmark ships 375 tasks total
across 5 projects and 4 source languages; we ignore the c/java pairs and the
`deltachat-core` / `incubator-milagro-crypto` projects entirely).

Toolchain: stable Rust 1.95, macOS (darwin 25.2.0). All three python-source projects were
built with their committed `Cargo.lock` intact.

---

## (a) Kill-gate verdicts

### Gate 1 — License: **PASS WITH MITIGATION**

Facts:

- The RustRepoTrans benchmark repo has **no top-level LICENSE / COPYING file** (verified: no
  license file at the dataset root, no license section in its `README.md`). It is therefore
  "all rights reserved" by default under copyright law — we have no grant to redistribute it.
- The *vendored upstream projects* inside it carry their own permissive licenses:
  - `libp2p` — MIT / Apache-2.0
  - `iceberg` (apache/iceberg-rust) — Apache-2.0
  - `charset-normalizer` — MIT

Mitigation (this is the design constraint the harness must honour):

1. **The harness clones the dataset at runtime.** We never vendor, fork, or redistribute the
   benchmark repo. Our repository contains only harness code + result artifacts (JSON scores,
   logs, diffs of *our own* generated output).
2. Gallery / documentation snippets are drawn from **permissively-licensed upstream code**
   (libp2p MIT/Apache, iceberg Apache-2.0, charset-normalizer MIT — each attributed) plus
   **model-generated output**, never from the benchmark repo's own curated task files.
3. Result files we publish must store *scores and test outcomes*, not verbatim copies of task
   prompts. If we need reproducibility, store the task **ID** (the benchmark's filename) and a
   content hash, not the content.

Recommended follow-up (**user decision, not a blocker**): open an issue / email SYSUSELab
asking for an explicit license grant (MIT or CC-BY-4.0) on the benchmark repo. That would let
us cache task content in our own artifacts and simplify the gallery. Until then, mitigation
1–3 keeps us clean.

### Gate 2 — Reference builds: **PASS** (confidence 9/10)

Facts established:

| project | build on stable 1.95 | errors | test outcome |
|---|---|---|---|
| `charset-normalizer/rust` | clean | 0 | full suite passes |
| `iceberg/rust` | clean | 0 | full suite passes (`make unit-test`) |
| `libp2p/rust` | clean | 0 | 617/624 pass, **4 fail**, 3 ignored — all 4 are environment/flake, not code rot (see (b)) |

3/3 projects build with zero errors. `Cargo.lock` pinning is what saved them — the lockfiles are
committed and must **never** be deleted or `cargo update`-d. ~12 GB of `target/` artifacts
already exist under `dataset/Evaluate/projects/*/rust/target/`; reuse them, do not clean.

Threshold was ">=7/10 equivalent confidence". 3/3 clean builds plus a fully explained set of
baseline test failures clears it comfortably. **Verdict: PASS.**

The 1-point deduction: libp2p's suite is network-dependent and partly flaky, which means the
harness cannot use "whole suite green" as its pass signal without baseline subtraction (see (b)
and (f)).

---

## (b) Baseline test failures (pre-existing — NOT model failures)

Run: `cargo test --no-fail-fast` at `dataset/Evaluate/projects/libp2p/rust`, unmodified tree.
Result: **624 tests across ~158 test binaries — 617 passed, 4 failed, 3 ignored.**

| # | test | crate / target | one-line cause | class |
|---|---|---|---|---|
| 1 | `tests::basic_resolve` | `libp2p-dns` (`--lib`) | live-DNS test; system resolver lists IPv6 nameserver `[2620:fe::fe]:53`, host has no route → `Os { code: 65, HostUnreachable, "No route to host" }` at `transports/dns/src/lib.rs:689` | environment |
| 2 | `test_discovery_async_std_ipv6` | `libp2p-mdns` (`--test use-async-std`) | IPv6 multicast mDNS discovery; "Swarm did not emit an event within 10s" — no usable IPv6 multicast on this host | environment |
| 3 | `test_discovery_tokio_ipv6` | `libp2p-mdns` (`--test use-tokio`) | same as #2, tokio runtime variant | environment |
| 4 | `connection::tests::idle_timeout_with_keep_alive_no` | `libp2p-swarm` (`--lib`) | timing-sensitive assertion at `swarm/src/connection.rs:946` expecting `ConnectionError::KeepAliveTimeout`; **confirmed flaky — reruns gave FAILED / ok / FAILED (2 of 3 fail)** | flake |

Ignored (expected, not failures): `wrong_peerid`, and two `hole_punching` doctests.

Root-cause corroboration for #1–#3: this host's only IPv6 default routes go through Tailscale
`utun` interfaces (`fd7a:115c:a1e0::` and `fe80::%utunN`) — there is no real IPv6 egress and no
IPv6 multicast. The paper's own published baseline log confirms these tests passed in their
environment (`results/rq1/test_result/translate_by_claude/libp2p/rust__python/...ecdsa__.rs__function__15.txt`
line 222: `test tests::basic_resolve ... ok`), so this is our environment, not dataset rot.

Relevance to our 108 tasks: **none of the 31 libp2p python→rust tasks touch `transports/dns`,
`protocols/mdns`, or `swarm/src/connection.rs`.** The task files hit `identity/*`,
`protocols/gossipsub`, `protocols/kad`, `swarm/src/lib.rs`, `swarm/src/dial_opts.rs`,
`swarm/src/behaviour/peer_addresses.rs`, `protocols/dcutr`, `muxers/mplex`, `transports/tls`.
Note that #4 lives in the *same crate* (`libp2p-swarm`) as 5 of our tasks, so crate-level
filtering does not dodge it — name-level baseline subtraction is required.

**Harness requirement:** these 4 test names go into a per-project baseline-failure allowlist.
A task is scored FAIL only if a test outside that allowlist fails.

charset-normalizer and iceberg have **empty** baseline-failure sets.

---

## (c) Task anatomy

### File layout

A single task is identified by one **filename** that appears in exactly two places:

```
dataset/Evaluate/
  function_pair_with_identical_functionality/<project>/rust__python/<TASK>.txt
  related_functions_and_datatypes_and_import/<project>/rust__python/rust/<TASK>.txt
  projects/<project>/rust/...          # the actual buildable Rust workspace
  projects/<project>/python/...        # the Python source project (reference only)
```

Task-id form (a path-encoded name, `/` → `__`, `.rs` → `__.rs`):

```
projects__libp2p__rust__identity__src__ecdsa__.rs__function__3.txt
└──────────── encodes projects/libp2p/rust/identity/src/ecdsa.rs ────┘  └ nth fn ┘
```

The paper decodes it back to a file path with:
`"/".join(name.split("__.rs")[0].split("__")) + ".rs"` (relative to `dataset/Evaluate/`).

### Format 1 — `function_pair_with_identical_functionality/<project>/rust__python/<TASK>.txt`

Plain text, **two records separated by a line containing `------`**. Record 0 is the Rust
(target) side, record 1 is the Python (source) side. Each record is:

```
<path>
…repo-relative path…
</path>
<function>
…function body…
</function>
```

There are no JSON fields anywhere — the whole benchmark is `<xml-ish>`-delimited plain text
parsed with `re.findall(r'<function>(.*?)</function>', …, re.DOTALL)`.

### Format 2 — `related_functions_and_datatypes_and_import/<project>/rust__python/rust/<TASK>.txt`

Plain text, **two sections separated by `------`**:

- section 0 = *related functions and datatype declarations* (raw Rust: `struct`/`enum`/`fn`
  definitions the target function depends on)
- section 1 = *dependency libraries* (raw Rust `use …;` lines)

Coverage: all 108 python→rust tasks have a matching dependencies file (verified, 0 missing,
0 malformed).

### Worked example — `projects__libp2p__rust__identity__src__ecdsa__.rs__function__3.txt`

`function_pair_with_identical_functionality/libp2p/rust__python/…`:

```
<path>
projects/libp2p/rust/identity/src/ecdsa.rs
</path>
<function>
    pub fn public(&self) -> &PublicKey {
        &self.public
    }
</function>
------

<path>
projects/libp2p/python/libp2p/peer/peerdata.py
</path>
<function>
    def get_pubkey(self) -> PublicKey:
        """
        :return: public key of the peer
        :raise PeerDataError: if public key not found
        """
        if self.pubkey is None:
            raise PeerDataError("public key not found")
        return self.pubkey

</function>
```

`related_functions_and_datatypes_and_import/libp2p/rust__python/rust/…` (same filename):

```
pub struct Keypair {
    secret: SecretKey,
    public: PublicKey,
}

pub struct PublicKey(VerifyingKey);

------
use super::error::DecodingError;
use core::cmp;
…
use zeroize::Zeroize;
use super::*;
```

### What the model actually sees

`translate_throughLLM.py` assembles exactly four payload slots:

1. `<python function>` — the Python source function (record 1 of Format 1)
2. `<rust function signature>` — **`query_func.split("{")[0]`**, i.e. the reference Rust
   function text truncated at the first `{`. Cheap and occasionally lossy (a signature
   containing `{` in a where-clause or default generic would be truncated early).
3. `<rust function dependencies, and data type declarations>` — section 0 of Format 2
4. `<rust function dependency libraries>` — section 1 of Format 2

Instruction text (verbatim from `translate_throughLLM.py`): translate the given `<corpus_lang>`
function to `<query_lang>` per the signature/dependencies/libraries provided, "Make sure to call
the relevant dependencies as much as possible", "Only response the translated function results."

The reference Rust body (the ground truth) is **not** shown to the model — but note that it is
saved into the response file afterwards as `<function>…</function>`, and `auto_test_rust.py`
re-reads it from the *pair* file, so a leak is only possible if a harness reuses the response
file as an input.

### Task counts

| project | python→rust tasks | dependency files |
|---|---|---|
| `charset-normalizer` | 33 | 33 |
| `iceberg` | 44 | 44 |
| `libp2p` | 31 | 31 |
| **total** | **108** | **108** |

Confirmed: **108** — matches the expected count exactly.

Per-file hot spots: charset-normalizer is concentrated in `src/entity.rs` (16), `src/utils.rs`
(9), `src/cd.rs` (7); iceberg's largest single file is `crates/catalog/glue/src/catalog.rs` (7);
libp2p spreads thinnest, max 5 in `identity/src/ed25519.rs`.

---

## (d) Paper-harness mechanics (`auto_test_rust.py`)

Entry point: `./run.sh <function_pair_dir> <llm_name> <dependencies_dir>` → translate → test →
`cnt_success.py` (pass@1 = successes / total). **All paths are relative — the scripts must be
run with cwd = `dataset/Evaluate/`.**

Per task, `auto_test_rust.py` does:

1. **Decode target file** from the task id (see (c)); back it up to `<file>.rs.copy`.
2. **Extract the model's function** from the response file: pull `<translated function>…</translated function>`,
   then try, in order, ` ```rust `, ` ```Rust `, `<rust function>`, `<rust function translation>`,
   `<rust translated function>`; fall back to the raw block. The extracted text is parsed with
   **tree-sitter-rust**, and every `(function_item)` node plus every `(use_declaration)` node is
   captured. So the model may emit several functions and imports; all are harvested.
3. **De-duplicate**: any extracted function whose text already appears in the task's dependency
   file is dropped (prevents re-defining a helper that already exists).
4. **Inject** (`change_target_function`): a plain Python `str.replace` of the *reference Rust
   function text* (read from the pair file) with the model's function, inside the real source
   file. Then each extracted `use …;` not already present in the file is inserted immediately
   before the first line that `startswith("use ") and endswith(";")`.
5. **Run tests** — `subprocess.run(test_cmd, cwd="projects/<project>/rust", timeout=700)`.

   | project | test command |
   |---|---|
   | `charset-normalizer` | `cargo test` (default) |
   | `libp2p` | `cargo test` (default) |
   | `iceberg` | `make unit-test` → `cargo test --no-fail-fast --doc --all-features --workspace` then `cargo test --no-fail-fast --lib --all-features --workspace` |
   | *(deltachat-core)* | `cargo nextest run` — out of scope |
   | *(incubator-milagro-crypto)* | `cargo test --all --all-features --release` — out of scope |

6. **Score**: `Success` iff `output.split("\n")[-3].startswith("test result: ok")`.
7. **Restore** the file from `.copy` in a `finally` block, delete the backup.

Before the loop, `main()` runs `projects/iceberg/rust/run_docker-compose.sh`, which brings up
five docker-compose stacks (glue catalog, hms catalog, s3 file_io, datafusion, rest catalog).
Only needed for iceberg's non-`--lib` targets; `make unit-test` is doc+lib only, which is why
iceberg passes for us without those services being exercised.

### Critical mechanics notes for our harness

- **There is no per-task test filter.** The paper runs the *entire project test suite* for every
  single task — 624 tests / ~158 binaries for libp2p, per task, 108 times. This is the single
  biggest cost and correctness problem to fix.
- **`cargo test` is fail-fast.** For charset-normalizer and libp2p the paper uses bare
  `cargo test`, so the first failing test binary aborts the run. On *our* machine libp2p-dns
  fails first, which would abort every libp2p task and mark all 31 as `Fail` — a 100% false
  negative rate. **Our harness must use `--no-fail-fast` (or per-test name filters) plus the
  baseline allowlist from (b).**
- **The `[-3]` success check is unsound under `--no-fail-fast`.** It only inspects the last test
  binary's summary line. With `--no-fail-fast` an early failure still leaves a trailing
  `test result: ok`, so iceberg (which *does* use `--no-fail-fast`) can be scored `Success`
  while tests failed. Do not reuse this predicate — parse all `test result:` lines, or better,
  use `cargo test --message-format json` / `--format json -Z unstable-options` on libtest.
- **`-p <crate>` filtering does not work here.** `cargo test -p libp2p-swarm --lib` fails to
  *compile* (14 errors, e.g. `Config::with_tokio_executor` not found) because workspace feature
  unification enables executor features that a single-package build does not. Any per-task
  narrowing must stay `--workspace`-wide and filter by **test name**, e.g.
  `cargo test --workspace --lib <name_filter> --no-fail-fast`.
- **Latent crash in `change_target_function`:** `index = next((…), None)` then `if index != -1`.
  When no top-level `use …;` line exists, `index` is `None`, `None != -1` is `True`, and
  `content.insert(None, …)` raises `TypeError`. The task is then recorded as `error` (counted in
  the denominator, never as a success). Reimplement this, do not port it verbatim.
- **`str.replace` injection is unanchored** — it replaces *every* occurrence of the reference
  function text in the file. Harmless in practice for these 108 tasks but worth an assertion
  (`count == 1`) in our version.

---

## (e) Prompt-size statistics (108 python→rust tasks)

Payload = source Python function + target Rust signature + dependencies (related decls + `use`
lines). Instruction boilerplate (~430 chars) excluded. Characters, not tokens; rough rule of
thumb ≈ 3.5 chars/token for code.

| slot | min | median | p90 | max | mean |
|---|---|---|---|---|---|
| **total payload** | **399** | **1,624** | **6,232** | **12,245** | **2,524** |
| python source fn | 56 | 307 | 1,159 | 3,047 | 485 |
| rust target signature | 26 | 61 | 133 | 219 | 69 |
| dependencies (decls + uses) | 243 | 1,120 | 4,889 | 11,109 | 1,969 |
| *(reference rust fn — ground truth, not in prompt)* | 57 | 233 | 1,178 | 3,677 | 484 |

Per project (total payload):

| project | n | min | median | p90 | max | mean |
|---|---|---|---|---|---|---|
| charset-normalizer | 33 | 618 | 1,545 | 4,663 | 10,363 | 2,401 |
| iceberg | 44 | 626 | 2,737 | 7,153 | 12,245 | 3,487 |
| libp2p | 31 | 399 | 730 | 2,006 | 5,917 | 1,287 |

Largest three tasks:

1. `iceberg / projects__iceberg__rust__crates__catalog__glue__src__catalog__.rs__function__11.txt` — 12,245
2. `charset-normalizer / projects__charset-normalizer__rust__src__entity__.rs__function__9.txt` — 10,363
3. `iceberg / projects__iceberg__rust__crates__catalog__glue__src__catalog__.rs__function__15.txt` — 9,384

Takeaway: **~3.5k tokens max** per prompt. Context is a non-issue for any modern model; the
dependency block dominates (78% of payload at the mean). Total corpus across all 108 tasks is
~273 KB — a single full-benchmark sweep is cheap on input tokens.

---

## (f) Harness-shape recommendation

Build a small Python runner (`portbench/run.py`, ~300 lines) that clones RustRepoTrans at a
pinned commit into a cache dir at first run, then treats the benchmark as read-only input: index
the 108 python→rust tasks by filename, parse the two `------`-delimited text files into a
`Task` dataclass (`task_id`, `target_path`, `reference_rust_fn`, `python_src_fn`,
`target_signature`, `dep_decls`, `dep_uses`), and reuse the *shape* of the paper's injection
logic — backup file, tree-sitter-rust extraction of `function_item`/`use_declaration` nodes from
the model output, drop functions already present in the dependency block, single anchored
`str.replace` of the reference body (assert exactly one match), insert missing `use` lines before
the first top-level `use …;` — but rewrite the two broken parts: the `index != -1` `None` crash
and the `[-3]` success predicate. Scoring should run `cargo test --workspace --no-fail-fast`
(iceberg keeps `make unit-test`, which is already `--no-fail-fast`), parse **every** `test result:`
line and every `test <name> ... FAILED` line, and mark a task FAIL only when a failing test name
is outside the per-project baseline allowlist from section (b) — with the flaky
`idle_timeout_with_keep_alive_no` retried up to 3× before it counts. Because per-package `-p`
filtering breaks feature unification, keep runs workspace-wide and narrow by test *name* only.
For speed, take a baseline snapshot once per project per session rather than per task, run tasks
sequentially against a single warm `target/` (12 GB already built; never clean it), and emit one
JSON-lines record per task (`task_id`, `verdict`, `failing_tests`, `compile_error`, `duration`,
`model`, `prompt_chars`) so results are diffable and the gallery can be generated from scores
plus permissively-licensed upstream snippets rather than benchmark content.
