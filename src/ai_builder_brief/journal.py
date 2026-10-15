"""Publish saved daily AI briefs into the clifan.com journal."""
from __future__ import annotations

import html
import re
from datetime import date
from pathlib import Path

_START = "<!-- AI-BUILDER-BRIEF:START -->"
_END = "<!-- AI-BUILDER-BRIEF:END -->"


def _inline(value: str) -> str:
    text = html.escape(value, quote=True)
    links: list[str] = []

    def hold(match: re.Match[str]) -> str:
        links.append(f'<a href="{match.group(2)}" rel="noopener">{match.group(1)}</a>')
        return f"@@LINK{len(links) - 1}@@"

    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", hold, text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    for index, link in enumerate(links):
        text = text.replace(f"@@LINK{index}@@", link)
    return text


def markdown_body(markdown: str) -> tuple[str, str]:
    lines = markdown.splitlines()
    title = next((line[2:].strip() for line in lines if line.startswith("# ")), "AI Builder Brief")
    blocks: list[str] = []
    paragraph: list[str] = []
    list_items: list[str] = []
    list_kind: str | None = None

    def flush_paragraph() -> None:
        nonlocal paragraph
        if paragraph:
            blocks.append(f"<p>{_inline(' '.join(paragraph))}</p>")
            paragraph = []

    def flush_list() -> None:
        nonlocal list_items, list_kind
        if list_items:
            tag = "ol" if list_kind == "ol" else "ul"
            blocks.append(f"<{tag}>" + "".join(f"<li>{item}</li>" for item in list_items) + f"</{tag}>")
            list_items, list_kind = [], None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
        elif stripped.startswith("# "):
            flush_paragraph()
            flush_list()
        elif stripped.startswith("## "):
            flush_paragraph()
            flush_list()
            blocks.append(f"<h2>{_inline(stripped[3:].strip())}</h2>")
        elif re.match(r"^\d+\.\s+", stripped):
            flush_paragraph()
            if list_kind != "ol":
                flush_list()
                list_kind = "ol"
            list_items.append(_inline(re.sub(r"^\d+\.\s+", "", stripped)))
        elif stripped.startswith("- "):
            flush_paragraph()
            if list_kind != "ul":
                flush_list()
                list_kind = "ul"
            list_items.append(_inline(stripped[2:].strip()))
        elif list_kind:
            list_items[-1] = f"{list_items[-1]} {_inline(stripped)}"
        else:
            paragraph.append(stripped)
    flush_paragraph()
    flush_list()
    return title, "\n".join(blocks)


def _compact_summary(value: str, limit: int = 76) -> str:
    value = re.sub(r"\s+\*\*Confidence:.*$", "", value, flags=re.S)
    value = re.sub(r"\s*\[[^\]]+\]\(https?://[^)\s]+\)", "", value)
    value = re.sub(r"\*\*([^*]+)\*\*", r"\1", value)
    value = re.sub(r"\s+·\s*$", "", value).strip()
    sentences = re.split(r"(?<=[.!?])\s+", value)
    summary = " ".join(sentences[:2]).strip() or value
    words = summary.split()
    if len(words) > limit:
        summary = " ".join(words[:limit]).rstrip(" ,;:") + "…"
    return summary


