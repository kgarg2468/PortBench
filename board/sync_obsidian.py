#!/usr/bin/env python3
"""Sync board/state.json into the user's Obsidian vault as one note per node.

Obsidian's graph view then shows the build DAG: wikilinks = dependency edges,
nested status tags (#pb/running etc.) drive the color groups configured in
the vault's .obsidian/graph.json. Run after every state.json change.
"""
import json
import pathlib
import shutil

STATE = pathlib.Path(__file__).parent / "state.json"
VAULT = pathlib.Path.home() / "Documents" / "Obsidian Vault" / "PortBench"


def title(node):
    return node["label"].split("\n")[0].replace("/", " ").strip()


def main():
    state = json.loads(STATE.read_text())
    nodes = {n["id"]: n for n in state["nodes"]}

    if VAULT.exists():
        shutil.rmtree(VAULT)
    VAULT.mkdir(parents=True)

    for n in state["nodes"]:
        deps = ", ".join(f"[[{title(nodes[d])}]]" for d in n["deps"])
        unlocks = ", ".join(
            f"[[{title(m)}]]" for m in state["nodes"] if n["id"] in m["deps"]
        )
        body = "\n".join(
            [
                "---",
                f"status: {n['status']}",
                "---",
                f"#pb/{n['status']}",
                "",
                f"# {n['label'].replace(chr(10), ' ')}",
                "",
                f"**Status:** {n['status']}",
                f"**Note:** {n.get('note', '')}",
                "",
                f"**Depends on:** {deps or '—'}",
                f"**Unlocks:** {unlocks or '—'}",
                "",
            ]
        )
        (VAULT / f"{title(n)}.md").write_text(body)

    board = [
        "---",
        "status: index",
        "---",
        "#pb/index",
        "",
        "# PortBench build board",
        "",
        f"Last updated: {state['updated']}",
        "",
        "Open the **graph view** (filter: `path:PortBench`) to see the DAG.",
        "Colors: blue = running, green = done, yellow = blocked, red = failed.",
        "",
    ]
    for n in state["nodes"]:
        board.append(f"- **{n['status'].upper()}** — [[{title(n)}]]: {n.get('note', '')}")
    (VAULT / "PortBench Board.md").write_text("\n".join(board) + "\n")
    print(f"synced {len(state['nodes'])} nodes -> {VAULT}")


if __name__ == "__main__":
    main()
