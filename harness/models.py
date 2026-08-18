"""Model adapters: shell out to the `claude` and `codex` CLIs.

Both adapters feed the prompt through a temp file on stdin. Prompt text is NEVER interpolated
into a shell command line, and no shell is used at all (subprocess with an argv list).

Both CLIs are *agentic*: out of the box they can read the filesystem. PortBench's own checkout
contains `dataset/`, i.e. the reference Rust answer for every task, so a model call made from
this repository is a model call that can look up the answer key. Every call therefore runs

  * with `cwd` set to a fresh, empty temp directory that is deleted afterwards, and
  * with tool use disabled as far as each CLI allows (see `_claude_argv` / `_codex_argv`),

and the exact argv plus the resolved model id are recorded in the run metadata. See
"Isolation of model calls" in harness/README.md for the residual risk.

Reasoning effort / thinking budget is left at each CLI's out-of-the-box default: the point of
the benchmark is what a user gets by typing the command, not a tuned configuration.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
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
    argv: list[str] = field(default_factory=list)
    resolved_model: str = ""


@contextmanager
def _empty_workspace():
    """A fresh empty directory to run a model CLI in, removed afterwards."""
    with tempfile.TemporaryDirectory(prefix="portbench-model-") as workdir:
        yield workdir


def _run(argv: list[str], prompt: str, timeout: int, cwd: str) -> subprocess.CompletedProcess:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", encoding="utf-8", delete=False) as fh:
        fh.write(prompt)
        prompt_path = fh.name
    try:
        with open(prompt_path, "r", encoding="utf-8") as stdin:
            return subprocess.run(
                argv, stdin=stdin, capture_output=True, text=True, timeout=timeout, cwd=cwd
            )
    finally:
        Path(prompt_path).unlink(missing_ok=True)


# ------------------------------------------------------------------ claude

def _claude_argv(model: str) -> list[str]:
    """One-shot, tool-less, configuration-less.

    `--tools ""` is the CLI's documented "disable all tools" form, so the model has no Read,
    Bash, Glob or WebFetch to reach the dataset with. `--safe-mode` drops CLAUDE.md, skills,
    plugins, hooks, custom agents and MCP servers; `--strict-mcp-config` with an empty config
    guarantees no MCP tool is reintroduced by any other source.
    """
    return [
        "claude", "-p",
        "--model", model,
        "--output-format", "json",
        "--safe-mode",
        "--no-session-persistence",
        "--disable-slash-commands",
        "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
        "--tools", "",
    ]


def _call_claude(model: str, prompt: str, timeout: int) -> ModelResult:
    argv = _claude_argv(model)
    started = time.monotonic()
    with _empty_workspace() as workdir:
        try:
            proc = _run(argv, prompt, timeout, cwd=workdir)
        except subprocess.TimeoutExpired as exc:
            raise TransportError(f"claude timed out after {timeout}s", timed_out=True) from exc
    duration = time.monotonic() - started

    # Acceptance: a nonzero exit means the CLI did not complete its turn. A residual final
    # message on a failed run is not an answer, it is debris -- take the transport retry.
    if proc.returncode != 0:
        raise TransportError(
            f"claude exited {proc.returncode}: {(proc.stderr or proc.stdout)[:400]}"
        )
    if not proc.stdout.strip():
        raise TransportError(f"claude produced no stdout (rc={proc.returncode}): {proc.stderr[:400]}")
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise TransportError(f"claude output was not JSON: {proc.stdout[:400]}") from exc

    if isinstance(envelope, list):  # defensive: some versions emit a list of messages
        envelope = envelope[-1]
    if not isinstance(envelope, dict):
        raise TransportError(f"claude envelope was not an object: {proc.stdout[:400]}")
    if envelope.get("is_error"):
        raise TransportError(f"claude reported is_error: {envelope.get('subtype')}")
    if "result" not in envelope:
        raise TransportError(f"claude envelope has no terminal result field: {proc.stdout[:400]}")

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
    return ModelResult(text, tokens_in, tokens_out, total, duration, argv=argv,
                       resolved_model=_claude_resolved_model(envelope))


def _claude_resolved_model(envelope: dict) -> str:
    """The concrete model id behind the alias, for the record."""
    for key in ("model", "modelId"):
        value = envelope.get(key)
        if isinstance(value, str) and value:
            return value
    usage = envelope.get("modelUsage")
    if isinstance(usage, dict) and usage:
        return ",".join(sorted(usage))
    return ""


# ------------------------------------------------------------------ codex

_TOKENS_USED_RE = re.compile(r"tokens used:?\s*([\d,]+)", re.I)

# Features that give the agent a way to touch the filesystem, the network or another agent.
# `--disable X` is the documented equivalent of `-c features.X=false`.
_CODEX_DISABLED_FEATURES = (
    "shell_tool", "unified_exec", "view_image",
    "browser_use", "browser_use_external", "computer_use",
    "multi_agent", "plugins", "skill_search", "apps", "image_generation",
)


def _codex_argv(model: str, out_path: str) -> list[str]:
    """Read-only sandbox, no user config, no rules, no tools, no web search.

    `-s read-only` is the sandbox floor; on top of it every filesystem/network-capable feature
    is switched off individually, so the turn has no tool to reach `dataset/` with even though
    the sandbox itself would permit reads.
    """
    argv = [
        "codex", "exec",
        "--skip-git-repo-check",
        "-s", "read-only",
        "-m", model,
        "--json",
        "-o", out_path,
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "-c", "tools.web_search=false",
    ]
    for feature in _CODEX_DISABLED_FEATURES:
        argv += ["--disable", feature]
    return argv


def _iter_events(stdout: str):
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


_USAGE_KEYS = ("total_token_usage", "usage", "last_token_usage")


def _usage_dict(node) -> dict | None:
    """The best token-usage object inside an arbitrarily nested event.

    Cumulative counts win over per-turn ones at every level, and the search descends, so all
    the shapes the CLI has shipped resolve without hard-coding a path:
      * `{"msg": {"type": "token_count", "info": {"total_token_usage": {...}}}}`
      * `{"type": "token_count", "total_token_usage": {...}}`
      * `{"type": "turn.completed", "usage": {"input_tokens": .., "output_tokens": ..}}`
    """
    if not isinstance(node, dict):
        return None
    for key in _USAGE_KEYS:
        value = node.get(key)
        if isinstance(value, dict) and "input_tokens" in value:
            return value
    if "input_tokens" in node:
        return node
    for value in node.values():
        found = _usage_dict(value)
        if found is not None:
            return found
    return None


def _codex_tokens(stdout: str) -> tuple[int | None, int | None, int | None]:
    """Token counts from the JSONL stream; fall back to the human 'tokens used' line.

    The last usage-bearing event wins: codex reports cumulative counts as the turn proceeds.
    """
    tin = tout = None
    for event in _iter_events(stdout):
        usage = _usage_dict(event)
        if usage is not None:
            tin = usage.get("input_tokens")
            tout = usage.get("output_tokens")
    if tin is not None or tout is not None:
        return tin, tout, (tin or 0) + (tout or 0)
    match = _TOKENS_USED_RE.search(stdout)
    if match:
        return None, None, int(match.group(1).replace(",", ""))
    return None, None, None


def _codex_final_message(stdout: str) -> str:
    """The last terminal assistant message in the JSONL stream, if there is one.

    Handles the legacy `{"msg": {"type": "agent_message", "message": ...}}` event and the
    current `{"type": "item.completed", "item": {"item_type": "agent_message", "text": ...}}`.
    """
    text = ""
    for event in _iter_events(stdout):
        item = event.get("item")
        if isinstance(item, dict):
            kind = item.get("item_type") or item.get("type")
            if kind in ("agent_message", "assistant_message") and (item.get("text") or "").strip():
                text = item["text"].strip()
                continue
        msg = event.get("msg") if isinstance(event.get("msg"), dict) else event
        kind = msg.get("type")
        if kind in ("agent_message", "assistant", "assistant_message"):
            candidate = msg.get("message") or msg.get("text") or ""
            if isinstance(candidate, str) and candidate.strip():
                text = candidate.strip()
    return text


def _codex_resolved_model(stdout: str) -> str:
    for event in _iter_events(stdout):
        for source in (event, event.get("msg") if isinstance(event.get("msg"), dict) else {}):
            if not isinstance(source, dict):
                continue
            value = source.get("model")
            if isinstance(value, str) and value:
                return value
    return ""


def _call_codex(model: str, prompt: str, timeout: int) -> ModelResult:
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
        out_path = fh.name
    argv = _codex_argv(model, out_path)
    started = time.monotonic()
    try:
        with _empty_workspace() as workdir:
            try:
                proc = _run(argv, prompt, timeout, cwd=workdir)
            except subprocess.TimeoutExpired as exc:
                raise TransportError(f"codex timed out after {timeout}s", timed_out=True) from exc
        duration = time.monotonic() - started

        if proc.returncode != 0:
            raise TransportError(
                f"codex exited {proc.returncode}: {(proc.stderr or proc.stdout)[:400]}"
            )

        text = ""
        try:
            text = Path(out_path).read_text(encoding="utf-8", errors="ignore").strip()
        except OSError:
            pass
        if not text:
            text = _codex_final_message(proc.stdout)
        if not text:
            raise TransportError(
                f"codex produced no terminal agent message (rc={proc.returncode}): "
                f"{proc.stderr[:400]}"
            )
        tin, tout, total = _codex_tokens(proc.stdout)
        return ModelResult(text, tin, tout, total, duration, argv=argv,
                           resolved_model=_codex_resolved_model(proc.stdout))
    finally:
        Path(out_path).unlink(missing_ok=True)


# ------------------------------------------------------------------ public

def argv_preview(model: str) -> list[str]:
    """The argv the harness will use, for the run metadata (temp paths shown as placeholders)."""
    if model in CLAUDE_MODELS:
        return _claude_argv(model)
    if model in CODEX_MODELS:
        return _codex_argv(model, "<last-message-file>")
    raise ValueError(f"unknown model {model!r}")


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
