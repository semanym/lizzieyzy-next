#!/usr/bin/env python3
"""Aggregate HumanSL move rows by phase and run an offline A/B check.

The input is the move-level JSONL produced by evaluate_strength_samples.py.
This script does not run KataGo or HumanSL again. It re-aggregates existing
move rows into game+side+phase samples:

- global: all analyzed moves
- opening: moves 1-60
- middle_game: moves 61-150
- endgame: moves 151+

A is the current KataGo-derived strength formula. B keeps the same features and
adds HumanSL-derived aggregates in a grouped holdout regression.
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
HUMANSL_FEATURES = [
    "human_sl_best_profile_value_mean",
    "human_sl_best_second_gap_mean",
    "human_sl_high_low_trend_mean",
    "human_sl_valid_rate",
]
RIDGE_LAMBDA = 1.0


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
        help="Deterministic game-key holdout ratio.",
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
    result = run_ab(samples, args.test_ratio)
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
    humansl_values = [profile_value(row.get("human_sl_best_profile")) for row in rows]
    humansl_values = [value for value in humansl_values if value is not None]
    humansl_valid = [
        0.0 if int_number(row.get("human_sl_anomalous_sample_count")) > 0 else 1.0 for row in rows
    ]
    return {
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
        "human_sl_best_profile_value_mean": round(mean(humansl_values), 6) if humansl_values else "",
        "human_sl_best_second_gap_mean": round(
            mean(float_number(row.get("human_sl_best_second_gap")) for row in rows), 6
        ),
        "human_sl_high_low_trend_mean": round(
            mean(float_number(row.get("human_sl_high_low_trend")) for row in rows), 6
        ),
        "human_sl_valid_rate": round(mean(humansl_valid), 6),
    }


def run_ab(samples: list[dict[str, Any]], test_ratio: float) -> dict[str, Any]:
    result: dict[str, Any] = {"phases": {}}
    for phase in PHASES:
        phase_rows = [
            row for row in samples if row["phase"] == phase and not row["insufficient_samples"]
        ]
        if len(phase_rows) < 20:
            result["phases"][phase] = {"status": "insufficient_rows", "rows": len(phase_rows)}
            continue
        train = [row for row in phase_rows if not is_test_game(row["game_key"], test_ratio)]
        test = [row for row in phase_rows if is_test_game(row["game_key"], test_ratio)]
        if len(train) < 10 or len(test) < 5:
            result["phases"][phase] = {
                "status": "insufficient_split",
                "rows": len(phase_rows),
                "train_rows": len(train),
                "test_rows": len(test),
            }
            continue
        current_metrics = evaluate_predictions(
            test,
            [float_number(row["current_rank_prediction"]) for row in test],
        )
        a_model = fit_ridge(train, BASE_FEATURES, "rank_value", RIDGE_LAMBDA)
        a_predictions = [predict(a_model, row, BASE_FEATURES) for row in test]
        a_metrics = evaluate_predictions(test, a_predictions)
        b_features = BASE_FEATURES + HUMANSL_FEATURES
        model = fit_ridge(train, b_features, "rank_value", RIDGE_LAMBDA)
        b_predictions = [predict(model, row, b_features) for row in test]
        b_metrics = evaluate_predictions(test, b_predictions)
        result["phases"][phase] = {
            "status": "ok",
            "rows": len(phase_rows),
            "train_rows": len(train),
            "test_rows": len(test),
            "current_formula": current_metrics,
            "A_base_ridge": a_metrics,
            "B_current_plus_humansl": b_metrics,
            "delta_mae_A_base_minus_current": round(a_metrics["mae"] - current_metrics["mae"], 6),
            "delta_mae_B_minus_A": round(b_metrics["mae"] - a_metrics["mae"], 6),
            "delta_mae_B_minus_current": round(b_metrics["mae"] - current_metrics["mae"], 6),
            "features_A": BASE_FEATURES,
            "features_B": b_features,
            "coefficients_A": {
                "intercept": a_model["intercept"],
                **dict(zip(BASE_FEATURES, a_model["coefficients"], strict=True)),
            },
            "coefficients_B": {
                "intercept": model["intercept"],
                **dict(zip(b_features, model["coefficients"], strict=True)),
            },
        }
    result["coverage"] = coverage(samples)
    return result


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
    for x_values, y in zip(x_rows, centered_y, strict=True):
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
    for feature, coefficient in zip(features, model["coefficients"], strict=True):
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
    errors = [prediction - float_number(row["rank_value"]) for row, prediction in zip(rows, predictions, strict=True)]
    abs_errors = [abs(error) for error in errors]
    per_rank: dict[str, list[float]] = defaultdict(list)
    for row, error in zip(rows, abs_errors, strict=True):
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
        f"- Split: deterministic game-key holdout, test_ratio={args.test_ratio}",
        f"- Ridge lambda: `{RIDGE_LAMBDA}`",
        f"- Exclude labels above: `{args.exclude_labels_above or 'none'}`",
        f"- Exclude HumanSL gap over: `{args.exclude_gap_over if args.exclude_gap_over >= 0 else 'none'}`",
        "",
        "## Coverage",
        "",
        "```json",
        json.dumps(result["coverage"], ensure_ascii=False, indent=2),
        "```",
        "",
        "## A/B Summary",
        "",
        "| phase | rows | test | current MAE | A base MAE | B +HumanSL MAE | delta B-A | delta B-current | A <=1 | B <=1 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for phase in PHASES:
        item = result["phases"].get(phase, {})
        if item.get("status") != "ok":
            lines.append(
                f"| {phase} | {item.get('rows', 0)} | 0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a |"
            )
            continue
        current = item["current_formula"]
        a = item["A_base_ridge"]
        b = item["B_current_plus_humansl"]
        lines.append(
            f"| {phase} | {item['rows']} | {item['test_rows']} | "
            f"{current['mae']:.3f} | {a['mae']:.3f} | {b['mae']:.3f} | "
            f"{item['delta_mae_B_minus_A']:.3f} | {item['delta_mae_B_minus_current']:.3f} | "
            f"{a['within_1_rank_accuracy']:.3f} | {b['within_1_rank_accuracy']:.3f} |"
        )
    lines.extend(
        [
            "",
            "Current is the existing fixed KataGo-derived formula.",
            "A is a ridge model trained on the existing aggregate KataGo features.",
            "B is the same ridge setup with HumanSL aggregates added.",
            "A negative delta means the right-hand model improved MAE on the holdout split.",
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
