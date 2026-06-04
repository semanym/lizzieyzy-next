#!/usr/bin/env python3
"""Fetch ranked SGF samples from open Go datasets.

The script streams SGF archives and stops once the requested rank buckets are
filled. It is designed for the Windows GPU calibration workflow where there is
no pre-existing local SGF directory.

Default sources:
- OGS 2025 SGF dump for ordinary amateur ranks, filtered to recent games.
- JGDB tarball for professional / top-professional labels.

Rank labeling is intentionally conservative. A game is copied into a rank bucket
only when both sides normalize to the same bucket. Professional ranks map to the
project-local 10d/11d labels:
- 1p..8p -> 10d
- 9p -> 11d

The 12d label is reserved for clearly named strong AI games such as AlphaGo,
KataGo, Leela Zero, ELF OpenGo, FineArt, or Golaxy.
"""

from __future__ import annotations

import argparse
import io
import re
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path


OGS_2025_SGF_URL = "https://za3k.com/ogs/ogs_games_2013_to_2025-05/sgfs-by-date.tar.gz"
JGDB_URL = "https://data.pjreddie.com/files/jgdb.tar.gz"
DEFAULT_OGS_MIN_DATE = "2025-01-01"
DEFAULT_RANKS = [f"{rank}k" for rank in range(18, 0, -1)] + [
    f"{rank}d" for rank in range(1, 13)
]
ORDINARY_RANKS = {f"{rank}k" for rank in range(18, 0, -1)} | {
    f"{rank}d" for rank in range(1, 10)
}
PRO_RANKS = {"10d", "11d", "12d"}
AI_NAME_MARKERS = (
    "alphago",
    "katago",
    "leela zero",
    "leelazero",
    "elf opengo",
    "fineart",
    "golaxy",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="Output rank directory root.")
    parser.add_argument("--per-rank", type=int, default=25, help="Target SGFs per rank bucket.")
    parser.add_argument("--min-moves", type=int, default=80, help="Skip games shorter than this.")
    parser.add_argument("--max-moves", type=int, default=500, help="Skip very long games.")
    parser.add_argument("--board-size", type=int, default=19, help="Only keep this board size.")
    parser.add_argument("--ranks", default=",".join(DEFAULT_RANKS))
    parser.add_argument("--ogs-url", default=OGS_2025_SGF_URL, help="OGS SGF tar.gz URL.")
    parser.add_argument(
        "--ogs-min-date",
        default=DEFAULT_OGS_MIN_DATE,
        help="Only keep OGS games on or after this date. Use empty string to disable.",
    )
    parser.add_argument("--jgdb-url", default=JGDB_URL, help="JGDB tar.gz URL.")
    parser.add_argument("--skip-ogs", action="store_true", help="Do not fetch OGS samples.")
    parser.add_argument("--skip-jgdb", action="store_true", help="Do not fetch JGDB samples.")
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="Exit successfully even if some buckets are not filled.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    out = Path(args.out)
    ranks = [rank.strip() for rank in args.ranks.split(",") if rank.strip()]
    needed = {rank: int(args.per_rank) for rank in ranks}
    counts = {rank: 0 for rank in ranks}

    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    if not args.skip_ogs:
        ordinary_needed = {rank: needed[rank] for rank in ranks if rank in ORDINARY_RANKS}
        stream_source(
            "OGS",
            args.ogs_url,
            out,
            ordinary_needed,
            counts,
            args,
            min_date=args.ogs_min_date,
        )
    if not args.skip_jgdb:
        pro_needed = {rank: needed[rank] for rank in ranks if rank in PRO_RANKS}
        stream_source(
            "JGDB",
            args.jgdb_url,
            out,
            pro_needed,
            counts,
            args,
            min_date="",
        )

    missing = [f"{rank}: {counts[rank]}/{needed[rank]}" for rank in ranks if counts[rank] < needed[rank]]
    for rank in ranks:
        print(f"[fetch] {rank}: {counts[rank]} SGFs")
    if missing:
        print("[warn] incomplete rank buckets:", file=sys.stderr)
        for item in missing:
            print(f"  {item}", file=sys.stderr)
        if not args.allow_partial:
            return 1
    return 0


