#!/usr/bin/env python3
"""Prepare a fixed-size ranked SGF sample set for HumanSL calibration runs.

Input layout:

  <input-root>/18k/*.sgf
  <input-root>/rank_18k/*.sgf
  ...
  <input-root>/9d/*.sgf
  <input-root>/10d/*.sgf
  <input-root>/11d/*.sgf
  <input-root>/12d/*.sgf

The script copies up to N SGFs per rank into the output directory and rewrites
BR/WR in the SGF root node to the directory rank. This gives the downstream
calibration scripts stable labels, including project-local 10d/11d/12d labels
that are not standard SGF ranks.
"""

from __future__ import annotations

import argparse
import csv
import re
import shutil
import sys
from pathlib import Path


DEFAULT_RANKS = [f"{rank}k" for rank in range(18, 0, -1)] + [
    f"{rank}d" for rank in range(1, 13)
]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", required=True, help="Root directory containing rank subdirs.")
    parser.add_argument("--out", required=True, help="Output directory for prepared SGFs.")
    parser.add_argument("--per-rank", type=int, default=25, help="Maximum SGFs to copy per rank.")
    parser.add_argument(
        "--ranks",
        default=",".join(DEFAULT_RANKS),
        help="Comma-separated rank labels to prepare.",
    )
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="Continue when a rank has fewer than --per-rank SGFs.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    input_root = Path(args.input_root)
    out_dir = Path(args.out)
    ranks = [rank.strip() for rank in args.ranks.split(",") if rank.strip()]
    if not input_root.is_dir():
        print(f"[error] input root not found: {input_root}", file=sys.stderr)
        return 1

    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest_rows: list[dict[str, str | int]] = []
    missing: list[str] = []
    for rank in ranks:
        source_dir = rank_dir(input_root, rank)
        if source_dir is None:
            missing.append(f"{rank}: no directory")
            continue
        sgfs = sorted(source_dir.rglob("*.sgf"))
        if len(sgfs) < args.per_rank:
            missing.append(f"{rank}: {len(sgfs)}/{args.per_rank} SGFs")
            if not args.allow_partial:
                continue
        selected = sgfs[: args.per_rank]
        rank_out = out_dir / rank
        rank_out.mkdir(parents=True, exist_ok=True)
        for index, source in enumerate(selected, start=1):
            target = rank_out / f"{rank}-{index:03d}-{safe_name(source.name)}"
            text = read_sgf(source)
            with target.open("w", encoding="utf-8", newline="") as handle:
                handle.write(rewrite_root_ranks(text, rank))
            manifest_rows.append(
                {
                    "rank": rank,
                    "index": index,
                    "source": str(source),
                    "prepared": str(target),
                }
            )

    write_manifest(out_dir / "manifest.csv", manifest_rows)
    summary = rank_counts(manifest_rows)
    for rank in ranks:
        print(f"[prepare] {rank}: {summary.get(rank, 0)} SGFs")
    if missing:
        print("[warn] incomplete ranks:", file=sys.stderr)
        for item in missing:
            print(f"  {item}", file=sys.stderr)
        if not args.allow_partial:
            print("[error] rerun with --allow-partial to continue anyway", file=sys.stderr)
            return 1
    print(f"[prepare] wrote {len(manifest_rows)} SGFs to {out_dir}")
    return 0


def rank_dir(root: Path, rank: str) -> Path | None:
    candidates = [root / rank, root / f"rank_{rank}"]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


def read_sgf(path: Path) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "shift_jis", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace")


def rewrite_root_ranks(text: str, rank: str) -> str:
    root_match = re.search(r"^\s*\(;([^;()]*)", text, flags=re.DOTALL)
    if not root_match:
        return text
    root = root_match.group(1)
    root = set_sgf_property(root, "BR", rank)
    root = set_sgf_property(root, "WR", rank)
    return text[: root_match.start(1)] + root + text[root_match.end(1) :]


def set_sgf_property(root: str, prop: str, value: str) -> str:
    encoded = value.replace("\\", "\\\\").replace("]", "\\]")
    pattern = re.compile(rf"{prop}((?:\[(?:\\.|[^\]])*\])+)")
    replacement = f"{prop}[{encoded}]"
    if pattern.search(root):
        return pattern.sub(replacement, root, count=1)
    return root + replacement


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


def write_manifest(path: Path, rows: list[dict[str, str | int]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["rank", "index", "source", "prepared"])
        writer.writeheader()
        writer.writerows(rows)


def rank_counts(rows: list[dict[str, str | int]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        rank = str(row["rank"])
        counts[rank] = counts.get(rank, 0) + 1
    return counts


if __name__ == "__main__":
    raise SystemExit(main())
