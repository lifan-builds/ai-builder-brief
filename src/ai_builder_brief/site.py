"""Static public site renderer for AI Builder Brief."""

from __future__ import annotations

from pathlib import Path


SITE_HTML = (Path(__file__).resolve().parents[2] / "docs" / "index.html").read_text(encoding="utf-8")


def render_site(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(SITE_HTML, encoding="utf-8")
    return path
