"""Model adapters: shell out to the `claude` and `codex` CLIs.

Both adapters feed the prompt through a temp file on stdin. Prompt text is NEVER interpolated
into a shell command line, and no shell is used at all (subprocess with an argv list).

Reasoning effort / thinking budget is left at each CLI's out-of-the-box default: the point of
the benchmark is what a user gets by typing the command, not a tuned configuration. The exact
argv used is recorded in the run metadata.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

CLAUDE_MODELS = ("fable", "opus", "sonnet", "haiku")
CODEX_MODELS = ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5")
ALL_MODELS = CLAUDE_MODELS + CODEX_MODELS

DEFAULT_TIMEOUT = 600


class TransportError(RuntimeError):
    """CLI crashed, timed out, or produced output we could not parse. Not a model failure."""

    def __init__(self, message: str, timed_out: bool = False):
        super().__init__(message)
        self.timed_out = timed_out


@dataclass
class ModelResult:
    text: str
    tokens_in: int | None = None
    tokens_out: int | None = None
    tokens_total: int | None = None
    duration_s: float = 0.0
    retried: bool = False
    argv: list[str] | None = None


def _run(argv: list[str], prompt: str, timeout: int) -> subprocess.CompletedProcess:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", encoding="utf-8", delete=False) as fh:
        fh.write(prompt)
        prompt_path = fh.name
    try:
        with open(prompt_path, "r", encoding="utf-8") as stdin:
            return subprocess.run(
                argv, stdin=stdin, capture_output=True, text=True, timeout=timeout
            )
    finally:
        Path(prompt_path).unlink(missing_ok=True)


# ------------------------------------------------------------------ claude

def _call_claude(model: str, prompt: str, timeout: int) -> ModelResult:
    argv = ["claude", "-p", "--model", model, "--output-format", "json"]
    started = time.monotonic()
    try:
        proc = _run(argv, prompt, timeout)
    except subprocess.TimeoutExpired as exc:
        raise TransportError(f"claude timed out after {timeout}s", timed_out=True) from exc
    duration = time.monotonic() - started

    if not proc.stdout.strip():
        raise TransportError(f"claude produced no stdout (rc={proc.returncode}): {proc.stderr[:400]}")
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise TransportError(f"claude output was not JSON: {proc.stdout[:400]}") from exc

    if isinstance(envelope, list):  # defensive: some versions emit a list of messages
        envelope = envelope[-1]
    if envelope.get("is_error"):
        raise TransportError(f"claude reported is_error: {envelope.get('subtype')}")

    text = envelope.get("result") or ""
    usage = envelope.get("usage") or {}
    tokens_in = sum(
        int(usage.get(k) or 0)
        for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    ) or None
    tokens_out = int(usage["output_tokens"]) if usage.get("output_tokens") is not None else None
    total = (tokens_in or 0) + (tokens_out or 0) or None
    if not text.strip():
        raise TransportError("claude returned an empty result field")
    return ModelResult(text, tokens_in, tokens_out, total, duration, argv=argv)


# ------------------------------------------------------------------ codex

_TOKENS_USED_RE = re.compile(r"tokens used:?\s*([\d,]+)", re.I)


def _codex_tokens(stdout: str) -> tuple[int | None, int | None, int | None]:
    """Prefer the JSONL token_count events; fall back to the human 'tokens used' line."""
    tin = tout = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        info = event.get("info") or event.get("token_count") or event.get("msg") or event
        if not isinstance(info, dict):
            continue
        usage = info.get("total_token_usage") or info.get("last_token_usage") or info
        if isinstance(usage, dict) and "input_tokens" in usage:
            tin = usage.get("input_tokens")
            tout = usage.get("output_tokens")
    if tin is not None or tout is not None:
        return tin, tout, (tin or 0) + (tout or 0)
    match = _TOKENS_USED_RE.search(stdout)
    if match:
        return None, None, int(match.group(1).replace(",", ""))
    return None, None, None


def _call_codex(model: str, prompt: str, timeout: int) -> ModelResult:
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
        out_path = fh.name
    argv = [
        "codex", "exec",
        "--skip-git-repo-check",
        "-s", "read-only",
        "-m", model,
        "--json",
        "-o", out_path,
    ]
    started = time.monotonic()
    try:
        try:
            proc = _run(argv, prompt, timeout)
        except subprocess.TimeoutExpired as exc:
            raise TransportError(f"codex timed out after {timeout}s", timed_out=True) from exc
        duration = time.monotonic() - started

        text = ""
        try:
            text = Path(out_path).read_text(encoding="utf-8", errors="ignore").strip()
        except OSError:
            pass
        if not text:
            # Fall back to the last assistant message in the JSONL stream.
            for line in reversed(proc.stdout.splitlines()):
                if '"agent_message"' in line or '"assistant"' in line:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    msg = event.get("msg") or event
                    candidate = msg.get("message") or msg.get("text") or ""
                    if candidate.strip():
                        text = candidate.strip()
                        break
        if not text:
            raise TransportError(
                f"codex produced no final message (rc={proc.returncode}): {proc.stderr[:400]}"
            )
        tin, tout, total = _codex_tokens(proc.stdout)
        return ModelResult(text, tin, tout, total, duration, argv=argv)
    finally:
        Path(out_path).unlink(missing_ok=True)


# ------------------------------------------------------------------ public

def call_model(model: str, prompt: str, timeout: int = DEFAULT_TIMEOUT) -> ModelResult:
    """Invoke a model CLI, retrying once on a transport-level failure."""
    if model in CLAUDE_MODELS:
        fn = _call_claude
    elif model in CODEX_MODELS:
        fn = _call_codex
    else:
        raise ValueError(f"unknown model {model!r}; valid: {', '.join(ALL_MODELS)}")

    try:
        return fn(model, prompt, timeout)
    except TransportError as first:
        try:
            result = fn(model, prompt, timeout)
        except TransportError as second:
            raise TransportError(f"{first} | retry: {second}", timed_out=second.timed_out) from second
        result.retried = True
        return result


def cli_versions() -> dict[str, str]:
    versions = {}
    for tool in ("claude", "codex", "cargo", "rustc", "make"):
        try:
            out = subprocess.run([tool, "--version"], capture_output=True, text=True, timeout=60)
            versions[tool] = out.stdout.strip().splitlines()[0] if out.stdout.strip() else "unknown"
        except Exception:
            versions[tool] = "unavailable"
    return versions
