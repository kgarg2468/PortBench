"""Shared tree-sitter-rust helpers.

Both `tasks` (leak stripping) and `inject` (anchor resolution) need to look at Rust source as
an AST rather than as text. Keeping the parser in one place avoids a second grammar load and
keeps the two callers agreeing on what "the same function" means.
"""

from __future__ import annotations

from dataclasses import dataclass

from tree_sitter import Language, Parser
import tree_sitter_rust as ts_rust

_RUST_LANG = Language(ts_rust.language())
_PARSER = Parser(_RUST_LANG)


@dataclass(frozen=True)
class FnNode:
    """One `function_item` node: its name, its exact source text and its byte range."""

    name: str
    text: str
    start_byte: int
    end_byte: int


def function_items(code: str) -> list[FnNode]:
    """Every `function_item` in source order, including inside `impl` / `mod` blocks.

    Nested helper functions are NOT reported separately: a `function_item` is a leaf here, so
    its inner functions travel with it. That matches how `inject._harvest` treats model output.
    """
    blob = code.encode("utf-8")
    root = _PARSER.parse(blob).root_node
    out: list[FnNode] = []

    def walk(node) -> None:
        if node.type == "function_item":
            name_node = node.child_by_field_name("name")
            name = (
                blob[name_node.start_byte:name_node.end_byte].decode("utf-8", "ignore")
                if name_node is not None else ""
            )
            out.append(FnNode(
                name=name,
                text=blob[node.start_byte:node.end_byte].decode("utf-8", "ignore"),
                start_byte=node.start_byte,
                end_byte=node.end_byte,
            ))
            return
        for child in node.children:
            walk(child)

    walk(root)
    return out


def replace_byte_range(code: str, start_byte: int, end_byte: int, replacement: str) -> str:
    """Splice `replacement` over a byte range of `code`, returning text."""
    blob = code.encode("utf-8")
    return (blob[:start_byte] + replacement.encode("utf-8") + blob[end_byte:]).decode(
        "utf-8", "ignore"
    )


def normalize(text: str) -> str:
    """Whitespace-insensitive form, for comparing "the same function" across files."""
    return " ".join((text or "").split())
