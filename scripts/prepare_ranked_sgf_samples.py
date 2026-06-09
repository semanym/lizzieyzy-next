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
    parser.add_argument("--min-date", default="", help="Only prepare SGFs dated on or after YYYY-MM-DD.")
    parser.add_argument(
        "--require-date",
        action="store_true",
        help="Reject SGFs with no parseable DT root property.",
    )
    parser.add_argument(
        "--require-same-rank",
        action="store_true",
        help="Only prepare SGFs whose original BR/WR normalize to the same rank.",
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
        candidates = sorted(source_dir.rglob("*.sgf"))
        sgfs: list[tuple[Path, str, dict[str, str], tuple[int, int, int] | None]] = []
        rejected = 0
        minimum_date = parse_date_key(args.min_date)
        for source in candidates:
            text = read_sgf(source)
            props = root_properties(text)
            date = parse_date_key(props.get("DT", ""))
            if args.require_date and date is None:
                rejected += 1
                continue
            if minimum_date and (date is None or date < minimum_date):
                rejected += 1
                continue
            if args.require_same_rank:
                black_rank = normalize_rank(props.get("BR", ""), props.get("PB", ""))
                white_rank = normalize_rank(props.get("WR", ""), props.get("PW", ""))
                if not black_rank or black_rank != white_rank:
                    rejected += 1
                    continue
            sgfs.append((source, text, props, date))
        if len(sgfs) < args.per_rank:
            note = f"{rank}: {len(sgfs)}/{args.per_rank} SGFs"
            if rejected:
                note += f" after filtering {rejected}"
            missing.append(note)
            if not args.allow_partial:
                continue
        selected = sgfs[: args.per_rank]
        rank_out = out_dir / rank
        rank_out.mkdir(parents=True, exist_ok=True)
        for index, (source, text, props, date) in enumerate(selected, start=1):
            target = rank_out / f"{rank}-{index:03d}-{safe_name(source.name)}"
            with target.open("w", encoding="utf-8", newline="") as handle:
                handle.write(rewrite_root_ranks(text, rank))
            manifest_rows.append(
                {
                    "rank": rank,
                    "index": index,
                    "source": str(source),
                    "prepared": str(target),
                    "date": format_date(date),
                    "black_rank": props.get("BR", ""),
                    "white_rank": props.get("WR", ""),
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


def root_properties(text: str) -> dict[str, str]:
    root_match = re.search(r"^\s*\(;([^;()]*)", text, flags=re.DOTALL)
    if not root_match:
        return {}
    props: dict[str, str] = {}
    root = root_match.group(1)
    for match in re.finditer(r"([A-Za-z]+)((?:\[(?:\\.|[^\]])*\])+)", root):
        values = re.findall(r"\[((?:\\.|[^\]])*)\]", match.group(2))
        if values:
            props[match.group(1).upper()] = unescape_sgf_value(values[0])
    return props


def unescape_sgf_value(value: str) -> str:
    return re.sub(r"\\(.)", r"\1", value)


def parse_date_key(raw: str) -> tuple[int, int, int] | None:
    for match in re.finditer(r"(\d{4})[-/.]?(\d{1,2})?[-/.]?(\d{1,2})?", str(raw or "")):
        year = int(match.group(1))
        month = int(match.group(2) or 1)
        day = int(match.group(3) or 1)
        if 1 <= month <= 12 and 1 <= day <= 31:
            return (year, month, day)
    return None


def format_date(date: tuple[int, int, int] | None) -> str:
    if date is None:
        return ""
    return f"{date[0]:04d}-{date[1]:02d}-{date[2]:02d}"


def normalize_rank(text: str, player_name: str = "") -> str | None:
    if is_strong_ai_name(player_name):
        return "12d"
    raw = str(text or "").strip()
    lowered = raw.lower()
    match = re.search(r"(\d+)", raw)
    if not match:
        if "pro" in lowered or "professional" in lowered:
            return "10d"
        return None
    number = int(match.group(1))
    if "k" in lowered or "級" in raw or "级" in raw:
        if 1 <= number <= 18:
            return f"{number}k"
        return None
    if "p" in lowered or "pro" in lowered or "professional" in lowered:
        return "11d" if number >= 9 else "10d"
    if "d" in lowered or "段" in raw:
        if 1 <= number <= 12:
            return f"{number}d"
    return None


def is_strong_ai_name(name: str) -> bool:
    lowered = str(name or "").lower()
    return any(marker in lowered for marker in ("alphago", "katago", "leela", "elf", "fineart", "golaxy"))


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
        writer = csv.DictWriter(
            handle,
            fieldnames=["rank", "index", "source", "prepared", "date", "black_rank", "white_rank"],
        )
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
