"""Run a project's test suite and turn its output into a verdict.

Deliberately does NOT reuse the paper's `output.split("\\n")[-3].startswith("test result: ok")`
predicate: under --no-fail-fast that only inspects the last test binary's summary, so an early
failure still scores as success. We parse every `test result:` summary and every
`test <name> ... FAILED` line instead, then subtract the per-project baseline allowlist.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import baseline, expected

# SPIKE (d)+(f): workspace-wide, never fail-fast. `-p <crate>` narrowing is not usable here
# because workspace feature unification changes what compiles.
TEST_COMMANDS: dict[str, list[str]] = {
    "charset-normalizer": ["cargo", "test", "--workspace", "--no-fail-fast"],
    "libp2p": ["cargo", "test", "--workspace", "--no-fail-fast"],
    # iceberg's Makefile target is already --no-fail-fast (doc tests then --lib --all-features).
    "iceberg": ["make", "unit-test"],
}

DEFAULT_TEST_TIMEOUT = 1800

PASS = "PASS"
COMPILE_ERROR = "COMPILE_ERROR"
TEST_FAIL = "TEST_FAIL"
SUITE_ERROR = "SUITE_ERROR"
EXTRACTION_ERROR = "EXTRACTION_ERROR"
TRANSPORT_ERROR = "TRANSPORT_ERROR"
TIMEOUT = "TIMEOUT"

_FAILED_TEST_RE = re.compile(r"^test (.+?) \.\.\. FAILED\s*$", re.M)
_SUMMARY_RE = re.compile(
    r"^test result: (ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored", re.M
)
# Every test binary brackets its own output with these two lines, which is the only
# per-target segmentation available: cargo's own `Running <target>` lines go to stderr while
# the binary's test lines go to stdout, so the two cannot be interleaved back together.
_RUNNING_RE = re.compile(r"^running (\d+) tests?\s*$")
_FAILED_LINE_RE = re.compile(r"^test (.+?) \.\.\. FAILED\s*$")
_SUMMARY_LINE_RE = re.compile(
    r"^test result: (ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored"
)
_ERROR_CODE_RE = re.compile(r"error\[(E\d{4})\]")
# cargo prints one of these per test target that did not come back clean.
_CARGO_TEST_FAILED_RE = re.compile(r"^error: test failed", re.M)
# A binary that segfaults/aborts never prints a `test result:` summary at all.
_ABORT_RE = re.compile(
    r"^error: (?:process didn't exit successfully|could not execute process|"
    r"test process (?:aborted|exited)|Failed to execute)", re.M
)


@dataclass
class TestRun:
    returncode: int
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool = False


@dataclass
class Verdict:
    verdict: str
    failing_tests: list[str] = field(default_factory=list)
    error_class_hint: list[str] = field(default_factory=list)
    error_text: str = ""
    summaries: list[tuple[str, int, int, int]] = field(default_factory=list)
    baselined: list[str] = field(default_factory=list)
    flaky_recovered: list[str] = field(default_factory=list)
    reason: str = ""


# The test command compiles and runs MODEL-GENERATED code. We cannot sandbox it here (see the
# security note in harness/README.md), but we can refuse to hand it the parent process's
# secrets: only this allowlist is forwarded, so API keys, tokens and cloud credentials sitting
# in the harness environment are not inherited by the test subprocess.
ENV_ALLOWLIST = (
    "PATH", "HOME", "TMPDIR", "TERM", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL",
    "CARGO_HOME", "RUSTUP_HOME", "RUSTUP_TOOLCHAIN", "RUST_BACKTRACE",
    "SDKROOT", "DEVELOPER_DIR",  # macOS linking
)

# Escape hatch for projects that genuinely need another variable, e.g.
# PORTBENCH_EXTRA_ENV=SSL_CERT_FILE,HTTPS_PROXY
EXTRA_ENV_VAR = "PORTBENCH_EXTRA_ENV"


def _env() -> dict[str, str]:
    allowed = list(ENV_ALLOWLIST)
    for name in os.environ.get(EXTRA_ENV_VAR, "").split(","):
        name = name.strip()
        if name:
            allowed.append(name)
    env = {k: os.environ[k] for k in allowed if k in os.environ}
    env["CARGO_TERM_COLOR"] = "never"
    return env


def run_tests(project_dir: Path, cmd: list[str], timeout: int = DEFAULT_TEST_TIMEOUT) -> TestRun:
    started = time.monotonic()
    try:
        proc = subprocess.run(
            cmd, cwd=str(project_dir), capture_output=True, text=True,
            timeout=timeout, env=_env(),
        )
    except subprocess.TimeoutExpired as exc:
        return TestRun(
            returncode=-1,
            stdout=exc.stdout or "" if isinstance(exc.stdout, str) else "",
            stderr=exc.stderr or "" if isinstance(exc.stderr, str) else "",
            duration_s=time.monotonic() - started,
            timed_out=True,
        )
    return TestRun(proc.returncode, proc.stdout, proc.stderr, time.monotonic() - started)


def parse_failures(run: TestRun) -> list[str]:
    """Every `test <name> ... FAILED` line, across all test binaries, de-duplicated."""
    seen: list[str] = []
    for stream in (run.stdout, run.stderr):
        for name in _FAILED_TEST_RE.findall(stream):
            name = name.strip()
            if name not in seen:
                seen.append(name)
    return seen


def parse_summaries(run: TestRun) -> list[tuple[str, int, int, int]]:
    """Every `test result:` line as (status, passed, failed, ignored)."""
    return [
        (m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4)))
        for stream in (run.stdout, run.stderr)
        for m in _SUMMARY_RE.finditer(stream)
    ]


@dataclass
class TestBlock:
    """One test binary's output: `running N tests` ... `test result: ...`."""

    declared: int = 0
    failures: list[str] = field(default_factory=list)
    summary: tuple[str, int, int, int] | None = None


