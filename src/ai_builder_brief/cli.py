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
from ai_builder_brief.journal import publish_journal


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-builder-brief")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--config", type=Path, default=Path("podcast.yaml"))
    run.add_argument("--sources", type=Path, default=Path("sources.yaml"))
    run.add_argument("--date", dest="episode_date", type=date.fromisoformat, default=date.today())
    run.add_argument("--fixture", action="store_true")
    run.add_argument("--shadow", action="store_true")
    run.add_argument("--review-only", action="store_true")
    journal = subparsers.add_parser("journal")
    journal.add_argument("--date", dest="brief_date", type=date.fromisoformat, default=None)
    journal.add_argument("--site-root", type=Path, required=True)
    journal.add_argument("--backfill", action="store_true")
    validate = subparsers.add_parser("validate")
    validate.add_argument("--config", type=Path, default=Path("podcast.yaml"))
    validate.add_argument("--date", dest="episode_date", default=None)
    validate.add_argument("--check-public", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv(override=False)
    args = build_parser().parse_args(argv)
    try:
        if args.command == "journal":
            root = Path("history")
            if args.backfill:
                paths = sorted(root.glob("*.md"))
            elif args.brief_date is not None:
                paths = [root / f"{args.brief_date.isoformat()}.md"]
            else:
                raise ValueError("journal requires --date or --backfill")
            results = []
            for path in paths:
                if "-x-" in path.name or not path.is_file() or not path.stem[:10].count("-") == 2:
                    continue
                results.append(f"{path.name}:{publish_journal(path, args.site_root)}")
            print("\n".join(results) or "no-journal-entry")
            return 0
        if args.command == "run":
            status = run_daily(
                config_path=args.config,
                sources_path=args.sources,
                episode_date=args.episode_date,
                fixture=args.fixture,
                shadow=args.shadow,
                review_only=args.review_only,
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
