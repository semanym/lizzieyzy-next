#!/usr/bin/env python3
"""Unit tests for ranked SGF preparation filters."""

from __future__ import annotations

import csv
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).resolve().parent / "prepare_ranked_sgf_samples.py"


class PrepareRankedSgfSamplesTest(unittest.TestCase):
    def test_prepare_filters_date_and_same_rank(self) -> None:
        with tempfile.TemporaryDirectory(prefix="prepare-ranked-test-") as tmp:
            root = Path(tmp)
            source_dir = root / "input" / "3d"
            source_dir.mkdir(parents=True)
            write_sgf(source_dir / "good.sgf", date="2025-02-03", black_rank="3d", white_rank="3d")
            write_sgf(source_dir / "old.sgf", date="2024-12-31", black_rank="3d", white_rank="3d")
            write_sgf(source_dir / "mismatch.sgf", date="2025-02-03", black_rank="3d", white_rank="4d")
            out_dir = root / "prepared"

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--input-root",
                    str(root / "input"),
                    "--out",
                    str(out_dir),
                    "--per-rank",
                    "1",
                    "--ranks",
                    "3d",
                    "--min-date",
                    "2025-01-01",
                    "--require-date",
                    "--require-same-rank",
                ],
                check=False,
                capture_output=True,
                text=True,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            prepared = list((out_dir / "3d").glob("*.sgf"))
            self.assertEqual(1, len(prepared))
            with (out_dir / "manifest.csv").open(encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual("2025-02-03", rows[0]["date"])
            self.assertEqual("3d", rows[0]["black_rank"])
            self.assertEqual("3d", rows[0]["white_rank"])


def write_sgf(path: Path, *, date: str, black_rank: str, white_rank: str) -> None:
    path.write_text(
        (
            f"(;FF[4]GM[1]SZ[19]DT[{date}]PB[B]PW[W]BR[{black_rank}]WR[{white_rank}]"
            "KM[6.5];B[pd];W[dd];B[pp];W[dp])"
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    unittest.main()
