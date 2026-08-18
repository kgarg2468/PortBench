"""Task discovery, parsing and prompt construction for the RustRepoTrans python->rust slice.

The benchmark is treated as strictly read-only input. Nothing from it is ever copied into
PortBench's own artifacts: results reference tasks by id + content hash only.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

DATASET_REPO = "https://github.com/SYSUSELab/RustRepoTrans"
# Pinned to the commit of the working clone this harness was developed against.
DATASET_COMMIT = "7026a8a9c8d4a524cbb554ff9b8100df4020114c"

LANG_PAIR = "rust__python"
QUERY_LANG = "rust"
CORPUS_LANG = "python"

# SPIKE (c): exact expected task counts. A mismatch means the dataset moved under us.
EXPECTED_COUNTS = {"charset-normalizer": 33, "iceberg": 44, "libp2p": 31}

PAIR_DIR = "function_pair_with_identical_functionality"
DEP_DIR = "related_functions_and_datatypes_and_import"

_FUNCTION_RE = re.compile(r"<function>(.*?)</function>", re.DOTALL)


class TaskError(RuntimeError):
    pass


@dataclass(frozen=True)
class Task:
    task_id: str          # benchmark filename without the .txt suffix
    project: str
    target_rel: str       # path relative to dataset/Evaluate
    target_path: Path     # absolute path to the real Rust source file
    reference_rust_fn: str
    python_src_fn: str
    target_signature: str
    dep_decls: str
    dep_uses: str
    content_hash: str     # sha256 over both source files; never store task text


def _decode_target_rel(task_id: str) -> str:
    """projects__libp2p__rust__identity__src__ecdsa__.rs__function__3 -> projects/libp2p/rust/identity/src/ecdsa.rs"""
    return "/".join(task_id.split("__.rs")[0].split("__")) + ".rs"


def _normalize(raw: bytes) -> str:
    return raw.decode("utf-8", "ignore").replace("\r\n", "\n").replace("\r", "\n")


def _split_records(text: str) -> list[str]:
    return text.split("------")


def evaluate_root(dataset: Path) -> Path:
    return Path(dataset).resolve() / "Evaluate"


def project_dir(dataset: Path, project: str) -> Path:
    return evaluate_root(dataset) / "projects" / project / "rust"


def ensure_dataset(dataset: Path) -> Path:
    """Clone RustRepoTrans at the pinned commit if the directory is absent.

    An existing clone is never touched (no fetch, no checkout, no cargo clean): the ~12 GB
    warm target/ tree and the committed Cargo.lock files are load-bearing.
    """
    dataset = Path(dataset)
    if dataset.exists():
        if not evaluate_root(dataset).is_dir():
            raise TaskError(f"{dataset} exists but has no Evaluate/ directory")
        return dataset.resolve()
    dataset.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", DATASET_REPO, str(dataset)], check=True)
    subprocess.run(["git", "checkout", "--detach", DATASET_COMMIT], cwd=dataset, check=True)
    return dataset.resolve()


def dataset_commit(dataset: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=dataset, capture_output=True, text=True, check=True
        )
        return out.stdout.strip()
    except Exception:
        return "unknown"


def _load_task(dataset: Path, project: str, filename: str) -> Task:
    root = evaluate_root(dataset)
    pair_file = root / PAIR_DIR / project / LANG_PAIR / filename
    dep_file = root / DEP_DIR / project / LANG_PAIR / QUERY_LANG / filename
    if not dep_file.exists():
        raise TaskError(f"{project}/{filename}: missing dependency file")

    pair_bytes = pair_file.read_bytes()
    dep_bytes = dep_file.read_bytes()
    # Hash the raw bytes, but normalise newlines before use: the .txt task files carry CRLF
    # while Python reads the .rs sources with universal newlines, and the injection anchor is
    # an exact substring match against that text.
    pair_records = _split_records(_normalize(pair_bytes))
    dep_records = _split_records(_normalize(dep_bytes))
    if len(pair_records) < 2 or len(dep_records) < 2:
        raise TaskError(f"{project}/{filename}: malformed '------' delimited file")

    try:
        reference = _FUNCTION_RE.findall(pair_records[0])[0].strip()
        python_fn = _FUNCTION_RE.findall(pair_records[1])[0].strip()
    except IndexError as exc:
        raise TaskError(f"{project}/{filename}: no <function> record") from exc

    task_id = filename[:-4] if filename.endswith(".txt") else filename
    target_rel = _decode_target_rel(task_id)
    target_path = root / target_rel
    if not target_path.exists():
        raise TaskError(f"{project}/{filename}: decoded target {target_rel} does not exist")

    digest = hashlib.sha256(pair_bytes + b"\x00" + dep_bytes).hexdigest()
    return Task(
        task_id=task_id,
        project=project,
        target_rel=target_rel,
        target_path=target_path,
        reference_rust_fn=reference,
        python_src_fn=python_fn,
        # Paper's signature slot: reference function text truncated at the first '{'.
        target_signature=reference.split("{")[0],
        dep_decls=dep_records[0],
        dep_uses=dep_records[1],
        content_hash=digest,
    )


def discover_tasks(dataset: Path) -> list[Task]:
    """Index all 108 python->rust tasks. Hard-errors if the per-project counts drift."""
    root = evaluate_root(dataset)
    tasks: list[Task] = []
    counts: dict[str, int] = {}
    for project in sorted(EXPECTED_COUNTS):
        pair_dir = root / PAIR_DIR / project / LANG_PAIR
        if not pair_dir.is_dir():
            raise TaskError(f"missing task directory {pair_dir}")
        names = sorted(n for n in os.listdir(pair_dir) if n.endswith(".txt"))
        counts[project] = len(names)
        for name in names:
            tasks.append(_load_task(dataset, project, name))
    if counts != EXPECTED_COUNTS:
        raise TaskError(f"task count mismatch: got {counts}, expected {EXPECTED_COUNTS}")
    return tasks


def select(tasks: list[Task], project: str | None = None,
           patterns: list[str] | None = None, limit: int | None = None) -> list[Task]:
    out = list(tasks)
    if project:
        out = [t for t in out if t.project == project]
    if patterns:
        out = [t for t in out
               if any(t.task_id == p or fnmatch.fnmatch(t.task_id, p) for p in patterns)]
    if limit is not None:
        out = out[:limit]
    return out


# --------------------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------------------

# Verbatim from the paper's translate_throughLLM.py, so scores stay comparable. Only the
# trailing output-format sentence is ours (the paper relied on the model volunteering a fence).
_PAPER_INSTRUCTION = (
    "Translate the given {corpus} function to {query} according to the {query} function "
    "signature, {query} function dependencies(including function and variable dependencies), "
    "and data type declarations and {query} function dependency libraries I provide(delimited "
    "with XML tags).Make sure to call the relevant dependencies as much as possible in the "
    "translated function Only response the translated function results."
)

_OUTPUT_RULE = (
    "\nReturn only the translated {query} function, inside a single ```{query} code block. "
    "Do not add explanation before or after the code block.\n"
)


def build_prompt(task: Task) -> str:
    head = _PAPER_INSTRUCTION.format(corpus=CORPUS_LANG, query=QUERY_LANG)
    return (
        f"{head}\n"
        f"<{CORPUS_LANG} function>\n{task.python_src_fn}\n</{CORPUS_LANG} function>\n"
        f"<{QUERY_LANG} function signature>\n{task.target_signature}\n</{QUERY_LANG} function signature>\n"
        f"<{QUERY_LANG} function dependencies, and data type declarations>\n{task.dep_decls}\n"
        f"</{QUERY_LANG} function dependencies and data type declarations>\n"
        f"<{QUERY_LANG} function dependency libraries>{task.dep_uses}\n"
        f"</{QUERY_LANG} function dependency libraries>\n"
        + _OUTPUT_RULE.format(query=QUERY_LANG)
    )


def truncate_error(text: str, limit: int = 8000) -> str:
    """Keep the tail: rustc/libtest put the summary and the failure list at the end."""
    text = text or ""
    if len(text) <= limit:
        return text
    return "...[truncated]...\n" + text[-limit:]


def build_repair_prompt(task: Task, previous_answer: str, error_text: str) -> str:
    return (
        build_prompt(task)
        + "\n---\n"
        + "Your previous answer was:\n"
        + f"<previous answer>\n{previous_answer.strip()}\n</previous answer>\n\n"
        + "Injecting it into the repository produced the following compiler / test output:\n"
        + f"<error>\n{truncate_error(error_text)}\n</error>\n\n"
        + "Fix the problem. Return only the corrected "
        + f"{QUERY_LANG} function inside a single ```{QUERY_LANG} code block.\n"
    )
