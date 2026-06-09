#!/usr/bin/env python3
"""Unit tests for HumanSL result bundle helpers."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import humansl_results

ALL_HUMANSL_PROFILES = [f"rank_{rank}k" for rank in range(18, 0, -1)] + [
    f"rank_{rank}d" for rank in range(1, 10)
]


class HumanSlResultsTest(unittest.TestCase):
    def test_package_validate_and_merge_bundle(self) -> None:
        with tempfile.TemporaryDirectory(prefix="humansl-results-test-") as tmp:
            root = Path(tmp)
            source_jsonl = root / "evaluation.jsonl"
            source_jsonl.write_text(json.dumps(sample_row(), ensure_ascii=False) + "\n", encoding="utf-8")
            source_move_jsonl = root / "move-evaluation.jsonl"
            source_move_jsonl.write_text(
                json.dumps(sample_move_row(), ensure_ascii=False) + "\n", encoding="utf-8"
            )
            bundle = root / "runner-a.zip"
            merged = root / "merged"

            package = subprocess.run(
                [
                    sys.executable,
                    str(Path(humansl_results.__file__)),
                    "package",
                    "--evaluation-jsonl",
                    str(source_jsonl),
                    "--move-jsonl",
                    str(source_move_jsonl),
                    "--out",
                    str(bundle),
                    "--machine-id",
                    "runner-a",
                    "--operator",
                    "tester",
                    "--katago-version",
                    "KataGo v1.15.0",
                    "--profiles",
                    ",".join(ALL_HUMANSL_PROFILES),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual("", package.stderr)
            self.assertEqual(0, package.returncode)
            self.assertTrue(bundle.is_file())

            summary = humansl_results.validate_bundle(bundle, require_humansl=True)
            self.assertEqual(1, summary["rows"])
            self.assertEqual(1, summary["move_rows"])
            self.assertEqual(1, summary["human_sl_rows"])

            humansl_results.merge_bundles([bundle], merged, require_humansl=True)
            merged_rows = humansl_results.load_jsonl_rows(merged / humansl_results.JSONL_NAME)
            merged_move_rows = humansl_results.load_jsonl_rows(merged / humansl_results.MOVE_JSONL_NAME)
            self.assertEqual(1, len(merged_rows))
            self.assertEqual(1, len(merged_move_rows))
            self.assertEqual("runner-a", merged_rows[0]["machine_id"])
            self.assertTrue((merged / humansl_results.CSV_NAME).is_file())

    def test_validate_rejects_missing_humansl_fields(self) -> None:
        row = sample_row()
        row.pop("human_sl_sample_count")

        with self.assertRaises(humansl_results.ValidationError):
            humansl_results.validate_rows([row], require_humansl=True)

    def test_validate_rejects_partial_humansl_profiles(self) -> None:
        row = sample_row()
        row["human_sl_profiles"] = ["rank_1d", "rank_2d"]
        row["human_sl_sample_count"] = 80
        row["human_sl_move_count"] = 80

        with self.assertRaises(humansl_results.ValidationError):
            humansl_results.validate_rows([row], require_humansl=True)

    def test_validate_rejects_partial_humansl_move_rows(self) -> None:
        row = sample_move_row()
        row["human_sl_log_probability_by_profile"] = {"rank_1d": -1.0}

        with self.assertRaises(humansl_results.ValidationError):
            humansl_results.validate_move_rows([row], require_humansl=True)

    def test_validate_allows_non_humansl_when_requested(self) -> None:
        row = sample_row()
        for key in list(row):
            if key.startswith("human_sl_"):
                row.pop(key)

        humansl_results.validate_rows([row], require_humansl=False)


def sample_row() -> dict[str, object]:
    return {
        "path": "/samples/game001.sgf",
        "side": "B",
        "player": "black-player",
        "fox_rank": "3d",
        "analyzed_moves": 80,
        "samples": 80,
        "max_visits": 32,
        "strength_band": "3-4d",
        "quality_score": 71.2,
        "human_sl_profiles": ALL_HUMANSL_PROFILES,
        "human_sl_sample_count": len(ALL_HUMANSL_PROFILES) * 80,
        "human_sl_move_count": 80,
        "human_sl_anomalous_sample_count": 0,
        "human_sl_best_profile": "rank_3d",
        "human_sl_best_second_gap": 0.123,
        "human_sl_high_low_trend": 0.456,
        "human_sl_avg_logp_rank_10k": -4.2,
        "human_sl_avg_logp_rank_1d": -3.1,
        "human_sl_avg_logp_rank_9d": -3.8,
    }


def sample_move_row() -> dict[str, object]:
    return {
        "path": "/samples/game001.sgf",
        "game_key": "game001",
        "side": "B",
        "player": "black-player",
        "fox_rank": "3d",
        "move_number": 1,
        "move": "D4",
        "human_sl_profiles": ALL_HUMANSL_PROFILES,
        "human_sl_log_probability_by_profile": {profile: -1.0 for profile in ALL_HUMANSL_PROFILES},
        "human_sl_status_by_profile": {profile: "ok" for profile in ALL_HUMANSL_PROFILES},
        "human_sl_best_profile": "rank_3d",
    }


if __name__ == "__main__":
    unittest.main()
