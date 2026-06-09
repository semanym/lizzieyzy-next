#!/usr/bin/env python3
"""Package, validate, and merge HumanSL evaluation result bundles."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = "lizzieyzy-human-sl-results-v1"
DEFAULT_HUMAN_MODEL_NAME = "b18c384nbt-humanv0.bin.gz"
DEFAULT_HUMAN_MODEL_SHA256 = "637746e44f0efe00ad1245a50aa9bbf0716efe364c43965ead97bd6835d84ab5"
DEFAULT_HUMAN_MODEL_BYTES = 99066230
JSONL_NAME = "evaluation.jsonl"
MOVE_JSONL_NAME = "move-evaluation.jsonl"
CSV_NAME = "evaluation_summary_rows.csv"
MANIFEST_NAME = "manifest.json"
CHECKSUMS_NAME = "checksums.sha256"
RUN_LOG_NAME = "run.log"

REQUIRED_ROW_FIELDS = ("path", "side", "player", "fox_rank", "analyzed_moves", "samples")
HUMANSL_REQUIRED_FIELDS = (
    "human_sl_profiles",
    "human_sl_sample_count",
    "human_sl_move_count",
    "human_sl_best_profile",
    "human_sl_best_second_gap",
    "human_sl_high_low_trend",
)

PREFERRED_FIELD_ORDER = [
    "bundle_id",
    "machine_id",
    "path",
    "sgf",
    "game_id",
    "side",
    "player",
    "fox_rank",
    "analyzed_moves",
    "samples",
    "max_visits",
    "analysis_source",
    "sgf_analysis_positions",
    "katago_analysis_positions",
    "strength_band",
    "quality_score",
    "first_choice_rate",
    "good_move_rate",
    "match_rate",
    "bad_move_rate",
    "average_difficulty",
    "weighted_point_loss",
    "average_score_loss",
    "average_score_equivalent_loss",
    "median_score_loss",
    "p75_score_equivalent_loss",
    "p90_score_equivalent_loss",
    "average_winrate_loss",
    "mistake_rate",
    "blunder_rate",
    "human_sl_profiles",
    "human_sl_sample_count",
    "human_sl_move_count",
    "human_sl_anomalous_sample_count",
    "human_sl_best_profile",
    "human_sl_best_second_gap",
    "human_sl_high_low_trend",
    "human_sl_stage_best_profile_by_stage",
    "human_sl_average_log_probability_by_profile",
    "human_sl_stage_average_log_probability_by_profile",
]


class ValidationError(RuntimeError):
    """Raised when a result bundle violates the exchange contract."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    package = subparsers.add_parser("package", help="Create a shareable HumanSL result bundle.")
    package.add_argument("--evaluation-jsonl", required=True, help="Source evaluation JSONL.")
    package.add_argument("--move-jsonl", help="Optional move-level evaluation JSONL.")
    package.add_argument("--out", required=True, help="Output .zip path.")
    package.add_argument("--machine-id", required=True, help="Stable ID for the runner machine.")
    package.add_argument("--operator", default="", help="Person or account who ran the batch.")
    package.add_argument("--katago-version", default="", help="KataGo version string.")
    package.add_argument("--katago-binary", default="", help="Path or label of the KataGo binary.")
    package.add_argument("--main-model-sha256", default="", help="SHA256 of the normal KataGo model.")
    package.add_argument("--human-model-name", default=DEFAULT_HUMAN_MODEL_NAME)
    package.add_argument("--human-model-sha256", default=DEFAULT_HUMAN_MODEL_SHA256)
    package.add_argument("--human-model-bytes", type=int, default=DEFAULT_HUMAN_MODEL_BYTES)
    package.add_argument("--profiles", default="", help="Comma-separated HumanSL profiles used.")
    package.add_argument("--max-visits", type=int, default=0, help="Normal KataGo visits per move.")
    package.add_argument("--human-max-visits", type=int, default=0, help="HumanSL visits per query.")
    package.add_argument("--rules", default="", help="Rules used for analysis.")
    package.add_argument("--run-log", help="Optional run log to include.")
    package.add_argument("--sgf-dir", help="Optional SGF directory to include under sgf/.")
    package.add_argument("--note", default="", help="Free-form note stored in manifest.json.")

    validate = subparsers.add_parser("validate", help="Validate a bundle .zip or directory.")
    validate.add_argument("bundles", nargs="+", help="Bundle zip files or directories.")
    validate.add_argument("--allow-no-humansl", action="store_true")

    merge = subparsers.add_parser("merge", help="Merge validated bundles for calibration.")
    merge.add_argument("bundles", nargs="+", help="Bundle zip files or directories.")
    merge.add_argument("--out-dir", required=True, help="Output directory.")
    merge.add_argument("--allow-no-humansl", action="store_true")

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.command == "package":
            package_bundle(args)
        elif args.command == "validate":
            for bundle in args.bundles:
                summary = validate_bundle(Path(bundle), require_humansl=not args.allow_no_humansl)
                print(
                    f"[ok] {bundle}: rows={summary['rows']} "
                    f"human_sl_rows={summary['human_sl_rows']} bundle_id={summary['bundle_id']}"
                )
        elif args.command == "merge":
            merge_bundles(
                [Path(bundle) for bundle in args.bundles],
                Path(args.out_dir),
                require_humansl=not args.allow_no_humansl,
            )
        else:
            raise ValidationError(f"unsupported command: {args.command}")
    except ValidationError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    return 0


