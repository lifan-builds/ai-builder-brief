"""CLI for the AI Builder Brief production show and benchmark suite."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from castforge.config import load_config
from castforge.validation import validate_project
from dotenv import load_dotenv

from ai_builder_brief.benchmark import (
    evaluate_brief_markdown,
    evaluate_history_corpus,
    fetch_algolia_hn_ground_truth,
    fetch_youtube_builder_ground_truth,
    load_hn_ground_truth,
    render_benchmark_markdown,
)
from ai_builder_brief.factcheck import (
    DEFAULT_FACTCHECK_MODEL,
    factcheck_brief,
    render_factcheck_markdown,
    should_fail_gate,
)
from ai_builder_brief.journal import publish_journal
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
    run.add_argument("--review-only", action="store_true")
    run.add_argument("--dry-run", action="store_true", help="Run deterministic review pipeline without LLM calls.")

    journal = subparsers.add_parser("journal")
    journal.add_argument("--date", dest="brief_date", type=date.fromisoformat, default=None)
    journal.add_argument("--site-root", type=Path, required=True)
    journal.add_argument("--backfill", action="store_true")

    validate = subparsers.add_parser("validate")
    validate.add_argument("--config", type=Path, default=Path("podcast.yaml"))
    validate.add_argument("--date", dest="episode_date", default=None)
    validate.add_argument("--check-public", action="store_true")

    bench = subparsers.add_parser("benchmark")
    bench.add_argument("--history-dir", type=Path, default=Path("history"))
    bench.add_argument("--ground-truth", type=Path, default=Path("fixtures/benchmark_hn_ground_truth.json"))
    bench.add_argument("--sources", type=Path, default=Path("sources.yaml"))
    bench.add_argument("--brief", type=Path, default=None, help="Evaluate a single brief Markdown file.")
    bench.add_argument("--start-date", type=date.fromisoformat, default=date(2026, 10, 29))
    bench.add_argument("--end-date", type=date.fromisoformat, default=None)
    bench.add_argument("--live-hn", action="store_true", help="Fetch fresh Algolia HN ground truth instead of fixture.")
    bench.add_argument("--live-youtube", action="store_true", help="Include high-velocity YouTube builder talks from sources.yaml.")
    bench.add_argument("--json", action="store_true", dest="as_json")
    bench.add_argument("-o", "--output", type=Path, default=None)

    factcheck = subparsers.add_parser("factcheck")
    factcheck.add_argument("--brief", type=Path, default=None, help="Path to a brief Markdown file or review JSON artifact.")
    factcheck.add_argument("--date", dest="brief_date", type=date.fromisoformat, default=None, help="Resolve history/YYYY-MM-DD.md.")
    factcheck.add_argument("--include-watchlist", action="store_true", help="Also verify Watchlist entries.")
    factcheck.add_argument("--model", default=None, help=f"Responses API model (default: {DEFAULT_FACTCHECK_MODEL}).")
    factcheck.add_argument("--max-workers", type=int, default=10, help="Concurrent verification workers.")
    factcheck.add_argument(
        "--fail-on",
        choices=("unsupported", "nuance_drift", "none"),
        default="unsupported",
        help="Exit non-zero when claims match or exceed this severity (default: unsupported).",
    )
    factcheck.add_argument("--json", action="store_true", dest="as_json")
    factcheck.add_argument("-o", "--output", type=Path, default=None)
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
                target = root / f"{args.brief_date.isoformat()}.md"
                if not target.is_file():
                    raise FileNotFoundError(f"Journal source brief not found: {target}")
                paths = [target]
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
                dry_run=args.dry_run,
            )
            print(status)
            return 0
        if args.command == "benchmark":
            end_dt = (
                datetime(args.end_date.year, args.end_date.month, args.end_date.day, 23, 59, tzinfo=UTC)
                if args.end_date
                else datetime.now(UTC)
            )
            start_dt = datetime(args.start_date.year, args.start_date.month, args.start_date.day, 0, 0, tzinfo=UTC) - timedelta(days=3)
            if args.live_hn:
                gt = fetch_algolia_hn_ground_truth(start=start_dt, end=end_dt)
            else:
                gt = load_hn_ground_truth(args.ground_truth)
            if args.live_youtube and args.sources.is_file():
                gt = [*gt, *fetch_youtube_builder_ground_truth(args.sources, start=start_dt, end=end_dt)]
            if args.brief is not None:
                prior = sorted(
                    p for p in args.brief.parent.glob("*.md")
                    if p.name != args.brief.name and "-x-" not in p.name and p.stem[:10].count("-") == 2 and p.name < args.brief.name
                )[-14:]
                single = evaluate_brief_markdown(args.brief, gt, prior_briefs=prior).to_dict()
                rendered = json.dumps(single, ensure_ascii=False, indent=2) + "\n"
            else:
                report = evaluate_history_corpus(
                    args.history_dir,
                    gt,
                    start_date=args.start_date,
                    end_date=args.end_date,
                )
                rendered = (
                    json.dumps(report, ensure_ascii=False, indent=2) + "\n"
                    if args.as_json
                    else render_benchmark_markdown(report)
                )
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(rendered, encoding="utf-8")
            else:
                print(rendered, end="")
            return 0
        if args.command == "factcheck":
            if args.brief is not None:
                target = args.brief
            elif args.brief_date is not None:
                target = Path("history") / f"{args.brief_date.isoformat()}.md"
            else:
                raise ValueError("factcheck requires --brief or --date")
            if not target.is_file():
                raise FileNotFoundError(f"Fact-check target not found: {target}")
            report = factcheck_brief(
                target,
                include_watchlist=args.include_watchlist,
                model=args.model,
                max_workers=max(1, args.max_workers),
            )
            rendered = (
                json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n"
                if args.as_json
                else render_factcheck_markdown(report)
            )
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(rendered, encoding="utf-8")
            else:
                print(rendered, end="")
            return 1 if should_fail_gate(report, fail_on=args.fail_on) else 0
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
