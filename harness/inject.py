"""Extract Rust from model output and inject it into the real project tree.

Shape follows the paper's auto_test_rust.py (tree-sitter harvest of function_item /
use_declaration nodes, de-dup against the dependency block, use-lines inserted before the first
top-level `use ...;`) but rewrites the `index != -1` None crash, resolves the injection anchor
through the AST instead of `str.replace`, refuses to inject an answer that does not contain the
requested function, and never leaves the tree dirty: every injection restores in a finally
block, and stale backups from a killed run are swept on startup.
"""

from __future__ import annotations

import re
import shutil
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Language, Parser
import tree_sitter_rust as ts_rust

from . import rustast
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


def require_target_function(functions: list[str], task: Task) -> str:
    """The answer must actually contain the function we asked for.

    Without this, a response consisting only of a novel `fn helper()` is happily injected over
    the target: the target disappears from the crate, and if nothing calls it the suite goes
    green -- a PASS for a translation that was never written. Extra helper functions the model
    emitted are kept and injected alongside; only the *absence* of the requested name is fatal.
    """
    target = _fn_name(task.target_signature)
    if not target:
        raise ExtractionError(
            f"could not read a function name out of the task signature: "
            f"{task.target_signature.strip()[:120]!r}"
        )
    harvested = [_fn_name(f) or "<anonymous>" for f in functions]
    if target in harvested:
        return target
    raise ExtractionError(
        f"model output does not define the requested function {target!r}; "
        f"harvested {harvested}"
    )


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


# --------------------------------------------------------------------------------------
# Anchor resolution
# --------------------------------------------------------------------------------------

_ORDINAL_RE = re.compile(r"__function__(\d+)$")


def task_ordinal(task_id: str) -> int | None:
    match = _ORDINAL_RE.search(task_id)
    return int(match.group(1)) if match else None


@dataclass(frozen=True)
class Anchor:
    start_byte: int
    end_byte: int
    matches: int          # how many AST nodes were byte-identical to the reference
    resolved_by: str      # "unique" | "ordinal"


def resolve_anchor(content: str, task: Task) -> Anchor:
    """Locate the single `function_item` this task is about.

    The paper's unanchored `str.replace(reference, answer)` rewrites *every* occurrence. Exactly
    one task (`...iceberg...io__.rs__function__12`, `pub fn location`) has two byte-identical
    occurrences -- `InputFile` and `OutputFile` -- so the paper scores the model on two edited
    functions instead of the one it was asked about. When more than one node matches we pick by
    the task id's `__function__N` ordinal against the file's `function_item` list, and never by
    text replacement.
    """
    nodes = rustast.function_items(content)
    reference = task.reference_rust_fn
    matches = [i for i, node in enumerate(nodes) if node.text == reference]
    if not matches:
        normalized = rustast.normalize(reference)
        matches = [i for i, node in enumerate(nodes)
                   if rustast.normalize(node.text) == normalized]
    if not matches:
        raise ExtractionError(
            f"reference function not found as an AST node in {task.target_rel}; cannot anchor"
        )
    if len(matches) == 1:
        node = nodes[matches[0]]
        return Anchor(node.start_byte, node.end_byte, 1, "unique")

    # Measured across all 108 tasks: `__function__N` is a 1-based index into the file's
    # `function_item` list for 93 of them and 0-based for 1, so 1-based is tried first.
    ordinal = task_ordinal(task.task_id)
    for index in ((ordinal - 1, ordinal) if ordinal is not None else ()):
        if index in matches:
            node = nodes[index]
            return Anchor(node.start_byte, node.end_byte, len(matches), "ordinal")
    raise ExtractionError(
        f"{task.target_rel}: {len(matches)} byte-identical candidates for "
        f"{task.task_id} and the ordinal does not select one of them"
    )


@contextmanager
def injected(task: Task, functions: list[str], uses: list[str]):
    """Write the model's functions into the real source file; always restore.

    Yields a dict of injection metadata (`anchor_count`, `ambiguous_anchor`, `anchor_resolved_by`).
    """
    path = task.target_path
    backup = Path(str(path) + BACKUP_SUFFIX)
    shutil.copyfile(path, backup)
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
        anchor = resolve_anchor(content, task)
        replacement = "\n" + "\n".join(functions) + "\n"
        content = rustast.replace_byte_range(
            content, anchor.start_byte, anchor.end_byte, replacement)
        content = _insert_uses(content, uses)
        path.write_text(content, encoding="utf-8", errors="ignore")
        yield {
            "anchor_count": anchor.matches,
            "ambiguous_anchor": anchor.matches > 1,
            "anchor_resolved_by": anchor.resolved_by,
        }
    finally:
        shutil.copyfile(backup, path)
        backup.unlink(missing_ok=True)
