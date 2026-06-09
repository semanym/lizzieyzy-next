#!/usr/bin/env python3
"""Prune ranked SGF samples to enforce player contribution limits."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import time
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path


AI_NAME_MARKERS = (
    "alphago",
    "amybot",
    "bot",
    "dangoapp",
    "doge_bot",
    "elf opengo",
    "fineart",
    "gnugo",
    "golaxy",
    "kata",
    "katago",
    "leela zero",
    "leelazero",
    "noob_bot",
)
DEFAULT_RANKS = [f"{rank}k" for rank in range(18, 0, -1)] + [
    f"{rank}d" for rank in range(1, 10)
] + ["10d", "11d"]


@dataclass
class SgfRecord:
    path: Path
    rank: str
    pb: str = ""
    pw: str = ""
    game_id: int = 0
    analyzed: bool = False
    player_ids: set[str] = field(default_factory=set)
    reasons: list[str] = field(default_factory=list)

    @property
    def player_names(self) -> list[str]:
        return [name for name in (self.pb, self.pw) if name]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="Rank directory root, containing rank/*.sgf files.")
    parser.add_argument(
        "--quarantine-root",
        default="",
        help="Where rejected SGFs are moved when --apply is set. Defaults beside --root.",
    )
    parser.add_argument("--per-rank", type=int, default=25, help="Maximum SGFs to keep per rank.")
    parser.add_argument("--max-games-per-player", type=int, default=2, help="Maximum kept SGFs per player.")
    parser.add_argument("--ranks", default=",".join(DEFAULT_RANKS), help="Comma-separated rank buckets to keep.")
    parser.add_argument("--metadata-timeout", type=int, default=12, help="OGS metadata request timeout.")
    parser.add_argument("--metadata-sleep", type=float, default=0.03, help="Delay between OGS metadata requests.")
    parser.add_argument(
        "--analysis-jsonl",
        action="append",
        default=[],
        help="Evaluation JSONL to read analyzed SGF paths from. May be repeated.",
    )
    parser.add_argument("--apply", action="store_true", help="Move rejected SGFs to quarantine.")
    parser.add_argument("--allow-bot-ai", action="store_true", help="Do not reject SGFs with bot/AI player names.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.root)
    quarantine = Path(args.quarantine_root) if args.quarantine_root else root.parent / "sgf-quarantine"
    ranks = [rank.strip() for rank in args.ranks.split(",") if rank.strip()]
    analysis_jsonl = [Path(path) for path in args.analysis_jsonl] or default_analysis_jsonl(root)
    analyzed_paths, analyzed_names = load_analyzed_refs(analysis_jsonl)
    records = load_records(root, ranks, analyzed_paths, analyzed_names)
    attach_ogs_metadata(records, args.metadata_timeout, args.metadata_sleep)
    kept, rejected = select_records(
        records,
        ranks,
        int(args.per_rank),
        int(args.max_games_per_player),
        allow_bot_ai=bool(args.allow_bot_ai),
    )
    if args.apply:
        move_rejected(root, quarantine, rejected)
    print_summary(root, quarantine, kept, rejected, apply=args.apply)
    return 0


def load_records(root: Path, ranks: list[str], analyzed_paths: set[str], analyzed_names: set[str]) -> list[SgfRecord]:
    records: list[SgfRecord] = []
    for rank in ranks:
        rank_dir = root / rank
        if not rank_dir.exists():
            continue
        for path in sorted(rank_dir.glob("*.sgf")):
            text = path.read_text(encoding="utf-8", errors="replace")
            record = SgfRecord(
                path=path,
                rank=rank,
                pb=sgf_prop(text, "PB"),
                pw=sgf_prop(text, "PW"),
                game_id=ogs_game_id_from_name(path.name),
                analyzed=str(path.resolve()) in analyzed_paths or canonical_sgf_name(path.name) in analyzed_names,
            )
            record.player_ids.update(ogs_player_ids_from_name(path.name))
            records.append(record)
    return records


def attach_ogs_metadata(records: list[SgfRecord], timeout: int, sleep_seconds: float) -> None:
    ogs_records = [record for record in records if record.game_id > 0]
    for index, record in enumerate(ogs_records, 1):
        try:
            data = fetch_ogs_metadata(record.game_id, timeout)
            record.player_ids.update(ogs_player_ids_from_metadata(data))
        except Exception as exc:  # noqa: BLE001 - audit should continue on bad metadata.
            record.reasons.append(f"metadata_failed:{type(exc).__name__}")
        if index == 1 or index % 50 == 0:
            print(f"[prune] metadata {index}/{len(ogs_records)}", flush=True)
        time.sleep(max(0.0, sleep_seconds))


def select_records(
    records: list[SgfRecord],
    ranks: list[str],
    per_rank: int,
    max_games_per_player: int,
    allow_bot_ai: bool,
) -> tuple[list[SgfRecord], list[SgfRecord]]:
    kept: list[SgfRecord] = []
    rejected: list[SgfRecord] = []
    id_counts: Counter[str] = Counter()
    name_counts: Counter[str] = Counter()
    kept_by_rank: Counter[str] = Counter()
    by_rank: dict[str, list[SgfRecord]] = {rank: [] for rank in ranks}
    for record in records:
        by_rank.setdefault(record.rank, []).append(record)

    for rank in ranks:
        for record in sorted(by_rank.get(rank, []), key=record_sort_key):
            reasons: list[str] = []
            if kept_by_rank[rank] >= per_rank:
                reasons.append("rank_over_quota")
            if not allow_bot_ai and has_ai_player(record):
                reasons.append("bot_or_ai_player")
            for player_id in sorted(record.player_ids):
                if id_counts[player_id] >= max_games_per_player:
                    reasons.append(f"player_id_over_limit:{player_id}")
                    break
            if not reasons:
                for name in record.player_names:
                    key = normalize_player_name(name)
                    if key and name_counts[key] >= max_games_per_player:
                        reasons.append(f"player_name_over_limit:{name}")
                        break
            if reasons:
                record.reasons.extend(reasons)
                rejected.append(record)
                continue
            kept.append(record)
            kept_by_rank[rank] += 1
            for player_id in record.player_ids:
                id_counts[player_id] += 1
            for name in record.player_names:
                key = normalize_player_name(name)
                if key:
                    name_counts[key] += 1
    return kept, rejected


def record_sort_key(record: SgfRecord) -> tuple[int, str]:
    # Prefer records with known OGS player ids, because contribution limits are
    # more reliable. Prefer already-analyzed records to avoid wasting GPU work.
    # File name order keeps selection deterministic.
    return (0 if record.analyzed else 1, 0 if record.player_ids else 1, record.path.name)


def move_rejected(root: Path, quarantine: Path, rejected: list[SgfRecord]) -> None:
    for record in rejected:
        relative = record.path.relative_to(root)
        target = quarantine / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target = target.with_name(f"{target.stem}-{short_hash(record.path.name)}{target.suffix}")
        shutil.move(str(record.path), str(target))


def print_summary(root: Path, quarantine: Path, kept: list[SgfRecord], rejected: list[SgfRecord], apply: bool) -> None:
    kept_by_rank = Counter(record.rank for record in kept)
    rejected_reasons: Counter[str] = Counter()
    for record in rejected:
        for reason in record.reasons:
            rejected_reasons[reason.split(":", 1)[0]] += 1
    over_id = contribution_overages(kept, by="id")
    over_name = contribution_overages(kept, by="name")
    summary = {
        "root": str(root),
        "quarantine": str(quarantine),
        "apply": apply,
        "kept_total": len(kept),
        "rejected_total": len(rejected),
        "kept_analyzed": sum(1 for record in kept if record.analyzed),
        "rejected_analyzed": sum(1 for record in rejected if record.analyzed),
        "kept_by_rank": dict(sorted(kept_by_rank.items())),
        "rejected_reasons": dict(sorted(rejected_reasons.items())),
        "kept_player_id_over_limit_count": len(over_id),
        "kept_player_name_over_limit_count": len(over_name),
        "rejected_examples": [
            {
                "rank": record.rank,
                "file": record.path.name,
                "players": record.player_names,
                "player_ids": sorted(record.player_ids),
                "analyzed": record.analyzed,
                "reasons": record.reasons,
            }
            for record in rejected[:30]
        ],
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


def default_analysis_jsonl(root: Path) -> list[Path]:
    # root is normally target/humansl-input/sgf-by-rank.
    target_root = root.parent.parent if len(root.parents) >= 2 else root.parent
    gpu_run = target_root / "humansl-gpu-run"
    return [gpu_run / "evaluation.jsonl", gpu_run / "move-evaluation.jsonl"]


def load_analyzed_refs(paths: list[Path]) -> tuple[set[str], set[str]]:
    analyzed_paths: set[str] = set()
    analyzed_names: set[str] = set()
    for path in paths:
        if not path.exists():
            continue
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                raw_path = str(row.get("path") or "").strip()
                if raw_path:
                    analyzed_path = Path(raw_path)
                    analyzed_paths.add(str(analyzed_path.resolve()))
                    analyzed_names.add(canonical_sgf_name(analyzed_path.name))
    print(
        f"[prune] analyzed SGF refs loaded: paths={len(analyzed_paths)} names={len(analyzed_names)}",
        flush=True,
    )
    return analyzed_paths, analyzed_names


def canonical_sgf_name(name: str) -> str:
    value = name
    while True:
        stripped = re.sub(r"^(?:1[0-8]k|[1-9]k|[1-9]d|10d|11d)-\d{3}-", "", value, count=1)
        if stripped == value:
            return value.lower()
        value = stripped


def contribution_overages(records: list[SgfRecord], by: str) -> list[tuple[str, int]]:
    counts: Counter[str] = Counter()
    for record in records:
        if by == "id":
            for player_id in record.player_ids:
                counts[player_id] += 1
        else:
            for name in record.player_names:
                key = normalize_player_name(name)
                if key:
                    counts[key] += 1
    return sorted((key, count) for key, count in counts.items() if count > 2)


def fetch_ogs_metadata(game_id: int, timeout: int) -> dict[str, object]:
    request = urllib.request.Request(
        f"https://online-go.com/api/v1/games/{game_id}",
        headers={"User-Agent": "lizzieyzy-next SGF sample prune", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def ogs_game_id_from_name(name: str) -> int:
    for pattern in (
        r"ogs-api-(\d+)\.sgf$",
        r"ogs-player-\d+(?:-players-[0-9_]+)?-game-(\d+)\.sgf$",
    ):
        match = re.search(pattern, name, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return 0


def ogs_player_ids_from_name(name: str) -> set[str]:
    match = re.search(r"ogs-player-(\d+)(?:-players-([0-9_]+))?-game-\d+\.sgf$", name, re.IGNORECASE)
    if not match:
        return set()
    if match.group(2):
        return {value for value in match.group(2).split("_") if value}
    return {match.group(1)}


def ogs_player_ids_from_metadata(data: dict[str, object]) -> set[str]:
    player_ids: set[str] = set()
    all_players = data.get("all_players")
    if isinstance(all_players, list):
        player_ids.update(str(int(value)) for value in all_players if is_number(value))
    players = data.get("players")
    if isinstance(players, dict):
        for color in ("black", "white"):
            player = players.get(color)
            if isinstance(player, dict) and is_number(player.get("id")):
                player_ids.add(str(int(float(str(player.get("id"))))))
    return player_ids


def has_ai_player(record: SgfRecord) -> bool:
    return any(is_ai_name(name) for name in record.player_names)


def is_ai_name(name: str) -> bool:
    normalized = normalize_player_name(name)
    return any(marker in normalized for marker in AI_NAME_MARKERS)


def normalize_player_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def sgf_prop(text: str, key: str) -> str:
    match = re.search(r"(?<![A-Z])" + re.escape(key) + r"\[((?:\\.|[^\]])*)\]", text)
    if not match:
        return ""
    return re.sub(r"\\(.)", r"\1", match.group(1)).strip()


def is_number(value: object) -> bool:
    try:
        int(float(str(value)))
        return True
    except (TypeError, ValueError):
        return False


def short_hash(text: str) -> str:
    value = 0
    for char in text:
        value = ((value * 33) + ord(char)) & 0xFFFFFFFF
    return f"{value:08x}"


if __name__ == "__main__":
    raise SystemExit(main())
