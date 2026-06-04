#!/usr/bin/env python3
"""Probe whether a KataGo binary can serve HumanSL analysis policies.

The probe intentionally stays outside the product runtime. It starts KataGo
analysis with ``-human-model``, sends minimal ``includePolicy`` requests for a
few HumanSL profiles, and reports compatibility, latency, and peak RSS.
"""

from __future__ import annotations

import argparse
import json
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

try:
    import resource
except ImportError:  # Windows does not provide the Unix resource module.
    resource = None


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


DEFAULT_PROFILES = tuple(
    [f"rank_{rank}k" for rank in range(18, 0, -1)]
    + [f"rank_{rank}d" for rank in range(1, 10)]
)
DEFAULT_MOVE = "D4"
DEFAULT_TIMEOUT_SECONDS = 45.0
DEFAULT_MAX_QUERIES = 64
POLICY_FLOOR = 1.0e-12
GTP_COLUMNS = "ABCDEFGHJKLMNOPQRSTUVWXYZ"


@dataclass
class ProfileProbeResult:
    profile: str
    ok: bool
    latency_ms: float
    move_probability: float | None
    error: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--katago", required=True, help="Path to the KataGo executable.")
    parser.add_argument("--config", required=True, help="KataGo analysis config path.")
    parser.add_argument("--model", required=True, help="Normal KataGo analysis model path.")
    parser.add_argument("--human-model", required=True, help="HumanSL model path.")
    parser.add_argument(
        "--profiles",
        default=",".join(DEFAULT_PROFILES),
        help="Comma-separated HumanSL profiles to probe.",
    )
    parser.add_argument(
        "--move",
        default=DEFAULT_MOVE,
        help="Actual move coordinate used to sample a returned humanPolicy.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="Seconds to wait for each KataGo analysis response.",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="Repeat each profile query to sample latency and batching cost.",
    )
    parser.add_argument(
        "--max-queries",
        type=int,
        default=DEFAULT_MAX_QUERIES,
        help="Hard cap for probe queries; use this as the spike batch upper bound.",
    )
    parser.add_argument(
        "--extra-arg",
        action="append",
        default=[],
        help="Extra argument appended to the KataGo analysis command. Repeat as needed.",
    )
    return parser.parse_args()


def split_profiles(raw_profiles: str) -> list[str]:
    profiles = [item.strip() for item in raw_profiles.split(",") if item.strip()]
    if not profiles:
        raise ValueError("at least one HumanSL profile is required")
    return profiles


def build_analysis_command(args: argparse.Namespace) -> list[str]:
    command = [
        args.katago,
        "analysis",
        "-model",
        args.model,
        "-config",
        args.config,
        "-human-model",
        args.human_model,
    ]
    command.extend(args.extra_arg or [])
    return command


def build_humansl_query(
    profile: str,
    request_id: str,
    *,
    board_size: int = 19,
    komi: float = 7.5,
    max_visits: int = 1,
) -> dict[str, Any]:
    return {
        "id": request_id,
        "moves": [],
        "initialStones": [],
        "rules": "chinese",
        "komi": komi,
        "boardXSize": board_size,
        "boardYSize": board_size,
        "includePolicy": True,
        "maxVisits": max_visits,
        "overrideSettings": {
            "humanSLProfile": profile,
        },
    }


def extract_human_policy(response: dict[str, Any]) -> Any:
    if "humanPolicy" in response:
        return response["humanPolicy"]
    root_info = response.get("rootInfo")
    if isinstance(root_info, dict) and "humanPolicy" in root_info:
        return root_info["humanPolicy"]
    return None


def extract_move_probability(policy: Any, move: str, board_size: int = 19) -> float | None:
    if policy is None:
        return None
    normalized_move = move.strip().upper()
    if isinstance(policy, dict):
        value = policy.get(normalized_move)
        if value is None:
            value = policy.get(normalized_move.lower())
        return coerce_probability(value)
    if isinstance(policy, list) and policy and all(isinstance(item, (int, float)) for item in policy):
        index = gtp_policy_index(normalized_move, board_size)
        if index is None or index >= len(policy):
            return None
        return coerce_probability(policy[index])
    if isinstance(policy, list):
        for item in policy:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            if str(item[0]).strip().upper() == normalized_move:
                return coerce_probability(item[1])
    return None


def coerce_probability(value: Any) -> float | None:
    try:
        probability = float(value)
    except (TypeError, ValueError):
        return None
    if probability < 0.0:
        return None
    return max(probability, POLICY_FLOOR)


def gtp_policy_index(move: str, board_size: int) -> int | None:
    if move == "PASS":
        return board_size * board_size
    if len(move) < 2:
        return None
    column = GTP_COLUMNS.find(move[0])
    if column < 0 or column >= board_size:
        return None
    try:
        row = int(move[1:])
    except ValueError:
        return None
    if row < 1 or row > board_size:
        return None
    return (row - 1) * board_size + column