def stream_source(
    name: str,
    url: str,
    out: Path,
    needed: dict[str, int],
    counts: dict[str, int],
    args: argparse.Namespace,
    min_date: str = "",
) -> None:
    if not needed:
        return
    print(f"[fetch] streaming {name}: {url}", flush=True)
    minimum = parse_date_key(min_date) if min_date else None
    last_skip_period = ""
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "lizzieyzy-next HumanSL calibration sampler",
        },
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        with tarfile.open(fileobj=response, mode="r|gz") as archive:
            for member in archive:
                if all(counts[rank] >= needed[rank] for rank in needed):
                    print(f"[fetch] {name}: quotas satisfied", flush=True)
                    return
                if not member.isfile() or not member.name.lower().endswith(".sgf"):
                    continue
                member_date = date_from_member_name(member.name)
                if minimum and member_date and member_date < minimum:
                    skip_period = f"{member_date[0]:04d}-{member_date[1]:02d}"
                    if skip_period != last_skip_period:
                        last_skip_period = skip_period
                        print(
                            f"[fetch] {name}: scanning {skip_period}; waiting for >= {min_date}",
                            flush=True,
                        )
                    continue
                extracted = archive.extractfile(member)
                if extracted is None:
                    continue
                raw = extracted.read()
                text = decode_sgf(raw)
                props = root_properties(text)
                if minimum and not meets_min_date(member.name, props, min_date):
                    continue
                if not acceptable_game(text, props, args):
                    continue
                rank = game_bucket_rank(props)
                if rank not in needed or counts[rank] >= needed[rank]:
                    continue
                counts[rank] += 1
                write_ranked_sgf(out, rank, counts[rank], member.name, text)
                print(f"[fetch] {name}: {rank} {counts[rank]}/{needed[rank]} {member.name}", flush=True)


def decode_sgf(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "shift_jis", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def root_properties(text: str) -> dict[str, str]:
    match = re.search(r"^\s*\(;([^;()]*)", text, flags=re.DOTALL)
    if not match:
        return {}
    props: dict[str, str] = {}
    for prop_match in re.finditer(r"([A-Z]+)\[((?:\\.|[^\]])*)\]", match.group(1)):
        props.setdefault(prop_match.group(1), unescape_sgf_value(prop_match.group(2)))
    return props


def unescape_sgf_value(value: str) -> str:
    return re.sub(r"\\(.)", r"\1", value)


def acceptable_game(text: str, props: dict[str, str], args: argparse.Namespace) -> bool:
    if int_prop(props, "SZ", 19) != int(args.board_size):
        return False
    if int_prop(props, "HA", 0) > 0:
        return False
    moves = len(re.findall(r";[BW]\[", text))
    return int(args.min_moves) <= moves <= int(args.max_moves)


def meets_min_date(member_name: str, props: dict[str, str], min_date: str) -> bool:
    minimum = parse_date_key(min_date)
    game_date = date_from_member_name(member_name) or parse_date_key(props.get("DT", ""))
    if minimum is None or game_date is None:
        return False
    return game_date >= minimum


def date_from_member_name(member_name: str) -> tuple[int, int, int] | None:
    match = re.search(r"(20\d{2})[/-](\d{1,2})[/-](\d{1,2})", member_name)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def parse_date_key(text: str) -> tuple[int, int, int] | None:
    match = re.search(r"(\d{4})(?:[-/.](\d{1,2}))?(?:[-/.](\d{1,2}))?", str(text or ""))
    if not match:
        return None
    year = int(match.group(1))
    month = int(match.group(2) or 1)
    day = int(match.group(3) or 1)
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return year, month, day


def int_prop(props: dict[str, str], key: str, default: int) -> int:
    try:
        return int(float(props.get(key, default)))
    except (TypeError, ValueError):
        return default


def game_bucket_rank(props: dict[str, str]) -> str | None:
    black_name = props.get("PB", "")
    white_name = props.get("PW", "")
    if is_strong_ai_name(black_name) and is_strong_ai_name(white_name):
        return "12d"
    black_rank = normalize_rank(props.get("BR", ""), black_name)
    white_rank = normalize_rank(props.get("WR", ""), white_name)
    if black_rank and black_rank == white_rank:
        return black_rank
    return None


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
        if number >= 9:
            return "11d"
        if 1 <= number <= 8:
            return "10d"
        return "10d"
    if "d" in lowered or "段" in raw:
        if 1 <= number <= 9:
            return f"{number}d"
        if 10 <= number <= 12:
            return f"{number}d"
    return None


def is_strong_ai_name(name: str) -> bool:
    lowered = str(name or "").lower()
    return any(marker in lowered for marker in AI_NAME_MARKERS)


def write_ranked_sgf(out: Path, rank: str, index: int, source_name: str, text: str) -> None:
    rank_dir = out / rank
    rank_dir.mkdir(parents=True, exist_ok=True)
    target = rank_dir / f"{rank}-{index:03d}-{safe_name(Path(source_name).name)}"
    with target.open("w", encoding="utf-8", newline="") as handle:
        handle.write(rewrite_root_ranks(text, rank))


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


if __name__ == "__main__":
    raise SystemExit(main())
