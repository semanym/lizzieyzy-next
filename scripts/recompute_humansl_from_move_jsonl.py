#!/usr/bin/env python3
"""Recompute only HumanSL fields using existing move-level KataGo metrics."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import evaluate_strength_samples as evaluator


HUMANSL_PREFIXES = (
    "human_sl_",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sgfs", nargs="+", help="Prepared SGF paths or glob patterns.")
    parser.add_argument("--move-jsonl", required=True, help="Existing move-evaluation JSONL.")
    parser.add_argument("--out-jsonl", required=True, help="Corrected summary JSONL output.")
    parser.add_argument("--out-move-jsonl", required=True, help="Corrected move JSONL output.")
    parser.add_argument("--katago", default=evaluator.DEFAULT_KATAGO)
    parser.add_argument("--model", default=evaluator.DEFAULT_MODEL)
    parser.add_argument("--config", default=evaluator.DEFAULT_CONFIG)
    parser.add_argument("--human-model", required=True)
    parser.add_argument("--human-profiles", required=True)
    parser.add_argument("--rules", default="Chinese")
    parser.add_argument("--max-visits", type=int, default=100)
    parser.add_argument("--human-max-visits", type=int, default=1)
    parser.add_argument("--human-batch-positions", type=int, default=16)
    parser.add_argument("--katago-response-timeout", type=float, default=1800)
    parser.add_argument("--limit-games", type=int, default=0)
    parser.add_argument(
        "--resume-jsonl",
        action="store_true",
        help="Append to existing outputs and skip games already complete in both corrected JSONLs.",
    )
    parser.add_argument(
        "--force-overwrite",
        action="store_true",
        help="Allow replacing existing output files when --resume-jsonl is not set.",
    )
    args = parser.parse_args()

    profiles = evaluator.split_humansl_profiles(args.human_profiles)
    games = evaluator.filter_games(
        evaluator.load_games(args.sgfs),
        include_handicap=False,
        min_moves=0,
        min_date="",
        require_same_rank=False,
        board_size=19,
        dedupe_chessid=True,
    )
    move_rows_by_game = load_move_rows(Path(args.move_jsonl))
    games = [game for game in games if evaluator.game_key(game.path) in move_rows_by_game]
    completed_summary_games: set[str] = set()
    completed_move_rows: set[tuple[str, str, int]] = set()
    out_jsonl = Path(args.out_jsonl)
    out_move_jsonl = Path(args.out_move_jsonl)
    if args.resume_jsonl:
        completed_summary_games = completed_summary_game_keys(out_jsonl, profiles)
        completed_move_games = completed_move_game_keys(out_move_jsonl, profiles)
        completed_move_rows = evaluator.completed_move_row_keys(out_move_jsonl)
        completed_games = completed_summary_games.intersection(completed_move_games)
        if completed_games:
            games = [game for game in games if evaluator.game_key(game.path) not in completed_games]
    if args.limit_games > 0:
        games = games[: args.limit_games]
    if not games:
        if args.resume_jsonl and completed_summary_games:
            print("No SGF files matched the requested filters; all matched games are already complete.")
            return 0
        raise SystemExit("no games with existing move rows matched")

    out_jsonl.parent.mkdir(parents=True, exist_ok=True)
    out_move_jsonl.parent.mkdir(parents=True, exist_ok=True)
    if not args.resume_jsonl and not args.force_overwrite:
        existing_outputs = [path for path in (out_jsonl, out_move_jsonl) if path.exists() and path.stat().st_size > 0]
        if existing_outputs:
            names = ", ".join(str(path) for path in existing_outputs)
            raise SystemExit(
                f"refusing to overwrite existing corrected output(s): {names}; "
                "use --resume-jsonl to continue or --force-overwrite to replace them"
            )
    write_mode = "a" if args.resume_jsonl else "w"

    katago = evaluator.KataGoProcess(
        Path(args.katago),
        Path(args.model),
        Path(args.config),
        args.katago_response_timeout,
        Path(args.human_model),
    )
    try:
        with out_jsonl.open(write_mode, encoding="utf-8") as summary_out, out_move_jsonl.open(
            write_mode, encoding="utf-8"
        ) as move_out:
            for index, game in enumerate(games, start=1):
                key = evaluator.game_key(game.path)
                existing_rows = move_rows_by_game[key]
                samples = samples_from_rows(existing_rows)
                if not samples:
                    print(f"[{index}/{len(games)} skipped] {game.path.name}: no samples", flush=True)
                    continue
                analyzed_moves = max(int(row.get("analyzed_moves") or 0) for row in existing_rows)
                max_visits = max(int(row.get("max_visits") or args.max_visits) for row in existing_rows)
                moves = game.moves[:analyzed_moves]
                queries = evaluator.build_humansl_queries(moves, profiles)
                print(
                    f"[{index}/{len(games)}] {game.path.name} humansl queries={len(queries)}",
                    flush=True,
                )
                human_results = katago.analyze_humansl_many(
                    queries,
                    rules=args.rules,
                    komi=game.komi,
                    size=game.size,
                    max_visits=args.human_max_visits,
                    batch_positions=args.human_batch_positions,
                )
                side_samples = {
                    "B": [sample for sample in samples if sample.color == "B"],
                    "W": [sample for sample in samples if sample.color == "W"],
                }
                human_features = {
                    "B": evaluator.humansl_side_features(human_results, profiles, "B"),
                    "W": evaluator.humansl_side_features(human_results, profiles, "W"),
                }
                for side in ("B", "W"):
                    row = evaluator.side_report(
                        game,
                        side,
                        side_samples[side],
                        analyzed_moves,
                        max_visits,
                        str(existing_rows[0].get("analysis_source") or "katago"),
                        int(existing_rows[0].get("sgf_analysis_positions") or 0),
                        int(existing_rows[0].get("katago_analysis_positions") or 0),
                        human_features[side],
                    )
                    summary_out.write(json.dumps(row, ensure_ascii=False) + "\n")
                corrected_move_rows = evaluator.move_detail_rows(
                    game,
                    samples,
                    analyzed_moves,
                    max_visits,
                    str(existing_rows[0].get("analysis_source") or "katago"),
                    int(existing_rows[0].get("sgf_analysis_positions") or 0),
                    int(existing_rows[0].get("katago_analysis_positions") or 0),
                    human_results,
                    profiles,
                )
                for row in corrected_move_rows:
                    move_key = evaluator.move_row_key(row)
                    if move_key in completed_move_rows:
                        continue
                    move_out.write(json.dumps(row, ensure_ascii=False) + "\n")
                    completed_move_rows.add(move_key)
                summary_out.flush()
                move_out.flush()
                b_best = human_features["B"].get("human_sl_best_profile", "-")
                w_best = human_features["W"].get("human_sl_best_profile", "-")
                print(
                    f"[{index}/{len(games)} done] {game.path.name} B={b_best} W={w_best}",
                    flush=True,
                )
    finally:
        katago.close()
    return 0


def completed_summary_game_keys(jsonl_path: Path, profiles: list[str]) -> set[str]:
    if not jsonl_path.exists():
        return set()
    profile_set = set(profiles)
    sides_by_game: dict[str, set[str]] = {}
    with jsonl_path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not has_complete_profiles(row, profile_set):
                continue
            key = str(row.get("game_key") or evaluator.game_key(Path(str(row.get("path") or ""))))
            side = str(row.get("side") or "")
            if key and side in {"B", "W"}:
                sides_by_game.setdefault(key, set()).add(side)
    return {key for key, sides in sides_by_game.items() if {"B", "W"}.issubset(sides)}


def completed_move_game_keys(jsonl_path: Path, profiles: list[str]) -> set[str]:
    if not jsonl_path.exists():
        return set()
    profile_set = set(profiles)
    rows_by_game: dict[str, dict[str, Any]] = {}
    with jsonl_path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not has_complete_profiles(row, profile_set):
                continue
            key = str(row.get("game_key") or evaluator.game_key(Path(str(row.get("path") or ""))))
            if key:
                entry = rows_by_game.setdefault(
                    key,
                    {
                        "move_numbers": set(),
                        "sides": set(),
                        "analyzed_moves": 0,
                    },
                )
                move_number = int_number(row.get("move_number"))
                if move_number > 0:
                    entry["move_numbers"].add(move_number)
                side = str(row.get("side") or "")
                if side in {"B", "W"}:
                    entry["sides"].add(side)
                entry["analyzed_moves"] = max(
                    int(entry["analyzed_moves"]),
                    int_number(row.get("analyzed_moves")),
                )
    completed: set[str] = set()
    for key, entry in rows_by_game.items():
        analyzed_moves = int(entry["analyzed_moves"])
        move_numbers = entry["move_numbers"]
        if (
            analyzed_moves > 0
            and len(move_numbers) >= analyzed_moves
            and max(move_numbers, default=0) >= analyzed_moves
            and {"B", "W"}.issubset(entry["sides"])
        ):
            completed.add(key)
    return completed


def has_complete_profiles(row: dict[str, Any], profile_set: set[str]) -> bool:
    if not profile_set:
        return True
    raw_profiles = row.get("human_sl_profiles")
    if isinstance(raw_profiles, list):
        row_profiles = {str(profile) for profile in raw_profiles}
    else:
        row_profiles = {part.strip() for part in str(raw_profiles or "").split(",") if part.strip()}
    if not profile_set.issubset(row_profiles):
        return False
    logp = row.get("human_sl_log_probability_by_profile")
    if isinstance(logp, dict):
        return profile_set.issubset({str(profile) for profile in logp})
    averages = row.get("human_sl_average_log_probability_by_profile")
    if isinstance(averages, dict):
        return profile_set.issubset({str(profile) for profile in averages})
    sample_count = int_number(row.get("human_sl_sample_count"))
    move_count = int_number(row.get("human_sl_move_count"))
    return sample_count >= max(1, move_count) * len(profile_set)


def load_move_rows(path: Path) -> dict[str, list[dict[str, Any]]]:
    rows_by_game: dict[str, list[dict[str, Any]]] = {}
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            key = str(row.get("game_key") or evaluator.game_key(Path(str(row.get("path") or ""))))
            if key:
                rows_by_game.setdefault(key, []).append(strip_humansl(row))
    return rows_by_game


def strip_humansl(row: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if not key.startswith(HUMANSL_PREFIXES)}


def samples_from_rows(rows: list[dict[str, Any]]) -> list[evaluator.Sample]:
    dedup: dict[tuple[str, int], evaluator.Sample] = {}
    for row in rows:
        side = str(row.get("side") or "")
        move_number = int(row.get("move_number") or 0)
        if side not in {"B", "W"} or move_number <= 0:
            continue
        dedup[(side, move_number)] = evaluator.Sample(
            move_number=move_number,
            color=side,
            move=str(row.get("move") or ""),
            winrate_loss=float(row.get("winrate_loss") or 0.0),
            score_loss=optional_float(row.get("score_loss")),
            first_choice=bool(row.get("first_choice")),
            ai_rank=int(row.get("ai_rank") or 0),
            category=str(row.get("category") or "unknown"),
            score_equivalent_loss=float(row.get("score_equivalent_loss") or 0.0),
            complexity=float(row.get("complexity") or 0.0),
            adjusted_weight=float(row.get("adjusted_weight") or 0.0),
        )
    return [dedup[key] for key in sorted(dedup, key=lambda item: (item[1], item[0]))]


def optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def int_number(value: Any) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