def parse_blocks(run: TestRun) -> list[TestBlock]:
    """Segment the output per test binary.

    Needed because failure *names* are not unique across binaries: `tests::roundtrip` failing in
    two crates is two failures but one name. Reconciling a global de-duplicated name list
    against summed summary counts therefore invents a mismatch and reports a false SUITE_ERROR.
    Counting per block keeps the two sides comparable.
    """
    blocks: list[TestBlock] = []
    for stream in (run.stdout, run.stderr):
        current: TestBlock | None = None
        for line in stream.splitlines():
            running = _RUNNING_RE.match(line)
            if running:
                current = TestBlock(declared=int(running.group(1)))
                blocks.append(current)
                continue
            failed = _FAILED_LINE_RE.match(line)
            if failed:
                if current is None:                     # a FAILED line with no header
                    current = TestBlock()
                    blocks.append(current)
                current.failures.append(failed.group(1).strip())
                continue
            summary = _SUMMARY_LINE_RE.match(line)
            if summary:
                if current is None:
                    current = TestBlock()
                    blocks.append(current)
                current.summary = (summary.group(1), int(summary.group(2)),
                                   int(summary.group(3)), int(summary.group(4)))
                current = None
    return blocks


def _count(pattern: re.Pattern, run: TestRun) -> int:
    return len(pattern.findall(run.stdout)) + len(pattern.findall(run.stderr))


def unaccounted_reasons(run: TestRun) -> list[str]:
    """Why a nonzero exit is NOT fully explained by the failures libtest actually named.

    A test binary that aborts (segfault, `std::process::abort`, a panic in the harness itself)
    prints no `test result:` summary and no `test <name> ... FAILED` line, but cargo still
    exits nonzero. Trusting the parsed output alone would score that suite PASS. Every nonzero
    exit must therefore be reconciled against what was actually reported.

    Returns an empty list when the exit code is fully accounted for -- notably the normal
    baseline case, where cargo exits 101 because allowlisted tests failed and every one of them
    was named and summarised.
    """
    if run.returncode == 0:
        return []

    reasons: list[str] = []
    summaries = parse_summaries(run)
    blocks = parse_blocks(run)
    failed_summaries = [s for s in summaries if s[0] == "FAILED"]
    reported_failed = sum(s[2] for s in summaries)
    # Per-block failure lines, NOT the de-duplicated global name list: the same test name
    # failing in two binaries is two failures and one name.
    printed_failures = sum(len(b.failures) for b in blocks)
    cargo_failed_targets = _count(_CARGO_TEST_FAILED_RE, run)
    aborts = _count(_ABORT_RE, run)

    if not summaries:
        reasons.append("nonzero exit with no test result summary at all")
    if aborts:
        reasons.append(f"{aborts} test process(es) aborted without reporting")
    if reported_failed != printed_failures:
        reasons.append(
            f"summaries report {reported_failed} failed but "
            f"{printed_failures} test failure line(s) were printed"
        )
    if cargo_failed_targets > len(failed_summaries):
        reasons.append(
            f"cargo reported {cargo_failed_targets} failing test target(s) but only "
            f"{len(failed_summaries)} produced a FAILED summary"
        )
    if summaries and not printed_failures and not reasons:
        reasons.append("nonzero exit but no test was reported as failing")
    return reasons


def is_compile_error(run: TestRun) -> bool:
    """A build failure, not a test failure.

    Two guards. The exit code must be nonzero -- a *successful* run whose output merely
    mentions the phrase (a test asserting on a cargo message, a doc example, a panic string)
    is not a compile error. And the evidence must be a real diagnostic: cargo's
    "could not compile" line, or an `error[Exxxx]` rustc code for the failures cargo phrases
    differently (link errors, `error: aborting due to N previous errors`).
    """
    if run.returncode == 0:
        return False
    text = run.stderr + "\n" + run.stdout
    return "could not compile" in text or bool(_ERROR_CODE_RE.search(text))


def error_codes(run: TestRun) -> list[str]:
    codes: list[str] = []
    for stream in (run.stderr, run.stdout):
        for code in _ERROR_CODE_RE.findall(stream):
            if code not in codes:
                codes.append(code)
    return sorted(codes)


