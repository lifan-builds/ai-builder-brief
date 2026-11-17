"""Render a concise, action-oriented X thread from a review artifact."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def render(path: Path) -> str:
    data = json.loads(path.read_text(encoding="utf-8"))
    candidates = [item for item in data.get("candidates", []) if isinstance(item, dict)]
    watchlist = [item for item in data.get("watchlist", []) if isinstance(item, dict)]
    # The public thread is a Top 10 view. Podcast readiness is a separate
    # production gate and must not hide useful review items from the thread.
    items = (candidates + watchlist)[:10]
    watch_start = len(candidates) + 1 if watchlist and len(candidates) < 10 else None
    lines = [
        f"AI Builder Brief — {data.get('review_date', '')}",
        "",
        "The AI developments most useful for builders today:",
    ]
    for index, item in enumerate(items, 1):
        if watch_start is not None and index == watch_start:
            lines.extend(["", "Also useful to watch:"])
        editorial = item.get("editorial", {})
        action = (editorial.get("builder_actions") or ["monitor"])[0]
        sources = item.get("sources") or []
        source_url = str(sources[0].get("url") or "").strip() if sources and isinstance(sources[0], dict) else ""
        lines.extend([
            "",
            f"{index}/ {item.get('title', '')}",
            item.get("summary", "")[:240],
            f"Builder move: {action}.",
        ])
        if source_url:
            lines.append(f"Source: {source_url}")
    lines.extend(["", "Full briefing and source manifest: [link]"])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("review", type=Path)
    parser.add_argument("-o", "--output", type=Path)
    args = parser.parse_args()
    text = render(args.review)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
