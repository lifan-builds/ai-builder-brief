"""Source-metadata collectors for the rolling AI Builder Brief window."""

from __future__ import annotations

import hashlib
import html
import json
import logging
import re
import shutil
import ssl
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

import yaml
import certifi

from castforge.models import SourceItem

log = logging.getLogger(__name__)
USER_AGENT = "AIBuilderBrief/0.1 (+https://github.com/lifan-builds/ai-builder-brief)"
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


def _request(url: str, *, opener: Callable[..., Any] = urlopen) -> bytes:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/json, text/html"})
    kwargs: dict[str, Any] = {"timeout": 30}
    if opener is urlopen:
        kwargs["context"] = SSL_CONTEXT
    with opener(request, **kwargs) as response:
        return response.read()


def _identifier(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


def _parse_datetime(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _plain_text(value: str, *, limit: int = 1200) -> str:
    parser = _TextExtractor()
    parser.feed(value or "")
    text = html.unescape(" ".join(parser.parts))
    return re.sub(r"\s+", " ", text).strip()[:limit]


BUILDER_TERMS = {
    "agent", "api", "benchmark", "code", "developer", "eval", "gpu", "inference", "library",
    "model", "open source", "release", "research", "sdk", "serving", "tool", "training",
}
NON_BUILDER_TERMS = {"ad campaign", "advertising", "marketing", "shopping"}


def _builder_adjustment(title: str, summary: str) -> float:
    haystack = f"{title} {summary}".casefold()
    matches = sum(1 for term in BUILDER_TERMS if term in haystack)
    penalty = 40 if any(term in haystack for term in NON_BUILDER_TERMS) else 0
    return (matches * 3 if matches else -25) - penalty


def _infer_category(configured: str, title: str, summary: str) -> str:
    if configured in {"developer tools", "research", "open source", "infrastructure"}:
        return configured
    text = f"{title} {summary}".casefold()
    if any(term in text for term in ("government", "regulation", "policy", "lawmakers", "responsible ai")):
        return "policy"
    if any(term in text for term in ("data center", "datacenter", "gpu", "chip", "compute infrastructure")):
        return "infrastructure"
    if any(term in text for term in ("sdk", "api", "developer", "library", "runtime", "framework")):
        return "developer tools"
    return configured


def _child_text(element: ET.Element, *names: str) -> str:
    wanted = set(names)
    for child in element:
        if child.tag.rsplit("}", 1)[-1] in wanted and child.text:
            return child.text.strip()
    return ""


def _entry_link(element: ET.Element) -> str:
    for child in element:
        if child.tag.rsplit("}", 1)[-1] != "link":
            continue
        if child.get("href") and child.get("rel", "alternate") == "alternate":
            return child.get("href", "").strip()
        if child.text:
            return child.text.strip()
    return ""


def collect_feed(
    definition: dict[str, Any],
    *,
    start: datetime,
    end: datetime,
    opener: Callable[..., Any] = urlopen,
) -> list[SourceItem]:
    root = ET.fromstring(_request(str(definition["url"]), opener=opener))
    entries = [element for element in root.iter() if element.tag.rsplit("}", 1)[-1] in {"item", "entry"}]
    items: list[SourceItem] = []
    limit = int(definition.get("limit", 20))
    for entry in entries:
        published = _parse_datetime(_child_text(entry, "pubDate", "published", "updated", "date"))
        if published is None or not start <= published <= end:
            continue
        url = _entry_link(entry)
        title = _plain_text(_child_text(entry, "title"), limit=300)
        summary = _plain_text(_child_text(entry, "description", "summary", "content"))
        if not url or not title or not summary:
            continue
        organization = str(definition.get("organization", definition["name"]))
        category = _infer_category(str(definition.get("category", "industry")), title, summary)
        if "github.com" in urlparse(url).netloc and category == "developer tools":
            title = f"{organization} {title}"
        score = float(definition.get("score", 50)) + _builder_adjustment(title, summary)
        items.append(
            SourceItem(
                id=_identifier(url),
                title=title,
                url=url,
                source=str(definition["name"]),
                published_at=published.isoformat().replace("+00:00", "Z"),
                summary=summary,
                authority=str(definition.get("authority", "signal")),
                organization=organization,
                category=category,
                metadata={
                    "canonical_url": url,
                    "score": score,
                    "selection_reason": "Fresh source within the rolling 24-hour window",
                },
            )
        )
        if len(items) >= limit:
            break
    return items


class _IndexParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self.current_href = ""
        self.current_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            self.current_href = dict(attrs).get("href") or ""
            self.current_text = []

    def handle_data(self, data: str) -> None:
        if self.current_href:
            self.current_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.current_href:
            self.links.append((self.current_href, _plain_text(" ".join(self.current_text), limit=300)))
            self.current_href = ""
            self.current_text = []


class _ArticleMetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.meta: dict[str, str] = {}
        self.times: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "meta":
            key = values.get("property") or values.get("name") or ""
            content = values.get("content") or ""
            if key and content:
                self.meta[key] = content
        elif tag == "time" and values.get("datetime"):
            self.times.append(values["datetime"] or "")


def collect_page_index(
    definition: dict[str, Any],
    *,
    start: datetime,
    end: datetime,
    opener: Callable[..., Any] = urlopen,
) -> list[SourceItem]:
    index_url = str(definition["url"])
    prefix = str(definition["link_prefix"])
    parser = _IndexParser()
    parser.feed(_request(index_url, opener=opener).decode("utf-8", errors="replace"))
    candidates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for href, anchor_text in parser.links:
        url = urljoin(index_url, href).split("#", 1)[0]
        if not url.startswith(prefix) or url.rstrip("/") == index_url.rstrip("/") or url in seen:
            continue
        seen.add(url)
        candidates.append((url, anchor_text))
        if len(candidates) >= 8:
            break

    items: list[SourceItem] = []
    for url, anchor_text in candidates:
        article = _ArticleMetaParser()
        article.feed(_request(url, opener=opener).decode("utf-8", errors="replace"))
        published = _parse_datetime(
            article.meta.get("article:published_time", "")
            or article.meta.get("datePublished", "")
            or article.meta.get("date", "")
            or (article.times[0] if article.times else "")
        )
        if published is None or not start <= published <= end:
            continue
        title = _plain_text(article.meta.get("og:title", "") or article.meta.get("twitter:title", "") or anchor_text, limit=300)
        summary = _plain_text(article.meta.get("og:description", "") or article.meta.get("description", "") or title)
        if not title:
            continue
        items.append(
            SourceItem(
                id=_identifier(url),
                title=title,
                url=url,
                source=str(definition["name"]),
                published_at=published.isoformat().replace("+00:00", "Z"),
                summary=summary,
                authority="primary",
                organization=str(definition.get("organization", definition["name"])),
                category=str(definition.get("category", "models")),
                metadata={
                    "canonical_url": url,
                    "score": float(definition.get("score", 90)),
                    "selection_reason": "Official announcement within the rolling window",
                },
            )
        )
    return items


def collect_hacker_news(
    *,
    start: datetime,
    end: datetime,
    limit: int,
    opener: Callable[..., Any] = urlopen,
) -> list[SourceItem]:
    ids = json.loads(_request("https://hacker-news.firebaseio.com/v0/topstories.json", opener=opener))[:limit]
    items: list[SourceItem] = []
    for story_id in ids:
        raw = json.loads(_request(f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json", opener=opener))
        if raw.get("type") != "story" or not raw.get("url") or not raw.get("title"):
            continue
        published = datetime.fromtimestamp(int(raw.get("time", 0)), tz=UTC)
        if not start <= published <= end:
            continue
        url = str(raw["url"])
        score = min(85, 25 + int(raw.get("score", 0)) / 10 + int(raw.get("descendants", 0)) / 20)
        items.append(
            SourceItem(
                id=f"hn-{story_id}",
                title=str(raw["title"]),
                url=url,
                source="Hacker News",
                published_at=published.isoformat().replace("+00:00", "Z"),
                summary=f"Hacker News signal: {raw.get('score', 0)} points and {raw.get('descendants', 0)} comments.",
                authority="signal",
                organization=urlparse(url).netloc.removeprefix("www."),
                category="trend",
                metadata={
                    "canonical_url": url,
                    "discussion_url": f"https://news.ycombinator.com/item?id={story_id}",
                    "score": score,
                    "selection_reason": "Strong developer-community attention",
                },
            )
        )
    return items


def collect_hugging_face(
    *,
    start: datetime,
    end: datetime,
    limit: int,
    daily_papers: bool,
    trending_models: bool,
    opener: Callable[..., Any] = urlopen,
) -> list[SourceItem]:
    items: list[SourceItem] = []
    if daily_papers:
        papers = json.loads(_request(f"https://huggingface.co/api/daily_papers?limit={limit}", opener=opener))
        for entry in papers[:limit]:
            paper = entry.get("paper") or entry
            published = _parse_datetime(paper.get("submittedOnDailyAt") or entry.get("publishedAt") or "")
            if published is None or not start <= published <= end:
                continue
            paper_id = str(paper.get("id", ""))
            title = str(paper.get("title", "")).strip()
            summary = str(paper.get("ai_summary") or paper.get("summary") or "").strip()
            if not paper_id or not title or not summary:
                continue
            url = f"https://huggingface.co/papers/{paper_id}"
            organization = (paper.get("organization") or {}).get("fullname") or "Research authors"
            items.append(
                SourceItem(
                    id=f"hf-paper-{paper_id}",
                    title=title,
                    url=url,
                    source="Hugging Face Daily Papers",
                    published_at=published.isoformat().replace("+00:00", "Z"),
                    summary=summary[:1200],
                    authority="primary",
                    organization=str(organization),
                    category="research",
                    metadata={
                        "canonical_url": url,
                        "score": 60 + int(paper.get("upvotes", 0)),
                        "selection_reason": "Fresh primary research selected by the Hugging Face community",
                    },
                )
            )
    if trending_models:
        models = json.loads(_request(f"https://huggingface.co/api/models?sort=trendingScore&limit={limit}", opener=opener))
        for model in models[:limit]:
            published = _parse_datetime(model.get("createdAt") or model.get("lastModified") or "")
            if published is None or not start <= published <= end:
                continue
            model_id = str(model.get("id", ""))
            if not model_id:
                continue
            url = f"https://huggingface.co/{model_id}"
            tags = ", ".join(str(tag) for tag in model.get("tags", [])[:6])
            items.append(
                SourceItem(
                    id=f"hf-model-{_identifier(model_id)}",
                    title=f"{model_id} trends on Hugging Face",
                    url=url,
                    source="Hugging Face model card",
                    published_at=published.isoformat().replace("+00:00", "Z"),
                    summary=f"The model card reports {model.get('likes', 0)} likes and tags: {tags}.",
                    authority="primary",
                    organization=model_id.split("/", 1)[0],
                    category="models",
                    metadata={
                        "canonical_url": url,
                        "signal_key": f"hf-model:{model_id}",
                        "hf_likes": int(model.get("likes", 0) or 0),
                        "hf_downloads": int(model.get("downloads", 0) or 0),
                        "hf_trending_score": float(model.get("trendingScore", 0) or 0),
                        "score": min(95, 55 + float(model.get("trendingScore", 0)) / 50),
                        "selection_reason": "New model with strong open-source trend activity",
                    },
                )
            )
    return items


STOP_WORDS = {
    "about", "after", "adds", "again", "from", "into", "launches", "more", "new", "over",
    "release", "releases", "says", "that", "their", "this", "using", "with", "your",
}


def _title_tokens(title: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9][a-z0-9.+-]+", title.casefold())
        if len(token) > 2 and token not in STOP_WORDS
    }


def assign_story_clusters(items: list[SourceItem]) -> list[SourceItem]:
    """Conservatively group matching URLs or strongly overlapping headlines."""
    ordered = sorted(
        items,
        key=lambda item: (
            {"primary": 0, "independent": 1, "analysis": 2, "signal": 3}[item.authority],
            -float(item.metadata.get("score", 0)),
            item.id,
        ),
    )
    representatives: list[tuple[str, str, set[str]]] = []
    result: list[SourceItem] = []
    for item in ordered:
        canonical = str(item.metadata.get("canonical_url") or item.url).rstrip("/")
        tokens = _title_tokens(item.title)
        cluster_id = ""
        for existing_id, existing_url, existing_tokens in representatives:
            overlap = len(tokens & existing_tokens)
            union = len(tokens | existing_tokens) or 1
            if canonical == existing_url or (overlap >= 3 and overlap / union >= 0.6):
                cluster_id = existing_id
                break
        if not cluster_id:
            cluster_id = re.sub(r"[^a-z0-9]+", "-", item.title.casefold()).strip("-")[:80] or item.id
            representatives.append((cluster_id, canonical, tokens))
        metadata = dict(item.metadata)
        metadata["cluster_id"] = cluster_id
        result.append(replace(item, metadata=metadata))
    return result


def collect_sources(
    config_path: Path,
    *,
    start: datetime,
    end: datetime,
    opener: Callable[..., Any] = urlopen,
) -> list[SourceItem]:
    raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    if int(raw.get("version", 0)) != 1:
        raise ValueError("sources config version must be 1")
    collected: list[SourceItem] = []
    for definition in raw.get("feeds", []):
        try:
            collected.extend(collect_feed(definition, start=start, end=end, opener=opener))
        except Exception as error:
            log.warning("source failed: %s: %s", definition.get("name"), error)
    for definition in raw.get("page_indexes", []):
        try:
            collected.extend(collect_page_index(definition, start=start, end=end, opener=opener))
        except Exception as error:
            log.warning("source failed: %s: %s", definition.get("name"), error)
    hn = raw.get("hacker_news") or {}
    if hn.get("enabled"):
        try:
            collected.extend(collect_hacker_news(start=start, end=end, limit=int(hn.get("limit", 20)), opener=opener))
        except Exception as error:
            log.warning("source failed: Hacker News: %s", error)
    hf = raw.get("hugging_face") or {}
    if hf.get("daily_papers") or hf.get("trending_models"):
        try:
            collected.extend(
                collect_hugging_face(
                    start=start,
                    end=end,
                    limit=int(hf.get("limit", 10)),
                    daily_papers=bool(hf.get("daily_papers")),
                    trending_models=bool(hf.get("trending_models")),
                    opener=opener,
                )
            )
        except Exception as error:
            log.warning("source failed: Hugging Face: %s", error)
    github = raw.get("github") or {}
    if github.get("enabled") and github.get("repositories"):
        collected.extend(
            collect_github_momentum(
                [str(repository) for repository in github.get("repositories", [])],
                start=start,
                end=end,
                opener=opener,
            )
        )
    x_panel = raw.get("x_panel") or {}
    if x_panel.get("enabled") and x_panel.get("accounts"):
        collected.extend(
            collect_x_panel(
                [str(account).lstrip("@").strip() for account in x_panel.get("accounts", [])],
                start=start,
                end=end,
            )
        )
    if not collected:
        raise RuntimeError("all configured sources failed or returned no items in the rolling window")
    return assign_story_clusters(collected)


def collect_github_momentum(
    repositories: list[str], *, start: datetime, end: datetime, opener: Callable[..., Any] = urlopen,
) -> list[SourceItem]:
    """Collect public GitHub release momentum as signal metadata."""

    items: list[SourceItem] = []
    for repository in repositories:
        try:
            raw = json.loads(_request(f"https://api.github.com/repos/{repository}/releases?per_page=10", opener=opener))
            repo = json.loads(_request(f"https://api.github.com/repos/{repository}", opener=opener))
        except Exception as error:
            log.warning("source failed: GitHub %s: %s", repository, error)
            continue
        for release in raw:
            published = _parse_datetime(str(release.get("published_at") or release.get("created_at") or ""))
            if published is None or not start <= published <= end or release.get("draft"):
                continue
            tag = str(release.get("tag_name") or "").strip()
            title = str(release.get("name") or tag or repository).strip()
            body = _plain_text(str(release.get("body") or ""), limit=1200)
            stars = int(repo.get("stargazers_count", 0) or 0)
            forks = int(repo.get("forks_count", 0) or 0)
            issues = int(repo.get("open_issues_count", 0) or 0)
            momentum_score = min(4, max(0, round(stars / 25_000 + forks / 10_000 + issues / 2_000)))
            items.append(SourceItem(
                id=f"github-{_identifier(str(release.get('html_url') or repository))}",
                title=f"{repository} {title}",
                url=str(release.get("html_url") or f"https://github.com/{repository}/releases"),
                source="GitHub releases",
                published_at=published.isoformat().replace("+00:00", "Z"),
                summary=body or f"GitHub release {tag} for {repository}.",
                authority="primary",
                organization=repository.split("/", 1)[0],
                category="developer tools",
                metadata={
                    "canonical_url": str(release.get("html_url") or repository),
                    "signal_key": f"github:{repository}",
                    "momentum": True,
                    "momentum_score": momentum_score,
                    "repository_stars": stars,
                    "repository_forks": forks,
                    "repository_open_issues": issues,
                    "score": 60 + momentum_score * 4,
                },
            ))
    return items


def collect_x_panel(
    accounts: list[str], *, start: datetime, end: datetime, fetcher: Callable[[str], list[dict[str, Any]]] | None = None,
) -> list[SourceItem]:
    """Best-effort expert observations; X outages never block collection."""

    if fetcher is None:
        fetcher = _twitter_cli_fetcher
    items: list[SourceItem] = []
    for account in accounts:
        try:
            posts = fetcher(account)
        except Exception as error:
            log.warning("optional X panel source failed: %s: %s", account, error)
            continue
        for post in posts:
            published = _parse_datetime(str(post.get("published_at") or ""))
            if published is None:
                # The compact twitter CLI omits the year for recent posts.
                raw_time = str(post.get("published_at") or "").strip()
                try:
                    published = datetime.strptime(raw_time, "%b %d %H:%M").replace(year=end.year, tzinfo=UTC)
                except ValueError:
                    published = None
            if published is None or not start <= published <= end:
                continue
            url = str(post.get("url") or "").strip()
            text = _plain_text(str(post.get("text") or ""), limit=1200)
            if not url or not text:
                continue
            items.append(SourceItem(
                id=f"x-{_identifier(url)}", title=f"@{account}: {text[:100]}", url=url,
                source="Approved X panel", published_at=published.isoformat().replace("+00:00", "Z"),
                summary=text, authority="analysis", organization=account, category="expert analysis",
                metadata={"canonical_url": url, "kind": "expert_analysis", "score": 35},
            ))
    return items


def _twitter_cli_fetcher(account: str) -> list[dict[str, Any]]:
    """Read approved-account posts through the installed read-only X CLI."""

    executable = shutil.which("twitter")
    if not executable:
        raise RuntimeError("twitter CLI is not installed")
    result = subprocess.run(
        [executable, "--compact", "user-posts", account, "--max", "20", "--json"],
        check=True,
        capture_output=True,
        text=True,
        timeout=45,
    )
    payload = json.loads(result.stdout)
    if isinstance(payload, dict) and payload.get("ok") is True:
        payload = payload.get("data")
    if not isinstance(payload, list):
        raise ValueError("twitter CLI returned a non-list payload")
    posts: list[dict[str, Any]] = []
    for post in payload:
        if not isinstance(post, dict):
            continue
        tweet_id = str(post.get("id") or "").strip()
        raw_time = str(post.get("time") or "").strip()
        if not tweet_id or not raw_time:
            continue
        posts.append({
            "published_at": raw_time,
            "url": f"https://x.com/{account}/status/{tweet_id}",
            "text": str(post.get("text") or ""),
        })
    return posts


def write_sources(items: list[SourceItem], path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"items": [item.to_dict() for item in items]}, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def read_sources(path: Path) -> list[SourceItem]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    records = raw.get("items", []) if isinstance(raw, dict) else raw
    if not isinstance(records, list):
        raise ValueError("source snapshot must contain an items list")
    return [SourceItem.from_dict(record) for record in records]
