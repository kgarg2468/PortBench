# AI quietly beat the borrow checker

**STATUS: DRAFT — not for publication until reviewed. Repo stays private.**

*PortBench: 8 frontier models, 108 repository-level Python→Rust translation tasks, every
answer judged by the target project's own test suite.*

---

In 2024, the RustRepoTrans paper measured how well LLMs translate real repository code into
Rust and found what everyone expected: models drowned in ownership. Borrow-checker and
lifetime errors were a defining failure mode. "The borrow checker is AI's final boss" became
the standing assumption, repeated in every thread about AI and Rust since.

We re-ran that experiment against today's models. Across **1,346 recorded attempts** by 8
frontier models on 108 real translation tasks, the borrow-checker/lifetime/move error family
(E0382, E0499–E0509, E0594–E0597, E0621, E0713, E0716, and friends) appeared exactly
**twice** — 0.15% — and both hits came from a single repair attempt by the smallest GPT
model in the lineup.

The final boss didn't put up a fight. Nobody mentioned it because the fight moved elsewhere.

## What PortBench measures

PortBench wraps the Python→Rust slice of [RustRepoTrans](https://github.com/SYSUSELab/RustRepoTrans)
(SYSUSELab): 108 functions taken from three real crates — apache/iceberg-rust, rust-libp2p,
and charset-normalizer-rs. For each task the model gets the Python source function, the
target Rust signature, and the surrounding declarations — no tools, no retrieval, no test
access, one shot. The emitted function is spliced into the actual crate and the project's
own test suite decides: `cargo test --workspace --no-fail-fast` (or `make unit-test` for
iceberg).

If the attempt fails, the model gets **exactly one repair round**: its own compiler or test
output handed back, nothing else changed.

Two things we did that benchmarks usually skip:

- **Mutation calibration.** Before any model ran, every task's reference solution was
  replaced with `unimplemented!()`. If the suite didn't notice, the task can't detect a
  wrong answer and was excluded. 8 of 108 tasks failed this check, so every published
  number is out of **100 scoreable tasks**.
- **Environment honesty.** Four timing-sensitive libp2p tests turned out to fail under
  machine load or (in one case) on the untouched reference tree. Every verdict they
  contaminated was re-run start-to-finish on an idle machine before publication —
  102 clean reruns. Details in the method notes; the raw JSONL for every attempt,
  including the contaminated ones, is in the repo.

## The leaderboard

Share of the 100 scoreable tasks that compile *and* pass the project's test suite:

| model | one-shot | + one repair | $ / solved task |
|---|---:|---:|---:|
| Claude Fable 5 | **92%** | **100%** | $0.104 |
| Claude Opus 5 | 91% | **100%** | $0.090 |
| Claude Sonnet 5 | 77% | 90% | $0.039 |
| GPT-5.5 | 72% | 91% | $0.035 |
| GPT-5.6 Sol | 72% | 90% | $0.035 |
| GPT-5.6 Terra | 68% | 87% | $0.008 |
| GPT-5.6 Luna | 62% | 85% | **$0.003** |
| Claude Haiku 4.5 | 59% | 78% | $0.028 |

Costs are list-price approximations from recorded token usage, total spend divided by tasks
solved — unsolved work is not free and is not excluded.

Two results stand out:

**Both frontier Claude models go 100/100 when shown their own compiler error once.** Not
"almost all". Every task. The one-shot gap between them (92 vs 91) is noise; the
with-repair ceiling is not. Pooled across all 8 models, one repair round recovers **62% of
all one-shot failures** — and the recovery rate scales with model strength, from ~46% for
the weakest to 100% for the strongest.

**The budget tier is absurdly cheap.** GPT-5.6 Luna solves 85 tasks for about 30 cents
total — $0.003 per solved task. If you're translating a million lines and can tolerate a
15% manual-fix rate, the cost of the first pass is a rounding error.

## What actually fails

The failure taxonomy over every failed attempt:

| rustc error family | count | what it means |
|---|---:|---|
| E0599 — no such method | 119 | model invented or misremembered an API |
| E0308 — type mismatch | 69 | wrong types at a boundary |
| E0609 — no such field | 47 | invented struct fields |
| E0433 / E0425 — unresolved imports/names | 40 | invented paths and modules |
| E0277 — trait bound not satisfied | 22 | wrong abstractions |
| borrow / lifetime / move family | **2** | the "final boss" |

This is not an ownership problem. It's an **API knowledge problem**: models hallucinate
methods, fields, and module paths on crates they half-remember. Ownership discipline —
the thing Rust is supposedly hostile to generators about — is essentially solved in
current frontier models. What they can't do is know a crate's exact surface from memory.

That reframes the tooling question. Retrieval over the target crate's docs/rustdoc would
attack the *actual* top three failure classes. Fighting the borrow checker with clever
prompting attacks a failure mode that no longer exists.

It also explains why the repair round is so effective: "no method named `x`, did you mean
`y`" is exactly the information the model lacked. The compiler isn't an adversary here;
it's the missing documentation.

## Honest caveats

- **Single sample per task.** This is pass@1 with one generation, not best-of-n. Rankings
  between adjacent models are within noise; the tiers are not.
- **Training-data contamination.** These crates are public and pre-2024; models have
  almost certainly seen them. That inflates absolute numbers but applies to every model
  equally — and the E0599/E0609 hallucinations show memorization is far from verbatim.
- **One language pair.** Python→Rust only. The C→Rust and Java→Rust slices are follow-up
  material.
- **Paper comparison is directional.** We rebuilt the harness (the original's success
  predicate had soundness issues we document in the repo), so our absolute numbers aren't
  comparable to the paper's — but error-*class* distributions are, and that's where the
  borrow-checker claim dies.
- **Dataset license.** RustRepoTrans carries no license, so PortBench never redistributes
  it: the harness clones it at run time, results store task IDs and content hashes only,
  and gallery code shows model output spliced into permissively-licensed upstream files.

## The living page

The leaderboard is a static site over versioned JSON — every sweep is a data commit. When
a new model ships, we run the 108 tasks (~$1–12 and an afternoon) and the page, the trend
line, and this taxonomy update themselves. Want a model added, or an open-weights lane?
PRs welcome: the harness, the aggregation, and every raw attempt record are in the repo.

*Dataset credit: RustRepoTrans by SYSUSELab — the task selection, paired functions, and
dependency context are entirely their work. PortBench adds the harness, the current-model
runs, the taxonomy, and the site.*