def package_bundle(args: argparse.Namespace) -> None:
    jsonl_path = Path(args.evaluation_jsonl)
    rows = load_jsonl_rows(jsonl_path)
    validate_rows(rows, require_humansl=True)
    move_rows: list[dict[str, Any]] = []
    if args.move_jsonl:
        move_jsonl_path = Path(args.move_jsonl)
        if not move_jsonl_path.exists():
            raise ValidationError(f"missing move-level JSONL: {move_jsonl_path}")
        move_rows = load_jsonl_rows(move_jsonl_path)
        if not move_rows:
            raise ValidationError(f"move-level JSONL is empty: {move_jsonl_path}")
        validate_move_rows(move_rows, require_humansl=True)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    bundle_id = bundle_id_from_rows(args.machine_id, rows)
    profiles = split_csv(args.profiles) or collect_profiles(rows)
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "bundle_id": bundle_id,
        "created_at": utc_now(),
        "machine_id": args.machine_id,
        "operator": args.operator,
        "katago_version": args.katago_version,
        "katago_binary": args.katago_binary,
        "main_model_sha256": args.main_model_sha256,
        "human_model": {
            "name": args.human_model_name,
            "sha256": args.human_model_sha256,
            "bytes": args.human_model_bytes,
        },
        "profiles": profiles,
        "max_visits": args.max_visits,
        "human_max_visits": args.human_max_visits,
        "rules": args.rules,
        "row_count": len(rows),
        "human_sl_row_count": count_humansl_rows(rows),
        "move_row_count": len(move_rows),
        "note": args.note,
    }

    with tempfile.TemporaryDirectory(prefix="humansl-bundle-") as tmp:
        root = Path(tmp)
        write_json(root / MANIFEST_NAME, manifest)
        write_jsonl(root / JSONL_NAME, rows)
        if move_rows:
            write_jsonl(root / MOVE_JSONL_NAME, move_rows)
        write_csv(root / CSV_NAME, rows)
        if args.run_log:
            shutil.copy2(args.run_log, root / RUN_LOG_NAME)
        else:
            (root / RUN_LOG_NAME).write_text("No run log was provided.\n", encoding="utf-8")
        if args.sgf_dir:
            copy_sgf_dir(Path(args.sgf_dir), root / "sgf")
        write_checksums(root)
        validate_bundle(root, require_humansl=True)
        with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    zf.write(path, path.relative_to(root).as_posix())

    print(f"[package] wrote {out_path}")