def read_katago_version(katago: str, timeout: float = 5.0) -> str:
    try:
        completed = subprocess.run(
            [katago, "version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"unavailable: {exc}"
    output = (completed.stdout + completed.stderr).strip()
    return output.splitlines()[0] if output else f"exit code {completed.returncode}"


class KataGoAnalysisProcess:
    def __init__(self, command: list[str], timeout: float) -> None:
        self.command = command
        self.timeout = timeout
        self.stderr_lines: list[str] = []
        self.stdout_queue: queue.Queue[str] = queue.Queue()
        self.process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.stdout_queue.put(line.rstrip("\n"))

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        for line in self.process.stderr:
            self.stderr_lines.append(line.rstrip("\n"))

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.process.poll() is not None:
            raise RuntimeError(f"KataGo exited before query: {self.describe_stderr()}")
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

        deadline = time.monotonic() + self.timeout
        expected_id = payload.get("id")
        while time.monotonic() < deadline:
            remaining = max(0.05, deadline - time.monotonic())
            try:
                line = self.stdout_queue.get(timeout=remaining)
            except queue.Empty:
                break
            if not line.strip():
                continue
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                continue
            if response.get("id") == expected_id:
                return response
        raise TimeoutError(f"timed out waiting for response {expected_id}: {self.describe_stderr()}")

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5.0)

    def describe_stderr(self) -> str:
        tail = self.stderr_lines[-8:]
        return " | ".join(tail) if tail else "no stderr captured"


def validate_response(
    profile: str,
    response: dict[str, Any],
    move: str,
    latency_ms: float,
) -> ProfileProbeResult:
    policy = extract_human_policy(response)
    if policy is None:
        return ProfileProbeResult(
            profile,
            False,
            latency_ms,
            None,
            "missing humanPolicy; check KataGo HumanSL support and -human-model loading",
        )
    probability = extract_move_probability(policy, move)
    if probability is None:
        return ProfileProbeResult(
            profile,
            False,
            latency_ms,
            None,
            f"humanPolicy present but move {move} probability could not be read",
        )
    return ProfileProbeResult(profile, True, latency_ms, probability)


def run_probe(args: argparse.Namespace) -> list[ProfileProbeResult]:
    profiles = split_profiles(args.profiles)
    total_queries = len(profiles) * max(args.repeats, 0)
    if args.repeats < 1:
        raise ValueError("--repeats must be >= 1")
    if total_queries > args.max_queries:
        raise ValueError(
            f"probe would send {total_queries} queries; raise --max-queries if this is intentional"
        )

    command = build_analysis_command(args)
    process = KataGoAnalysisProcess(command, args.timeout)
    results: list[ProfileProbeResult] = []
    try:
        for repeat in range(args.repeats):
            for profile in profiles:
                request_id = f"humansl-{profile}-{repeat + 1}"
                query = build_humansl_query(profile, request_id)
                start = time.perf_counter()
                try:
                    response = process.request(query)
                    latency_ms = (time.perf_counter() - start) * 1000.0
                    results.append(validate_response(profile, response, args.move, latency_ms))
                except Exception as exc:  # noqa: BLE001 - spike should report all runtime failures.
                    latency_ms = (time.perf_counter() - start) * 1000.0
                    results.append(ProfileProbeResult(profile, False, latency_ms, None, str(exc)))
    finally:
        process.close()
    return results


def peak_child_rss_mib() -> float:
    if resource is None:
        return 0.0
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    # macOS reports bytes, Linux reports KiB.
    divisor = 1024.0 * 1024.0 if sys.platform == "darwin" else 1024.0
    return usage.ru_maxrss / divisor


def print_report(
    args: argparse.Namespace,
    version_text: str,
    results: Iterable[ProfileProbeResult],
    total_seconds: float,
) -> bool:
    result_list = list(results)
    ok = all(item.ok for item in result_list)
    print("# HumanSL feasibility probe")
    print(f"katago_version: {version_text}")
    print(f"query_count: {len(result_list)} / max_queries {args.max_queries}")
    print(f"total_latency_ms: {total_seconds * 1000.0:.1f}")
    print(f"peak_child_rss_mib: {peak_child_rss_mib():.1f}")
    print()
    for item in result_list:
        status = "OK" if item.ok else "FAIL"
        probability = (
            "n/a" if item.move_probability is None else f"{item.move_probability:.8g}"
        )
        print(
            f"{status} profile={item.profile} latency_ms={item.latency_ms:.1f} "
            f"move={args.move} probability={probability}"
        )
        if item.error:
            print(f"  reason: {item.error}")
    print()
    print(
        "batch_upper_bound_note: this spike sent no more than --max-queries requests; "
        "increase it only after observed latency and RSS are acceptable."
    )
    return ok


def main() -> int:
    args = parse_args()
    for path_name in ("config", "model", "human_model"):
        path = Path(getattr(args, path_name))
        if not path.exists():
            print(f"[error] {path_name.replace('_', '-')} does not exist: {path}", file=sys.stderr)
            return 2
    version_text = read_katago_version(args.katago)
    start = time.perf_counter()
    try:
        results = run_probe(args)
    except Exception as exc:  # noqa: BLE001 - produce a readable compatibility failure.
        print("# HumanSL feasibility probe")
        print(f"katago_version: {version_text}")
        print(f"FAIL startup_or_probe_error: {exc}")
        return 1
    total_seconds = time.perf_counter() - start
    return 0 if print_report(args, version_text, results, total_seconds) else 1


if __name__ == "__main__":
    raise SystemExit(main())
