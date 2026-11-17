#!/usr/bin/env python3
"""Fail-closed validation for the daily brief source catalog."""

from __future__ import annotations

import argparse
from pathlib import Path

from ai_builder_brief.brief_sources import validate_path

DEFAULT_MANIFEST = Path(__file__).resolve().parents[1] / "brief_sources.yaml"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()
    errors = validate_path(args.path)
    if errors:
        for error in errors:
            print(f"ERROR: {error}")
        return 1
    print(f"valid daily brief source manifest: {args.path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
