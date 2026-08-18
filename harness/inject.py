"""Extract Rust from model output and inject it into the real project tree.

Shape follows the paper's auto_test_rust.py (tree-sitter harvest of function_item /
use_declaration nodes, de-dup against the dependency block, single str.replace of the
reference body, use-lines inserted before the first top-level `use ...;`) but rewrites the
`index != -1` None crash and never leaves the tree dirty: every injection restores in a
finally block, and stale backups from a killed run are swept on startup.
"""

from __future__ import annotations

import re
import shutil
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Language, Parser
import tree_sitter_rust as ts_rust

from .tasks import Task

BACKUP_SUFFIX = ".portbench-bak"

_RUST_LANG = Language(ts_rust.language())
_PARSER = Parser(_RUST_LANG)

# Fence flavours the paper tolerated, plus the tagged forms some models emit.
_FENCE_PATTERNS = [
    r"```(?:rust|Rust|rs|RUST)\s*\n(.*?)```",
    r"```\s*\n(.*?)```",
    r"<rust function>(.*?)</rust function>",
    r"<rust function translation>(.*?)</rust function translation>",
    r"<rust translated function>(.*?)</rust translated function>",
    r"<translated function>(.*?)</translated function>",
]


class ExtractionError(RuntimeError):
    pass


@dataclass
class Extraction:
    functions: list[str] = field(default_factory=list)
    uses: list[str] = field(default_factory=list)
    source: str = ""          # which candidate block the harvest came from


def _harvest(code: str) -> tuple[list[str], list[str]]:
    """Collect every top-level-reachable function_item and use_declaration."""
    blob = code.encode("utf-8")
    root = _PARSER.parse(blob).root_node
    functions: list[str] = []
    uses: list[str] = []

    def walk(node) -> None:
        if node.type == "function_item":
            functions.append(blob[node.start_byte:node.end_byte].decode("utf-8", "ignore"))
            return  # do not descend: nested helper fns travel with their parent
        if node.type == "use_declaration":
            uses.append(blob[node.start_byte:node.end_byte].decode("utf-8", "ignore").strip())
            return
        for child in node.children:
            walk(child)

    walk(root)
    return functions, uses


def extract(raw_model_output: str) -> Extraction:
    """Pull Rust functions + imports out of a model response.

    Tries fenced/tagged blocks in order, then the raw text. The first candidate that yields
    at least one function_item wins.
    """
    if not raw_model_output or not raw_model_output.strip():
        raise ExtractionError("empty model output")

    candidates: list[tuple[str, str]] = []
    for pattern in _FENCE_PATTERNS:
        for match in re.findall(pattern, raw_model_output, re.DOTALL):
            candidates.append((pattern, match.strip()))
    candidates.append(("<raw>", raw_model_output.strip()))

    for label, code in candidates:
        if not code:
            continue
        functions, uses = _harvest(code)
        if functions:
            return Extraction(functions=functions, uses=uses, source=label)
    raise ExtractionError("no rust function_item found in model output")


_FN_NAME_RE = re.compile(r"\bfn\s+([A-Za-z_][A-Za-z0-9_]*)")


def _fn_name(text: str) -> str | None:
    match = _FN_NAME_RE.search(text)
    return match.group(1) if match else None


def dedup_against_dependencies(extraction: Extraction, task: Task) -> list[str]:
    """Drop any harvested function whose text already appears in the dependency block.

    Prevents the model from re-defining a helper the file already has (paper behaviour), with
    one correction: the function we are actually asking for is never dropped. For 2 of the 108
    tasks the dependency block quotes the target function itself, so the paper's plain
    "text in dependencies -> discard" rule throws the answer away and loses the task.
    """
    target = _fn_name(task.target_signature)
    kept = [
        f for f in extraction.functions
        if f.strip() and (f not in task.dep_decls or (target and _fn_name(f) == target))
    ]
    if not kept:
        raise ExtractionError("all extracted functions are duplicates of the dependency block")
    return kept


def _insert_uses(content: str, uses: list[str]) -> str:
    missing = []
    for use in uses:
        if use and use not in content and use not in missing:
            missing.append(use)
    if not missing:
        return content
    lines = content.split("\n")
    index = next(
        (i for i, line in enumerate(lines)
         if line.startswith("use ") and line.rstrip().endswith(";")),
        None,
    )
    # Paper bug: `if index != -1` is True when index is None -> list.insert(None, ...)
    # raises TypeError and the whole task is lost. Fall back to the top of the file.
    if index is None:
        index = 0
    lines.insert(index, "\n".join(missing))
    return "\n".join(lines)


def sweep_backups(root: Path) -> list[str]:
    """Restore any backup left behind by a killed run. Returns the paths restored."""
    restored = []
    for backup in Path(root).rglob("*" + BACKUP_SUFFIX):
        original = Path(str(backup)[: -len(BACKUP_SUFFIX)])
        shutil.copyfile(backup, original)
        backup.unlink()
        restored.append(str(original))
    return restored


@contextmanager
def injected(task: Task, functions: list[str], uses: list[str]):
    """Write the model's functions into the real source file; always restore.

    Yields a dict of injection metadata (`anchor_count`, `ambiguous_anchor`).
    """
    path = task.target_path
    backup = Path(str(path) + BACKUP_SUFFIX)
    shutil.copyfile(path, backup)
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
        anchor = task.reference_rust_fn
        count = content.count(anchor)
        if count == 0:
            raise ExtractionError(
                f"reference function not found verbatim in {task.target_rel}; cannot anchor"
            )
        replacement = "\n" + "\n".join(functions) + "\n"
        content = content.replace(anchor, replacement)
        content = _insert_uses(content, uses)
        path.write_text(content, encoding="utf-8", errors="ignore")
        yield {"anchor_count": count, "ambiguous_anchor": count > 1}
    finally:
        shutil.copyfile(backup, path)
        backup.unlink(missing_ok=True)
