#!/usr/bin/env python3
"""Emit GitHub Actions outputs for one scheduled UTC cron event."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ai_builder_brief.schedule import scheduled_attempt  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cron", required=True)
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--publication-enabled", action="store_true")
    args = parser.parse_args()
    decision = scheduled_attempt(
        args.cron,
        args.date,
        publication_enabled=args.publication_enabled,
    )
    print(f"run={'true' if decision.run else 'false'}")
    print(f"shadow={'true' if decision.shadow else 'false'}")
    print(f"episode_date={decision.episode_date.isoformat()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
