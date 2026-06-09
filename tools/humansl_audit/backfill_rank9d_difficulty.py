#!/usr/bin/env python3
"""Backfill rank_9d HumanSL difficulty fields in an existing move JSONL.

This tool intentionally lives outside scripts/ because it is a repair/backfill
utility for already-produced data. It only rewrites move-level rows. Rows that
already contain the target difficulty and candidate-probability fields are
copied as-is; missing rows are recomputed from the SGF position with one normal
KataGo query and one HumanSL rank_9d query.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import evaluate_strength_samples as evaluator


TARGET_PROFILE = "rank_9d"
DIFFICULTY_FIELD = "human_sl_rank_9d_mistake_probability_loss_1.5"
CANDIDATES_FIELD = "human_sl_rank_9d_candidate_probabilities"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sgfs", nargs="+", help="SGF paths or glob patterns matching existing rows.")
    parser.add_argument("--move-jsonl", required=True, help="Existing move-evaluation JSONL.")
    parser.add_argument("--out-move-jsonl", required=True, help="Backfilled move JSONL output.")
    parser.add_argument("--katago", default=evaluator.DEFAULT_KATAGO)
    parser.add_argument("--model", default=evaluator.DEFAULT_MODEL)
    parser.add_argument("--config", default=evaluator.DEFAULT_CONFIG)
    parser.add_argument("--human-model", required=True)
    parser.add_argument("--rules", default="Chinese")
    parser.add_argument("--max-visits", type=int, default=32)
    parser.add_argument("--human-max-visits", type=int, default=1)
    parser.add_argument("--batch-positions", type=int, default=8)
    parser.add_argument("--human-batch-positions", type=int, default=16)
    parser.add_argument("--katago-response-timeout", type=float, default=1800)
    parser.add_argument("--limit-rows", type=int, default=0, help="Debug limit for missing rows.")
    parser.add_argument(
        "--force-overwrite",
        action="store_true",
        help="Allow replacing an existing non-empty output file.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source_path = Path(args.move_jsonl)
    out_path = Path(args.out_move_jsonl)
    if not source_path.exists():
        raise SystemExit(f"missing move JSONL: {source_path}")
    if out_path.exists() and out_path.stat().st_size > 0 and not args.force_overwrite:
        raise SystemExit(f"refusing to overwrite {out_path}; pass --force-overwrite")

    rows = load_rows(source_path)
    games_by_key = {
        evaluator.game_key(game.path): game
        for game in evaluator.load_games(args.sgfs)
    }
    missing = [row for row in rows if needs_backfill(row)]
    if args.limit_rows > 0:
        missing = missing[: args.limit_rows]
    print(
        f"[backfill] rows={len(rows)} missing={len(missing)} "
        f"games={len(games_by_key)} threshold={evaluator.HUMANSL_MISTAKE_THRESHOLD}",
        flush=True,
    )

    patched_by_key: dict[tuple[str, str, int], dict[str, Any]] = {}
    if missing:
        katago = evaluator.KataGoProcess(
            Path(args.katago),
            Path(args.model),
            Path(args.config),
            args.katago_response_timeout,
            Path(args.human_model),
        )
        try:
            patched_by_key = backfill_rows(args, katago, missing, games_by_key)
        finally:
            katago.close()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            key = move_row_key(row)
            handle.write(json.dumps(patched_by_key.get(key, row), ensure_ascii=False) + "\n")
    print(f"[backfill] wrote {out_path} patched={len(patched_by_key)}", flush=True)
    return 0


def backfill_rows(
    args: argparse.Namespace,
    katago: evaluator.KataGoProcess,
    missing_rows: list[dict[str, Any]],
    games_by_key: dict[str, evaluator.Game],
) -> dict[tuple[str, str, int], dict[str, Any]]:
    rows_by_game: dict[str, list[dict[str, Any]]] = {}
    for row in missing_rows:
        key = str(row.get("game_key") or evaluator.game_key(Path(str(row.get("path") or ""))))
        if key:
            rows_by_game.setdefault(key, []).append(row)

    patched: dict[tuple[str, str, int], dict[str, Any]] = {}
    processed = 0
    for game_index, (game_key, rows) in enumerate(sorted(rows_by_game.items()), start=1):
        game = games_by_key.get(game_key)
        if game is None:
            print(f"[warn] skip game_key={game_key}: SGF not found", file=sys.stderr, flush=True)
            continue
        rows = sorted(rows, key=lambda row: int_number(row.get("move_number")))
        move_numbers = [int_number(row.get("move_number")) for row in rows]
        positions = [game.moves[: max(0, move_number - 1)] for move_number in move_numbers]
        print(
            f"[{game_index}/{len(rows_by_game)}] {game.path.name} rows={len(rows)}",
            flush=True,
        )
        analyses = katago.analyze_many(
            positions,
            rules=args.rules,
            komi=game.komi,
            size=game.size,
            max_visits=args.max_visits,
            batch_positions=args.batch_positions,
        )
        queries = []
        row_analysis_pairs = []
        for row, analysis in zip(rows, analyses):
            move_number = int_number(row.get("move_number"))
            side = str(row.get("side") or "")
            move = str(row.get("move") or "")
            if move_number <= 0 or side not in {"B", "W"} or not move:
                continue
            query = evaluator.HumanSlQuery(
                move_number,
                side,
                move,
                TARGET_PROFILE,
                game.moves[: max(0, move_number - 1)],
                evaluator.humansl_candidate_moves(analysis),
            )
            queries.append(query)
            row_analysis_pairs.append((row, analysis))
        if not queries:
            continue
        results = katago.analyze_humansl_many(
            queries,
            rules=args.rules,
            komi=game.komi,
            size=game.size,
            max_visits=args.human_max_visits,
            batch_positions=args.human_batch_positions,
        )
        for (row, _analysis), result in zip(row_analysis_pairs, results):
            patched_row = dict(row)
            patched_row[DIFFICULTY_FIELD] = (
                "" if result.mistake_probability is None else round(result.mistake_probability, 12)
            )
            patched_row[CANDIDATES_FIELD] = result.candidate_probabilities or []
            profiles = normalize_profiles(patched_row.get("human_sl_profiles"))
            if TARGET_PROFILE not in profiles:
                profiles.append(TARGET_PROFILE)
            patched_row["human_sl_profiles"] = profiles
            patched[move_row_key(row)] = patched_row
            processed += 1
            if args.limit_rows > 0 and processed >= args.limit_rows:
                return patched
    return patched


def load_rows(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as exc:
                print(f"[warn] skip invalid JSON line {line_number}: {exc}", file=sys.stderr)
    return rows


def needs_backfill(row: dict[str, Any]) -> bool:
    return row.get(DIFFICULTY_FIELD) in {None, ""} or not row.get(CANDIDATES_FIELD)


def move_row_key(row: dict[str, Any]) -> tuple[str, str, int]:
    return (
        str(row.get("game_key") or evaluator.game_key(Path(str(row.get("path") or "")))),
        str(row.get("side") or ""),
        int_number(row.get("move_number")),
    )


def normalize_profiles(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def int_number(value: Any) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
