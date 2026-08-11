"""CLI for the AI Builder Brief production show."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from castforge.config import load_config
from castforge.validation import validate_project
from dotenv import load_dotenv

from ai_builder_brief.pipeline import run_daily


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-builder-brief")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--config", type=Path, default=Path("podcast.yaml"))
    run.add_argument("--sources", type=Path, default=Path("sources.yaml"))
    run.add_argument("--date", dest="episode_date", type=date.fromisoformat, default=date.today())
    run.add_argument("--fixture", action="store_true")
    run.add_argument("--shadow", action="store_true")
    validate = subparsers.add_parser("validate")
    validate.add_argument("--config", type=Path, default=Path("podcast.yaml"))
    validate.add_argument("--date", dest="episode_date", default=None)
    validate.add_argument("--check-public", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv(override=False)
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            status = run_daily(
                config_path=args.config,
                sources_path=args.sources,
                episode_date=args.episode_date,
                fixture=args.fixture,
                shadow=args.shadow,
            )
            print(status)
            return 0
        config = load_config(args.config)
        errors = validate_project(config, episode_date=args.episode_date, check_public=args.check_public)
        if errors:
            for error in errors:
                print(f"ERROR: {error}", file=sys.stderr)
            return 1
        print("AI Builder Brief validation passed")
        return 0
    except Exception as error:
        logging.getLogger("ai_builder_brief").error("%s", error)
        return 1
