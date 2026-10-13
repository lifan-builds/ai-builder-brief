from pathlib import Path

from ai_builder_brief.journal import markdown_body, publish_journal


def test_markdown_body_keeps_ranked_items_in_one_ordered_list() -> None:
    title, body = markdown_body(
        "# Brief — 2026-10-13\n\n## Top 10\n\n1. **First**\n\n2. **Second**\n\n## Watchlist\n\n- **Later**"
    )
    assert title == "Brief — 2026-10-13"
    assert body.count("<ol>") == 1
    assert body.count("<li>") == 3
    assert body.count("<ul>") == 1


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
