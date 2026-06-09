#!/usr/bin/env python3
"""Unit tests for the HumanSL feasibility probe helpers."""

from __future__ import annotations

import argparse
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import probe_humansl_feasibility as probe


class HumanSlFeasibilityProbeTest(unittest.TestCase):
    def test_build_analysis_command_includes_human_model(self) -> None:
        args = argparse.Namespace(
            katago="/opt/katago",
            config="analysis.cfg",
            model="default.bin.gz",
            human_model="b18c384nbt-humanv0.bin.gz",
            extra_arg=["-override-config", "maxVisits0=1"],
        )

        command = probe.build_analysis_command(args)

        self.assertEqual(
            [
                "/opt/katago",
                "analysis",
                "-model",
                "default.bin.gz",
                "-config",
                "analysis.cfg",
                "-human-model",
                "b18c384nbt-humanv0.bin.gz",
                "-override-config",
                "maxVisits0=1",
            ],
            command,
        )

    def test_build_humansl_query_requests_policy_for_profile(self) -> None:
        query = probe.build_humansl_query("rank_1d", "request-1")

        self.assertTrue(query["includePolicy"])
        self.assertEqual("rank_1d", query["overrideSettings"]["humanSLProfile"])
        self.assertEqual("request-1", query["id"])
        self.assertEqual([], query["moves"])

    def test_validate_response_accepts_root_human_policy(self) -> None:
        response = {"id": "x", "humanPolicy": {"D4": 0.03125}}

        result = probe.validate_response("rank_10k", response, "D4", 12.3)

        self.assertTrue(result.ok)
        self.assertEqual(0.03125, result.move_probability)

    def test_validate_response_accepts_nested_human_policy(self) -> None:
        response = {"id": "x", "rootInfo": {"humanPolicy": [["D4", 0.125]]}}

        result = probe.validate_response("rank_9d", response, "D4", 12.3)

        self.assertTrue(result.ok)
        self.assertEqual(0.125, result.move_probability)

    def test_validate_response_rejects_missing_human_policy(self) -> None:
        result = probe.validate_response("rank_1d", {"id": "x"}, "D4", 12.3)

        self.assertFalse(result.ok)
        self.assertIn("missing humanPolicy", result.error or "")

    def test_numeric_policy_uses_top_left_coordinate_index(self) -> None:
        policy = [0.0] * (19 * 19 + 1)
        policy[15 * 19 + 3] = 0.5

        probability = probe.extract_move_probability(policy, "D4")

        self.assertEqual(0.5, probability)

    def test_split_profiles_rejects_empty_input(self) -> None:
        with self.assertRaises(ValueError):
            probe.split_profiles(" , ")


if __name__ == "__main__":
    unittest.main()
