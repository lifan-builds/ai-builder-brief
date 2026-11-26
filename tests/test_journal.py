import subprocess
import sys
from pathlib import Path

import pytest

from ai_builder_brief.journal import _top_ten_body, publish_journal


def test_publish_journal_writes_entry_archive_and_parent_link(tmp_path: Path) -> None:
    history = tmp_path / "history"
    history.mkdir()
    brief = history / "2026-10-13.md"
    brief.write_text("# Brief — 2026-10-13\n\nRead on the day.\n", encoding="utf-8")
    site = tmp_path / "site"
    (site / "docs" / "journal").mkdir(parents=True)
    (site / "docs" / "journal" / "index.html").write_text("<main></main>", encoding="utf-8")

    assert publish_journal(brief, site) == "published"
    assert publish_journal(brief, site) == "already-published"
    assert (site / "docs" / "journal" / "ai-builder-brief" / "2026-10-13.html").is_file()
    assert (site / "docs" / "journal" / "ai-builder-brief" / "index.html").is_file()
    assert "AI-BUILDER-BRIEF:START" in (site / "docs" / "journal" / "index.html").read_text()


def test_publish_journal_runs_sibling_sync_clifan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    history = tmp_path / "history"
    history.mkdir()
    brief = history / "2026-10-13.md"
    brief.write_text("# Brief — 2026-10-13\n\nRead on the day.\n", encoding="utf-8")
    site = tmp_path / "lifan-builds"
    (site / "docs" / "journal").mkdir(parents=True)
    (site / "docs" / "journal" / "index.html").write_text("<main></main>", encoding="utf-8")
    sync_script = tmp_path / "scripts" / "sync_clifan.py"
    sync_script.parent.mkdir()
    sync_script.touch()
    calls: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, check: calls.append(cmd))

    publish_journal(brief, site)

    assert calls == [[sys.executable, str(sync_script), "--life-root", str(site)]]


def test_public_entry_keeps_only_compact_top_ten_cards() -> None:
    title, intro, body, count = _top_ten_body(
        """# Daily AI News Top 10 — 2026-10-13

Read on the day: **Systems are becoming the story.**

## Top 10

1. **A useful release** — **October 13, 2026**. The first sentence is the useful summary. The second sentence adds the builder consequence. **Confidence: high.** [Release](https://example.com/release) · [Docs](https://example.com/docs)

2. **A useful paper** — **October 12, 2026**. This paper gives a concrete workflow lesson for builders. **Confidence: medium.** [Paper](https://example.com/paper)

## Watchlist

- **Later item:** keep this private.

## Source panel activity

Internal collection notes stay private.

## What changed from yesterday

- Internal diff details stay private.
"""
    )
    assert title == "Daily AI News Top 10 — 2026-10-13"
    assert intro == "Systems are becoming the story."
    assert count == 2
    assert body.count('class="story-card"') == 2
    assert "Confidence:" not in body
    assert "Watchlist" not in body
    assert "Source panel activity" not in body
    assert "What changed from yesterday" not in body


def test_top_ten_body_parses_parenthetical_and_multiline_legacy_formats() -> None:
    _, _, body_paren, count_paren = _top_ten_body(
        """# Daily AI News Top 10 — 2026-09-20

1. **GitHub copilot CLI release** (September 20, 2026). Terminal agent workflows tighten around repo context. https://github.com/github/copilot-cli

2. **OpenAI eval harness** (September 19, 2026). Structured traces make regressions easier to catch. [Repo](https://github.com/openai/evals)
"""
    )
    assert count_paren == 2
    assert body_paren.count('class="story-card"') == 2
    assert "github/copilot-cli" in body_paren

    _, _, body_multi, count_multi = _top_ten_body(
        """# AI Builder Brief — 2026-09-01

1. Ray Serve latency routing
   Original disclosure: Sep 1, 2026. Prefix-aware routing reduces p99 tail latency across multi-node clusters. https://docs.ray.io/en/latest/serve/index.html
"""
    )
    assert count_multi == 1
    assert "Ray Serve latency routing" in body_multi
    assert "docs.ray.io" in body_multi


def test_top_ten_body_renders_scannable_structured_cards_and_inline_code() -> None:
    title, intro, body, count = _top_ten_body(
        """# AI Builder Brief — November 26, 2026

Coverage window: November 23, 18:00 UTC–November 26, 18:00 UTC, 2026.

Read on the day: Verification discipline defines today's `agent` cycle.

## 30-Second Scan

| # | Lane | Story | Key Signal |
| :-: | --- | --- | --- |
| 1 | `Infra` | Anthropic Rust rewrite | `~70%` less CPU |

## Top 10

1. **Anthropic migrates its Messages API from Python to Rust**
   `Infra & Agents` · Nov 25 · `@ClaudeDevs` `5.7K` likes · [Engineering post](https://www.anthropic.com/research/migrating-api-to-rust)
   - **What happened:** Claude drafted the Rust request path and shadow diffs cut CPU `~70%` and median TTFT `~20%`.
   - **Builder move:** Build production shadow comparators before agent rewrites.
   - **Caveat:** CPU tracks one region only.

## Watchlist

- **Later item:** stays private.
"""
    )
    assert title == "AI Builder Brief — November 26, 2026"
    assert intro == "Verification discipline defines today's agent cycle."
    assert "`" not in intro
    assert count == 1
    assert "TOP 10 · Infra &amp; Agents · Nov 25 · @ClaudeDevs 5.7K likes" in body
    assert "<code>~70%</code>" in body
    assert "`~70%`" not in body
    assert 'class="story-move"' in body
    assert "Build production shadow comparators before agent rewrites." in body
    assert 'class="story-caveat"' in body
    assert "CPU tracks one region only." in body
    assert "Watchlist" not in body

