#!/usr/bin/env python3
"""Aggregate HumanSL move rows by phase and run an offline strength experiment.

The input is the move-level JSONL produced by evaluate_strength_samples.py.
This script does not run KataGo or HumanSL again. It re-aggregates existing
move rows into game+side+phase samples:

- global: all analyzed moves
- opening: moves 1-60
- middle_game: moves 61-150
- endgame: moves 151+

The primary comparison is:

- control: existing KataGo-derived strength formula
- experiment_a: existing formula with difficulty replaced by rank_9d HumanSL
  mistake probability at score-loss threshold 1.5
- experiment_b: grouped cross-validated ridge model using experiment_a features
  plus HumanSL aggregates

The split is rank-stratified and grouped by game key so both sides and all
phases of the same game stay in the same fold.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import evaluate_strength_samples as evaluator


PHASES = ("global", "opening", "middle_game", "endgame")
PHASE_MIN_SAMPLES = {
    "global": 30,
    "opening": 15,
    "middle_game": 20,
    "endgame": 10,
}
BASE_FEATURES = [
    "first_choice_rate",
    "good_move_rate",
    "match_rate",
    "mistake_rate",
    "blunder_rate",
    "weighted_point_loss",
    "average_score_equivalent_loss",
    "median_score_loss",
    "p75_score_equivalent_loss",
    "p90_score_equivalent_loss",
    "average_difficulty",
]
HUMANSL_DIFFICULTY_THRESHOLD = 1.5
BASE_FEATURES_WITHOUT_DIFFICULTY = [
    feature for feature in BASE_FEATURES if feature != "average_difficulty"
]
HUMANSL_FEATURES = [
    "human_sl_best_profile_value_mean",
    "human_sl_best_second_gap_mean",
    "human_sl_high_low_trend_mean",
    "human_sl_valid_rate",
]
RIDGE_LAMBDA = 1.0
DEFAULT_FOLDS = 5


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("move_jsonl", help="Path to move-evaluation.jsonl.")
    parser.add_argument(
        "--out-dir",
        default="target/humansl-phase-ab",
        help="Directory for CSV/JSON/Markdown outputs.",
    )
    parser.add_argument(
        "--test-ratio",
        type=float,
        default=0.2,
        help="Legacy single-holdout ratio used only when --folds is 1.",
    )
    parser.add_argument(
        "--folds",
        type=int,
        default=DEFAULT_FOLDS,
        help=(
            "Rank-stratified grouped CV folds. Default 5 is recommended for 25 games per rank."
        ),
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=0,
        help="Override phase minimum sample counts. 0 uses phase defaults.",
    )
    parser.add_argument(
        "--exclude-labels-above",
        default="",
        help="Exclude labels stronger than this rank, e.g. 9d to keep HumanSL-comparable labels only.",
    )
    parser.add_argument(
        "--exclude-gap-over",
        type=int,
        default=-1,
        help="Exclude game-side samples whose HumanSL best-profile gap exceeds this value.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    samples = aggregate_move_rows(
        Path(args.move_jsonl),
        args.min_samples,
        exclude_labels_above=args.exclude_labels_above,
        exclude_gap_over=args.exclude_gap_over,
    )
    if not samples:
        print("[error] no usable phase samples")
        return 1

    write_csv(out_dir / "phase-strength-samples.csv", samples)
    result = run_ab(samples, args.test_ratio, args.folds)
    (out_dir / "ab-experiment-results.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_markdown(out_dir / "ab-experiment-report.md", samples, result, args)
    print(f"[phase-ab] wrote {out_dir}")
    return 0


def aggregate_move_rows(
    path: Path,
    min_samples_override: int,
    *,
    exclude_labels_above: str = "",
    exclude_gap_over: int = -1,
) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"[warn] skip invalid JSON line {line_number}: {exc}")
                continue
            game_key = str(row.get("game_key") or row.get("path") or "")
            side = str(row.get("side") or "")
            if not game_key or side not in {"B", "W"}:
                continue
            move_number = int_number(row.get("move_number"))
            if move_number <= 0:
                continue
            buckets[(game_key, side, "global")].append(row)
            buckets[(game_key, side, phase_for_move(move_number))].append(row)

    samples: list[dict[str, Any]] = []
    for (game_key, side, phase), rows in sorted(buckets.items()):
        sample = aggregate_bucket(game_key, side, phase, rows, min_samples_override)
        if sample:
            samples.append(sample)
    samples = filter_samples(samples, exclude_labels_above, exclude_gap_over)
    return samples


def aggregate_bucket(
    game_key: str,
    side: str,
    phase: str,
    rows: list[dict[str, Any]],
    min_samples_override: int,
) -> dict[str, Any] | None:
    if not rows:
        return None
    rank_label = str(rows[0].get("fox_rank") or "")
    rank_index = rank_to_index(rank_label)
    rank_value = rank_to_value(rank_label)
    if rank_index is None or rank_value is None:
        return None
    sample_count = len(rows)
    min_samples = min_samples_override if min_samples_override > 0 else PHASE_MIN_SAMPLES[phase]
    first_choice_rate = mean(1.0 if row.get("first_choice") else 0.0 for row in rows)
    good_move_rate = mean(
        1.0 if str(row.get("category") or "") in {"excellent", "great", "good"} else 0.0
        for row in rows
    )
    mistake_rate = mean(
        1.0 if str(row.get("category") or "") in {"mistake", "blunder"} else 0.0
        for row in rows
    )
    blunder_rate = mean(1.0 if str(row.get("category") or "") == "blunder" else 0.0 for row in rows)
    match_rate = evaluator.match_rate(first_choice_rate, good_move_rate, mistake_rate)
    score_losses = [float_number(row.get("score_loss")) for row in rows if row.get("score_loss") is not None]
    equivalent_losses = [float_number(row.get("score_equivalent_loss")) for row in rows]
    weighted_point_loss = weighted_loss(rows, mean(equivalent_losses))
    average_score_equivalent_loss = mean(equivalent_losses)
    median_score_loss = statistics.median(score_losses) if score_losses else average_score_equivalent_loss
    p75_score_equivalent_loss = percentile(equivalent_losses, 0.75)
    p90_score_equivalent_loss = percentile(equivalent_losses, 0.90)
    average_difficulty = 100.0 * mean(float_number(row.get("complexity")) for row in rows)
    difficulty_values = [humansl_mistake_probability(row) for row in rows]
    difficulty_values = [value for value in difficulty_values if value is not None]
    human_sl_9d_mistake_difficulty = (
        100.0 * mean(difficulty_values) if difficulty_values else average_difficulty
    )
    human_sl_9d_difficulty_coverage = (
        len(difficulty_values) / sample_count if sample_count > 0 else 0.0
    )
    quality_score = evaluator.quality_score(
        weighted_point_loss,
        average_score_equivalent_loss,
        median_score_loss,
        p75_score_equivalent_loss,
        p90_score_equivalent_loss,
        first_choice_rate,
        good_move_rate,
        mistake_rate,
        blunder_rate,
        match_rate,
        average_difficulty,
    )
    current_rank_prediction = evaluator.regressed_rank_value(
        weighted_point_loss,
        average_score_equivalent_loss,
        median_score_loss,
        p75_score_equivalent_loss,
        p90_score_equivalent_loss,
        first_choice_rate,
        good_move_rate,
        mistake_rate,
        blunder_rate,
        match_rate,
        average_difficulty,
    )
    experiment_a_rank_prediction = evaluator.regressed_rank_value(
        weighted_point_loss,
        average_score_equivalent_loss,
        median_score_loss,
        p75_score_equivalent_loss,
        p90_score_equivalent_loss,
        first_choice_rate,
        good_move_rate,
        mistake_rate,
        blunder_rate,
        match_rate,
        human_sl_9d_mistake_difficulty,
    )
    humansl_values = [profile_value(row.get("human_sl_best_profile")) for row in rows]
    humansl_values = [value for value in humansl_values if value is not None]
    humansl_valid = [
        0.0 if int_number(row.get("human_sl_anomalous_sample_count")) > 0 else 1.0 for row in rows
    ]
    sample = {
        "game_key": game_key,
        "side": side,
        "phase": phase,
        "player": str(rows[0].get("player") or ""),
        "rank_label": rank_label,
        "rank_index": rank_index,
        "rank_value": rank_value,
        "sample_count": sample_count,
        "insufficient_samples": sample_count < min_samples,
        "quality_score": round(quality_score, 4),
        "current_rank_prediction": round(current_rank_prediction, 6),
        "experiment_a_rank_prediction": round(experiment_a_rank_prediction, 6),
        "first_choice_rate": round(first_choice_rate, 6),
        "good_move_rate": round(good_move_rate, 6),
        "match_rate": round(match_rate, 6),
        "mistake_rate": round(mistake_rate, 6),
        "blunder_rate": round(blunder_rate, 6),
        "weighted_point_loss": round(weighted_point_loss, 6),
        "average_score_equivalent_loss": round(average_score_equivalent_loss, 6),
        "median_score_loss": round(median_score_loss, 6),
        "p75_score_equivalent_loss": round(p75_score_equivalent_loss, 6),
        "p90_score_equivalent_loss": round(p90_score_equivalent_loss, 6),
        "average_difficulty": round(average_difficulty, 6),
        "human_sl_9d_mistake_difficulty": round(human_sl_9d_mistake_difficulty, 6),
        "human_sl_9d_difficulty_coverage": round(human_sl_9d_difficulty_coverage, 6),
        "human_sl_best_profile_value_mean": round(mean(humansl_values), 6) if humansl_values else "",
        "human_sl_best_second_gap_mean": round(
            mean(float_number(row.get("human_sl_best_second_gap")) for row in rows), 6
        ),
        "human_sl_high_low_trend_mean": round(
            mean(float_number(row.get("human_sl_high_low_trend")) for row in rows), 6
        ),
        "human_sl_valid_rate": round(mean(humansl_valid), 6),
    }
    return sample


def run_ab(samples: list[dict[str, Any]], test_ratio: float, folds: int) -> dict[str, Any]:
    result: dict[str, Any] = {"phases": {}, "split": split_description(folds, test_ratio)}
    for phase in PHASES:
        phase_rows = [
            row for row in samples if row["phase"] == phase and not row["insufficient_samples"]
        ]
        if len(phase_rows) < 20:
            result["phases"][phase] = {"status": "insufficient_rows", "rows": len(phase_rows)}
            continue
        if folds <= 1:
            split_rows = [
                (
                    [row for row in phase_rows if not is_test_game(row["game_key"], test_ratio)],
                    [row for row in phase_rows if is_test_game(row["game_key"], test_ratio)],
                    "holdout",
                )
            ]
        else:
            split_rows = stratified_group_folds(phase_rows, folds)
        fold_results = []
        for train, test, fold_name in split_rows:
            if len(train) < 10 or len(test) < 5:
                continue
            current_metrics = evaluate_predictions(
                test,
                [float_number(row["current_rank_prediction"]) for row in test],
            )
            a_metrics = evaluate_predictions(
                test,
                [float_number(row["experiment_a_rank_prediction"]) for row in test],
            )
            b_features = experiment_features() + HUMANSL_FEATURES
            b_model = fit_ridge(train, b_features, "rank_value", RIDGE_LAMBDA)
            b_predictions = [predict(b_model, row, b_features) for row in test]
            b_metrics = evaluate_predictions(test, b_predictions)
            fold_results.append(
                {
                    "fold": fold_name,
                    "train_rows": len(train),
                    "test_rows": len(test),
                    "control_current_formula": current_metrics,
                    "experiment_a_9d_difficulty": a_metrics,
                    "experiment_b_9d_difficulty_plus_humansl": b_metrics,
                    "delta_mae_A_minus_control": round(
                        a_metrics["mae"] - current_metrics["mae"], 6
                    ),
                    "delta_mae_B_minus_A": round(b_metrics["mae"] - a_metrics["mae"], 6),
                    "delta_mae_B_minus_control": round(
                        b_metrics["mae"] - current_metrics["mae"], 6
                    ),
                    "coefficients_B": {
                        "intercept": b_model["intercept"],
                        **dict(zip(b_features, b_model["coefficients"])),
                    },
                }
            )
        if not fold_results:
            result["phases"][phase] = {
                "status": "insufficient_split",
                "rows": len(phase_rows),
                "folds": len(split_rows),
            }
            continue
        phase_result: dict[str, Any] = {
            "status": "ok",
            "rows": len(phase_rows),
            "folds": len(fold_results),
            "test_rows": sum(item["test_rows"] for item in fold_results),
            "control_current_formula": average_metric_block(
                item["control_current_formula"] for item in fold_results
            ),
            "experiment_a_9d_difficulty": average_metric_block(
                item["experiment_a_9d_difficulty"] for item in fold_results
            ),
            "experiment_b_9d_difficulty_plus_humansl": average_metric_block(
                item["experiment_b_9d_difficulty_plus_humansl"] for item in fold_results
            ),
            "delta_mae_A_minus_control": round(
                mean(item["delta_mae_A_minus_control"] for item in fold_results), 6
            ),
            "delta_mae_B_minus_A": round(
                mean(item["delta_mae_B_minus_A"] for item in fold_results), 6
            ),
            "delta_mae_B_minus_control": round(
                mean(item["delta_mae_B_minus_control"] for item in fold_results), 6
            ),
            "features_control": BASE_FEATURES,
            "features_A": experiment_features(),
            "features_B": experiment_features() + HUMANSL_FEATURES,
            "fold_results": fold_results,
        }
        result["phases"][phase] = phase_result
    result["coverage"] = coverage(samples)
    return result


def split_description(folds: int, test_ratio: float) -> dict[str, Any]:
    if folds <= 1:
        return {
            "method": "single_group_holdout",
            "test_ratio": test_ratio,
            "note": "Use only for quick smoke checks; 5-fold stratified grouped CV is recommended.",
        }
    return {
        "method": "rank_stratified_group_cv",
        "folds": folds,
        "group_key": "game_key",
        "note": "Recommended for 25 games per rank: each fold holds out about five games per rank.",
    }


def stratified_group_folds(
    rows: list[dict[str, Any]], folds: int
) -> list[tuple[list[dict[str, Any]], list[dict[str, Any]], str]]:
    fold_count = max(2, folds)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row["game_key"])].append(row)
    rank_groups: dict[str, list[str]] = defaultdict(list)
    for game_key, game_rows in groups.items():
        ranks = sorted({str(row.get("rank_label") or "") for row in game_rows})
        rank_groups[ranks[0] if ranks else ""].append(game_key)
    game_fold: dict[str, int] = {}
    for rank, game_keys in rank_groups.items():
        ordered = sorted(game_keys, key=lambda key: stable_hash(f"{rank}:{key}"))
        for index, game_key in enumerate(ordered):
            game_fold[game_key] = index % fold_count
    splits = []
    for fold in range(fold_count):
        train: list[dict[str, Any]] = []
        test: list[dict[str, Any]] = []
        for row in rows:
            target = test if game_fold[str(row["game_key"])] == fold else train
            target.append(row)
        splits.append((train, test, f"fold_{fold + 1}"))
    return splits


def stable_hash(value: str) -> int:
    return int(hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16], 16)


def humansl_mistake_probability(row: dict[str, Any]) -> float | None:
    direct = optional_float_number(row.get("human_sl_rank_9d_mistake_probability_loss_1.5"))
    if direct is not None:
        return direct
    return humansl_mistake_probability_from_candidates(
        row.get("human_sl_rank_9d_candidate_probabilities"), HUMANSL_DIFFICULTY_THRESHOLD
    )


def humansl_mistake_probability_from_candidates(value: Any, threshold: float) -> float | None:
    candidates = parse_json_value(value)
    if not isinstance(candidates, list):
        return None
    acceptable_probability = 0.0
    found = False
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        probability = optional_float_number(candidate.get("human_sl_probability_rank_9d"))
        if probability is None:
            continue
        if float_number(candidate.get("score_loss")) <= threshold:
            acceptable_probability += probability
            found = True
    if not found:
        return None
    return max(0.0, min(1.0, 1.0 - acceptable_probability))


def parse_json_value(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def experiment_features() -> list[str]:
    return BASE_FEATURES_WITHOUT_DIFFICULTY + ["human_sl_9d_mistake_difficulty"]


def average_metric_block(items: Any) -> dict[str, Any]:
    blocks = list(items)
    if not blocks:
        return {}
    metric_keys = ["mae", "rmse", "within_1_rank_accuracy", "within_2_rank_accuracy"]
    averaged = {key: round(mean(block[key] for block in blocks), 6) for key in metric_keys}
    per_rank_values: dict[str, list[float]] = defaultdict(list)
    for block in blocks:
        for rank, value in block.get("per_rank_mae", {}).items():
            per_rank_values[rank].append(float_number(value))
    averaged["per_rank_mae"] = {
        rank: round(mean(values), 6) for rank, values in sorted(per_rank_values.items())
    }
    return averaged


def filter_samples(
    samples: list[dict[str, Any]],
    exclude_labels_above: str,
    exclude_gap_over: int,
) -> list[dict[str, Any]]:
    ceiling = rank_to_value(exclude_labels_above) if exclude_labels_above else None
    filtered: list[dict[str, Any]] = []
    for row in samples:
        rank_value = rank_to_value(str(row.get("rank_label") or ""))
        if ceiling is not None and rank_value is not None and rank_value > ceiling:
            continue
        if exclude_gap_over >= 0:
            human_value = optional_float_number(row.get("human_sl_best_profile_value_mean"))
            if human_value is not None and rank_value is not None and abs(human_value - rank_value) > exclude_gap_over:
                continue
        filtered.append(row)
    return filtered


def fit_ridge(rows: list[dict[str, Any]], features: list[str], target: str, penalty: float) -> dict[str, Any]:
    means = {feature: mean(float_number(row.get(feature)) for row in rows) for feature in features}
    stds = {
        feature: standard_deviation([float_number(row.get(feature)) for row in rows]) or 1.0
        for feature in features
    }
    x_rows = [[(float_number(row.get(feature)) - means[feature]) / stds[feature] for feature in features] for row in rows]
    y_values = [float_number(row[target]) for row in rows]
    y_mean = mean(y_values)
    centered_y = [value - y_mean for value in y_values]
    n = len(features)
    matrix = [[0.0 for _ in range(n)] for _ in range(n)]
    rhs = [0.0 for _ in range(n)]
    for x_values, y in zip(x_rows, centered_y):
        for i in range(n):
            rhs[i] += x_values[i] * y
            for j in range(n):
                matrix[i][j] += x_values[i] * x_values[j]
    for i in range(n):
        matrix[i][i] += penalty
    coefficients = solve_linear_system(matrix, rhs)
    return {
        "intercept": round(y_mean, 6),
        "coefficients": [round(value, 6) for value in coefficients],
        "means": means,
        "stds": stds,
    }


def predict(model: dict[str, Any], row: dict[str, Any], features: list[str]) -> float:
    total = float(model["intercept"])
    for feature, coefficient in zip(features, model["coefficients"]):
        total += coefficient * (
            (float_number(row.get(feature)) - float(model["means"][feature]))
            / float(model["stds"][feature])
        )
    return total


def solve_linear_system(matrix: list[list[float]], rhs: list[float]) -> list[float]:
    n = len(rhs)
    a = [row[:] + [rhs[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda row: abs(a[row][col]))
        if abs(a[pivot][col]) < 1e-12:
            continue
        a[col], a[pivot] = a[pivot], a[col]
        scale = a[col][col]
        a[col] = [value / scale for value in a[col]]
        for row in range(n):
            if row == col:
                continue
            factor = a[row][col]
            if factor == 0.0:
                continue
            a[row] = [value - factor * a[col][idx] for idx, value in enumerate(a[row])]
    return [a[row][n] for row in range(n)]


def evaluate_predictions(rows: list[dict[str, Any]], predictions: list[float]) -> dict[str, Any]:
    errors = [prediction - float_number(row["rank_value"]) for row, prediction in zip(rows, predictions)]
    abs_errors = [abs(error) for error in errors]
    per_rank: dict[str, list[float]] = defaultdict(list)
    for row, error in zip(rows, abs_errors):
        per_rank[str(row["rank_label"])].append(error)
    return {
        "mae": round(mean(abs_errors), 6),
        "rmse": round(math.sqrt(mean(error * error for error in errors)), 6),
        "within_1_rank_accuracy": round(mean(1.0 if error <= 1.0 else 0.0 for error in abs_errors), 6),
        "within_2_rank_accuracy": round(mean(1.0 if error <= 2.0 else 0.0 for error in abs_errors), 6),
        "per_rank_mae": {rank: round(mean(values), 6) for rank, values in sorted(per_rank.items())},
    }


def coverage(samples: list[dict[str, Any]]) -> dict[str, Any]:
    by_phase = Counter(row["phase"] for row in samples)
    valid_by_phase = Counter(row["phase"] for row in samples if not row["insufficient_samples"])
    by_rank = Counter(row["rank_label"] for row in samples if row["phase"] == "global")
    return {
        "rows_by_phase": dict(sorted(by_phase.items())),
        "valid_rows_by_phase": dict(sorted(valid_by_phase.items())),
        "global_rows_by_rank": dict(sorted(by_rank.items())),
    }


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_markdown(
    path: Path,
    samples: list[dict[str, Any]],
    result: dict[str, Any],
    args: argparse.Namespace,
) -> None:
    lines = [
        "# HumanSL Phase A/B Experiment",
        "",
        f"- Input: `{args.move_jsonl}`",
        f"- Rows: `{len(samples)}` player-phase samples",
        f"- Split: `{result['split']['method']}`",
        f"- Folds: `{args.folds}`",
        f"- Ridge lambda: `{RIDGE_LAMBDA}`",
        f"- Exclude labels above: `{args.exclude_labels_above or 'none'}`",
        f"- Exclude HumanSL gap over: `{args.exclude_gap_over if args.exclude_gap_over >= 0 else 'none'}`",
        "- Decision: do not reserve a fixed validation/experiment split for 25 games per rank; "
        "use rank-stratified grouped cross-validation so every game is tested once.",
        "",
        "## Coverage",
        "",
        "```json",
        json.dumps(result["coverage"], ensure_ascii=False, indent=2),
        "```",
        "",
        "## A/B Summary",
        "",
        "| phase | rows | tested | control MAE | A 9d-diff MAE | B +HumanSL MAE | delta A-control | delta B-A | delta B-control | control <=1 | A <=1 | B <=1 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for phase in PHASES:
        item = result["phases"].get(phase, {})
        if item.get("status") != "ok":
            lines.append(
                f"| {phase} | {item.get('rows', 0)} | 0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |"
            )
            continue
        current = item["control_current_formula"]
        a = item["experiment_a_9d_difficulty"]
        b = item["experiment_b_9d_difficulty_plus_humansl"]
        lines.append(
            f"| {phase} | {item['rows']} | {item['test_rows']} | "
            f"{current['mae']:.3f} | {a['mae']:.3f} | {b['mae']:.3f} | "
            f"{item['delta_mae_A_minus_control']:.3f} | {item['delta_mae_B_minus_A']:.3f} | "
            f"{item['delta_mae_B_minus_control']:.3f} | "
            f"{current['within_1_rank_accuracy']:.3f} | "
            f"{a['within_1_rank_accuracy']:.3f} | "
            f"{b['within_1_rank_accuracy']:.3f} |"
        )
    lines.extend(
        [
            "",
            "Control is the existing fixed KataGo-derived formula.",
            "Experiment A keeps the fixed formula but replaces difficulty with the HumanSL rank_9d mistake probability at score-loss threshold 1.5.",
            "Experiment B uses grouped cross-validated ridge regression on experiment A features plus HumanSL aggregates.",
            "A negative delta means the right-hand model improved average MAE across folds.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def phase_for_move(move_number: int) -> str:
    if 1 <= move_number <= 60:
        return "opening"
    if 61 <= move_number <= 150:
        return "middle_game"
    return "endgame"


def rank_to_index(label: str) -> int | None:
    label = label.strip().lower()
    if label.endswith("k"):
        value = int_number(label[:-1])
        if 1 <= value <= 18:
            return 18 - value
    if label.endswith("d"):
        value = int_number(label[:-1])
        if 1 <= value <= 11:
            return 17 + value
    return None


def rank_to_value(label: str) -> int | None:
    label = label.strip().lower()
    if label.endswith("k"):
        value = int_number(label[:-1])
        if 1 <= value <= 18:
            return -value
    if label.endswith("d"):
        value = int_number(label[:-1])
        if 1 <= value <= 11:
            return value
    return None


def profile_value(profile: Any) -> float | None:
    text = str(profile or "")
    if not text.startswith("rank_"):
        return None
    return rank_to_value(text.removeprefix("rank_"))


def weighted_loss(rows: list[dict[str, Any]], fallback: float) -> float:
    weight_sum = sum(float_number(row.get("adjusted_weight")) for row in rows)
    if weight_sum <= 0:
        return fallback
    return sum(
        float_number(row.get("score_equivalent_loss")) * float_number(row.get("adjusted_weight"))
        for row in rows
    ) / weight_sum


def percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = fraction * (len(ordered) - 1)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def is_test_game(game_key: str, test_ratio: float) -> bool:
    digest = hashlib.sha256(game_key.encode("utf-8", errors="replace")).hexdigest()
    bucket = int(digest[:8], 16) / 0xFFFFFFFF
    return bucket < max(0.0, min(0.9, test_ratio))


def mean(values: Any) -> float:
    collected = [float_number(value) for value in values]
    return statistics.fmean(collected) if collected else 0.0


def standard_deviation(values: list[float]) -> float:
    return statistics.pstdev(values) if len(values) > 1 else 0.0


def int_number(value: Any) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0


def float_number(value: Any) -> float:
    if value is None or value == "":
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(number) or math.isinf(number):
        return 0.0
    return number


def optional_float_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(number) or math.isinf(number):
        return None
    return number


if __name__ == "__main__":
    raise SystemExit(main())
