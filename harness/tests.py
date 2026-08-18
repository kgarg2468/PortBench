"""Offline unit tests for the output parser and the verdict function.

No cargo, no model, no dataset -- everything runs against synthetic cargo output, so this is
the cheap regression gate for the scoring rules. Run with `python -m harness.run --unit-test`.
"""

from __future__ import annotations

import sys

from . import score
from .score import TestRun

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


def _check(name: str, got, want) -> bool:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" + ("" if ok else f"\n       got={got!r}\n       want={want!r}"))
    return ok


def run_unit_tests() -> int:
    results: list[bool] = []

    # 1. Clean run -> PASS.
    v = score.score("libp2p", ".", TestRun(0, OK_OUTPUT, "", 1.0))
    results.append(_check("clean run is PASS", v.verdict, score.PASS))

    # 2. Nonzero exit fully explained by an allowlisted failure -> still PASS.
    v = score.score("libp2p", ".", TestRun(101, BASELINE_STDOUT, BASELINE_STDERR, 1.0))
    results.append(_check("baselined failure + rc=101 is PASS", v.verdict, score.PASS))
    results.append(_check("  ...and is recorded as baselined",
                          v.baselined, ["tests::basic_resolve"]))

    # 3. A named, non-allowlisted failure -> TEST_FAIL.
    fail_out = OK_OUTPUT + (
        "\nrunning 1 test\ntest cd::wrong_answer ... FAILED\n"
        "test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.1s\n"
    )
    v = score.score("charset-normalizer", ".",
                    TestRun(101, fail_out, "error: test failed, to rerun pass `--lib`\n", 1.0))
    results.append(_check("named failure is TEST_FAIL", v.verdict, score.TEST_FAIL))
    results.append(_check("  ...with the failing name", v.failing_tests, ["cd::wrong_answer"]))

    # 4. REGRESSION (PR #1 review): a test binary aborts without printing a summary or a
    #    FAILED line, while other binaries report normally. Must NOT be PASS.
    crash_stderr = (
        "error: test failed, to rerun pass `-p charset-normalizer --lib`\n"
        "error: process didn't exit successfully: `/target/debug/deps/x-1` (signal: 11, SIGSEGV)\n"
    )
    v = score.score("charset-normalizer", ".", TestRun(101, OK_OUTPUT, crash_stderr, 1.0))
    results.append(_check("crashed binary is not PASS", v.verdict != score.PASS, True))
    results.append(_check("crashed binary is SUITE_ERROR", v.verdict, score.SUITE_ERROR))
    results.append(_check("  ...with a reason", bool(v.reason), True))

    # 5. Nonzero exit with clean summaries and no failure at all -> SUITE_ERROR.
    v = score.score("charset-normalizer", ".", TestRun(101, OK_OUTPUT, "", 1.0))
    results.append(_check("unexplained rc!=0 is SUITE_ERROR", v.verdict, score.SUITE_ERROR))

    # 6. Compile failure still wins over everything else.
    v = score.score("charset-normalizer", ".", TestRun(
        101, "", "error[E0308]: mismatched types\nerror: could not compile `x` (lib test)\n", 1.0))
    results.append(_check("compile failure is COMPILE_ERROR", v.verdict, score.COMPILE_ERROR))
    results.append(_check("  ...with rustc codes", v.error_class_hint, ["E0308"]))

    # 7. Timeout wins over everything.
    v = score.score("libp2p", ".", TestRun(-1, "", "", 1.0, timed_out=True))
    results.append(_check("timeout is TIMEOUT", v.verdict, score.TIMEOUT))

    # 8. Summary/name mismatch (2 failures counted, 1 named) -> SUITE_ERROR.
    skewed = OK_OUTPUT + (
        "\nrunning 2 tests\ntest a::one ... FAILED\n"
        "test result: FAILED. 0 passed; 2 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.1s\n"
    )
    v = score.score("libp2p", ".", TestRun(101, skewed, "", 1.0))
    results.append(_check("summary/name skew is not PASS", v.verdict != score.PASS, True))

    # 9. The environment handed to the test subprocess carries no stray secrets.
    import os
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

    passed = sum(results)
    print(f"\nunit tests: {passed}/{len(results)} ok")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(run_unit_tests())
