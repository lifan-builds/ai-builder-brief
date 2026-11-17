from __future__ import annotations

import json
import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).parents[1] / "scripts" / "render_x_thread.py"
_SPEC = importlib.util.spec_from_file_location("render_x_thread", _SCRIPT)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
render = _MODULE.render


def _record(number: int, *, tier: str = "shortlist", action: str | None = "build") -> dict:
    return {
        "rank": number,
        "tier": tier,
        "title": f"Story {number}",
        "summary": f"Useful summary for story {number}.",
        "podcast_ready": number == 1,
        "editorial": {"builder_actions": [action] if action else []},
        "sources": [{"url": f"https://example.com/{number}"}],
    }


def test_renderer_includes_watchlist_to_make_top_ten(tmp_path) -> None:
    payload = {
        "review_date": "2026-09-12",
        "candidates": [_record(i) for i in range(1, 4)],
        "watchlist": [_record(i, tier="watchlist") for i in range(4, 11)],
    }
    path = tmp_path / "review.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    output = render(path)

    assert [line.split("/", 1)[0] for line in output.splitlines() if "/ Story " in line] == [str(i) for i in range(1, 11)]
    assert "Also useful to watch:" in output
    assert "Source: https://example.com/10" in output


def test_renderer_does_not_filter_on_podcast_readiness(tmp_path) -> None:
    payload = {
        "review_date": "2026-09-12",
        "candidates": [_record(1), _record(2, action=None)],
        "watchlist": [],
    }
    path = tmp_path / "review.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    output = render(path)

    assert "2/ Story 2" in output
    assert "Builder move: monitor." in output


def test_renderer_caps_at_ten_records(tmp_path) -> None:
    payload = {
        "review_date": "2026-09-12",
        "candidates": [_record(i) for i in range(1, 11)],
        "watchlist": [_record(11, tier="watchlist")],
    }
    path = tmp_path / "review.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    output = render(path)

    assert "10/ Story 10" in output
    assert "11/ Story 11" not in output