def validate_bundle(bundle: Path, *, require_humansl: bool) -> dict[str, Any]:
    with extracted_bundle(bundle) as root:
        manifest = load_manifest(root)
        validate_manifest(manifest)
        verify_checksums(root)
        rows = load_jsonl_rows(root / JSONL_NAME)
        validate_rows(rows, require_humansl=require_humansl)
        move_jsonl_path = root / MOVE_JSONL_NAME
        move_rows = load_jsonl_rows(move_jsonl_path) if move_jsonl_path.exists() else []
        if move_rows:
            validate_move_rows(move_rows, require_humansl=require_humansl)
        csv_path = root / CSV_NAME
        if csv_path.exists():
            csv_rows = load_csv_rows(csv_path)
            if len(csv_rows) != len(rows):
                raise ValidationError(
                    f"{CSV_NAME} row count {len(csv_rows)} does not match {JSONL_NAME} {len(rows)}"
                )
        expected_count = int(manifest.get("row_count") or 0)
        if expected_count and expected_count != len(rows):
            raise ValidationError(
                f"manifest row_count {expected_count} does not match {JSONL_NAME} {len(rows)}"
            )
        expected_move_count = int(manifest.get("move_row_count") or 0)
        if expected_move_count and expected_move_count != len(move_rows):
            raise ValidationError(
                f"manifest move_row_count {expected_move_count} does not match "
                f"{MOVE_JSONL_NAME} {len(move_rows)}"
            )
        return {
            "bundle_id": manifest.get("bundle_id", ""),
            "rows": len(rows),
            "move_rows": len(move_rows),
            "human_sl_rows": count_humansl_rows(rows),
            "manifest": manifest,
        }


def merge_bundles(bundles: list[Path], out_dir: Path, *, require_humansl: bool) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    merged_rows: list[dict[str, Any]] = []
    merged_move_rows: list[dict[str, Any]] = []
    manifests: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    seen_moves: set[tuple[str, str, str, int]] = set()
    for bundle in bundles:
        with extracted_bundle(bundle) as root:
            summary = validate_bundle(root, require_humansl=require_humansl)
            manifest = dict(summary["manifest"])
            manifests.append(manifest)
            bundle_id = str(manifest.get("bundle_id") or bundle.stem)
            machine_id = str(manifest.get("machine_id") or "")
            for row in load_jsonl_rows(root / JSONL_NAME):
                key = (
                    str(row.get("path") or row.get("sgf") or ""),
                    str(row.get("side") or ""),
                    str(row.get("player") or ""),
                )
                if key in seen:
                    continue
                seen.add(key)
                enriched = dict(row)
                enriched.setdefault("bundle_id", bundle_id)
                enriched.setdefault("machine_id", machine_id)
                merged_rows.append(enriched)
            move_jsonl_path = root / MOVE_JSONL_NAME
            if move_jsonl_path.exists():
                for row in load_jsonl_rows(move_jsonl_path):
                    key = (
                        str(row.get("path") or row.get("sgf") or ""),
                        str(row.get("side") or ""),
                        str(row.get("player") or ""),
                        int_number(row.get("move_number")),
                    )
                    if key in seen_moves:
                        continue
                    seen_moves.add(key)
                    enriched = dict(row)
                    enriched.setdefault("bundle_id", bundle_id)
                    enriched.setdefault("machine_id", machine_id)
                    merged_move_rows.append(enriched)

    validate_rows(merged_rows, require_humansl=require_humansl)
    if merged_move_rows:
        validate_move_rows(merged_move_rows, require_humansl=require_humansl)
    write_jsonl(out_dir / JSONL_NAME, merged_rows)
    if merged_move_rows:
        write_jsonl(out_dir / MOVE_JSONL_NAME, merged_move_rows)
    write_csv(out_dir / CSV_NAME, merged_rows)
    write_json(
        out_dir / MANIFEST_NAME,
        {
            "schema_version": SCHEMA_VERSION,
            "bundle_id": "merged-" + short_hash(json.dumps([m.get("bundle_id") for m in manifests])),
            "created_at": utc_now(),
            "source_bundle_count": len(manifests),
            "row_count": len(merged_rows),
            "human_sl_row_count": count_humansl_rows(merged_rows),
            "move_row_count": len(merged_move_rows),
            "source_bundles": manifests,
        },
    )
    write_checksums(out_dir)
    print(f"[merge] wrote {out_dir / JSONL_NAME}")
    print(
        "[next] python3 scripts/analyze_strength_calibration.py "
        f"{out_dir / JSONL_NAME} --out {out_dir / 'calibration-analysis'}"
    )


