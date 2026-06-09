#!/usr/bin/env python3
"""Validate corrected HumanSL output against labeled ranks.

Reads the summary JSONL produced by recompute_humansl_from_move_jsonl.py (or the
main evaluate_strength_samples.py run) and reports, per game side, the labeled
rank versus the HumanSL best profile. A side is flagged anomalous when the
HumanSL best profile differs from the labeled rank by more than --max-gap ranks.

The HumanSL model only supports profiles up to rank_9d, so labels stronger than
9d (e.g. project-local 10d/11d professional tiers) are reported but never
flagged, since the model cannot place them above its 9d ceiling.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

# Ordered weakest -> strongest. Adjacent entries are exactly one rank apart.
RANK_ORDER = [f"{n}k" for n in range(18, 0, -1)] + [f"{n}d" for n in range(1, 10)]
RANK_INDEX = {label: index for index, label in enumerate(RANK_ORDER)}


def rank_label(value: str) -> str:
    return re.sub(r"^rank_", "", str(value or "").strip())


def path_parts(value: str) -> list[str]:
    return re.split(r"[\\/]+", str(value or "").strip())


def path_name(value: str) -> str:
    parts = [part for part in path_parts(value) if part]
    return parts[-1] if parts else ""


def path_parent_name(value: str) -> str:
    parts = [part for part in path_parts(value) if part]
    return parts[-2] if len(parts) >= 2 else ""


def rank_index(label: str) -> int | None:
    return RANK_INDEX.get(rank_label(label))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("jsonl", help="Corrected summary JSONL path.")
    parser.add_argument("--max-gap", type=int, default=2, help="Allowed rank gap before flagging.")
    parser.add_argument("--show", choices=["all", "anomalies"], default="all")
    args = parser.parse_args()

    path = Path(args.jsonl)
    if not path.is_file():
        print(f"[error] not found: {path}", file=sys.stderr)
        return 1

    by_game: dict[str, dict[str, dict[str, Any]]] = {}
    for line in path.open(encoding="utf-8", errors="replace"):
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        side = str(row.get("side") or "")
        if side not in {"B", "W"}:
            continue
        name = path_name(row.get("path"))
        label = rank_label(path_parent_name(row.get("path")))
        by_game.setdefault(name, {})[side] = {
            "label": label,
            "player": row.get("player"),
            "best": rank_label(row.get("human_sl_best_profile")),
            "band": row.get("strength_band"),
        }

    total = 0
    flagged = 0
    for name in sorted(by_game):
        sides = by_game[name]
        lines = []
        game_flagged = False
        for side in ("B", "W"):
            info = sides.get(side)
            if not info:
                continue
            total += 1
            label_idx = rank_index(info["label"])
            best_idx = rank_index(info["best"])
            note = "ok"
            if label_idx is None:
                # Label above HumanSL ceiling (e.g. 10d/11d): report only.
                note = "label>9d_ceiling"
            elif best_idx is None:
                note = "no_humansl"
            else:
                gap = abs(best_idx - label_idx)
                if gap > args.max_gap:
                    note = f"ANOMALY gap={gap}"
                    game_flagged = True
                    flagged += 1
                else:
                    note = f"gap={gap}"
            lines.append(
                f"  {side} label={info['label']:<4} humanSL={info['best'] or '-':<4}"
                f" band={info['band'] or '-':<20} player={info['player']} [{note}]"
            )
        if args.show == "anomalies" and not game_flagged:
            continue
        print(name)
        for entry in lines:
            print(entry)
    print(f"\nsides={total} anomalies={flagged}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
