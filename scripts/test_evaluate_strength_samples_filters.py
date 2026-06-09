#!/usr/bin/env python3
"""Unit tests for strength sample SGF filters."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import evaluate_strength_samples as evaluator


class EvaluateStrengthSamplesFilterTest(unittest.TestCase):
    def test_filter_games_can_require_min_date_and_same_rank(self) -> None:
        with tempfile.TemporaryDirectory(prefix="strength-filter-test-") as tmp:
            root = Path(tmp)
            good = write_sgf(root / "good.sgf", date="2025-02-03", black_rank="3d", white_rank="3d")
            old = write_sgf(root / "old.sgf", date="2024-12-31", black_rank="3d", white_rank="3d")
            mismatch = write_sgf(root / "mismatch.sgf", date="2025-02-03", black_rank="3d", white_rank="4d")

            games = evaluator.load_games([str(good), str(old), str(mismatch)])
            filtered = evaluator.filter_games(
                games,
                include_handicap=False,
                min_moves=4,
                min_date="2025-01-01",
                require_same_rank=True,
                board_size=19,
                dedupe_chessid=False,
            )

        self.assertEqual([good.name], [game.path.name for game in filtered])

    def test_filter_games_normalizes_professional_rank_labels(self) -> None:
        with tempfile.TemporaryDirectory(prefix="strength-filter-test-") as tmp:
            path = write_sgf(
                Path(tmp) / "pro.sgf",
                date="2025-02-03",
                black_rank="9p",
                white_rank="professional 9",
            )

            filtered = evaluator.filter_games(
                evaluator.load_games([str(path)]),
                include_handicap=False,
                min_moves=4,
                min_date="2025-01-01",
                require_same_rank=True,
                board_size=19,
                dedupe_chessid=False,
            )

        self.assertEqual(1, len(filtered))

    def test_rank_9d_mistake_probability_counts_probability_outside_acceptable_moves(
        self,
    ) -> None:
        policy = {"D4": 0.40, "Q16": 0.25, "K10": 0.10, "pass": 0.02}

        mistake_probability = evaluator.extract_mistake_probability(
            policy, ["D4", "Q16", "D4"], 19
        )

        self.assertAlmostEqual(0.35, mistake_probability)

    def test_humansl_good_moves_uses_katago_good_move_threshold(self) -> None:
        analysis = {
            "moveInfos": [
                {"move": "D4", "order": 0, "scoreMean": 10.0},
                {"move": "Q16", "order": 1, "scoreMean": 9.0},
                {"move": "K10", "order": 2, "scoreMean": 8.7},
            ]
        }

        self.assertEqual(["D4", "Q16"], evaluator.humansl_good_moves(analysis))

    def test_humansl_acceptable_moves_supports_current_experiment_threshold(self) -> None:
        analysis = {
            "moveInfos": [
                {"move": "D4", "order": 0, "scoreMean": 10.0},
                {"move": "Q16", "order": 1, "scoreMean": 9.4},
                {"move": "K10", "order": 2, "scoreMean": 8.6},
            ]
        }

        self.assertEqual(["D4", "Q16", "K10"], evaluator.humansl_acceptable_moves(analysis, 1.5))

    def test_candidate_mistake_probability_can_be_recomputed_from_saved_candidates(self) -> None:
        candidates = [
            {"move": "D4", "score_loss": 0.0, "human_sl_probability_rank_9d": 0.4},
            {"move": "Q16", "score_loss": 1.4, "human_sl_probability_rank_9d": 0.3},
            {"move": "K10", "score_loss": 2.0, "human_sl_probability_rank_9d": 0.1},
        ]

        self.assertAlmostEqual(0.3, evaluator.candidate_mistake_probability(candidates, 1.5))


def write_sgf(path: Path, *, date: str, black_rank: str, white_rank: str) -> Path:
    path.write_text(
        (
            f"(;FF[4]GM[1]SZ[19]DT[{date}]PB[B]PW[W]BR[{black_rank}]WR[{white_rank}]"
            "KM[6.5];B[pd];W[dd];B[pp];W[dp])"
        ),
        encoding="utf-8",
    )
    return path


if __name__ == "__main__":
    unittest.main()
