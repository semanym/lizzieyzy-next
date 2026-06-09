#!/usr/bin/env python3
"""Unit tests for HumanSL recompute resume helpers."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import recompute_humansl_from_move_jsonl as recompute
import evaluate_strength_samples as evaluator


class RecomputeHumanSlResumeTest(unittest.TestCase):
    def test_completed_summary_requires_all_requested_profiles(self) -> None:
        profiles = ["rank_18k", "rank_17k"]
        with tempfile.TemporaryDirectory(prefix="recompute-humansl-test-") as tmp:
            path = Path(tmp) / "evaluation.jsonl"
            rows = [
                summary_row("complete.sgf", "B", profiles),
                summary_row("complete.sgf", "W", profiles),
                summary_row("partial.sgf", "B", ["rank_18k"]),
                summary_row("partial.sgf", "W", ["rank_18k"]),
            ]
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

            completed = recompute.completed_summary_game_keys(path, profiles)

        self.assertIn(evaluator.game_key(Path("complete.sgf")), completed)
        self.assertNotIn(evaluator.game_key(Path("partial.sgf")), completed)

    def test_completed_move_requires_profile_complete_rows(self) -> None:
        profiles = ["rank_18k", "rank_17k"]
        with tempfile.TemporaryDirectory(prefix="recompute-humansl-test-") as tmp:
            path = Path(tmp) / "move-evaluation.jsonl"
            rows = [
                move_row("complete.sgf", "B", 1, 2, profiles),
                move_row("complete.sgf", "W", 2, 2, profiles),
                move_row("partial.sgf", "B", 1, 2, ["rank_18k"]),
                move_row("partial.sgf", "W", 2, 2, ["rank_18k"]),
            ]
            path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

            completed = recompute.completed_move_game_keys(path, profiles)

        self.assertIn(evaluator.game_key(Path("complete.sgf")), completed)
        self.assertNotIn(evaluator.game_key(Path("partial.sgf")), completed)

    def test_has_complete_profiles_accepts_summary_counts(self) -> None:
        row = {
            "human_sl_profiles": ["rank_18k", "rank_17k"],
            "human_sl_sample_count": 20,
            "human_sl_move_count": 10,
        }

        self.assertTrue(recompute.has_complete_profiles(row, {"rank_18k", "rank_17k"}))


def summary_row(path: str, side: str, profiles: list[str]) -> dict[str, object]:
    return {
        "path": path,
        "side": side,
        "human_sl_profiles": profiles,
        "human_sl_average_log_probability_by_profile": {profile: -1.0 for profile in profiles},
        "human_sl_sample_count": len(profiles),
        "human_sl_move_count": 1,
    }


def move_row(
    path: str, side: str, move_number: int, analyzed_moves: int, profiles: list[str]
) -> dict[str, object]:
    return {
        "path": path,
        "side": side,
        "move_number": move_number,
        "analyzed_moves": analyzed_moves,
        "human_sl_profiles": profiles,
        "human_sl_log_probability_by_profile": {profile: -1.0 for profile in profiles},
    }


if __name__ == "__main__":
    unittest.main()
