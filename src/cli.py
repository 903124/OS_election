#!/usr/bin/env python3
"""
cli.py — Command-line entry point for the Wikipedia elections pipeline.

Subcommands:
    senate     Parse U.S. Senate cycles (polling + results) for a year range.
    house      Parse U.S. House election results for a year range.
    state-leg  Parse state legislature results (state senates + state houses).
    statewide  Parse statewide executive results (gov, AG, SoS, treasurer).
    presidential  Parse county-level presidential results per state.
    lean       Predicted partisan lean per congressional district from the
               district->county mapping (resources/district_counties.json)
               + county presidential votes (supports 2004-2024).
    crosswalk  Rebuild resources/district_counties.json from boundary
               geometry (UCLA cdmaps x 2010 Census counties; ~185 MB of
               cached, resumable downloads; needs shapely + pyproj).
    all        Run all six pipelines.
    polling-check  Quality-check polling CSVs (file size + data sanity) and
               report which files need improvement; exit code 1 when any
               file is flagged.
    fetch      Fetch one article's raw wikitext (debugging helper).

Year ranges are inclusive and cover even (federal election) years only:
odd bounds are clamped inward, e.g. 2019–2023 processes 2020 and 2022.

Rate-limit options apply to every subcommand:
    --delay        minimum seconds between Wikipedia API requests (default 1.0)
    --batch-size   titles per API request, max 50 (default 50)

Examples:
    python cli.py all
    python cli.py senate --start-year 2018 --end-year 2024
    python cli.py house --start-year 2012 --end-year 2024 --delay 0.5
    python cli.py state-leg --start-year 2018 --end-year 2024
    python cli.py statewide --start-year 2018 --end-year 2024
    python cli.py presidential --start-year 2020 --end-year 2024
    python cli.py lean                     # 2004-2024 from local presidential CSVs
    python cli.py lean --fetch-missing     # fetch missing presidential years first
    python cli.py crosswalk                # regenerate the district->county mapping
    python cli.py polling-check            # QC polling CSVs (size + data sanity)
    python cli.py fetch "2018 United States Senate election in Arizona"
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import List, Optional

import district_lean
import house_elections
import presidential_elections
import senate_elections
import state_legislatures
import statewide_elections
import wiki_utils

try:
    import polling_qc

    _QC_MIN_BYTES = polling_qc.DEFAULT_MIN_BYTES
    _QC_PEER_RATIO = polling_qc.DEFAULT_PEER_RATIO
except ModuleNotFoundError:
    # polling_qc is optional: only the `polling-check` subcommand needs it.
    # The data pipelines (senate/house/...) must keep working in checkouts
    # (e.g. GitHub Actions) where the module is not present.
    polling_qc = None
    _QC_MIN_BYTES = 1500
    _QC_PEER_RATIO = 0.25

logger = logging.getLogger("cli")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--output", "-o", default="data",
        help="Base output directory; CSVs land in <dir>/senate/, <dir>/house/, "
             "<dir>/state_senate/, <dir>/state_house/, <dir>/statewide/, "
             "<dir>/presidential/ and <dir>/district_lean/ (default: data)",
    )
    common.add_argument(
        "--start-year", type=int, default=2018,
        help="First election year to process (inclusive, default: 2018)",
    )
    common.add_argument(
        "--end-year", type=int, default=2024,
        help="Last election year to process (inclusive, default: 2024)",
    )
    common.add_argument("--api-url", default=None, help="MediaWiki API endpoint")
    common.add_argument("--user-agent", default=None, help="Descriptive User-Agent header")
    common.add_argument(
        "--delay", type=float, default=None,
        help="Minimum seconds between API requests (default: 1.0)",
    )
    common.add_argument(
        "--batch-size", type=int, default=None,
        help="Titles per API request, max 50 (default: 50)",
    )
    common.add_argument("--max-retries", type=int, default=None, help="Retry attempts")
    common.add_argument("--timeout", type=int, default=None, help="Per-request timeout (s)")
    common.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO)",
    )

    parser = argparse.ArgumentParser(
        prog="wiki-elections",
        description="Parse U.S. election data from Wikipedia via the MediaWiki API.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("senate", parents=[common], help="Parse Senate cycles (year range)")

    house = sub.add_parser("house", parents=[common], help="Parse House results (year range)")

    sub.add_parser(
        "state-leg", parents=[common],
        help="Parse state legislature results (year range)",
    )

    sub.add_parser(
        "statewide", parents=[common],
        help="Parse statewide executive results (year range)",
    )

    sub.add_parser(
        "presidential", parents=[common],
        help="Parse county-level presidential results (year range)",
    )

    lean = sub.add_parser(
        "lean", parents=[common],
        help="Predicted partisan lean per congressional district "
             "(district->county mapping + county presidential votes)",
    )
    lean.set_defaults(start_year=2004)   # full three-vintage range
    lean.add_argument(
        "--mapping", default=None,
        help="Path to the district->county mapping JSON "
             "(default: resources/district_counties.json; regenerate with "
             "`cli.py crosswalk`)",
    )
    lean.add_argument(
        "--presidential-dir", default=None,
        help="Directory holding presidential_results_{year}.csv "
             "(default: <output>/presidential)",
    )
    lean.add_argument(
        "--fetch-missing", action="store_true",
        help="Fetch missing presidential years via the API before computing",
    )

    crosswalk = sub.add_parser(
        "crosswalk",
        help="Rebuild resources/district_counties.json from boundary geometry "
             "(UCLA cdmaps districts x 2010 Census counties; downloads ~185 MB, "
             "cached and resumable)",
    )
    crosswalk.add_argument("--cache-dir", default=None,
                           help="download/checkpoint cache (default: <repo>/.geo_cache)")
    crosswalk.add_argument("--output", default=None,
                           help="mapping JSON path (default: resources/district_counties.json)")
    crosswalk.add_argument("--force", action="store_true",
                           help="ignore caches and redo everything")
    crosswalk.add_argument("--workers", type=int, default=None,
                           help="parallel downloads (default: 6)")
    crosswalk.add_argument(
        "--log-level", default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO)",
    )

    sub.add_parser("all", parents=[common], help="Run all six pipelines")

    qc = sub.add_parser(
        "polling-check", parents=[common],
        help="Quality-check polling CSVs (file size + data sanity) — "
             "reports which files need improvement",
    )
    qc.add_argument(
        "--pipeline", default="senate",
        help="Which pipeline's polling CSVs to check (default: senate)",
    )
    qc.add_argument(
        "--data-dir", default=None,
        help="Base data directory (default: --output value, i.e. data)",
    )
    qc.add_argument(
        "--min-size", type=int, default=_QC_MIN_BYTES,
        help=f"Flag per-year polling files smaller than this many bytes "
             f"(default: {_QC_MIN_BYTES})",
    )
    qc.add_argument(
        "--peer-ratio", type=float, default=_QC_PEER_RATIO,
        help="Flag files below this fraction of the kind-median size "
             f"(default: {_QC_PEER_RATIO})",
    )
    qc.add_argument(
        "--no-report", action="store_true",
        help="Do not write polling_qc_report.json",
    )

    fetch = sub.add_parser("fetch", parents=[common], help="Fetch one article (debug)")
    fetch.add_argument("title", help="Exact Wikipedia article title")

    return parser


def make_client(args: argparse.Namespace) -> wiki_utils.WikiAPIClient:
    """Build a WikiAPIClient from CLI options, falling back to env defaults."""
    overrides = {
        k: getattr(args, k)
        for k in ("api_url", "user_agent", "delay", "batch_size", "max_retries", "timeout")
        if getattr(args, k, None) is not None
    }
    return wiki_utils.WikiAPIClient(**overrides)


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    # crosswalk defines its own (smaller) option set, so --log-level may be
    # absent; fall back defensively instead of crashing on args.log_level.
    log_level = getattr(logging, getattr(args, "log_level", "INFO"), logging.INFO)
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    client = make_client(args)
    wiki_utils.set_default_client(client)

    if args.command == "polling-check":
        if polling_qc is None:
            logger.error(
                "polling-check needs the polling_qc module (src/polling_qc.py), "
                "which is not present in this checkout. The data pipelines "
                "(senate/house/state-leg/statewide/presidential/lean) are "
                "unaffected."
            )
            return 1
        rep = polling_qc.check_pipeline(
            args.data_dir or args.output,
            pipeline=args.pipeline,
            min_bytes=args.min_size,
            peer_ratio=args.peer_ratio,
            report=not args.no_report,
        )
        return 1 if rep["needs_improvement"] else 0

    if args.command == "crosswalk":
        # geometry build: no wiki client involved; heavy downloads, all cached
        import district_counties
        district_counties.run(
            cache_dir=args.cache_dir,
            output_path=args.output,
            force=args.force,
            workers=args.workers or district_counties.DOWNLOAD_WORKERS,
        )
        return 0

    ua = client._session.headers["User-Agent"]
    logger.info("API endpoint : %s", client.api_url)
    logger.info("User-Agent   : %s", ua)
    logger.info(
        "Rate limiting: >= %.1fs between requests, batches of %d titles",
        client._limiter.min_interval, client.batch_size,
    )
    logger.info("Year range  : %d-%d (even years)", args.start_year, args.end_year)

    if args.command == "fetch":
        text = client.fetch_single(args.title)
        if text is None:
            logger.error("Article not found: %s", args.title)
            return 1
        print(text)
        return 0

    if args.command in ("senate", "all"):
        senate_elections.run(
            start_year=args.start_year,
            end_year=args.end_year,
            output_dir=args.output,
            client=client,
        )

    if args.command in ("house", "all"):
        house_elections.run(
            start_year=args.start_year,
            end_year=args.end_year,
            out_dir=args.output,
            client=client,
        )

    if args.command in ("state-leg", "all"):
        state_legislatures.run(
            start_year=args.start_year,
            end_year=args.end_year,
            output_dir=args.output,
            client=client,
        )

    if args.command in ("statewide", "all"):
        statewide_elections.run(
            start_year=args.start_year,
            end_year=args.end_year,
            output_dir=args.output,
            client=client,
        )

    if args.command in ("presidential", "all"):
        presidential_elections.run(
            start_year=args.start_year,
            end_year=args.end_year,
            output_dir=args.output,
            client=client,
        )

    if args.command in ("lean", "all"):
        # the crosswalk only covers the 2000s/2010s/2020s maps, so lean is
        # always computed across the full 2004-2024 range regardless of the
        # year inputs (which stay authoritative for the other pipelines)
        lean_start = args.start_year if args.command == "lean" else min(args.start_year, 2004)
        lean_end = args.end_year if args.command == "lean" else min(args.end_year, 2024)
        district_lean.run(
            start_year=lean_start,
            end_year=lean_end,
            output_dir=args.output,
            client=client,
            mapping_path=getattr(args, "mapping", None),
            presidential_dir=getattr(args, "presidential_dir", None),
            fetch_missing=getattr(args, "fetch_missing", False),
        )

    logger.info(
        "All requested pipelines finished. Output: %s/{senate,house,state_senate,"
        "state_house,statewide,presidential,district_lean}/", args.output,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
