"""Attach captured model code + compiler output to gallery entries.

The scored runs do not persist model output, so the gallery built by `aggregate.py` is an
index of observed failures with placeholder panes. A capture lane re-runs the selected
(model, task) pairs with `--keep-artifacts --out gallery-runs`, producing fresh generations
whose code and raw failure text ARE kept.

A capture is a *fresh sample*: the model generates new code, which may fail differently or
pass. To keep every card self-consistent, an entry is only populated when the capture's
verdict string equals the entry's, and the card's codes / bucket / title are then updated
to describe the capture (the code shown must be the code that produced the error shown).
Entries whose capture diverged keep their placeholders and availability flags stay false.

Deterministic: same inputs -> byte-identical gallery.json. Run from the repo root:

    python3 -m analysis.attach_gallery --gallery site/data/real/gallery.json \
        --captures gallery-runs/
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from analysis import buckets
    from analysis.aggregate import _title
except ImportError:                   # direct execution / odd sys.path
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from analysis import buckets
    from analysis.aggregate import _title

MAX_CODE_CHARS = 6000
MAX_ERROR_CHARS = 4000

CAPTURE_NOTE = (
    "Code and compiler output are a fresh capture of the same model x task (the scored "
    "run kept only the verdict). The capture failed with the same verdict; codes shown "
    "are the capture's own."
)


def _clip(text: str, limit: int) -> str:
    text = text.rstrip("\n")
    if len(text) <= limit:
        return text
    # Keep the tail for errors-like text; callers pass code with head-keep via _clip_head.
    return text[:limit].rstrip() + "\n// ... truncated ..."


def _scrub_paths(text: str) -> str:
    """Strip the machine-local prefix from absolute paths in compiler output."""
    out = []
    for line in text.splitlines():
        idx = line.find("/Users/")
        while idx != -1:
            end = line.find("portbench/", idx)
            if end == -1:
                break
            line = line[:idx] + line[end + len("portbench/"):]
            idx = line.find("/Users/")
        out.append(line)
    return "\n".join(out)


def _clip_error(text: str, limit: int) -> str:
    text = _scrub_paths(text.strip("\n"))
    if len(text) <= limit:
        return text
    return "... truncated ...\n" + text[-limit:].lstrip()


def load_captures(captures_dir: Path) -> dict[tuple[str, str, int], dict]:
    """(model, task_id, attempt) -> capture record with an `artifacts_path` Path added."""
    out: dict[tuple[str, str, int], dict] = {}
    for jsonl in sorted(captures_dir.glob("*/*.jsonl")):
        for line in jsonl.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            rel = record.get("artifacts_dir")
            if not rel:
                continue
            record["artifacts_path"] = captures_dir / rel
            key = (str(record.get("model")), str(record.get("task_id")),
                   int(record.get("attempt", 0)))
            out[key] = record       # later files win; capture lanes run one file per model
    return out


def attach(gallery: dict, captures: dict[tuple[str, str, int], dict]) -> tuple[int, int]:
    attached = skipped = 0
    for entry in gallery.get("entries", []):
        key = (entry["model"], entry["task_id"], int(entry.get("attempt", 0)))
        cap = captures.get(key)
        if cap is None or cap.get("verdict") != entry.get("verdict"):
            skipped += 1
            continue
        art: Path = cap["artifacts_path"]
        code_file = art / "injected.rs"
        error_file = art / "error.txt"
        if not code_file.is_file():
            skipped += 1
            continue

        codes = sorted(cap.get("error_class_hint") or [])
        entry["error_class_hint"] = codes
        entry["failing_tests"] = sorted(cap.get("failing_tests") or [])
        entry["bucket"] = buckets.bucket_for_attempt(str(entry.get("verdict")), codes)
        entry["title"] = _title(
            {"verdict": entry["verdict"], "error_class_hint": codes,
             "failing_tests": entry["failing_tests"]},
            entry["bucket"])
        entry["model_code"] = _clip(code_file.read_text(encoding="utf-8", errors="ignore"),
                                    MAX_CODE_CHARS)
        entry["model_code_available"] = True
        if error_file.is_file():
            entry["compiler_error"] = _clip_error(
                error_file.read_text(encoding="utf-8", errors="ignore"), MAX_ERROR_CHARS)
            entry["compiler_error_available"] = True
        entry["capture_note"] = CAPTURE_NOTE
        attached += 1
    gallery["code_capture"] = "fresh-capture"
    gallery["comment"] = (
        "Entries were selected from the scored runs (which keep verdicts, not code). Panes "
        "marked available carry a fresh capture of the same model x task that reproduced "
        "the same verdict; its code and compiler output are shown verbatim and the entry's "
        "codes/bucket describe the capture. Entries without a matching reproduction keep "
        "their placeholders."
    )
    return attached, skipped


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--gallery", type=Path, required=True)
    ap.add_argument("--captures", type=Path, required=True)
    args = ap.parse_args(argv)

    gallery = json.loads(args.gallery.read_text(encoding="utf-8"))
    captures = load_captures(args.captures)
    attached, skipped = attach(gallery, captures)
    args.gallery.write_text(json.dumps(gallery, indent=1, sort_keys=True) + "\n",
                            encoding="utf-8")
    print(f"attached {attached}, left placeholder {skipped} "
          f"(captures indexed: {len(captures)}) -> {args.gallery}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
