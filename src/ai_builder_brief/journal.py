"""Publish saved daily AI briefs into the clifan.com journal."""
from __future__ import annotations

import html
import re
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

_START = "<!-- AI-BUILDER-BRIEF:START -->"
_END = "<!-- AI-BUILDER-BRIEF:END -->"


def _compact_summary(value: str, limit: int = 76) -> str:
    value = re.sub(r"\s+\*\*Confidence:.*$", "", value, flags=re.S)
    value = re.sub(r"\s+Confidence:\s+(?:high|medium|low).*$", "", value, flags=re.I | re.S)
    value = re.sub(r"\s*\[[^\]]+\]\(https?://[^)\s]+\)", "", value)
    value = re.sub(r"https?://[^\s)]+", "", value)
    value = re.sub(r"\*\*([^*]+)\*\*", r"\1", value)
    value = re.sub(r"\s+·\s*$", "", value).strip()
    value = re.sub(r"\s{2,}", " ", value)
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", value) if len(s.strip()) > 12]
    summary = " ".join(sentences[:2]).strip() or value
    words = summary.split()
    if len(words) > limit:
        summary = " ".join(words[:limit]).rstrip(" ,;:") + "…"
    return summary


def _extract_links(text: str) -> list[tuple[str, str]]:
    md_links = re.findall(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", text)
    if md_links:
        return md_links[:3]
    bare_urls = re.findall(r"https?://[^\s),;]+", text)
    results: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw_url in bare_urls:
        url = raw_url.rstrip(".")
        if url in seen:
            continue
        seen.add(url)
        parsed = urlparse(url)
        host = (parsed.netloc or "source").removeprefix("www.")
        path_parts = [p for p in parsed.path.split("/") if p]
        if host == "github.com" and len(path_parts) >= 2:
            label = f"{path_parts[0]}/{path_parts[1]}"
        else:
            label = host
        results.append((label, url))
        if len(results) >= 3:
            break
    return results


def _parse_ranked_items(markdown: str) -> list[tuple[str, str, str, list[tuple[str, str]]]]:
    lines = markdown.splitlines()
    raw_items: list[tuple[str, list[str]]] = []
    cur_first: str | None = None
    cur_rest: list[str] = []
    in_items = False

    for line in lines:
        stripped = line.strip()
        if re.match(
            r"^(?:##\s+|\*\*)?(?:Watchlist|Source panel|What changed|Builder workflow|Unified-ranking)",
            stripped,
            re.I,
        ):
            if in_items:
                break
        m_num = re.match(r"^(\d+)\.\s+(.*)$", stripped)
        if m_num and int(m_num.group(1)) <= 12:
            if cur_first is not None:
                raw_items.append((cur_first, cur_rest))
            cur_first = m_num.group(2).strip()
            cur_rest = []
            in_items = True
        elif in_items and stripped:
            cur_rest.append(stripped)

    if cur_first is not None:
        raw_items.append((cur_first, cur_rest))

    parsed_items: list[tuple[str, str, str, list[tuple[str, str]]]] = []
    for first_line, rest_lines in raw_items[:10]:
        combined = " ".join([first_line, *rest_lines]).strip()
        links = _extract_links(combined)

        # Pattern 1: **Title** — **Date**. Body
        m1 = re.match(r"^\*\*(.+?)\*\*\s+[—–-]\s+\*\*(.+?)\*\*\.\s*(.*)$", combined)
        # Pattern 2: **Title** (Date). Body
        m2 = re.match(r"^\*\*(.+?)\*\*\s*\(([^)]+)\)\.\s*(.*)$", combined)
        # Pattern 3: **Title**. Body
        m3 = re.match(r"^\*\*(.+?)\*\*\.\s*(.*)$", combined)

        if m1:
            story_title, story_date, story_body = m1.groups()
        elif m2:
            story_title, story_date, story_body = m2.groups()
        elif m3:
            story_title, story_body = m3.groups()
            story_date = "Brief item"
        elif rest_lines and not first_line.startswith("**"):
            # Pattern 4: Plain title on line 1, followed by indented paragraphs
            story_title = first_line.strip("* ")
            rest_text = " ".join(rest_lines)
            m_date = re.match(
                r"^((?:Original|New)[^:]{0,28}:\s*[^.]+)\.\s*(.*)$",
                rest_text,
                re.I,
            )
            if m_date:
                story_date, story_body = m_date.groups()
            else:
                story_date, story_body = "Field note", rest_text
        else:
            story_title, story_date, story_body = re.sub(r"\*\*([^*]+)\*\*", r"\1", first_line), "Field note", " ".join(rest_lines)

        parsed_items.append((story_title.strip(), story_date.strip(), _compact_summary(story_body), links))

    return parsed_items


def _top_ten_body(markdown: str) -> tuple[str, str, str, int]:
    """Build the public, compact view from the brief's Top 10 section."""
    lines = markdown.splitlines()
    title = next((line[2:].strip() for line in lines if line.startswith("# ")), "AI Builder Brief")
    intro = ""
    for line in lines:
        if line.strip().startswith("Read on the day:"):
            intro = _compact_summary(line.split(":", 1)[1].strip(), 42)
            break

    items = _parse_ranked_items(markdown)
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
    body = '<section class="story-grid" aria-label="Top 10 stories">' + "".join(cards) + "</section>"
    return title, intro, body, len(items[:10])


def _entry_html(
    day: date,
    title: str,
    intro: str,
    body: str,
    story_count: int,
    prev_day: date | None = None,
    next_day: date | None = None,
) -> str:
    display_title = f"AI Builder Brief · {day.strftime('%B %-d, %Y')}"
    nav_parts = ['<a href="./">← All AI Builder Brief entries</a>']
    if prev_day:
        nav_parts.append(f'<a href="{prev_day.isoformat()}.html">← Older ({prev_day.strftime("%b %-d")})</a>')
    if next_day:
        nav_parts.append(f'<a href="{next_day.isoformat()}.html">Newer ({next_day.strftime("%b %-d")}) →</a>')
    nav_parts.append('<a href="https://github.com/lifan-builds/ai-builder-brief">Source repository ↗</a>')
    bottom_links = " ".join(nav_parts)

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="{html.escape(intro or display_title)}"><link rel="canonical" href="https://clifan.com/journal/ai-builder-brief/{day.isoformat()}.html"><meta property="og:type" content="article"><meta property="og:title" content="{html.escape(display_title)} — Lifan Chen"><meta property="og:description" content="{html.escape(intro or display_title)}"><meta property="og:url" content="https://clifan.com/journal/ai-builder-brief/{day.isoformat()}.html"><meta property="og:image" content="https://clifan.com/ai-builder-brief/og.jpg"><meta name="twitter:card" content="summary_large_image"><meta name="twitter:image" content="https://clifan.com/ai-builder-brief/og.jpg"><title>{html.escape(display_title)} — Lifan Chen</title>
<script>try{{if(localStorage.getItem('clifan-theme')==='dark')document.documentElement.setAttribute('data-theme','dark');}}catch(_){{}}</script><link rel="stylesheet" href="../../styles.css"><script type="module" src="../../site.js"></script></head>
<body><div class="scanlines" aria-hidden="true"></div>
<header class="site-header"><div class="header-inner"><a class="wordmark" href="../../" aria-label="Back to Lifan Builds home"><span class="pixel-mark" aria-hidden="true">LB</span><span>Lifan Chen</span><span class="wordmark-role">AI Builder Brief</span></a><nav aria-label="Journal navigation"><a href="../../#quests">Builds</a><a href="../">Journal</a><a href="./">All Briefs</a><button type="button" class="theme-toggle" data-theme-toggle aria-pressed="false" aria-label="Toggle color theme"><span data-theme-label>Light</span><kbd aria-hidden="true">T</kbd></button><a class="nav-button" href="https://clifan.substack.com/">Substack ↗</a></nav></div></header>
<main><article class="brief-entry wrap"><div class="brief-hero"><p class="eyebrow">AI Builder Brief · {day.strftime("%Y.%m.%d")}</p><h1>{html.escape(display_title)}</h1><p class="brief-entry-meta">{story_count} source-linked stories · <time datetime="{day.isoformat()}">{day.strftime("%B %-d, %Y")}</time></p><p class="brief-lede">{html.escape(intro)}</p></div>{body}<p class="journal-links">{bottom_links}</p></article></main>
<footer class="site-footer wrap"><span><a href="../">← Journal</a></span><div class="footer-links"><a href="./">Brief Archive</a><a href="https://github.com/lifan-builds">GitHub ↗</a><a href="https://x.com/LifanBuilds">X ↗</a></div></footer></body></html>"""


def _archive_html(entries: list[tuple[date, str, str, int]]) -> str:
    cards = "\n".join(
        f'<a class="brief-archive-card" href="{day.isoformat()}.html" data-search="{html.escape((headline + " " + intro + " " + day.isoformat()).lower(), quote=True)}">'
        f'<span class="journal-meta"><span>AI Builder Brief · {count} stories</span><time datetime="{day.isoformat()}">{day.strftime("%b %-d, %Y")}</time></span>'
        f'<strong>{html.escape(headline)}</strong>'
        f'<span class="archive-card-lede">{html.escape(intro)}</span>'
        f'<span>Read {day.strftime("%b %-d")} field notes →</span></a>'
        for day, headline, intro, count in entries
    )
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="description" content="Daily AI Builder Brief field notes from Lifan Chen: {len(entries)} source-linked editions covering agent systems, evaluations, and model releases."><link rel="canonical" href="https://clifan.com/journal/ai-builder-brief/"><meta property="og:type" content="website"><meta property="og:title" content="AI Builder Brief Archive · Lifan Chen"><meta property="og:description" content="Daily AI Builder Brief field notes from Lifan Chen: source-linked editions covering agent systems, evaluations, and model releases."><meta property="og:url" content="https://clifan.com/journal/ai-builder-brief/"><meta property="og:image" content="https://clifan.com/ai-builder-brief/og.jpg"><meta name="twitter:card" content="summary_large_image"><meta name="twitter:title" content="AI Builder Brief Archive · Lifan Chen"><meta name="twitter:description" content="Daily AI Builder Brief field notes from Lifan Chen: source-linked editions covering agent systems, evaluations, and model releases."><meta name="twitter:image" content="https://clifan.com/ai-builder-brief/og.jpg"><title>AI Builder Brief Archive ({len(entries)} Editions) · Lifan Chen</title><script>try{{if(localStorage.getItem('clifan-theme')==='dark')document.documentElement.setAttribute('data-theme','dark');}}catch(_){{}}</script><link rel="stylesheet" href="../../styles.css"><script type="module" src="../../site.js"></script></head>
<body><div class="scanlines" aria-hidden="true"></div><header class="site-header"><div class="header-inner"><a class="wordmark" href="../../" aria-label="Back to Lifan Builds home"><span class="pixel-mark" aria-hidden="true">LB</span><span>Lifan Chen</span><span class="wordmark-role">AI Builder Brief Archive</span></a><nav aria-label="Journal navigation"><a href="../../#quests">Builds</a><a href="../">Journal</a><a href="../../#about">About</a><button type="button" class="theme-toggle" data-theme-toggle aria-pressed="false" aria-label="Toggle color theme"><span data-theme-label>Light</span><kbd aria-hidden="true">T</kbd></button><a class="nav-button" href="https://clifan.substack.com/">Substack ↗</a></nav></div></header>
<main><section class="journal-hero wrap"><h1>Daily notes from <span class="cover-headline-accent">the frontier.</span></h1><p class="hero-lede">Source-linked AI developments, benchmark audits, and agent engineering field notes across {len(entries)} daily editions.</p><div class="archive-search-bar"><label for="brief-search" class="sr-only">Filter daily briefs by topic, model, or lab</label><input type="search" id="brief-search" class="archive-search-input" placeholder="Filter {len(entries)} briefs by topic, lab, or tool (e.g. MCP, Cursor, Anthropic, Qwen, Jepsen)…" autocomplete="off"><span class="archive-search-count" id="brief-search-count">{len(entries)} editions</span></div></section><section class="brief-archive wrap" id="brief-archive-grid" aria-label="AI Builder Brief entries">{cards}</section></main>
<footer class="site-footer wrap"><span><a href="../../journal/">← Main journal</a></span><div class="footer-links"><a href="../../ai-builder-brief/">Pipeline Overview</a><a href="https://github.com/lifan-builds/ai-builder-brief">Source repository ↗</a></div></footer></body></html>"""


def _update_parent_journal(site_root: Path, entries: list[tuple[date, str, str, int]]) -> None:
    path = site_root / "docs" / "journal" / "index.html"
    text = path.read_text(encoding="utf-8")
    latest = entries[0] if entries else None
    latest_text = f" · latest {latest[0].strftime('%b %-d')}" if latest else ""
    section = f"""{_START}
      <section class="brief-portal wrap" aria-labelledby="brief-portal-title">
        <div>
          <h2 id="brief-portal-title">The frontier, <em>with receipts.</em></h2>
          <p>Daily field notes on the AI developments that change what builders make, evaluate, and prepare for · {len(entries)} source-linked editions archived locally.</p>
        </div>
        <a class="button primary" href="ai-builder-brief/">Browse {len(entries)} entries{latest_text} →</a>
      </section>
      {_END}"""
    pattern = re.compile(re.escape(_START) + r".*?" + re.escape(_END), re.S)
    text = pattern.sub(section, text) if pattern.search(text) else text.replace("</main>", section + "\n  </main>", 1)
    path.write_text(text, encoding="utf-8")


def _headline_from_items(day: date, items: list[tuple[str, str, str, list[tuple[str, str]]]]) -> str:
    if not items:
        return f"AI Builder Brief · {day.strftime('%b %-d, %Y')}"
    top_titles = []
    for story_title, _, _, _ in items[:2]:
        short = re.sub(r"\s+", " ", story_title).strip().rstrip(".")
        if len(short) > 54:
            short = short[:52].rsplit(" ", 1)[0] + "…"
        top_titles.append(short)
    return " · ".join(top_titles)


def publish_journal(brief_path: Path, site_root: Path) -> str:
    match = re.search(r"(\d{4}-\d{2}-\d{2})", brief_path.name)
    if not match:
        raise ValueError(f"brief path must contain an ISO date: {brief_path}")
    day = date.fromisoformat(match.group(1))
    target = site_root / "docs" / "journal" / "ai-builder-brief"
    target.mkdir(parents=True, exist_ok=True)

    md_by_day: dict[date, Path] = {}
    for path in sorted(brief_path.parent.glob("*.md")):
        found = re.search(r"^(\d{4}-\d{2}-\d{2})\.md$", path.name)
        if found:
            md_by_day[date.fromisoformat(found.group(1))] = path
    md_by_day[day] = brief_path
    ordered_days = sorted(md_by_day.keys())
    day_index = ordered_days.index(day)
    prev_day = ordered_days[day_index - 1] if day_index > 0 else None
    next_day = ordered_days[day_index + 1] if day_index + 1 < len(ordered_days) else None

    raw_md = brief_path.read_text(encoding="utf-8")
    title, intro, body, story_count = _top_ten_body(raw_md)
    entry = target / f"{day.isoformat()}.html"
    rendered = _entry_html(day, title, intro, body, story_count, prev_day=prev_day, next_day=next_day)
    changed = not entry.is_file() or entry.read_text(encoding="utf-8") != rendered
    entry.write_text(rendered, encoding="utf-8")

    entries: list[tuple[date, str, str, int]] = []
    for entry_day in reversed(ordered_days):
        path = md_by_day[entry_day]
        md_text = path.read_text(encoding="utf-8")
        _, e_intro, _, e_count = _top_ten_body(md_text)
        e_items = _parse_ranked_items(md_text)
        headline = _headline_from_items(entry_day, e_items)
        short_intro = _compact_summary(e_intro, 24).replace("—", " · ")
        entries.append((entry_day, headline.replace("—", " · "), short_intro, e_count))
    (target / "index.html").write_text(_archive_html(entries), encoding="utf-8")
    _update_parent_journal(site_root, entries)
    return "published" if changed else "already-published"