def _failure_excerpt(run: TestRun) -> str:
    """Text handed to the repair round: the failure list plus summaries."""
    chunks = []
    for stream in (run.stdout, run.stderr):
        for match in re.finditer(r"^failures:\n(?:.*\n)*?\n", stream, re.M):
            chunks.append(match.group(0))
    chunks.extend(line for line in run.stdout.splitlines() if line.startswith("test result: FAILED"))
    return "\n".join(chunks) if chunks else run.stdout


def _rerun_by_name(project_dir: Path, project: str, name: str, timeout: int) -> bool:
    """Re-run one test by exact name. True only if it genuinely ran and genuinely passed.

    "The failure name is absent" is not evidence of recovery: a rerun that fails to compile, or
    that matches no test at all, also prints no `... FAILED` line for that name and would count
    as a flake recovery for code that never executed. The rerun must exit 0, execute at least
    one test, and report no failures.
    """
    cmd = TEST_COMMANDS[project]
    if cmd[0] != "cargo":
        # iceberg goes through make; fall back to a direct cargo invocation of the same shape.
        cmd = ["cargo", "test", "--no-fail-fast", "--lib", "--all-features", "--workspace"]
    run = run_tests(project_dir, [*cmd, name, "--", "--exact"], timeout=timeout)
    if run.timed_out or run.returncode != 0:
        return False
    summaries = parse_summaries(run)
    executed = sum(passed + failed for _, passed, failed, _ in summaries)
    if executed < 1:
        return False
    if any(failed for _, _, failed, _ in summaries):
        return False
    return name not in parse_failures(run)


_AUTO = object()


def missing_target_reasons(project: str, run: TestRun, override=_AUTO) -> list[str]:
    """Did every test target this project has actually report a summary?

    Exit codes are not evidence that the tests ran. A generated function is free to call
    `std::process::exit(0)`; its test binary dies before libtest prints the binary's summary,
    cargo sees success for that target, and the other targets' green summaries are all that is
    left to score -- a PASS for code that ran no tests. The count of `test result:` lines is
    fixed for a project, so a target that has gone missing is visible directly.

    Skipped when the project has no recorded snapshot (see `--snapshot-targets`).
    """
    want = expected.expected(project) if override is _AUTO else override
    if not want:
        return []
    got = len(parse_summaries(run))
    if got < want:
        return [f"only {got} test-target summaries, expected {want} for {project} "
                f"(a test binary exited before reporting)"]
    return []


def score(project: str, project_dir: Path, run: TestRun, timeout: int = DEFAULT_TEST_TIMEOUT,
          expected_summaries=_AUTO) -> Verdict:
    """Turn one test run into a verdict, applying baseline subtraction and flaky retries.

    `expected_summaries` defaults to the project's recorded test-target count; pass an explicit
    `None` to disable the check (offline unit tests run on a handful of synthetic summaries).
    """
    codes = error_codes(run)

    if run.timed_out:
        return Verdict(TIMEOUT, error_class_hint=codes,
                       error_text=f"test command exceeded {timeout}s")

    if is_compile_error(run):
        return Verdict(COMPILE_ERROR, error_class_hint=codes, error_text=run.stderr)

    summaries = parse_summaries(run)
    failing = parse_failures(run)
    allowed = baseline.allowlist(project)
    baselined = [f for f in failing if f in allowed]
    outside = [f for f in failing if f not in allowed]

    recovered: list[str] = []
    for name in list(outside):
        if name not in baseline.FLAKY_TESTS:
            continue
        for _ in range(baseline.FLAKY_RETRIES):
            if _rerun_by_name(project_dir, project, name, timeout):
                outside.remove(name)
                recovered.append(name)
                break

    reasons = unaccounted_reasons(run) + missing_target_reasons(project, run, expected_summaries)

    if outside:
        # Genuine named failures: report TEST_FAIL (which drives the repair round) but keep any
        # accounting anomaly visible on the record rather than dropping it.
        return Verdict(TEST_FAIL, failing_tests=outside, error_class_hint=codes,
                       error_text=_failure_excerpt(run), summaries=summaries,
                       baselined=baselined, flaky_recovered=recovered,
                       reason="; ".join(reasons))

    if reasons:
        # An exit code the parsed output does not explain, or a test target that never
        # reported. Never PASS on either.
        return Verdict(SUITE_ERROR, error_class_hint=codes, summaries=summaries,
                       baselined=baselined, flaky_recovered=recovered,
                       reason="; ".join(reasons),
                       error_text=_failure_excerpt(run) + "\n" + run.stderr[-4000:])

    return Verdict(PASS, error_class_hint=codes, summaries=summaries,
                   baselined=baselined, flaky_recovered=recovered)


def snapshot_baseline(project: str, project_dir: Path, timeout: int = DEFAULT_TEST_TIMEOUT) -> list[str]:
    """Run the suite on the unmodified tree and report failing test names (SPIKE (f))."""
    run = run_tests(project_dir, TEST_COMMANDS[project], timeout=timeout)
    return parse_failures(run)