def _top_ten_body(markdown: str) -> tuple[str, str, str, int]:
    """Build the public, compact view from the private brief's Top 10 section."""
    lines = markdown.splitlines()
    title = next((line[2:].strip() for line in lines if line.startswith("# ")), "AI Builder Brief")
    intro = ""
    for line in lines:
        if line.startswith("Read on the day:"):
            intro = _compact_summary(line.split(":", 1)[1].strip(), 42)
            break

    top_lines: list[str] = []
    in_top = False
    for line in lines:
        stripped = line.strip()
        if stripped == "## Top 10":
            in_top = True
            continue
        if in_top and stripped.startswith("## "):
            break
        if in_top:
            top_lines.append(stripped)

    items: list[tuple[str, str, str, list[tuple[str, str]]]] = []
    current: list[str] = []

    def flush_item() -> None:
        if not current:
            return
        current_text = " ".join(part for part in current if part)
        match = re.match(r"^\*\*(.+?)\*\*\s+—\s+\*\*(.+?)\*\*\.\s*(.*)$", current_text)
        if match:
            story_title, story_date, story_body = match.groups()
        else:
            story_title, story_date, story_body = current_text, "", ""
        links = re.findall(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", current_text)
        items.append((story_title, story_date, _compact_summary(story_body), links[:3]))

    for line in top_lines:
        if re.match(r"^\d+\.\s+", line):
            flush_item()
            current = []
            current.append(re.sub(r"^\d+\.\s+", "", line))
        elif current and line:
            current.append(line)
    flush_item()

    cards: list[str] = []
    for rank, (story_title, story_date, summary, links) in enumerate(items[:10], start=1):
        source_html = "".join(
            f'<a class="source-chip" href="{html.escape(url, quote=True)}" rel="noopener">{html.escape(label)} ↗</a>'
            for label, url in links
        )
        cards.append(
            f'<article class="story-card"><div class="story-rank">{rank:02d}</div>'
            f'<div class="story-content"><p class="story-meta">TOP 10 · {html.escape(story_date)}</p>'
            f'<h2>{html.escape(story_title)}</h2><p class="story-summary">{html.escape(summary)}</p>'
            f'<div class="story-links">{source_html}</div></div></article>'
        )
    body = "<section class=\"story-grid\" aria-label=\"Top 10 stories\">" + "".join(cards) + "</section>"
    return title, intro, body, len(items[:10])


def _entry_html(day: date, title: str, intro: str, body: str, story_count: int) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="{html.escape(title)}"><title>{html.escape(title)} — Lifan Builds Journal</title>
<link rel="stylesheet" href="../../styles.css"></head>
<body><div class="scanlines" aria-hidden="true"></div>
<header class="site-header wrap"><a class="wordmark" href="../../" aria-label="Back to Lifan Builds home"><span class="pixel-mark">LB</span><span>LIFAN BUILDS</span></a><nav aria-label="Journal navigation"><a href="../">Journal</a><a href="../../#quests">Quests</a><a class="nav-button" href="https://clifan.substack.com/">Substack ↗</a></nav></header>
<main><article class="brief-entry wrap"><div class="brief-hero"><p class="eyebrow">AI BUILDER BRIEF / {day.strftime("%Y.%m.%d")}</p><h1>{html.escape(title)}</h1><p class="brief-entry-meta">{story_count} source-linked stories · <time datetime="{day.isoformat()}">{day.strftime("%B %-d, %Y")}</time></p><p class="brief-lede">{html.escape(intro)}</p></div>{body}<p class="journal-links"><a href="../">← All AI Builder Brief entries</a> <a href="https://github.com/lifan-builds/ai-builder-brief">Source repository ↗</a></p></article></main>
<footer class="site-footer wrap"><span><a href="../">← Journal</a></span><span><a href="https://github.com/lifan-builds">GitHub</a> · <a href="https://x.com/LifanBuilds">X</a></span></footer></body></html>"""


def _archive_html(entries: list[tuple[date, str]]) -> str:
    cards = "\n".join(
        f'<a class="brief-archive-card" href="{day.isoformat()}.html"><span class="journal-meta">AI BUILDER BRIEF <time datetime="{day.isoformat()}">{day.strftime("%b %-d, %Y")}</time></span><strong>{html.escape(title)}</strong><span>Read the field notes →</span></a>'
        for day, title in entries
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="Daily AI Builder Brief field notes from Lifan Builds."><title>AI Builder Brief Journal — Lifan Builds</title><link rel="stylesheet" href="../../styles.css"></head>
<body><div class="scanlines" aria-hidden="true"></div><header class="site-header wrap"><a class="wordmark" href="../../" aria-label="Back to Lifan Builds home"><span class="pixel-mark">LB</span><span>LIFAN BUILDS</span></a><nav aria-label="Journal navigation"><a href="../../#quests">Quests</a><a href="../../#about">About</a><a class="nav-button" href="https://clifan.substack.com/">Substack ↗</a></nav></header>
<main><section class="journal-hero wrap"><p class="eyebrow">JOURNAL / AI BUILDER BRIEF</p><h1>Daily notes from<br><em>the frontier.</em></h1><p class="hero-lede">Source-linked AI developments, technical insight, and builder behavior—kept as a durable field journal.</p></section><section class="brief-archive wrap" aria-label="AI Builder Brief entries">{cards}</section></main>
<footer class="site-footer wrap"><span><a href="../../journal/">← Main journal</a></span><span><a href="https://github.com/lifan-builds/ai-builder-brief">Source repository ↗</a></span></footer></body></html>"""


def _update_parent_journal(site_root: Path, entries: list[tuple[date, str]]) -> None:
    path = site_root / "docs" / "journal" / "index.html"
    text = path.read_text(encoding="utf-8")
    latest = entries[0] if entries else None
    latest_text = f" · latest {latest[0].strftime('%b %-d')}" if latest else ""
    section = f"""{_START}
    <section class="brief-portal wrap" aria-labelledby="brief-portal-title"><div><p class="eyebrow">DAILY / AI BUILDER BRIEF</p><h2 id="brief-portal-title">The frontier,<br><em>with receipts.</em></h2><p>Daily field notes on the AI developments that change what builders make, use, and prepare for.</p></div><a class="button primary" href="ai-builder-brief/">Browse {len(entries)} entries{latest_text} ↗</a></section>
    {_END}"""
    pattern = re.compile(re.escape(_START) + r".*?" + re.escape(_END), re.S)
    text = pattern.sub(section, text) if pattern.search(text) else text.replace("</main>", section + "\n  </main>", 1)
    path.write_text(text, encoding="utf-8")


def publish_journal(brief_path: Path, site_root: Path) -> str:
    match = re.search(r"(\d{4}-\d{2}-\d{2})", brief_path.name)
    if not match:
        raise ValueError(f"brief path must contain an ISO date: {brief_path}")
    day = date.fromisoformat(match.group(1))
    target = site_root / "docs" / "journal" / "ai-builder-brief"
    target.mkdir(parents=True, exist_ok=True)
    title, intro, body, story_count = _top_ten_body(brief_path.read_text(encoding="utf-8"))
    entry = target / f"{day.isoformat()}.html"
    rendered = _entry_html(day, title, intro, body, story_count)
    changed = not entry.is_file() or entry.read_text(encoding="utf-8") != rendered
    entry.write_text(rendered, encoding="utf-8")
    entries: list[tuple[date, str]] = []
    for path in sorted(brief_path.parent.glob("*.md"), reverse=True):
        found = re.search(r"(\d{4}-\d{2}-\d{2})", path.name)
        if not found or "-x-" in path.name:
            continue
        entry_day = date.fromisoformat(found.group(1))
        entry_title, _ = markdown_body(path.read_text(encoding="utf-8"))
        entries.append((entry_day, entry_title))
    entries.sort(reverse=True)
    (target / "index.html").write_text(_archive_html(entries), encoding="utf-8")
    _update_parent_journal(site_root, entries)
    return "published" if changed else "already-published"
