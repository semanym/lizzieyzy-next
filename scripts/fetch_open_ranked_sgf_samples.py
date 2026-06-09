#!/usr/bin/env python3
"""Fetch ranked SGF samples from open Go datasets.

The script streams SGF archives and stops once the requested rank buckets are
filled. It is designed for the Windows GPU calibration workflow where there is
no pre-existing local SGF directory.

Default sources:
- OGS 2025 SGF dump for ordinary amateur ranks, filtered to recent games.
- JGDB tarball for professional / top-professional labels.

Rank labeling is intentionally conservative. A game is copied into a rank bucket
only when both sides normalize to the same bucket. Professional ranks map to the
project-local 10d/11d labels:
- 1p..8p -> 10d
- 9p -> 11d

The 12d label is reserved for clearly named strong AI games such as AlphaGo,
KataGo, Leela Zero, ELF OpenGo, FineArt, or Golaxy.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import io
import json
import re
import shutil
import sys
import tarfile
import time
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path


NETWORK_ERRORS = (urllib.error.URLError, TimeoutError, http.client.RemoteDisconnected, ConnectionError)

OGS_2025_SGF_URL = "https://za3k.com/ogs/ogs_games_2013_to_2025-05/sgfs-by-date.tar.gz"
JGDB_URL = "https://data.pjreddie.com/files/jgdb.tar.gz"
DEFAULT_OGS_MIN_DATE = "2025-01-01"
OGS_GAME_METADATA_URL = "https://online-go.com/api/v1/games/{game_id}"
OGS_GAME_SGF_URL = "https://online-go.com/api/v1/games/{game_id}/sgf"
OGS_PLAYERS_URL = "https://online-go.com/api/v1/players/?format=json&page={page}"
OGS_PLAYER_GAMES_URL = "https://online-go.com/api/v1/players/{player_id}/games?format=json&ordering=-ended&page={page}"
DEFAULT_RANKS = [f"{rank}k" for rank in range(18, 0, -1)] + [
    f"{rank}d" for rank in range(1, 13)
]
ORDINARY_RANKS = {f"{rank}k" for rank in range(18, 0, -1)} | {
    f"{rank}d" for rank in range(1, 10)
}
PRO_RANKS = {"10d", "11d", "12d"}
AI_NAME_MARKERS = (
    "amybot",
    "alphago",
    "bot",
    "dangoapp",
    "doge_bot",
    "katago",
    "leela zero",
    "leelazero",
    "elf opengo",
    "fineart",
    "gnugo",
    "golaxy",
    "noob_bot",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="Output rank directory root.")
    parser.add_argument("--per-rank", type=int, default=25, help="Target SGFs per rank bucket.")
    parser.add_argument(
        "--append",
        action="store_true",
        help="Keep existing rank directories and continue filling missing buckets.",
    )
    parser.add_argument(
        "--stop-after-accepted",
        type=int,
        default=0,
        help="Stop after accepting this many new SGFs across all buckets. 0 means no early stop.",
    )
    parser.add_argument("--min-moves", type=int, default=80, help="Skip games shorter than this.")
    parser.add_argument("--max-moves", type=int, default=500, help="Skip very long games.")
    parser.add_argument("--board-size", type=int, default=19, help="Only keep this board size.")
    parser.add_argument("--ranks", default=",".join(DEFAULT_RANKS))
    parser.add_argument("--ogs-url", default=OGS_2025_SGF_URL, help="OGS SGF tar.gz URL.")
    parser.add_argument(
        "--ogs-min-date",
        default=DEFAULT_OGS_MIN_DATE,
        help="Only keep OGS games on or after this date. Use empty string to disable.",
    )
    parser.add_argument("--jgdb-url", default=JGDB_URL, help="JGDB tar.gz URL.")
    parser.add_argument("--http-retries", type=int, default=2, help="HTTP open retry attempts.")
    parser.add_argument("--retry-delay", type=int, default=10, help="Seconds between HTTP retries.")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP open timeout in seconds.")
    parser.add_argument(
        "--ogs-api-fallback",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="If the OGS dump is unavailable, sample public OGS SGFs through the game API.",
    )
    parser.add_argument("--ogs-api-start", type=int, default=80000000, help="First OGS game id to try.")
    parser.add_argument("--ogs-api-min", type=int, default=70000000, help="Lowest OGS game id to try.")
    parser.add_argument("--ogs-api-step", type=int, default=1, help="Game id decrement step.")
    parser.add_argument("--ogs-api-sleep", type=float, default=0.5, help="Seconds between OGS API requests.")
    parser.add_argument("--ogs-api-max-requests", type=int, default=250000, help="Maximum OGS API SGF requests.")
    parser.add_argument(
        "--ogs-player-fallback",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Before random game-id scanning, crawl public games from candidate OGS players.",
    )
    parser.add_argument(
        "--ogs-player-max-pages",
        type=int,
        default=100,
        help="Maximum OGS player-list pages to inspect during targeted fallback.",
    )
    parser.add_argument(
        "--ogs-player-games-pages",
        type=int,
        default=4,
        help="Maximum public game-list pages to inspect per candidate player.",
    )
    parser.add_argument(
        "--ogs-player-max-games",
        type=int,
        default=2,
        help="Maximum newly accepted SGFs any one OGS player may contribute.",
    )
    parser.add_argument(
        "--ogs-seed-game-limit",
        type=int,
        default=120,
        help="Maximum existing OGS game ids to inspect before falling back to player-list pages.",
    )
    parser.add_argument(
        "--ogs-api-progress-interval",
        type=int,
        default=25,
        help="Print OGS API fallback progress every N requests.",
    )
    parser.add_argument(
        "--prefer-ogs-api",
        action="store_true",
        help="Use the OGS game API directly instead of opening the bulk dump first.",
    )
    parser.add_argument("--skip-ogs", action="store_true", help="Do not fetch OGS samples.")
    parser.add_argument("--skip-jgdb", action="store_true", help="Do not fetch JGDB samples.")
    parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="Exit successfully even if some buckets are not filled.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    out = Path(args.out)
    ranks = [rank.strip() for rank in args.ranks.split(",") if rank.strip()]
    needed = {rank: int(args.per_rank) for rank in ranks}
    counts = {rank: 0 for rank in ranks}
    initial_counts = dict(counts)

    if out.exists() and not args.append:
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    if args.append:
        counts.update(existing_rank_counts(out, ranks))
        initial_counts = dict(counts)
    args._initial_counts = dict(initial_counts)
    args._accepted_new = 0
    seen_hashes = existing_sgf_hashes(out, ranks) if args.append else set()

    if not args.skip_ogs:
        ordinary_needed = {rank: needed[rank] for rank in ranks if rank in ORDINARY_RANKS}
        if args.prefer_ogs_api:
            print("[fetch] using OGS game API before the bulk dump", flush=True)
            sample_ogs_api(out, ordinary_needed, counts, seen_hashes, args)
        else:
            try:
                stream_source(
                    "OGS",
                    args.ogs_url,
                    out,
                    ordinary_needed,
                    counts,
                    seen_hashes,
                    args,
                    min_date=args.ogs_min_date,
                )
            except (urllib.error.HTTPError, *NETWORK_ERRORS) as exc:
                if not args.ogs_api_fallback:
                    raise
                print(
                    f"[fetch] OGS dump unavailable ({exc}); falling back to OGS game API",
                    file=sys.stderr,
                    flush=True,
                )
                sample_ogs_api(out, ordinary_needed, counts, seen_hashes, args)
    if not args.skip_jgdb:
        pro_needed = {rank: needed[rank] for rank in ranks if rank in PRO_RANKS}
        stream_source(
            "JGDB",
            args.jgdb_url,
            out,
            pro_needed,
            counts,
            seen_hashes,
            args,
            min_date="",
        )

    accepted = sum(counts[rank] - initial_counts.get(rank, 0) for rank in ranks)
    missing = [f"{rank}: {counts[rank]}/{needed[rank]}" for rank in ranks if counts[rank] < needed[rank]]
    for rank in ranks:
        print(f"[fetch] {rank}: {counts[rank]} SGFs")
    print(f"[fetch] accepted_new: {accepted} SGFs")
    if missing:
        print("[warn] incomplete rank buckets:", file=sys.stderr)
        for item in missing:
            print(f"  {item}", file=sys.stderr)
        if not args.allow_partial:
            return 1
    return 0


def stream_source(
    name: str,
    url: str,
    out: Path,
    needed: dict[str, int],
    counts: dict[str, int],
    seen_hashes: set[str],
    args: argparse.Namespace,
    min_date: str = "",
) -> None:
    if not needed:
        return
    print(f"[fetch] streaming {name}: {url}", flush=True)
    minimum = parse_date_key(min_date) if min_date else None
    last_skip_period = ""
    player_name_counts = existing_player_name_contributions(out)
    max_games_per_player = max(1, int(getattr(args, "ogs_player_max_games", 2)))
    with open_url_with_retries(url, args.http_retries, args.retry_delay, args.timeout) as response:
        with tarfile.open(fileobj=response, mode="r|gz") as archive:
            for member in archive:
                if all(counts[rank] >= needed[rank] for rank in needed):
                    print(f"[fetch] {name}: quotas satisfied", flush=True)
                    return
                if not member.isfile() or not member.name.lower().endswith(".sgf"):
                    continue
                member_date = date_from_member_name(member.name)
                if minimum and member_date and member_date < minimum:
                    skip_period = f"{member_date[0]:04d}-{member_date[1]:02d}"
                    if skip_period != last_skip_period:
                        last_skip_period = skip_period
                        print(
                            f"[fetch] {name}: scanning {skip_period}; waiting for >= {min_date}",
                            flush=True,
                        )
                    continue
                extracted = archive.extractfile(member)
                if extracted is None:
                    continue
                raw = extracted.read()
                text = decode_sgf(raw)
                props = root_properties(text)
                if minimum and not meets_min_date(member.name, props, min_date):
                    continue
                if not acceptable_game(text, props, args):
                    continue
                player_names = game_player_names(props)
                if has_ai_player(player_names):
                    continue
                if player_name_limit_reached(player_names, player_name_counts, max_games_per_player):
                    continue
                rank = game_bucket_rank(props)
                if rank not in needed or counts[rank] >= needed[rank]:
                    continue
                digest = ranked_sgf_hash(rank, text)
                if digest in seen_hashes:
                    continue
                seen_hashes.add(digest)
                counts[rank] += 1
                args._accepted_new += 1
                increment_player_name_counts(player_names, player_name_counts)
                write_ranked_sgf(out, rank, counts[rank], member.name, text)
                print(f"[fetch] {name}: {rank} {counts[rank]}/{needed[rank]} {member.name}", flush=True)
                if reached_acceptance_limit(args, counts):
                    print(f"[fetch] {name}: accepted SGF limit reached", flush=True)
                    return


def open_url_with_retries(url: str, retries: int, retry_delay: int, timeout: int):
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "lizzieyzy-next HumanSL calibration sampler",
        },
    )
    attempts = max(1, int(retries))
    for attempt in range(1, attempts + 1):
        try:
            print(f"[fetch] opening {url} attempt {attempt}/{attempts}", flush=True)
            return urllib.request.urlopen(request, timeout=timeout)
        except (urllib.error.HTTPError, *NETWORK_ERRORS) as exc:
            status = getattr(exc, "code", None)
            retryable_status = status in {429, 500, 502, 503, 504}
            retryable = retryable_status or not isinstance(exc, urllib.error.HTTPError)
            if attempt >= attempts or not retryable:
                print(f"[fetch] open failed permanently: {exc}", file=sys.stderr, flush=True)
                raise
            print(
                f"[fetch] open failed: {exc}; retrying in {retry_delay}s",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(max(1, int(retry_delay)))
    raise RuntimeError("unreachable")


def sample_ogs_api(
    out: Path,
    needed: dict[str, int],
    counts: dict[str, int],
    seen_hashes: set[str],
    args: argparse.Namespace,
) -> None:
    if not needed:
        return
    player_name_counts = existing_player_name_contributions(out)
    if args.ogs_player_fallback:
        sample_ogs_players(out, needed, counts, seen_hashes, args, player_name_counts)
        if all(counts[rank] >= needed[rank] for rank in needed) or reached_acceptance_limit(args, counts):
            return
    start = int(args.ogs_api_start)
    stop = int(args.ogs_api_min)
    step = max(1, int(args.ogs_api_step))
    max_requests = max(1, int(args.ogs_api_max_requests))
    progress_interval = max(1, int(args.ogs_api_progress_interval))
    scanned = 0
    failures = 0
    reject_counts: Counter[str] = Counter()
    game_id = start
    print(
        f"[fetch] OGS API fallback scanning game ids {start} down to {stop}, max_requests={max_requests}",
        flush=True,
    )
    while game_id >= stop and scanned < max_requests:
        if all(counts[rank] >= needed[rank] for rank in needed):
            print("[fetch] OGS API fallback: quotas satisfied", flush=True)
            return
        scanned += 1
        if scanned == 1 or scanned % progress_interval == 0:
            accepted = int(getattr(args, "_accepted_new", 0) or 0)
            print(
                f"[fetch] OGS API heartbeat scanned={scanned}/{max_requests} "
                f"accepted_new={accepted} failures={failures} next_game={game_id} "
                f"rejects={format_reject_counts(reject_counts)}",
                flush=True,
            )
        url = OGS_GAME_SGF_URL.format(game_id=game_id)
        try:
            text = fetch_text_url(url, args.timeout)
        except urllib.error.HTTPError as exc:
            failures += 1
            if exc.code == 429:
                if reached_acceptance_limit(args, counts) or (
                    int(getattr(args, "stop_after_accepted", 0) or 0) > 0
                    and int(getattr(args, "_accepted_new", 0) or 0) > 0
                ):
                    print(
                        "[fetch] OGS API throttled after accepting SGFs; ending this incremental batch",
                        flush=True,
                    )
                    return
                delay = max(float(args.ogs_api_sleep) * 10.0, 30.0)
                print(f"[fetch] OGS API throttled at game {game_id}; sleeping {delay:.1f}s", flush=True)
                time.sleep(delay)
            elif exc.code not in {400, 403, 404}:
                print(f"[fetch] OGS API game {game_id} failed: {exc}", flush=True)
            game_id -= step
            continue
        except NETWORK_ERRORS as exc:
            failures += 1
            print(f"[fetch] OGS API game {game_id} failed: {exc}", flush=True)
            time.sleep(max(1.0, float(args.ogs_api_sleep)))
            game_id -= step
            continue

        props = root_properties(text)
        player_names = game_player_names(props)
        if not meets_min_date(str(game_id), props, args.ogs_min_date):
            reject_counts["date"] += 1
            if scanned % 1000 == 0:
                print(f"[fetch] OGS API scanned {scanned}; reached older games near {game_id}", flush=True)
            game_id -= step
            time.sleep(max(0.0, float(args.ogs_api_sleep)))
            continue
        rejection = game_rejection_reason(text, props, args, needed, counts)
        if rejection:
            reject_counts[rejection] += 1
            game_id -= step
            time.sleep(max(0.0, float(args.ogs_api_sleep)))
            continue
        if has_ai_player(player_names):
            reject_counts["bot_or_ai"] += 1
            game_id -= step
            time.sleep(max(0.0, float(args.ogs_api_sleep)))
            continue
        if player_name_limit_reached(player_names, player_name_counts, int(args.ogs_player_max_games)):
            reject_counts["player_name_full"] += 1
            game_id -= step
            time.sleep(max(0.0, float(args.ogs_api_sleep)))
            continue
        rank = game_bucket_rank(props)
        assert rank is not None
        digest = ranked_sgf_hash(rank, text)
        if digest in seen_hashes:
            reject_counts["duplicate"] += 1
            game_id -= step
            time.sleep(max(0.0, float(args.ogs_api_sleep)))
            continue
        seen_hashes.add(digest)
        counts[rank] += 1
        args._accepted_new += 1
        increment_player_name_counts(player_names, player_name_counts)
        write_ranked_sgf(out, rank, counts[rank], f"ogs-api-{game_id}.sgf", text)
        print(
            f"[fetch] OGS API: {rank} {counts[rank]}/{needed[rank]} game {game_id}",
            flush=True,
        )
        if reached_acceptance_limit(args, counts):
            print("[fetch] OGS API fallback: accepted SGF limit reached", flush=True)
            return
        if scanned % progress_interval == 0:
            filled = ", ".join(f"{rank}:{counts[rank]}/{needed[rank]}" for rank in sorted(needed))
            print(
                f"[fetch] OGS API progress scanned={scanned} failures={failures} "
                f"rejects={format_reject_counts(reject_counts)} {filled}",
                flush=True,
            )
        game_id -= step
        time.sleep(max(0.0, float(args.ogs_api_sleep)))


def sample_ogs_players(
    out: Path,
    needed: dict[str, int],
    counts: dict[str, int],
    seen_hashes: set[str],
    args: argparse.Namespace,
    player_name_counts: Counter[str],
) -> None:
    if not needed:
        return
    max_player_pages = max(0, int(args.ogs_player_max_pages))
    max_game_pages = max(1, int(args.ogs_player_games_pages))
    max_games_per_player = max(1, int(args.ogs_player_max_games))
    player_counts = existing_ogs_player_contributions(out)
    reject_counts: Counter[str] = Counter()
    scanned_players = 0
    accepted_before = int(getattr(args, "_accepted_new", 0) or 0)
    sample_ogs_seed_players(
        out,
        needed,
        counts,
        seen_hashes,
        args,
        max_game_pages,
        max_games_per_player,
        player_counts,
        player_name_counts,
        reject_counts,
    )
    if all(counts[rank] >= needed[rank] for rank in needed) or reached_acceptance_limit(args, counts):
        return
    if max_player_pages <= 0:
        print("[fetch] OGS player-list fallback disabled after seed player fallback", flush=True)
        return
    print(
        f"[fetch] OGS player fallback scanning up to {max_player_pages} player pages; "
        f"max_games_per_player={max_games_per_player}",
        flush=True,
    )
    for page in range(1, max_player_pages + 1):
        if all(counts[rank] >= needed[rank] for rank in needed) or reached_acceptance_limit(args, counts):
            break
        try:
            data = fetch_json_url(OGS_PLAYERS_URL.format(page=page), args.timeout)
        except (urllib.error.HTTPError, *NETWORK_ERRORS, json.JSONDecodeError) as exc:
            print(f"[fetch] OGS player page {page} failed: {exc}", flush=True)
            time.sleep(max(1.0, float(args.ogs_api_sleep)))
            continue
        players = data.get("results") or []
        if not players:
            break
        for player in players:
            if all(counts[rank] >= needed[rank] for rank in needed) or reached_acceptance_limit(args, counts):
                break
            scanned_players += 1
            player_id = int_number(player.get("id"))
            if player_id <= 0:
                continue
            if player_counts.get(player_id, 0) >= max_games_per_player:
                reject_counts["player_full"] += 1
                continue
            candidate_rank = ogs_ranking_bucket(player.get("ranking"))
            if candidate_rank not in needed or counts.get(candidate_rank, 0) >= needed[candidate_rank]:
                reject_counts["player_rank_not_needed"] += 1
                continue
            accepted = sample_ogs_player_games(
                out,
                needed,
                counts,
                seen_hashes,
                args,
                player_id,
                max_game_pages,
                max_games_per_player,
                player_counts,
                player_name_counts,
                reject_counts,
            )
            if accepted:
                print(
                    f"[fetch] OGS player {player_id}: accepted {accepted}; "
                    f"player_contribution={player_counts.get(player_id, 0)}/{max_games_per_player}",
                    flush=True,
                )
        if page == 1 or page % 10 == 0:
            accepted_now = int(getattr(args, "_accepted_new", 0) or 0) - accepted_before
            print(
                f"[fetch] OGS player heartbeat pages={page}/{max_player_pages} "
                f"players={scanned_players} accepted_new={accepted_now} "
                f"rejects={format_reject_counts(reject_counts)}",
                flush=True,
            )
    accepted_now = int(getattr(args, "_accepted_new", 0) or 0) - accepted_before
    print(
        f"[fetch] OGS player fallback done players={scanned_players} "
        f"accepted_new={accepted_now} rejects={format_reject_counts(reject_counts)}",
        flush=True,
    )


def sample_ogs_seed_players(
    out: Path,
    needed: dict[str, int],
    counts: dict[str, int],
    seen_hashes: set[str],
    args: argparse.Namespace,
    max_game_pages: int,
    max_games_per_player: int,
    player_counts: Counter[int],
    player_name_counts: Counter[str],
    reject_counts: Counter[str],
) -> None:
    game_ids = existing_ogs_seed_game_ids(out, needed, counts, int(args.ogs_seed_game_limit))
    if not game_ids:
        print("[fetch] OGS seed player fallback: no existing OGS game ids in missing buckets", flush=True)
        return
    accepted_before = int(getattr(args, "_accepted_new", 0) or 0)
    seen_players: set[int] = set()
    print(
        f"[fetch] OGS seed player fallback inspecting {len(game_ids)} existing game ids "
        "from missing buckets",
        flush=True,
    )
    for index, game_id in enumerate(game_ids, 1):
        if all(counts[rank] >= needed[rank] for rank in needed) or reached_acceptance_limit(args, counts):
            break
        try:
            game = fetch_json_url(OGS_GAME_METADATA_URL.format(game_id=game_id), args.timeout)
        except (urllib.error.HTTPError, *NETWORK_ERRORS, json.JSONDecodeError) as exc:
            reject_counts["seed_game_failed"] += 1
            print(f"[fetch] OGS seed game {game_id} metadata failed: {exc}", flush=True)
            time.sleep(max(1.0, float(args.ogs_api_sleep)))
            continue
        player_ids = ogs_game_player_ids(game)
        if not player_ids:
            reject_counts["seed_no_players"] += 1
            continue
        for seed_player_id in player_ids:
            if seed_player_id > 0:
                player_counts[seed_player_id] += 1
        for player_id in player_ids:
            if player_id in seen_players:
                continue
            seen_players.add(player_id)
            if player_counts.get(player_id, 0) >= max_games_per_player:
                reject_counts["player_full"] += 1
                continue
            accepted = sample_ogs_player_games(
                out,
                needed,
                counts,
                seen_hashes,
                args,
                player_id,
                max_game_pages,
                max_games_per_player,
                player_counts,
                player_name_counts,
                reject_counts,
            )
            if accepted:
                print(
                    f"[fetch] OGS seed player {player_id}: accepted {accepted}; "
                    f"player_contribution={player_counts.get(player_id, 0)}/{max_games_per_player}",
                    flush=True,
                )
            if all(counts[rank] >= needed[rank] for rank in needed) or reached_acceptance_limit(args, counts):
                break
        if index == 1 or index % 10 == 0:
            accepted_now = int(getattr(args, "_accepted_new", 0) or 0) - accepted_before
            print(
                f"[fetch] OGS seed player heartbeat games={index}/{len(game_ids)} "
                f"players={len(seen_players)} accepted_new={accepted_now} "
                f"rejects={format_reject_counts(reject_counts)}",
                flush=True,
            )
    accepted_now = int(getattr(args, "_accepted_new", 0) or 0) - accepted_before
    print(
        f"[fetch] OGS seed player fallback done games={len(game_ids)} "
        f"players={len(seen_players)} accepted_new={accepted_now} "
        f"rejects={format_reject_counts(reject_counts)}",
        flush=True,
    )


def sample_ogs_player_games(
    out: Path,
    needed: dict[str, int],
    counts: dict[str, int],
    seen_hashes: set[str],
    args: argparse.Namespace,
    player_id: int,
    max_game_pages: int,
    max_games_per_player: int,
    player_counts: Counter[int],
    player_name_counts: Counter[str],
    reject_counts: Counter[str],
) -> int:
    accepted = 0
    for page in range(1, max_game_pages + 1):
        if player_counts.get(player_id, 0) >= max_games_per_player:
            break
        if all(counts[rank] >= needed[rank] for rank in needed) or reached_acceptance_limit(args, counts):
            break
        try:
            data = fetch_json_url(OGS_PLAYER_GAMES_URL.format(player_id=player_id, page=page), args.timeout)
        except (urllib.error.HTTPError, *NETWORK_ERRORS, json.JSONDecodeError) as exc:
            reject_counts["player_games_failed"] += 1
            print(f"[fetch] OGS player {player_id} games page {page} failed: {exc}", flush=True)
            time.sleep(max(1.0, float(args.ogs_api_sleep)))
            break
        games = data.get("results") or []
        if not games:
            break
        for game in games:
            if player_counts.get(player_id, 0) >= max_games_per_player:
                break
            if reached_acceptance_limit(args, counts):
                break
            game_id = int_number(game.get("id"))
            if game_id <= 0:
                reject_counts["missing_game_id"] += 1
                continue
            if not ogs_game_metadata_is_promising(game, args):
                reject_counts["metadata"] += 1
                continue
            player_ids = ogs_game_player_ids(game)
            if player_id not in player_ids:
                player_ids.append(player_id)
                player_ids = sorted(set(player_ids))
            if any(player_counts.get(pid, 0) >= max_games_per_player for pid in player_ids):
                reject_counts["player_full"] += 1
                continue
            try:
                text = fetch_text_url(OGS_GAME_SGF_URL.format(game_id=game_id), args.timeout)
            except urllib.error.HTTPError as exc:
                reject_counts["sgf_http"] += 1
                if exc.code == 429:
                    delay = max(float(args.ogs_api_sleep) * 10.0, 30.0)
                    print(f"[fetch] OGS player crawl throttled at game {game_id}; sleeping {delay:.1f}s", flush=True)
                    time.sleep(delay)
                    return accepted
                continue
            except NETWORK_ERRORS as exc:
                reject_counts["sgf_failed"] += 1
                print(f"[fetch] OGS player game {game_id} failed: {exc}", flush=True)
                time.sleep(max(1.0, float(args.ogs_api_sleep)))
                continue
            props = root_properties(text)
            player_names = game_player_names(props)
            if not meets_min_date(str(game_id), props, args.ogs_min_date):
                reject_counts["date"] += 1
                continue
            rejection = game_rejection_reason(text, props, args, needed, counts)
            if rejection:
                reject_counts[rejection] += 1
                continue
            if has_ai_player(player_names):
                reject_counts["bot_or_ai"] += 1
                continue
            if player_name_limit_reached(player_names, player_name_counts, max_games_per_player):
                reject_counts["player_name_full"] += 1
                continue
            rank = game_bucket_rank(props)
            assert rank is not None
            digest = ranked_sgf_hash(rank, text)
            if digest in seen_hashes:
                reject_counts["duplicate"] += 1
                continue
            seen_hashes.add(digest)
            counts[rank] += 1
            args._accepted_new += 1
            for pid in player_ids:
                player_counts[pid] += 1
            increment_player_name_counts(player_names, player_name_counts)
            accepted += 1
            print(
                f"[fetch] OGS player: {rank} {counts[rank]}/{needed[rank]} "
                f"game {game_id} players={','.join(str(pid) for pid in player_ids)}",
                flush=True,
            )
            write_ranked_sgf(
                out,
                rank,
                counts[rank],
                ogs_player_source_name(player_id, player_ids, game_id),
                text,
            )
            if all(counts[rank] >= needed[rank] for rank in needed):
                return accepted
            time.sleep(max(0.0, float(args.ogs_api_sleep)))
    return accepted


def fetch_text_url(url: str, timeout: int) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "lizzieyzy-next HumanSL calibration sampler",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    return decode_sgf(raw)


def fetch_json_url(url: str, timeout: int) -> dict[str, object]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "lizzieyzy-next HumanSL calibration sampler",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
    return json.loads(raw.decode("utf-8"))


def existing_ogs_player_contributions(out: Path) -> Counter[int]:
    counts: Counter[int] = Counter()
    if not out.exists():
        return counts
    pattern = re.compile(r"ogs-player-(\d+)(?:-players-([0-9_]+))?-game-(\d+)\.sgf$", re.IGNORECASE)
    for path in out.glob("*/*.sgf"):
        match = pattern.search(path.name)
        if match:
            player_ids = [int(match.group(1))]
            if match.group(2):
                player_ids = [int(value) for value in match.group(2).split("_") if value]
            for player_id in set(player_ids):
                counts[player_id] += 1
    return counts


def existing_player_name_contributions(out: Path) -> Counter[str]:
    counts: Counter[str] = Counter()
    if not out.exists():
        return counts
    for path in out.glob("*/*.sgf"):
        text = path.read_text(encoding="utf-8", errors="replace")
        props = root_properties(text)
        increment_player_name_counts(game_player_names(props), counts)
    return counts


def game_player_names(props: dict[str, str]) -> list[str]:
    return [name for name in (props.get("PB", "").strip(), props.get("PW", "").strip()) if name]


def normalize_player_name(name: str) -> str:
    return re.sub(r"\s+", " ", str(name or "").strip().lower())


def has_ai_player(player_names: list[str]) -> bool:
    return any(is_strong_ai_name(name) for name in player_names)


def player_name_limit_reached(player_names: list[str], counts: Counter[str], max_games: int) -> bool:
    return any(counts.get(normalize_player_name(name), 0) >= max_games for name in player_names if name)


def increment_player_name_counts(player_names: list[str], counts: Counter[str]) -> None:
    for name in player_names:
        normalized = normalize_player_name(name)
        if normalized:
            counts[normalized] += 1


def existing_ogs_seed_game_ids(
    out: Path,
    needed: dict[str, int],
    counts: dict[str, int],
    limit: int,
) -> list[int]:
    if not out.exists() or limit <= 0:
        return []
    patterns = [
        re.compile(r"ogs-api-(\d+)\.sgf$", re.IGNORECASE),
        re.compile(r"ogs-player-\d+(?:-players-[0-9_]+)?-game-(\d+)\.sgf$", re.IGNORECASE),
    ]
    game_ids: list[int] = []
    seen: set[int] = set()
    missing_ranks = sorted(
        [rank for rank in needed if counts.get(rank, 0) < needed[rank]],
        key=lambda rank: (
            -(needed[rank] - counts.get(rank, 0)),
            -rank_strength_index(rank),
        ),
    )
    # Start from the rank buckets that still need SGFs. Existing games from the
    # same bucket are the best source of player ids likely to produce useful
    # nearby samples.
    for rank in missing_ranks:
        rank_dir = out / rank
        if not rank_dir.exists():
            continue
        for path in sorted(rank_dir.glob("*.sgf"), reverse=True):
            game_id = ogs_game_id_from_name(path.name, patterns)
            if game_id > 0 and game_id not in seen:
                seen.add(game_id)
                game_ids.append(game_id)
                if len(game_ids) >= limit:
                    return game_ids
    return game_ids


def rank_strength_index(rank: str) -> int:
    match = re.fullmatch(r"(\d+)([kd])", rank)
    if not match:
        return -999
    value = int(match.group(1))
    unit = match.group(2)
    if unit == "k":
        return -value
    return value


def ogs_game_id_from_name(name: str, patterns: list[re.Pattern[str]]) -> int:
    for pattern in patterns:
        match = pattern.search(name)
        if match:
            return int_number(match.group(1))
    return 0


def ogs_game_player_ids(game: dict[str, object]) -> list[int]:
    ids: list[int] = []
    all_players = game.get("all_players")
    if isinstance(all_players, list):
        ids.extend(int_number(value) for value in all_players)
    for color in ("black", "white"):
        value = game.get(color)
        if isinstance(value, int):
            ids.append(value)
    players = game.get("players")
    if isinstance(players, dict):
        for color in ("black", "white"):
            player = players.get(color)
            if isinstance(player, dict):
                player_id = int_number(player.get("id"))
                if player_id > 0:
                    ids.append(player_id)
    return sorted(set(ids))


def ogs_player_source_name(source_player_id: int, player_ids: list[int], game_id: int) -> str:
    ids = sorted({pid for pid in player_ids if pid > 0} | {source_player_id})
    return f"ogs-player-{source_player_id}-players-{'_'.join(str(pid) for pid in ids)}-game-{game_id}.sgf"


def ogs_game_metadata_is_promising(game: dict[str, object], args: argparse.Namespace) -> bool:
    width = int_number(game.get("width"), -1)
    if width > 0 and width != int(args.board_size):
        return False
    height = int_number(game.get("height"), -1)
    if height > 0 and height != int(args.board_size):
        return False
    handicap = int_number(game.get("handicap"), 0)
    if handicap > 0:
        return False
    if bool(game.get("annulled")):
        return False
    ended = str(game.get("ended") or "")
    if args.ogs_min_date and ended and parse_date_key(ended) and parse_date_key(args.ogs_min_date):
        if parse_date_key(ended) < parse_date_key(args.ogs_min_date):
            return False
    return True


def ogs_ranking_bucket(value: object) -> str | None:
    try:
        ranking = float(value)
    except (TypeError, ValueError):
        return None
    if ranking < 30:
        kyu = round(30 - ranking)
        if 1 <= kyu <= 18:
            return f"{kyu}k"
        return None
    dan = round(ranking - 29)
    if 1 <= dan <= 9:
        return f"{dan}d"
    return None


def int_number(value: object, default: int = 0) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return default


def existing_rank_counts(out: Path, ranks: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for rank in ranks:
        rank_dir = out / rank
        counts[rank] = len(list(rank_dir.glob("*.sgf"))) if rank_dir.exists() else 0
    return counts


def existing_sgf_hashes(out: Path, ranks: list[str]) -> set[str]:
    hashes: set[str] = set()
    for rank in ranks:
        rank_dir = out / rank
        if not rank_dir.exists():
            continue
        for path in rank_dir.glob("*.sgf"):
            hashes.add(text_hash(path.read_text(encoding="utf-8", errors="replace")))
    return hashes


def ranked_sgf_hash(rank: str, text: str) -> str:
    return text_hash(rewrite_root_ranks(text, rank))


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def reached_acceptance_limit(args: argparse.Namespace, counts: dict[str, int]) -> bool:
    limit = int(getattr(args, "stop_after_accepted", 0) or 0)
    if limit <= 0:
        return False
    return int(getattr(args, "_accepted_new", 0) or 0) >= limit


def decode_sgf(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030", "shift_jis", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def root_properties(text: str) -> dict[str, str]:
    match = re.search(r"^\s*\(;([^;()]*)", text, flags=re.DOTALL)
    if not match:
        return {}
    props: dict[str, str] = {}
    for prop_match in re.finditer(r"([A-Z]+)\[((?:\\.|[^\]])*)\]", match.group(1)):
        props.setdefault(prop_match.group(1), unescape_sgf_value(prop_match.group(2)))
    return props


def unescape_sgf_value(value: str) -> str:
    return re.sub(r"\\(.)", r"\1", value)


def acceptable_game(text: str, props: dict[str, str], args: argparse.Namespace) -> bool:
    return game_rejection_reason(text, props, args, {}, {}) is None


def game_rejection_reason(
    text: str,
    props: dict[str, str],
    args: argparse.Namespace,
    needed: dict[str, int],
    counts: dict[str, int],
) -> str | None:
    if int_prop(props, "SZ", 19) != int(args.board_size):
        return "board_size"
    if int_prop(props, "HA", 0) > 0:
        return "handicap"
    moves = len(re.findall(r";[BW]\[", text))
    if moves < int(args.min_moves):
        return "short"
    if moves > int(args.max_moves):
        return "long"
    rank = game_bucket_rank(props)
    if rank is None:
        return "rank_mismatch"
    if needed and rank not in needed:
        return "rank_not_requested"
    if needed and counts.get(rank, 0) >= needed[rank]:
        return "rank_full"
    return None


def format_reject_counts(reject_counts: Counter[str]) -> str:
    if not reject_counts:
        return "none"
    return ",".join(f"{key}:{value}" for key, value in reject_counts.most_common(6))


def meets_min_date(member_name: str, props: dict[str, str], min_date: str) -> bool:
    minimum = parse_date_key(min_date)
    game_date = date_from_member_name(member_name) or parse_date_key(props.get("DT", ""))
    if minimum is None or game_date is None:
        return False
    return game_date >= minimum


def date_from_member_name(member_name: str) -> tuple[int, int, int] | None:
    match = re.search(r"(20\d{2})[/-](\d{1,2})[/-](\d{1,2})", member_name)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def parse_date_key(text: str) -> tuple[int, int, int] | None:
    match = re.search(r"(\d{4})(?:[-/.](\d{1,2}))?(?:[-/.](\d{1,2}))?", str(text or ""))
    if not match:
        return None
    year = int(match.group(1))
    month = int(match.group(2) or 1)
    day = int(match.group(3) or 1)
    if not (1 <= month <= 12 and 1 <= day <= 31):
        return None
    return year, month, day


def int_prop(props: dict[str, str], key: str, default: int) -> int:
    try:
        return int(float(props.get(key, default)))
    except (TypeError, ValueError):
        return default


def game_bucket_rank(props: dict[str, str]) -> str | None:
    black_name = props.get("PB", "")
    white_name = props.get("PW", "")
    if is_strong_ai_name(black_name) and is_strong_ai_name(white_name):
        return "12d"
    black_rank = normalize_rank(props.get("BR", ""), black_name)
    white_rank = normalize_rank(props.get("WR", ""), white_name)
    if black_rank and black_rank == white_rank:
        return black_rank
    return None


def normalize_rank(text: str, player_name: str = "") -> str | None:
    if is_strong_ai_name(player_name):
        return "12d"
    raw = str(text or "").strip()
    lowered = raw.lower()
    match = re.search(r"(\d+)", raw)
    if not match:
        if "pro" in lowered or "professional" in lowered:
            return "10d"
        return None
    number = int(match.group(1))
    if "k" in lowered or "級" in raw or "级" in raw:
        if 1 <= number <= 18:
            return f"{number}k"
        return None
    if "p" in lowered or "pro" in lowered or "professional" in lowered:
        if number >= 9:
            return "11d"
        if 1 <= number <= 8:
            return "10d"
        return "10d"
    if "d" in lowered or "段" in raw:
        if 1 <= number <= 9:
            return f"{number}d"
        if 10 <= number <= 12:
            return f"{number}d"
    return None


def is_strong_ai_name(name: str) -> bool:
    lowered = str(name or "").lower()
    return any(marker in lowered for marker in AI_NAME_MARKERS)


def write_ranked_sgf(out: Path, rank: str, index: int, source_name: str, text: str) -> None:
    rank_dir = out / rank
    rank_dir.mkdir(parents=True, exist_ok=True)
    target = rank_dir / f"{rank}-{index:03d}-{safe_name(Path(source_name).name)}"
    with target.open("w", encoding="utf-8", newline="") as handle:
        handle.write(rewrite_root_ranks(text, rank))


def rewrite_root_ranks(text: str, rank: str) -> str:
    root_match = re.search(r"^\s*\(;([^;()]*)", text, flags=re.DOTALL)
    if not root_match:
        return text
    root = root_match.group(1)
    root = set_sgf_property(root, "BR", rank)
    root = set_sgf_property(root, "WR", rank)
    return text[: root_match.start(1)] + root + text[root_match.end(1) :]


def set_sgf_property(root: str, prop: str, value: str) -> str:
    encoded = value.replace("\\", "\\\\").replace("]", "\\]")
    pattern = re.compile(rf"{prop}((?:\[(?:\\.|[^\]])*\])+)")
    replacement = f"{prop}[{encoded}]"
    if pattern.search(root):
        return pattern.sub(replacement, root, count=1)
    return root + replacement


def safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)


if __name__ == "__main__":
    raise SystemExit(main())
