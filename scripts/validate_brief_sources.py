#!/usr/bin/env python3
"""Fail-closed schema and optional live-health validation for brief_sources.yaml."""

from __future__ import annotations

import argparse
from pathlib import Path

from ai_builder_brief.brief_sources import validate_path

DEFAULT_MANIFEST = Path(__file__).resolve().parents[1] / "brief_sources.yaml"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", nargs="?", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--check-live",
        action="store_true",
        help="Verify HTTP 200 reachability and non-empty payloads across catalog sources.",
    )
    args = parser.parse_args(argv)
    errors = validate_path(args.path, check_live=args.check_live)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    mode = "schema + live HTTP" if args.check_live else "schema"
    print(f"valid daily brief source manifest ({mode}): {args.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
