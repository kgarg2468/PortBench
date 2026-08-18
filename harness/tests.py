"""Offline unit tests for the output parser, the verdict function and the run-safety rails.

No cargo, no model, no dataset -- everything runs against synthetic cargo output and temp
directories, so this is the cheap regression gate for the scoring rules. Run with
`python -m harness.run --unit-test`.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

from . import expected, inject, lock, score, tasks
from .run import ResultsWriter, RunIdCollision, snapshot_health_problems
from .score import TestRun
from .tasks import Task

# A clean workspace run: several binaries, all green, exit 0.
OK_OUTPUT = "\n".join(
    f"running 3 tests\ntest a::t{i} ... ok\n"
    f"test result: ok. 3 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.1s\n"
    for i in range(3)
)

# The real baseline shape: cargo exits 101, but every failure is named AND summarised.
BASELINE_STDOUT = (
    OK_OUTPUT
    + "\nrunning 1 test\ntest tests::basic_resolve ... FAILED\n"
    + "test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 15.0s\n"
)
BASELINE_STDERR = "error: test failed, to rerun pass `-p libp2p-dns --lib`\n"

REFERENCE_FN = "pub fn answer(x: u32) -> u32 {\n    x + 1\n}"


def _check(name: str, got, want) -> bool:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r}\n       want={want!r}"))
    return ok


def _fake_task(**overrides) -> Task:
    fields = dict(
        task_id="projects__demo__rust__src__lib__.rs__function__3",
        project="charset-normalizer",
        target_rel="projects/demo/rust/src/lib.rs",
        target_path=Path("/nonexistent/lib.rs"),
        reference_rust_fn=REFERENCE_FN,
        python_src_fn="def answer(x): return x + 1",
        target_signature="pub fn answer(x: u32) -> u32 ",
        dep_decls="struct Thing;",
        dep_uses="use std::fmt;",
        content_hash="0" * 64,
    )
    fields.update(overrides)
    return Task(**fields)


def run_unit_tests() -> int:
    results: list[bool] = []
    # The offline fixtures carry a handful of synthetic summaries, so the real per-project
    # test-target count must not be applied to them.
    NO_TARGET_CHECK = {"expected_summaries": None}

    # 1. Clean run -> PASS.
    v = score.score("libp2p", ".", TestRun(0, OK_OUTPUT, "", 1.0), **NO_TARGET_CHECK)
    results.append(_check("clean run is PASS", v.verdict, score.PASS))

    # 2. Nonzero exit fully explained by an allowlisted failure -> still PASS.
    v = score.score("libp2p", ".", TestRun(101, BASELINE_STDOUT, BASELINE_STDERR, 1.0),
                    **NO_TARGET_CHECK)
    results.append(_check("baselined failure + rc=101 is PASS", v.verdict, score.PASS))
    results.append(_check("  ...and is recorded as baselined",
                          v.baselined, ["tests::basic_resolve"]))

    # 3. A named, non-allowlisted failure -> TEST_FAIL.
    fail_out = OK_OUTPUT + (
        "\nrunning 1 test\ntest cd::wrong_answer ... FAILED\n"
        "test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.1s\n"
    )
    v = score.score("charset-normalizer", ".",
                    TestRun(101, fail_out, "error: test failed, to rerun pass `--lib`\n", 1.0),
                    **NO_TARGET_CHECK)
    results.append(_check("named failure is TEST_FAIL", v.verdict, score.TEST_FAIL))
    results.append(_check("  ...with the failing name", v.failing_tests, ["cd::wrong_answer"]))

    # 4. REGRESSION (PR #1 review): a test binary aborts without printing a summary or a
    #    FAILED line, while other binaries report normally. Must NOT be PASS.
    crash_stderr = (
        "error: test failed, to rerun pass `-p charset-normalizer --lib`\n"
        "error: process didn't exit successfully: `/target/debug/deps/x-1` (signal: 11, SIGSEGV)\n"
    )
    v = score.score("charset-normalizer", ".", TestRun(101, OK_OUTPUT, crash_stderr, 1.0),
                    **NO_TARGET_CHECK)
    results.append(_check("crashed binary is not PASS", v.verdict != score.PASS, True))
    results.append(_check("crashed binary is SUITE_ERROR", v.verdict, score.SUITE_ERROR))
    results.append(_check("  ...with a reason", bool(v.reason), True))

    # 5. Nonzero exit with clean summaries and no failure at all -> SUITE_ERROR.
    v = score.score("charset-normalizer", ".", TestRun(101, OK_OUTPUT, "", 1.0), **NO_TARGET_CHECK)
    results.append(_check("unexplained rc!=0 is SUITE_ERROR", v.verdict, score.SUITE_ERROR))

    # 6. Compile failure still wins over everything else.
    v = score.score("charset-normalizer", ".", TestRun(
        101, "", "error[E0308]: mismatched types\nerror: could not compile `x` (lib test)\n", 1.0),
        **NO_TARGET_CHECK)
    results.append(_check("compile failure is COMPILE_ERROR", v.verdict, score.COMPILE_ERROR))
    results.append(_check("  ...with rustc codes", v.error_class_hint, ["E0308"]))

    # 6b. #5: the phrase alone, on a SUCCESSFUL run, is not a compile error.
    chatty = OK_OUTPUT + "\ntest output mentioning could not compile in a fixture\n"
    results.append(_check("'could not compile' on rc=0 is not COMPILE_ERROR",
                          score.is_compile_error(TestRun(0, chatty, "", 1.0)), False))
    results.append(_check("  ...and such a run still PASSes",
                          score.score("libp2p", ".", TestRun(0, chatty, "", 1.0),
                                      **NO_TARGET_CHECK).verdict, score.PASS))
    # 6c. #5: a rustc error code with no cargo phrase is still a compile error.
    results.append(_check("error[Exxxx] with rc!=0 is COMPILE_ERROR",
                          score.is_compile_error(
                              TestRun(101, "", "error[E0432]: unresolved import\n", 1.0)), True))

    # 7. Timeout wins over everything.
    v = score.score("libp2p", ".", TestRun(-1, "", "", 1.0, timed_out=True), **NO_TARGET_CHECK)
    results.append(_check("timeout is TIMEOUT", v.verdict, score.TIMEOUT))

    # 8. Summary/name mismatch (2 failures counted, 1 named) -> SUITE_ERROR.
    skewed = OK_OUTPUT + (
        "\nrunning 2 tests\ntest a::one ... FAILED\n"
        "test result: FAILED. 0 passed; 2 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.1s\n"
    )
    v = score.score("libp2p", ".", TestRun(101, skewed, "", 1.0), **NO_TARGET_CHECK)
    results.append(_check("summary/name skew is not PASS", v.verdict != score.PASS, True))

    # 8b. #4: the SAME test name failing in two binaries is two failures and one name.
    #     Reconciling the deduplicated name list against summed counts invented a mismatch.
    two_binaries = OK_OUTPUT + (
        "\nrunning 1 test\ntest shared::roundtrip ... FAILED\n"
        "test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.1s\n"
        "\nrunning 1 test\ntest shared::roundtrip ... FAILED\n"
        "test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.1s\n"
    )
    two_stderr = ("error: test failed, to rerun pass `-p a --lib`\n"
                  "error: test failed, to rerun pass `-p b --lib`\n")
    run = TestRun(101, two_binaries, two_stderr, 1.0)
    results.append(_check("two binaries, same failing name: reconciles",
                          score.unaccounted_reasons(run), []))
    v = score.score("charset-normalizer", ".", run, **NO_TARGET_CHECK)
    results.append(_check("  ...verdict is TEST_FAIL, not SUITE_ERROR", v.verdict, score.TEST_FAIL))
    results.append(_check("  ...reported once by name", v.failing_tests, ["shared::roundtrip"]))
    results.append(_check("  ...but counted twice per binary",
                          sum(len(b.failures) for b in score.parse_blocks(run)), 2))

    # 8c. #3: a test target that never printed a summary (exit(0) before reporting) is not PASS,
    #     even though cargo exited 0 and every summary present is green.
    v = score.score("libp2p", ".", TestRun(0, OK_OUTPUT, "", 1.0), expected_summaries=4)
    results.append(_check("missing test-target summary is SUITE_ERROR", v.verdict, score.SUITE_ERROR))
    results.append(_check("  ...with a reason naming the count",
                          "expected 4" in v.reason, True))
    v = score.score("libp2p", ".", TestRun(0, OK_OUTPUT, "", 1.0), expected_summaries=3)
    results.append(_check("full complement of summaries is PASS", v.verdict, score.PASS))

    # 9. The environment handed to the test subprocess carries no stray secrets.
    os.environ["PORTBENCH_UNITTEST_FAKE_TOKEN"] = "sk-should-not-leak"
    try:
        env = score._env()
    finally:
        os.environ.pop("PORTBENCH_UNITTEST_FAKE_TOKEN", None)
    results.append(_check("env scrub drops unknown vars",
                          "PORTBENCH_UNITTEST_FAKE_TOKEN" in env, False))
    results.append(_check("env scrub keeps PATH", "PATH" in env, True))
    results.append(_check("env scrub is an allowlist",
                          set(env) - set(score.ENV_ALLOWLIST) - {"CARGO_TERM_COLOR"}, set()))

    # 10. #6: the answer must contain the function that was asked for.
    task = _fake_task()
    helper_only = inject.extract("```rust\nfn helper(a: u32) -> u32 { a }\n```")
    try:
        inject.require_target_function(helper_only.functions, task)
        results.append(_check("wrong-function answer is rejected", "accepted", "rejected"))
    except inject.ExtractionError as exc:
        results.append(_check("wrong-function answer is rejected", True, True))
        results.append(_check("  ...and says what was harvested", "helper" in str(exc), True))

    good = inject.extract("```rust\nfn helper(a: u32) -> u32 { a }\n" + REFERENCE_FN + "\n```")
    results.append(_check("right function present is accepted",
                          inject.require_target_function(good.functions, task), "answer"))
    results.append(_check("  ...and the extra helper is kept", len(good.functions), 2))

    # 11. #12: a dependency block quoting the answer is stripped, exactly.
    leaky = f"struct Thing;\n\n{REFERENCE_FN}\n\npub fn other() -> u8 {{ 0 }}\n"
    results.append(_check("leak is detected", tasks.contains_reference(leaky, REFERENCE_FN), True))
    cleaned, stripped = tasks.strip_leaked_reference(leaky, REFERENCE_FN)
    results.append(_check("leak is stripped", stripped, True))
    results.append(_check("  ...and the reference is gone",
                          tasks.contains_reference(cleaned, REFERENCE_FN), False))
    results.append(_check("  ...while real dependencies survive",
                          "struct Thing;" in cleaned and "pub fn other" in cleaned, True))
    untouched, stripped = tasks.strip_leaked_reference("struct Thing;", REFERENCE_FN)
    results.append(_check("a clean dependency block is untouched",
                          (untouched, stripped), ("struct Thing;", False)))

    # 12. #8: an anchor with two byte-identical candidates resolves by ordinal, not by replace.
    dup_file = (
        "fn a() {}\n" + REFERENCE_FN + "\nfn b() {}\n" + REFERENCE_FN + "\n"
    )
    #   function_item order: a(0), answer(1), b(2), answer(3) -> `__function__2` is 1-based -> 1
    anchor = inject.resolve_anchor(dup_file, _fake_task(
        task_id="projects__demo__rust__src__lib__.rs__function__2"))
    results.append(_check("duplicate anchor is seen as ambiguous", anchor.matches, 2))
    results.append(_check("  ...resolved by ordinal", anchor.resolved_by, "ordinal"))
    results.append(_check("  ...selecting the first occurrence",
                          dup_file[anchor.start_byte:anchor.end_byte] == REFERENCE_FN
                          and anchor.start_byte < dup_file.rindex("pub fn answer"), True))
    anchor4 = inject.resolve_anchor(dup_file, _fake_task(
        task_id="projects__demo__rust__src__lib__.rs__function__4"))
    results.append(_check("  ...and the other ordinal selects the second",
                          anchor4.start_byte == dup_file.rindex("pub fn answer"), True))

    # 13. #15: a run id that already exists is refused, and --resume dedupes.
    with tempfile.TemporaryDirectory() as out:
        ResultsWriter(Path(out), "haiku", "r1")
        (Path(out) / "haiku" / "r1.jsonl").write_text(
            json.dumps({"task_id": "t1", "attempt": 0, "verdict": score.PASS}) + "\n",
            encoding="utf-8")
        try:
            ResultsWriter(Path(out), "haiku", "r1")
            results.append(_check("existing run id is refused", "accepted", "refused"))
        except RunIdCollision:
            results.append(_check("existing run id is refused", True, True))
        resumed = ResultsWriter(Path(out), "haiku", "r1", resume=True)
        results.append(_check("  ...--resume reads the existing attempts",
                              resumed.existing, {("t1", 0): score.PASS}))
        results.append(_check("  ...and reports the finished task as done",
                              resumed.done_task_ids(), {"t1"}))

    # 13b. REVIEW (PR #2): an attempt 0 the repair round owes a follow-up to is NOT done.
    with tempfile.TemporaryDirectory() as out:
        (Path(out) / "haiku").mkdir(parents=True)
        (Path(out) / "haiku" / "r2.jsonl").write_text("\n".join(
            json.dumps(r) for r in [
                {"task_id": "owed", "attempt": 0, "verdict": score.TEST_FAIL},
                {"task_id": "repaired", "attempt": 0, "verdict": score.TEST_FAIL},
                {"task_id": "repaired", "attempt": 1, "verdict": score.PASS},
                {"task_id": "passed", "attempt": 0, "verdict": score.PASS},
                {"task_id": "unrepairable", "attempt": 0, "verdict": score.TRANSPORT_ERROR},
            ]) + "\n", encoding="utf-8")
        w = ResultsWriter(Path(out), "haiku", "r2", resume=True)
        results.append(_check("resume: repair-eligible attempt 0 alone is NOT done",
                              w.done_task_ids(repair=True),
                              {"repaired", "passed", "unrepairable"}))
        results.append(_check("  ...but is done when --no-repair",
                              w.done_task_ids(repair=False),
                              {"owed", "repaired", "passed", "unrepairable"}))
        # Re-running the owed task must append attempt 1 without duplicating attempt 0.
        from .run import AttemptRecord
        w.write(AttemptRecord(run_id="r2", model="haiku", task_id="owed", project="p",
                              attempt=0, verdict=score.TEST_FAIL))
        w.write(AttemptRecord(run_id="r2", model="haiku", task_id="owed", project="p",
                              attempt=1, verdict=score.PASS))
        lines = [json.loads(l) for l in
                 (Path(out) / "haiku" / "r2.jsonl").read_text().splitlines() if l.strip()]
        results.append(_check("  ...re-run does not duplicate attempt 0",
                              sum(1 for r in lines
                                  if r["task_id"] == "owed" and r["attempt"] == 0), 1))
        results.append(_check("  ...and appends the missing attempt 1",
                              sum(1 for r in lines
                                  if r["task_id"] == "owed" and r["attempt"] == 1), 1))
        results.append(_check("  ...after which the task is done",
                              "owed" in w.done_task_ids(repair=True), True))

    # 14. #9: a second runner cannot take a lock the first one holds.
    with tempfile.TemporaryDirectory() as dataset:
        lock.acquire(Path(dataset), "run-a")
        try:
            lock.acquire(Path(dataset), "run-b")
            results.append(_check("second runner is locked out", "acquired", "refused"))
        except lock.LockError as exc:
            results.append(_check("second runner is locked out", True, True))
            results.append(_check("  ...and is told who holds it", "run-a" in str(exc), True))
        lock.release(Path(dataset))
        results.append(_check("  ...lock is released", lock.lock_path(Path(dataset)).exists(), False))
        # A lock left by a dead process is reclaimed rather than blocking forever.
        lock.lock_path(Path(dataset)).write_text(
            json.dumps({"pid": 2 ** 22, "run_id": "ghost"}), encoding="utf-8")
        lock.acquire(Path(dataset), "run-c")
        results.append(_check("stale lock is reclaimed",
                              lock.read_lock(Path(dataset)).get("run_id"), "run-c"))
        lock.release(Path(dataset))

    # 14b. REVIEW (PR #2): a lock carrying no readable metadata must be treated as HELD.
    #      Reclaiming it on sight let a second runner delete a lock the first had just created
    #      but not yet written, and both then injected into the same tree.
    with tempfile.TemporaryDirectory() as dataset:
        path = lock.lock_path(Path(dataset))
        path.write_text("", encoding="utf-8")
        try:
            lock.acquire(Path(dataset), "run-d")
            results.append(_check("empty lock file is treated as held", "acquired", "refused"))
        except lock.LockError as exc:
            results.append(_check("empty lock file is treated as held", True, True))
            results.append(_check("  ...and says it is unreadable, not stale",
                                  "no readable metadata" in str(exc), True))
        results.append(_check("  ...and is not deleted", path.exists(), True))
        # Only once it is provably older than the grace period may it be reclaimed.
        old = time.time() - (lock.UNREADABLE_LOCK_GRACE_S + 60)
        os.utime(path, (old, old))
        lock.acquire(Path(dataset), "run-e")
        results.append(_check("  ...but an aged unreadable lock is reclaimed",
                              lock.read_lock(Path(dataset)).get("run_id"), "run-e"))
        lock.release(Path(dataset))
        # A lock is never observable without its metadata: creation is atomic.
        lock.acquire(Path(dataset), "run-f")
        results.append(_check("a fresh lock always carries a pid",
                              lock.read_lock(Path(dataset)).get("pid"), os.getpid()))
        results.append(_check("  ...and release leaves a foreign lock alone",
                              _release_foreign_lock(Path(dataset)), True))

    # 14c. REVIEW (PR #2): a snapshot may only be recorded from a healthy run.
    good = TestRun(0, OK_OUTPUT, "", 1.0)
    results.append(_check("healthy snapshot run is accepted",
                          snapshot_health_problems(good, 3), []))
    results.append(_check("timed-out snapshot run is refused",
                          bool(snapshot_health_problems(
                              TestRun(-1, OK_OUTPUT, "", 1.0, timed_out=True), 3)), True))
    results.append(_check("non-compiling snapshot run is refused",
                          bool(snapshot_health_problems(
                              TestRun(101, "", "error: could not compile `x`\n", 1.0), 0)), True))
    results.append(_check("unreconciled snapshot run is refused",
                          bool(snapshot_health_problems(TestRun(101, OK_OUTPUT, "", 1.0), 3)), True))
    results.append(_check("zero-summary snapshot run is refused",
                          bool(snapshot_health_problems(TestRun(0, "", "", 1.0), 0)), True))

    with tempfile.TemporaryDirectory() as cache_dir:
        original_cache = expected.CACHE_PATH
        expected.CACHE_PATH = Path(cache_dir) / "expected_summaries.json"
        try:
            results.append(_check("first snapshot is recorded", expected.record("p", 158), True))
            results.append(_check("  ...and is readable back", expected.expected("p"), 158))
            try:
                expected.record("p", 0)
                results.append(_check("zero count is refused", "recorded", "refused"))
            except expected.SnapshotRefused:
                results.append(_check("zero count is refused", True, True))
            try:
                expected.record("p", 12)
                results.append(_check("lowering a count needs --force-snapshot",
                                      "recorded", "refused"))
            except expected.SnapshotRefused:
                results.append(_check("lowering a count needs --force-snapshot", True, True))
            results.append(_check("  ...and the old count survives the refusal",
                                  expected.expected("p"), 158))
            results.append(_check("  ...while --force-snapshot lowers it",
                                  expected.record("p", 12, force=True), True))
            results.append(_check("raising a count needs no force", expected.record("p", 200), True))
        finally:
            expected.CACHE_PATH = original_cache

    # 15. #14: codex JSONL shapes that used to be ignored.
    from . import models
    turn_completed = json.dumps({
        "type": "turn.completed",
        "usage": {"input_tokens": 1200, "cached_input_tokens": 900, "output_tokens": 340},
    })
    results.append(_check("codex turn.completed usage is parsed",
                          models._codex_tokens(turn_completed), (1200, 340, 1540)))
    legacy = json.dumps({"msg": {"type": "token_count",
                                 "info": {"total_token_usage": {"input_tokens": 10,
                                                                "output_tokens": 5}}}})
    results.append(_check("codex legacy token_count still parsed",
                          models._codex_tokens(legacy), (10, 5, 15)))
    results.append(_check("codex human fallback still parsed",
                          models._codex_tokens("tokens used: 1,234"), (None, None, 1234)))
    results.append(_check("codex with no token info is null",
                          models._codex_tokens("nothing here"), (None, None, None)))

    item_completed = "\n".join([
        json.dumps({"type": "item.started", "item": {"item_type": "reasoning", "text": "hmm"}}),
        json.dumps({"type": "item.completed",
                    "item": {"id": "item_1", "item_type": "agent_message", "text": "```rust\nfn f(){}\n```"}}),
        turn_completed,
    ])
    results.append(_check("codex item.completed text is the final message",
                          models._codex_final_message(item_completed), "```rust\nfn f(){}\n```"))
    legacy_msg = json.dumps({"msg": {"type": "agent_message", "message": "hello"}})
    results.append(_check("codex legacy agent_message still parsed",
                          models._codex_final_message(legacy_msg), "hello"))

    # 16. #11: model argv is isolated (no tools) and recorded.
    claude_argv = models.argv_preview("haiku")
    results.append(_check("claude argv disables all tools",
                          claude_argv[claude_argv.index("--tools") + 1], ""))
    results.append(_check("claude argv drops user configuration",
                          "--safe-mode" in claude_argv and "--strict-mcp-config" in claude_argv, True))
    codex_argv = models.argv_preview("gpt-5.6-sol")
    results.append(_check("codex argv stays read-only",
                          codex_argv[codex_argv.index("-s") + 1], "read-only"))
    results.append(_check("codex argv disables the shell tool",
                          "shell_tool" in codex_argv and "unified_exec" in codex_argv, True))

    # 17. #2: a flaky rerun only counts when it actually ran the test and came back clean.
    passing = "running 1 test\ntest x::flaky ... ok\ntest result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.1s\n"
    empty = "running 0 tests\ntest result: ok. 0 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.0s\n"
    results.append(_check("rerun that executed nothing is not a recovery",
                          _rerun_verdict(empty, 0), False))
    results.append(_check("rerun that failed to build is not a recovery",
                          _rerun_verdict("", 101), False))
    results.append(_check("rerun that genuinely passed is a recovery",
                          _rerun_verdict(passing, 0), True))

    # 18. --keep-artifacts: the failure gallery needs the code the model actually wrote, which
    #     the harness otherwise reverts and forgets. Fake adapter, fake cargo, temp dir.
    results.extend(_artifact_checks())

    passed = sum(results)
    print(f"\nunit tests: {passed}/{len(results)} ok")
    return 0 if passed == len(results) else 1


GOOD_ANSWER = ("Here is the translation.\n\n```rust\nuse std::fmt::Debug;\n\n"
               "pub fn answer(x: u32) -> u32 {\n    x + 2\n}\n```\n")
PROSE_ANSWER = "I could not translate that function.\n"


def _artifact_checks() -> list[bool]:
    """Drive `run_task` end to end with a fake model CLI and a fake cargo, under --keep-artifacts.

    Covers both artifact shapes in one run: attempt 0 injects and fails its tests (answer +
    injected + error), attempt 1 answers in prose (answer + error, and NO injected.rs, because
    nothing reached the tree).
    """
    from . import models
    from .run import ARTIFACT_ERROR_LIMIT, ResultsWriter, run_task

    answers = [GOOD_ANSWER, PROSE_ANSWER]
    served: list[str] = []

    def fake_call_model(model, prompt, timeout=None):
        text = answers[min(len(served), len(answers) - 1)]
        served.append(text)
        return models.ModelResult(text, 10, 20, 30, 0.1, resolved_model="fake-model-1")

    # charset-normalizer expects 4 summaries; OK_OUTPUT carries 3 plus this failing one.
    fail_run = TestRun(101, OK_OUTPUT + (
        "\nrunning 1 test\ntest cd::wrong_answer ... FAILED\n\n"
        "failures:\n\n---- cd::wrong_answer stdout ----\n"
        "assertion `left == right` failed\n  left: 3\n right: 4\n\n"
        "failures:\n    cd::wrong_answer\n\n"
        "test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; "
        "finished in 0.1s\n"), "error: test failed, to rerun pass `--lib`\n", 1.0)

    checks: list[bool] = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        target = root / "lib.rs"
        original = "fn other() {}\n\n" + REFERENCE_FN + "\n"
        target.write_text(original, encoding="utf-8")
        task = _fake_task(target_path=target)
        out = root / "results"

        real_call, real_run_tests = models.call_model, score.run_tests
        models.call_model = fake_call_model                     # type: ignore[assignment]
        score.run_tests = lambda *a, **k: fail_run              # type: ignore[assignment]
        try:
            kept = ResultsWriter(out, "haiku", "kept", keep_artifacts=True)
            run_task(task, "haiku", "kept", root / "dataset", kept, 10, 10, True)
            served.clear()
            plain = ResultsWriter(out, "haiku", "plain")
            run_task(task, "haiku", "plain", root / "dataset", plain, 10, 10, True)
        finally:
            models.call_model = real_call                       # type: ignore[assignment]
            score.run_tests = real_run_tests                    # type: ignore[assignment]

        base = out / "haiku" / "kept.artifacts" / task.task_id
        a0, a1 = base / "attempt0", base / "attempt1"
        checks.append(_check("keep-artifacts: attempt 0 writes all three files",
                             sorted(p.name for p in a0.iterdir()),
                             ["answer.md", "error.txt", "injected.rs"]))
        checks.append(_check("  ...answer.md is the raw model text",
                             a0.joinpath("answer.md").read_text(), GOOD_ANSWER))
        injected = a0.joinpath("injected.rs").read_text()
        checks.append(_check("  ...injected.rs carries the use line and the function",
                             "use std::fmt::Debug;" in injected and "x + 2" in injected, True))
        checks.append(_check("  ...and not the surrounding file",
                             "fn other()" in injected, False))
        checks.append(_check("  ...error.txt carries the failure output",
                             "cd::wrong_answer" in a0.joinpath("error.txt").read_text(), True))
        checks.append(_check("keep-artifacts: an unextractable answer writes no injected.rs",
                             sorted(p.name for p in a1.iterdir()),
                             ["answer.md", "error.txt"]))
        checks.append(_check("  ...but still keeps the raw answer",
                             a1.joinpath("answer.md").read_text(), PROSE_ANSWER))
        checks.append(_check("  ...and the extraction error",
                             "function_item" in a1.joinpath("error.txt").read_text(), True))
        checks.append(_check("  ...and the tree is still restored",
                             target.read_text(), original))

        records = [json.loads(l) for l in
                   (out / "haiku" / "kept.jsonl").read_text().splitlines() if l.strip()]
        checks.append(_check("keep-artifacts: the record points at the directory",
                             [r.get("artifacts_dir") for r in records],
                             [f"haiku/kept.artifacts/{task.task_id}/attempt0",
                              f"haiku/kept.artifacts/{task.task_id}/attempt1"]))
        checks.append(_check("  ...relative to the results root, so it resolves",
                             (out / records[0]["artifacts_dir"] / "answer.md").exists(), True))

        # Default: nothing written, and the JSONL schema is untouched.
        plain_records = [json.loads(l) for l in
                         (out / "haiku" / "plain.jsonl").read_text().splitlines() if l.strip()]
        checks.append(_check("no flag: no artifacts directory",
                             (out / "haiku" / "plain.artifacts").exists(), False))
        checks.append(_check("  ...and no artifacts_dir key on the records",
                             [("artifacts_dir" in r) for r in plain_records], [False, False]))
        checks.append(_check("  ...while the run still scored normally",
                             [r["verdict"] for r in plain_records],
                             [score.TEST_FAIL, score.EXTRACTION_ERROR]))

        # A runaway panic loop can print megabytes; error.txt is capped.
        kept.record_artifacts("big-task", 0, answer="a", injected=None,
                              error_text="x" * (ARTIFACT_ERROR_LIMIT * 2) + "TAIL")
        capped = (out / "haiku" / "kept.artifacts" / "big-task" / "attempt0" / "error.txt")
        checks.append(_check("error.txt is truncated",
                             len(capped.read_text()) <= ARTIFACT_ERROR_LIMIT + 200, True))
        checks.append(_check("  ...keeping the tail, where the failure list is",
                             capped.read_text().endswith("TAIL"), True))
    return checks


def _release_foreign_lock(dataset: Path) -> bool:
    """True if `release` left a lock held by another pid in place."""
    path = lock.lock_path(dataset)
    path.write_text(json.dumps({"pid": 2 ** 22, "run_id": "someone-else"}), encoding="utf-8")
    lock.release(dataset)
    survived = path.exists()
    path.unlink(missing_ok=True)
    return survived


def _rerun_verdict(stdout: str, returncode: int) -> bool:
    """Drive `score._rerun_by_name`'s acceptance rules against canned output."""
    original = score.run_tests
    score.run_tests = lambda *a, **k: TestRun(returncode, stdout, "", 0.1)  # type: ignore[assignment]
    try:
        return score._rerun_by_name(Path("."), "libp2p", "x::flaky", 10)
    finally:
        score.run_tests = original  # type: ignore[assignment]


if __name__ == "__main__":
    sys.exit(run_unit_tests())
