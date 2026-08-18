# PortBench

How good are today's frontier AI models at translating real Python code into
working, test-passing Rust?

PortBench runs frontier models against the 108 Python→Rust translation tasks
from [RustRepoTrans](https://github.com/WinterSunset95/RustRepoTrans) (SYSU
SELab) — each task verified by a real test suite — and publishes:

- a **leaderboard**: pass rate per model, one-shot and after one repair round
  (the model is shown the compiler error once and retries),
- a **failure taxonomy**: borrow-checker vs dependency vs logic errors,
- **cost per solved task**: tokens burned per passing solution,
- a **failure gallery**: real model output next to the compiler error that
  rejected it.

Results live as versioned JSON in this repo; the site renders them
client-side, so adding a new model run is just a data commit.

## Status

Work in progress — build graph in [board/state.json](board/state.json).

## Credit

The task dataset is RustRepoTrans, by its original authors. PortBench adds
the harness, current-model runs, taxonomy, and site.
