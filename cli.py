#!/usr/bin/env python3
"""
cli.py — Command-line entry point for the Wikipedia elections pipeline.

Subcommands:
    senate   Parse U.S. Senate cycles (polling + results) for a year range.
    house    Parse U.S. House election results for a year range.
    all      Run both pipelines.
    fetch    Fetch one article's raw wikitext (debugging helper).

Year ranges are inclusive and cover even (federal election) years only:
odd bounds are clamped inward, e.g. 2019–2023 processes 2020 and 2022.

Rate-limit options apply to every subcommand:
    --delay        minimum seconds between Wikipedia API requests (default 1.0)
    --batch-size   titles per API request, max 50 (default 50)

Examples:
    python cli.py all
    python cli.py senate --start-year 2018 --end-year 2024
    python cli.py house --start-year 2012 --end-year 2024 --delay 0.5
    python cli.py fetch "2018 United States Senate election in Arizona"
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import List, Optional

import house_elections
import senate_elections
import wiki_utils

logger = logging.getLogger("cli")


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--output", "-o", default="data",
        help="Output directory for CSV/JSON files (default: data)",
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

    sub.add_parser("all", parents=[common], help="Run both pipelines")

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
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    client = make_client(args)
    wiki_utils.set_default_client(client)

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

    logger.info("All requested pipelines finished. Output: %s/", args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
