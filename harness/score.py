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

from . import baseline

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
EXTRACTION_ERROR = "EXTRACTION_ERROR"
TRANSPORT_ERROR = "TRANSPORT_ERROR"
TIMEOUT = "TIMEOUT"

_FAILED_TEST_RE = re.compile(r"^test (.+?) \.\.\. FAILED\s*$", re.M)
_SUMMARY_RE = re.compile(
    r"^test result: (ok|FAILED)\. (\d+) passed; (\d+) failed; (\d+) ignored", re.M
)
_ERROR_CODE_RE = re.compile(r"error\[(E\d{4})\]")


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


def _env() -> dict[str, str]:
    env = os.environ.copy()
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


def is_compile_error(run: TestRun) -> bool:
    # cargo prints "could not compile `<crate>`" for every build failure, and nothing else.
    return "could not compile" in run.stderr or "could not compile" in run.stdout


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
    """Re-run one test by exact name. True if it passed."""
    cmd = TEST_COMMANDS[project]
    if cmd[0] != "cargo":
        # iceberg goes through make; fall back to a direct cargo invocation of the same shape.
        cmd = ["cargo", "test", "--no-fail-fast", "--lib", "--all-features", "--workspace"]
    run = run_tests(project_dir, [*cmd, name, "--", "--exact"], timeout=timeout)
    if run.timed_out:
        return False
    return name not in parse_failures(run)


def score(project: str, project_dir: Path, run: TestRun, timeout: int = DEFAULT_TEST_TIMEOUT) -> Verdict:
    """Turn one test run into a verdict, applying baseline subtraction and flaky retries."""
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

    if outside:
        return Verdict(TEST_FAIL, failing_tests=outside, error_class_hint=codes,
                       error_text=_failure_excerpt(run), summaries=summaries,
                       baselined=baselined, flaky_recovered=recovered)

    if not summaries:
        # No test binary reported at all and no compile error we recognised: treat the raw
        # cargo failure as a compile problem rather than silently passing.
        if run.returncode != 0:
            return Verdict(COMPILE_ERROR, error_class_hint=codes, error_text=run.stderr)

    return Verdict(PASS, error_class_hint=codes, summaries=summaries,
                   baselined=baselined, flaky_recovered=recovered)


def snapshot_baseline(project: str, project_dir: Path, timeout: int = DEFAULT_TEST_TIMEOUT) -> list[str]:
    """Run the suite on the unmodified tree and report failing test names (SPIKE (f))."""
    run = run_tests(project_dir, TEST_COMMANDS[project], timeout=timeout)
    return parse_failures(run)
