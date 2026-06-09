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