def load_manifest(root: Path) -> dict[str, Any]:
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.exists():
        raise ValidationError(f"missing {MANIFEST_NAME}")
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"invalid {MANIFEST_NAME}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValidationError(f"{MANIFEST_NAME} must be a JSON object")
    return data


def validate_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValidationError(
            f"unsupported schema_version: {manifest.get('schema_version')!r}; expected {SCHEMA_VERSION}"
        )
    if not str(manifest.get("bundle_id") or "").strip():
        raise ValidationError("manifest bundle_id is required")
    human_model = manifest.get("human_model")
    if isinstance(human_model, dict):
        if human_model.get("name") != DEFAULT_HUMAN_MODEL_NAME:
            raise ValidationError(f"unexpected human model name: {human_model.get('name')}")
        if human_model.get("sha256") != DEFAULT_HUMAN_MODEL_SHA256:
            raise ValidationError("unexpected human model SHA256")
        if int(human_model.get("bytes") or 0) != DEFAULT_HUMAN_MODEL_BYTES:
            raise ValidationError("unexpected human model byte size")


def validate_rows(rows: list[dict[str, Any]], *, require_humansl: bool) -> None:
    if not rows:
        raise ValidationError("evaluation rows are empty")
    for index, row in enumerate(rows, start=1):
        missing = [field for field in REQUIRED_ROW_FIELDS if field not in row]
        if missing:
            raise ValidationError(f"row {index} missing required fields: {', '.join(missing)}")
        if str(row.get("side")) not in {"B", "W"}:
            raise ValidationError(f"row {index} has invalid side: {row.get('side')!r}")
        if int_number(row.get("samples")) < 0 or int_number(row.get("analyzed_moves")) < 0:
            raise ValidationError(f"row {index} has negative sample or analyzed move count")
        if require_humansl:
            missing_humansl = [field for field in HUMANSL_REQUIRED_FIELDS if field not in row]
            if missing_humansl:
                raise ValidationError(
                    f"row {index} missing HumanSL fields: {', '.join(missing_humansl)}"
                )
            profiles = row_profiles(row.get("human_sl_profiles"))
            if not profiles:
                raise ValidationError(f"row {index} has no HumanSL profiles")
            sample_count = int_number(row.get("human_sl_sample_count"))
            move_count = int_number(row.get("human_sl_move_count"))
            if sample_count <= 0:
                raise ValidationError(f"row {index} has no HumanSL samples")
            if move_count <= 0:
                raise ValidationError(f"row {index} has no HumanSL moves")
            if sample_count < move_count * len(profiles):
                raise ValidationError(
                    f"row {index} has incomplete HumanSL samples: "
                    f"{sample_count} samples for {move_count} moves and {len(profiles)} profiles"
                )
            averages = row.get("human_sl_average_log_probability_by_profile")
            if isinstance(averages, dict) and not set(profiles).issubset({str(key) for key in averages}):
                raise ValidationError(f"row {index} has incomplete HumanSL average log probabilities")


def validate_move_rows(rows: list[dict[str, Any]], *, require_humansl: bool) -> None:
    if not rows:
        raise ValidationError("move rows are empty")
    seen: set[tuple[str, str, int]] = set()
    for index, row in enumerate(rows, start=1):
        key = (
            str(row.get("game_key") or row.get("path") or ""),
            str(row.get("side") or ""),
            int_number(row.get("move_number")),
        )
        if not key[0] or key[1] not in {"B", "W"} or key[2] <= 0:
            raise ValidationError(f"move row {index} has invalid game/side/move key")
        if key in seen:
            raise ValidationError(f"duplicate move row key: {key[0]} {key[1]} {key[2]}")
        seen.add(key)
        if require_humansl:
            profiles = row_profiles(row.get("human_sl_profiles"))
            if not profiles:
                raise ValidationError(f"move row {index} has no HumanSL profiles")
            logp = row.get("human_sl_log_probability_by_profile")
            if not isinstance(logp, dict) or not set(profiles).issubset({str(key) for key in logp}):
                raise ValidationError(f"move row {index} has incomplete HumanSL log probabilities")
            statuses = row.get("human_sl_status_by_profile")
            if isinstance(statuses, dict) and not set(profiles).issubset({str(key) for key in statuses}):
                raise ValidationError(f"move row {index} has incomplete HumanSL statuses")


