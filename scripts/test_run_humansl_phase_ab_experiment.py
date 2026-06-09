#!/usr/bin/env python3
"""Unit tests for the HumanSL phase experiment splitter."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_humansl_phase_ab_experiment as phase_ab


class HumanSlPhaseExperimentSplitTest(unittest.TestCase):
    def test_stratified_group_folds_keep_games_together_and_cover_each_rank(self) -> None:
        rows = []
        for rank in ("1d", "2d"):
            for game in range(25):
                for side in ("B", "W"):
                    rows.append(
                        {
                            "game_key": f"{rank}-game-{game}",
                            "rank_label": rank,
                            "side": side,
                        }
                    )

        folds = phase_ab.stratified_group_folds(rows, 5)

        self.assertEqual(5, len(folds))
        tested_games = set()
        for train, test, _name in folds:
            train_games = {row["game_key"] for row in train}
            test_games = {row["game_key"] for row in test}
            self.assertFalse(train_games.intersection(test_games))
            self.assertEqual({"1d", "2d"}, {row["rank_label"] for row in test})
            self.assertEqual(10, len(test_games))
            tested_games.update(test_games)
        self.assertEqual(50, len(tested_games))


if __name__ == "__main__":
    unittest.main()