def load_jsonl_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise ValidationError(f"missing {path}")
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValidationError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            if not isinstance(row, dict):
                raise ValidationError(f"{path}:{line_number}: row must be a JSON object")
            rows.append(row)
    return rows


def load_csv_rows(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8", errors="replace", newline="") as handle:
        return list(csv.DictReader(handle))


def write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = ordered_fieldnames(rows)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: csv_value(row.get(field)) for field in fieldnames})


def ordered_fieldnames(rows: list[dict[str, Any]]) -> list[str]:
    all_fields: set[str] = set()
    for row in rows:
        all_fields.update(row.keys())
    ordered = [field for field in PREFERRED_FIELD_ORDER if field in all_fields]
    ordered.extend(sorted(all_fields.difference(ordered)))
    return ordered


def csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def copy_sgf_dir(source: Path, target: Path) -> None:
    if not source.is_dir():
        raise ValidationError(f"SGF directory not found: {source}")
    target.mkdir(parents=True, exist_ok=True)
    for path in source.rglob("*.sgf"):
        destination = target / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)


def write_checksums(root: Path) -> None:
    lines: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == CHECKSUMS_NAME:
            continue
        lines.append(f"{sha256_file(path)}  {path.relative_to(root).as_posix()}")
    (root / CHECKSUMS_NAME).write_text("\n".join(lines) + "\n", encoding="utf-8")


def verify_checksums(root: Path) -> None:
    checksum_path = root / CHECKSUMS_NAME
    if not checksum_path.exists():
        raise ValidationError(f"missing {CHECKSUMS_NAME}")
    for line_number, line in enumerate(checksum_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            raise ValidationError(f"{CHECKSUMS_NAME}:{line_number}: invalid checksum line")
        expected, relative = parts
        relative = relative.strip()
        path = root / relative
        if not path.exists():
            raise ValidationError(f"checksum target missing: {relative}")
        actual = sha256_file(path)
        if actual != expected:
            raise ValidationError(f"checksum mismatch for {relative}")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bundle_id_from_rows(machine_id: str, rows: list[dict[str, Any]]) -> str:
    first_paths = [str(row.get("path") or "") for row in rows[:20]]
    return f"{safe_slug(machine_id)}-{short_hash(json.dumps(first_paths, ensure_ascii=False))}"


def short_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def safe_slug(value: str) -> str:
    slug = "".join(char if char.isalnum() or char in "-_" else "-" for char in value.strip())
    return slug.strip("-") or "unknown-machine"


def collect_profiles(rows: list[dict[str, Any]]) -> list[str]:
    profiles: set[str] = set()
    for row in rows:
        profiles.update(row_profiles(row.get("human_sl_profiles")))
    return sorted(profiles)


def row_profiles(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(item) for item in raw if str(item)]
    if isinstance(raw, str):
        return split_csv(raw)
    return []


def split_csv(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def count_humansl_rows(rows: Iterable[dict[str, Any]]) -> int:
    return sum(1 for row in rows if int_number(row.get("human_sl_sample_count")) > 0)


def int_number(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class extracted_bundle:
    def __init__(self, bundle: Path) -> None:
        self.bundle = bundle
        self.temp: tempfile.TemporaryDirectory[str] | None = None
        self.root: Path | None = None

    def __enter__(self) -> Path:
        if self.bundle.is_dir():
            self.root = self.bundle
            return self.root
        if not self.bundle.exists():
            raise ValidationError(f"bundle not found: {self.bundle}")
        self.temp = tempfile.TemporaryDirectory(prefix="humansl-extract-")
        self.root = Path(self.temp.name)
        try:
            with zipfile.ZipFile(self.bundle) as zf:
                zf.extractall(self.root)
        except zipfile.BadZipFile as exc:
            raise ValidationError(f"invalid zip bundle: {self.bundle}") from exc
        return self.root

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.temp is not None:
            self.temp.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
