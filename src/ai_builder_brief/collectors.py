"""Source-metadata collectors for the rolling AI Builder Brief window."""

from __future__ import annotations

import gzip
import hashlib
import html
import json
import logging
import math
import re
import shutil
import ssl
import statistics
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

import certifi
import yaml
from castforge.models import SourceItem

log = logging.getLogger(__name__)
USER_AGENT = "AIBuilderBrief/0.2 (+https://github.com/lifan-builds/ai-builder-brief)"
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

PRIMARY_ARTIFACT_HOSTS = (
    "openai.com",
    "deploymentsafety.openai.com",
    "alignment.openai.com",
    "platform.openai.com",
    "anthropic.com",
    "claude.dev",
    "claude.com",
    "docs.anthropic.com",
    "deepmind.google",
    "research.google",
    "blog.google",
    "ai.meta.com",
    "mistral.ai",
    "api-docs.deepseek.com",
    "qwen.ai",
    "docs.qwencloud.com",
    "huggingface.co",
    "arxiv.org",
    "blog.cloudflare.com",
    "together.ai",
    "cursor.com",
    "opencode.ai",
    "github.blog",
    "epoch.ai",
    "metr.org",
    "apolloresearch.ai",
)

DISCOVERY_ONLY_HOSTS = {
    "news.ycombinator.com",
    "x.com",
    "twitter.com",
    "nitter.cf",
    "reddit.com",
    "old.reddit.com",
    "techmeme.com",
    "news.google.com",
}


@dataclass(frozen=True, slots=True)
class XPanelHealth:
    """Sanitized health summary for the approved read-only X panel."""

    configured_accounts: int
    attempted_accounts: int
    successful_accounts: int
    in_window_posts: int
    failed_accounts: tuple[str, ...] = ()
    required_success_ratio: float = 0.8
    required_configured_accounts: int | None = None
    required_successful_accounts: int | None = None

    @property
    def healthy(self) -> bool:
        configured = self.required_configured_accounts
        successful = self.required_successful_accounts
        if configured is None or successful is None:
            configured = self.configured_accounts
            successful = self.successful_accounts
        return (
            configured > 0
            and successful / configured >= self.required_success_ratio
            and self.in_window_posts > 0
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "configured_accounts": self.configured_accounts,
            "attempted_accounts": self.attempted_accounts,
            "successful_accounts": self.successful_accounts,
            "success_ratio": (
                self.successful_accounts / self.configured_accounts
                if self.configured_accounts
                else 0.0
            ),
            "required_success_ratio": self.required_success_ratio,
            "required_configured_accounts": self.required_configured_accounts,
            "required_successful_accounts": self.required_successful_accounts,
            "in_window_posts": self.in_window_posts,
            "failed_accounts": list(self.failed_accounts),
            "healthy": self.healthy,
        }


@dataclass(frozen=True, slots=True)
class XPanelResult:
    items: tuple[SourceItem, ...]
    health: XPanelHealth


@dataclass(frozen=True, slots=True)
class CollectionResult:
    items: tuple[SourceItem, ...]
    x_panel: XPanelHealth

    @property
    def healthy(self) -> bool:
        return any(item.authority in {"primary", "independent"} for item in self.items)

    def health_dict(self) -> dict[str, Any]:
        return {"healthy": self.healthy, "x_panel": self.x_panel.to_dict()}


def _request(url: str, *, opener: Callable[..., Any] = urlopen, timeout: int = 30) -> bytes:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/rss+xml, application/atom+xml, application/json, text/xml, text/html, */*",
        },
    )
    if opener is not urlopen:
        with opener(request, timeout=timeout) as response:
            raw: bytes = response.read()
            return gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw

    last_error: Exception | None = None
    for _ in range(2):
        try:
            with opener(request, timeout=timeout, context=SSL_CONTEXT) as response:
                raw = response.read()
                if raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
                if raw:
                    return raw
        except Exception as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"empty response from {url}")


_YOUTUBE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def extract_youtube_video_id(url: str) -> str:
    """Extract canonical 11-character YouTube video ID from watch/short/live/youtu.be URLs."""
    raw = (url or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    host = parsed.netloc.lower().removeprefix("www.").removeprefix("m.")
    parts = [p for p in parsed.path.split("/") if p]
    candidate = ""
    if host == "youtu.be" and parts:
        candidate = parts[0]
    elif host in {"youtube.com", "youtube-nocookie.com"}:
        if parts and parts[0] == "watch":
            candidate = (parse_qs(parsed.query).get("v") or [""])[0]
        elif len(parts) >= 2 and parts[0] in {"shorts", "live", "embed", "v"}:
            candidate = parts[1]
    return candidate if _YOUTUBE_ID_RE.match(candidate) else ""


def canonical_source_url(url: str) -> str:
    """Canonicalize URLs across tracking params, YouTube video variants, and arXiv/HF paper mirrors."""
    raw = (url or "").strip()
    if not raw:
        return ""
    yt_id = extract_youtube_video_id(raw)
    if yt_id:
        return f"https://www.youtube.com/watch?v={yt_id}"
    parsed = urlparse(raw)
    scheme = "https" if parsed.scheme in {"http", "https"} else parsed.scheme
    host = parsed.netloc.lower().removeprefix("www.")
    path = parsed.path.rstrip("/")
    arxiv_match = re.match(
        r"^(?:/(?:abs|html|pdf)|/papers)/(\d{4}\.\d{4,5})(?:v\d+)?(?:\.pdf)?$",
        path,
    )
    if arxiv_match and host in {"arxiv.org", "huggingface.co"}:
        return f"https://arxiv.org/abs/{arxiv_match.group(1)}"
    query = ""
    if host == "qwen.ai" and parsed.query.startswith("id="):
        query = f"?{parsed.query.split('&', 1)[0]}"
    return f"{scheme}://{host}{path}{query}" if host else raw.rstrip("/")


def _identifier(url: str) -> str:
    return hashlib.sha256(canonical_source_url(url).encode("utf-8")).hexdigest()[:16]


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
    "agent", "api", "benchmark", "changelog", "cli", "code", "compiler", "context",
    "developer", "eval", "gpu", "harness", "inference", "kernel", "kv cache",
    "latency", "library", "migration", "model", "open source", "open weights",
    "postmortem", "quantization", "release", "research", "runtime", "sandbox",
    "sdk", "serving", "tool", "training", "verification", "vulnerability", "weights",
}
NON_BUILDER_TERMS = {
    "ad campaign", "advertising", "before anyone asks", "case study",
    "disrupt 2026", "files s-1", "initial public offering", "interim ceo",
    "leads in legal intelligence", "marketing", "memorandum of understanding",
    "raises $", "seed round", "series a", "series b", "series c", "shopping",
    "sponsored", "stock price", "targets $", "valuation",
}
AI_TOPIC_TERMS = {
    "ai", "agent", "agents", "agentic", "aider", "anthropic", "artificial intelligence",
    "benchmark", "benchmarks", "blackwell", "chatgpt", "claude", "claude code", "cline",
    "codex", "coding agent", "coding agents", "cohere", "cuda", "cursor", "deepmind",
    "deepseek", "diffusion", "embedding", "embeddings", "eval", "evals", "fine-tuning",
    "flashattention", "gemini", "generative ai", "genai", "ggml", "gguf", "gpt", "gpu",
    "grpo", "grok", "hugging face", "huggingface", "inference", "jax", "kimi",
    "kv cache", "langchain", "language model", "language models", "litellm", "llama",
    "llama.cpp", "llamaindex", "llm", "llms", "lora", "machine learning", "mcp",
    "minimax", "mistral", "ml", "mlx", "model", "model context protocol", "models",
    "moonshot", "neural", "ollama", "open weights", "open-source ai", "openai",
    "opencode", "openrouter", "plugin", "plugins", "prompt", "prompt injection",
    "pydantic", "pytorch", "quantization", "qwen", "rag", "rlhf", "safetensors",
    "sglang", "skill", "skills", "speculative decoding", "swe-bench", "token",
    "tokens", "training", "transformer", "transformers", "triton", "vllm",
    "watermark", "weights", "xai",
}
NON_AI_CONTEXT_RE = re.compile(
    r"\b(?:ice\s+agent|fbi\s+agent|border\s+agent|real\s+estate\s+agent|secret\s+agent|insurance\s+agent|travel\s+agent|tesla\s+model\s+[3ysx]|car's\s+navigation)\b",
    re.IGNORECASE,
)


def _contains_term(text: str, term: str) -> bool:
    if term.endswith("$"):
        return term in text
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text))


def _matches_topic_filter(title: str, summary: str, topic_filter: str) -> bool:
    if not topic_filter:
        return True
    if topic_filter != "ai":
        raise ValueError(f"unsupported topic filter: {topic_filter}")
    text = f"{title} {summary}".casefold()
    if NON_AI_CONTEXT_RE.search(text):
        return False
    return any(_contains_term(text, term) for term in AI_TOPIC_TERMS)


def _builder_adjustment(title: str, summary: str) -> float:
    haystack = f"{title} {summary}".casefold()
    matches = sum(1 for term in BUILDER_TERMS if _contains_term(haystack, term))
    fluff_hits = sum(1 for term in NON_BUILDER_TERMS if _contains_term(haystack, term))
    penalty = min(60, fluff_hits * 35)
    return (min(21, matches * 3) if matches else -20) - penalty


def _infer_category(configured: str, title: str, summary: str) -> str:
    if configured in {"developer tools", "research", "open source", "infrastructure"}:
        return configured
    text = f"{title} {summary}".casefold()
    if any(_contains_term(text, term) for term in ("government", "regulation", "policy", "lawmakers", "responsible ai")):
        return "policy"
    if any(_contains_term(text, term) for term in ("data center", "datacenter", "gpu", "chip", "compute infrastructure")):
        return "infrastructure"
    if any(_contains_term(text, term) for term in ("sdk", "api", "developer", "library", "runtime", "framework", "cli", "mcp")):
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
        summary = _plain_text(_child_text(entry, "description", "summary", "content", "encoded"))
        if not url or not title or not summary:
            continue
        if not _matches_topic_filter(
            title,
            summary,
            str(definition.get("topic_filter") or ""),
        ):
            continue
        organization = str(definition.get("organization", definition["name"]))
        category = _infer_category(str(definition.get("category", "industry")), title, summary)
        if "github.com" in urlparse(url).netloc and category == "developer tools" and not title.lower().startswith(organization.lower()):
            title = f"{organization} {title}"
        score = float(definition.get("score", 50)) + _builder_adjustment(title, summary)
        canon_url = canonical_source_url(url)
        items.append(
            SourceItem(
                id=_identifier(canon_url),
                title=title,
                url=url,
                source=str(definition["name"]),
                published_at=published.isoformat().replace("+00:00", "Z"),
                summary=summary,
                authority=str(definition.get("authority", "signal")),
                organization=organization,
                category=category,
                metadata={
                    "canonical_url": canon_url,
                    "source_kind": str(definition.get("source_kind", "feed")),
                    "score": score,
                    "selection_reason": "Fresh source within the rolling window",
                    **(
                        {"product_family": str(definition["product_family"])}
                        if definition.get("product_family")
                        else {}
                    ),
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
        self._in_ld_json = False
        self._ld_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "meta":
            key = values.get("property") or values.get("name") or ""
            content = values.get("content") or ""
            if key and content:
                self.meta[key] = content
        elif tag == "time" and values.get("datetime"):
            self.times.append(values["datetime"] or "")
        elif tag == "script" and (values.get("type") or "").lower() == "application/ld+json":
            self._in_ld_json = True
            self._ld_chunks = []

    def handle_data(self, data: str) -> None:
        if self._in_ld_json:
            self._ld_chunks.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_ld_json:
            self._in_ld_json = False
            raw_ld = "".join(self._ld_chunks)
            m_pub = re.search(r'"datePublished"\s*:\s*"([^"]+)"', raw_ld)
            if m_pub and "datePublished" not in self.meta:
                self.meta["datePublished"] = m_pub.group(1)
            m_desc = re.search(r'"description"\s*:\s*"([^"]+)"', raw_ld)
            if m_desc and "description" not in self.meta:
                self.meta["description"] = m_desc.group(1)


_INDEX_SKIP_SEGMENTS = ("/topic/", "/tag/", "/category/", "/page/", "/author/", "/authors/")


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
        if (
            not url.startswith(prefix)
            or url.rstrip("/") == index_url.rstrip("/")
            or any(seg in url.lower() for seg in _INDEX_SKIP_SEGMENTS)
            or url in seen
        ):
            continue
        seen.add(url)
        candidates.append((url, anchor_text))
        if len(candidates) >= 14:
            break

    items: list[SourceItem] = []
    for url, anchor_text in candidates:
        try:
            article_html = _request(url, opener=opener).decode("utf-8", errors="replace")
        except Exception:
            continue
        article = _ArticleMetaParser()
        article.feed(article_html)
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
        if not _matches_topic_filter(title, summary, str(definition.get("topic_filter") or "")):
            continue
        canon_url = canonical_source_url(url)
        base_score = float(definition.get("score", 90)) + _builder_adjustment(title, summary)
        items.append(
            SourceItem(
                id=_identifier(canon_url),
                title=title,
                url=url,
                source=str(definition["name"]),
                published_at=published.isoformat().replace("+00:00", "Z"),
                summary=summary,
                authority="primary",
                organization=str(definition.get("organization", definition["name"])),
                category=_infer_category(str(definition.get("category", "models")), title, summary),
                metadata={
                    "canonical_url": canon_url,
                    "source_kind": "announcement",
                    "score": base_score,
                    "selection_reason": "Official announcement within the rolling window",
                },
            )
        )
    return items


def _classify_hn_target_authority(url: str, extracted_summary: str) -> tuple[str, str]:
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    if host in DISCOVERY_ONLY_HOSTS:
        return "signal", "trend"
    if host == "github.com":
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) >= 2 and parts[0] not in {"trending", "topics", "orgs", "features"}:
            return "primary", "developer tools"
    if any(host == dom or host.endswith(f".{dom}") for dom in PRIMARY_ARTIFACT_HOSTS):
        cat = "research" if host in {"arxiv.org", "research.google", "epoch.ai", "metr.org"} else "developer tools"
        return "primary", cat
    if len(extracted_summary.split()) >= 14:
        return "independent", "developer tools"
    return "signal", "trend"


def _fetch_linked_meta_summary(url: str, *, opener: Callable[..., Any] = urlopen) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    if host in DISCOVERY_ONLY_HOSTS or url.lower().endswith(".pdf"):
        return ""
    try:
        raw_html = _request(url, opener=opener, timeout=6).decode("utf-8", errors="replace")[:160_000]
    except Exception:
        return ""
    meta = _ArticleMetaParser()
    try:
        meta.feed(raw_html)
    except Exception:
        return ""
    desc = _plain_text(
        meta.meta.get("og:description", "")
        or meta.meta.get("twitter:description", "")
        or meta.meta.get("description", ""),
        limit=900,
    )
    if len(desc.split()) >= 8:
        return desc
    # Fallback: extract first substantive paragraph
    paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", raw_html, flags=re.I | re.S)
    for para in paragraphs[:6]:
        clean = _plain_text(para, limit=700)
        if len(clean.split()) >= 14:
            return clean
    return desc


def collect_hacker_news(
    *,
    start: datetime,
    end: datetime,
    limit: int,
    topic_filter: str = "",
    min_points: int = 50,
    use_algolia: bool | None = None,
    enrich_links: bool | None = None,
    opener: Callable[..., Any] = urlopen,
) -> list[SourceItem]:
    if use_algolia is None:
        use_algolia = opener is urlopen
    if enrich_links is None:
        enrich_links = opener is urlopen

    raw_stories: list[dict[str, Any]] = []
    if use_algolia:
        params = urlencode(
            {
                "tags": "story",
                "numericFilters": f"created_at_i>={int(start.timestamp())},created_at_i<={int(end.timestamp())},points>={min_points}",
                "hitsPerPage": min(100, max(limit * 2, 60)),
            }
        )
        payload = json.loads(_request(f"https://hn.algolia.com/api/v1/search?{params}", opener=opener))
        seen_canon: set[str] = set()
        hits = sorted(payload.get("hits", []), key=lambda h: (-int(h.get("points") or 0), -int(h.get("num_comments") or 0)))
        for hit in hits:
            url = str(hit.get("url") or "").strip()
            title = str(hit.get("title") or "").strip()
            if not url or not title:
                continue
            canon = canonical_source_url(url)
            if canon in seen_canon:
                continue
            url_host = urlparse(url).netloc.lower().removeprefix("www.")
            domain_hint = url_host if any(url_host == d or url_host.endswith(f".{d}") for d in PRIMARY_ARTIFACT_HOSTS) else ""
            if not _matches_topic_filter(title, domain_hint, topic_filter):
                continue
            seen_canon.add(canon)
            raw_stories.append(
                {
                    "id": str(hit.get("objectID") or ""),
                    "title": title,
                    "url": url,
                    "canonical_url": canon,
                    "points": int(hit.get("points") or 0),
                    "comments": int(hit.get("num_comments") or 0),
                    "published": datetime.fromtimestamp(int(hit.get("created_at_i") or 0), tz=UTC),
                }
            )
            if len(raw_stories) >= limit:
                break
    else:
        ids = json.loads(_request("https://hacker-news.firebaseio.com/v0/topstories.json", opener=opener))[:limit]
        for story_id in ids:
            raw = json.loads(_request(f"https://hacker-news.firebaseio.com/v0/item/{story_id}.json", opener=opener))
            if raw.get("type") != "story" or not raw.get("url") or not raw.get("title"):
                continue
            if not _matches_topic_filter(str(raw["title"]), "", topic_filter):
                continue
            published = datetime.fromtimestamp(int(raw.get("time", 0)), tz=UTC)
            if not start <= published <= end:
                continue
            url = str(raw["url"])
            raw_stories.append(
                {
                    "id": str(story_id),
                    "title": str(raw["title"]),
                    "url": url,
                    "canonical_url": canonical_source_url(url),
                    "points": int(raw.get("score", 0) or 0),
                    "comments": int(raw.get("descendants", 0) or 0),
                    "published": published,
                }
            )

    summaries_by_id: dict[str, str] = {}
    if enrich_links and raw_stories:
        to_enrich = raw_stories[:18]
        if opener is urlopen and len(to_enrich) > 1:
            with ThreadPoolExecutor(max_workers=8, thread_name_prefix="hn-meta") as pool:
                fetched = list(pool.map(lambda s: (s["id"], _fetch_linked_meta_summary(s["url"], opener=opener)), to_enrich))
            summaries_by_id = dict(fetched)
        else:
            summaries_by_id = {s["id"]: _fetch_linked_meta_summary(s["url"], opener=opener) for s in to_enrich}

    items: list[SourceItem] = []
    for story in raw_stories:
        points = int(story["points"])
        comments = int(story["comments"])
        url = str(story["url"])
        canon_url = str(story["canonical_url"])
        title = str(story["title"])
        extracted = summaries_by_id.get(story["id"], "")
        if extracted:
            summary = f"{extracted} (Hacker News: {points} points, {comments} comments.)"[:1200]
        else:
            summary = f"Hacker News signal: {points} points and {comments} comments."
        authority, category = _classify_hn_target_authority(url, extracted)
        category = _infer_category(category, title, summary)
        adj = _builder_adjustment(title, extracted or title)
        score = min(
            96.0,
            max(
                25.0,
                46.0 + math.log1p(points) * 5.8 + math.log1p(comments) * 1.8 + max(-35.0, adj * 0.5),
            ),
        )
        items.append(
            SourceItem(
                id=f"hn-{story['id']}",
                title=title,
                url=url,
                source="Hacker News",
                published_at=story["published"].isoformat().replace("+00:00", "Z"),
                summary=summary,
                authority=authority,
                organization=urlparse(url).netloc.removeprefix("www."),
                category=category,
                metadata={
                    "canonical_url": canon_url,
                    "source_kind": "community_discussion",
                    "discussion_url": f"https://news.ycombinator.com/item?id={story['id']}",
                    "community_signal": True,
                    "community_signal_type": "hacker_news",
                    "hn_points": points,
                    "hn_comments": comments,
                    "momentum_score": min(
                        4,
                        int(points >= 30)
                        + int(points >= 100)
                        + int(comments >= 50 or points >= 200)
                        + int(points >= 350),
                    ),
                    "score": round(score, 2),
                    "selection_reason": "High-engagement developer-community attention",
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
            canon_url = canonical_source_url(url)
            organization = (paper.get("organization") or {}).get("fullname") or "Research authors"
            upvotes = int(paper.get("upvotes", 0) or 0)
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
                        "canonical_url": canon_url,
                        "source_kind": "daily_paper",
                        "community_signal": True,
                        "community_signal_type": "hugging_face",
                        "hf_upvotes": upvotes,
                        "momentum_score": min(4, int(upvotes >= 10) + int(upvotes >= 25) + int(upvotes >= 50) + int(upvotes >= 100)),
                        "score": min(96, 62 + upvotes * 0.6),
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
            tags_list = [str(tag) for tag in model.get("tags", [])[:8]]
            tags = ", ".join(tags_list)
            likes = int(model.get("likes", 0) or 0)
            downloads = int(model.get("downloads", 0) or 0)
            trending_score = float(model.get("trendingScore", 0) or 0)
            momentum = min(4, int(likes >= 25) + int(likes >= 100) + int(trending_score >= 50) + int(trending_score >= 200))
            items.append(
                SourceItem(
                    id=f"hf-model-{_identifier(model_id)}",
                    title=f"{model_id} open weights release on Hugging Face",
                    url=url,
                    source="Hugging Face model card",
                    published_at=published.isoformat().replace("+00:00", "Z"),
                    summary=(
                        f"Open weights and inference artifacts for {model_id} ({likes} likes, "
                        f"{downloads} downloads); tags: {tags}."
                    ),
                    authority="primary",
                    organization=model_id.split("/", 1)[0],
                    category="models",
                    metadata={
                        "canonical_url": canonical_source_url(url),
                        "source_kind": "model_card",
                        "signal_key": f"hf-model:{model_id}",
                        "community_signal": True,
                        "community_signal_type": "hugging_face",
                        "hf_likes": likes,
                        "hf_downloads": downloads,
                        "hf_trending_score": trending_score,
                        "momentum_score": momentum,
                        "score": min(94, 58 + trending_score / 45),
                        "selection_reason": "New open-weight model with strong Hugging Face trend activity",
                    },
                )
            )
    return items


STOP_WORDS = {
    "about", "after", "adds", "again", "and", "api", "ai", "build", "from", "for",
    "gpt", "introduc", "into", "launches", "more", "new", "over", "release", "releases", "says",
    "that", "the", "their", "this", "using", "with", "your",
}

TOPIC_STOP_WORDS = STOP_WORDS | {
    "agent", "agents", "artificial", "available", "builder", "builders", "company",
    "developer", "developers", "latest", "model", "models", "open", "research",
    "first", "good", "great", "people", "really", "today", "tool", "tools",
    "update", "version", "work", "access", "bring", "brings", "change", "changes",
    "offer", "offers", "provide", "provides", "support", "stronger", "use",
}

PRODUCT_FAMILY_PATTERNS = {
    "ollama": (r"\bollama\b",),
    "vllm": (r"\bvllm\b",),
    "sglang": (r"\bsglang\b", r"github\.com/sgl-project/sglang"),
    "llama.cpp": (r"\bllama\.cpp\b", r"github\.com/ggml-org/llama\.cpp"),
    "qwen": (r"\bqwen(?:\s*\d+(?:\.\d+)*)?\b",),
    "transformers": (r"github\.com/huggingface/transformers",),
}


def _title_tokens(title: str) -> set[str]:
    tokens: set[str] = set()
    for raw in re.findall(r"[a-z0-9][a-z0-9.+-]+", title.casefold()):
        token = re.sub(r"^[^a-z0-9]+|[^a-z0-9]+$", "", raw)
        if token.endswith("ing") and len(token) > 6:
            token = token[:-3]
        elif token.endswith("ed") and len(token) > 5:
            token = token[:-2]
        if len(token) > 2 and token not in STOP_WORDS:
            tokens.add(token)
    return tokens


def _topic_tokens(item: SourceItem) -> set[str]:
    return {
        token
        for token in _title_tokens(f"{item.title} {item.summary}")
        if len(token) > 3 and token not in TOPIC_STOP_WORDS
    }


def _organization_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")


def _item_organizations(item: SourceItem, organizations: dict[str, str]) -> set[str]:
    affiliation = str(item.metadata.get("affiliated_organization") or "").strip()
    found = {_organization_key(affiliation)} if affiliation else set()
    if item.authority == "primary" and item.organization:
        found.add(_organization_key(item.organization))
    text = f"{item.title} {item.summary}".casefold()
    for name, key in organizations.items():
        if re.search(rf"(?<![a-z0-9]){re.escape(name)}(?![a-z0-9])", text):
            found.add(key)
    return {value for value in found if value}


def _without_entity_topics(
    topics: set[str],
    item: SourceItem,
    item_organizations: set[str],
) -> set[str]:
    entity_tokens = {
        token
        for organization in item_organizations
        for token in re.findall(r"[a-z0-9]+", organization)
    }
    entity_tokens.update(
        re.findall(r"[a-z0-9]+", str(item.metadata.get("x_account") or "").casefold())
    )
    return topics - entity_tokens


def _product_family(item: SourceItem) -> str:
    configured = str(item.metadata.get("product_family") or "").strip().casefold()
    if configured:
        return configured
    # Match only title and URL (never summary) so papers mentioning vLLM or Qwen
    # as benchmark baselines do not collapse into product release clusters.
    subject_text = f"{item.title} {item.url}".casefold()
    if re.search(r"\b(?:vs\.?|versus|beat[s]?|over|against|for|on|using|improve[s]?)\s+qwen", item.title.casefold()):
        return ""
    for product_family, patterns in PRODUCT_FAMILY_PATTERNS.items():
        if any(re.search(pattern, subject_text) for pattern in patterns):
            return product_family
    return ""


def assign_story_clusters(
    items: list[SourceItem],
    *,
    organizations: Iterable[str] = (),
) -> list[SourceItem]:
    """Group explicit product families and canonical URLs before headline similarity."""
    ordered = sorted(
        items,
        key=lambda item: (
            {"primary": 0, "independent": 1, "analysis": 2, "signal": 3}[item.authority],
            -float(item.metadata.get("score", 0)),
            item.id,
        ),
    )
    organization_map = {
        name.casefold(): _organization_key(name)
        for name in organizations
        if name.strip()
    }
    organization_labels = {
        _organization_key(name): name
        for name in organizations
        if name.strip()
    }
    representatives: list[tuple[str, str, set[str], set[str], set[str]]] = []
    result: list[SourceItem] = []
    for item in ordered:
        canonical = canonical_source_url(str(item.metadata.get("canonical_url") or item.url))
        tokens = _title_tokens(item.title)
        item_organizations = _item_organizations(item, organization_map)
        non_entity_title_tokens = _without_entity_topics(tokens, item, item_organizations)
        topic_tokens = _without_entity_topics(
            _topic_tokens(item),
            item,
            item_organizations,
        )
        product_family = _product_family(item)
        cluster_id = (
            f"product-{re.sub(r'[^a-z0-9]+', '-', product_family).strip('-')}"
            if product_family
            else ""
        )
        if not cluster_id:
            for existing_id, existing_url, existing_tokens, existing_topics, existing_organizations in representatives:
                overlap = len(tokens & existing_tokens)
                union = len(tokens | existing_tokens) or 1
                shared_topics = topic_tokens & existing_topics
                existing_non_entity = existing_tokens - {
                    tok for org in existing_organizations for tok in re.findall(r"[a-z0-9]+", org)
                }
                meaningful_title_overlap = {
                    token
                    for token in (non_entity_title_tokens & existing_non_entity)
                    if len(token) >= 8
                }
                same_theme = bool(
                    item_organizations & existing_organizations
                    and meaningful_title_overlap
                    and bool(shared_topics)
                )
                if (
                    (canonical and canonical == existing_url)
                    or same_theme
                    or (overlap >= 3 and overlap / union >= 0.6)
                ):
                    cluster_id = existing_id
                    break
        if not cluster_id:
            cluster_id = re.sub(r"[^a-z0-9]+", "-", item.title.casefold()).strip("-")[:80] or item.id
        if not any(existing_id == cluster_id for existing_id, *_ in representatives):
            representatives.append((
                cluster_id,
                canonical,
                tokens,
                topic_tokens,
                item_organizations,
            ))
        metadata = dict(item.metadata)
        metadata["canonical_url"] = canonical or item.url
        metadata["cluster_id"] = cluster_id
        metadata["theme_key"] = cluster_id
        metadata["mentioned_organizations"] = sorted(item_organizations)
        metadata["subject_organizations"] = sorted(
            organization_labels.get(organization, organization)
            for organization in item_organizations
        )
        metadata["topic_tokens"] = sorted(topic_tokens)
        if product_family:
            metadata["product_family"] = product_family
        result.append(replace(item, metadata=metadata))
    return result


def collect_sources(
    config_path: Path,
    *,
    start: datetime,
    end: datetime,
    opener: Callable[..., Any] = urlopen,
) -> CollectionResult:
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
            collected.extend(
                collect_hacker_news(
                    start=start,
                    end=end,
                    limit=int(hn.get("limit", 40)),
                    min_points=int(hn.get("min_points", 50)),
                    use_algolia=hn.get("use_algolia") if opener is urlopen else False,
                    enrich_links=hn.get("enrich_links") if opener is urlopen else False,
                    topic_filter=str(hn.get("topic_filter") or ""),
                    opener=opener,
                )
            )
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
                list(github.get("repositories", [])),
                start=start,
                end=end,
                opener=opener,
            )
        )
    youtube_channels = raw.get("youtube_channels") or []
    if youtube_channels:
        try:
            collected.extend(
                collect_youtube_channels(
                    list(youtube_channels),
                    start=start,
                    end=end,
                    opener=opener,
                )
            )
        except Exception as error:
            log.warning("source failed: YouTube channels: %s", error)
    x_panel = raw.get("x_panel") or {}
    x_health = XPanelHealth(0, 0, 0, 0)
    if x_panel.get("enabled") and x_panel.get("accounts"):
        x_result = collect_x_panel(
            list(x_panel.get("accounts", [])),
            start=start,
            end=end,
            topic_filter=str(x_panel.get("topic_filter") or ""),
        )
        collected.extend(x_result.items)
        x_health = x_result.health
    if not collected:
        raise RuntimeError("all configured sources failed or returned no items in the rolling window")
    configured_organizations = {
        str(definition.get("organization") or definition.get("name") or "")
        for section in ("feeds", "page_indexes", "youtube_channels")
        for definition in raw.get(section, [])
        if isinstance(definition, dict)
    }
    configured_organizations.update(
        str(account.get("organization") or "")
        for account in x_panel.get("accounts", [])
        if isinstance(account, dict)
    )
    return CollectionResult(
        tuple(assign_story_clusters(collected, organizations=configured_organizations)),
        x_health,
    )


def collect_github_momentum(
    repositories: list[Any],
    *,
    start: datetime,
    end: datetime,
    opener: Callable[..., Any] = urlopen,
) -> list[SourceItem]:
    """Collect public GitHub release momentum as primary release artifacts."""
    items: list[SourceItem] = []
    for configured in repositories:
        if isinstance(configured, dict):
            repository = str(configured.get("repository") or "")
            product_family = str(configured.get("product_family") or "")
        else:
            repository = str(configured)
            product_family = ""
        if not repository:
            continue
        try:
            raw = json.loads(_request(f"https://api.github.com/repos/{repository}/releases?per_page=10", opener=opener))
            repo = json.loads(_request(f"https://api.github.com/repos/{repository}", opener=opener))
        except Exception as error:
            log.warning("source failed: GitHub %s: %s", repository, error)
            continue
        for release in raw:
            published = _parse_datetime(str(release.get("published_at") or release.get("created_at") or ""))
            if published is None or not start <= published <= end or release.get("draft") or release.get("prerelease"):
                continue
            tag = str(release.get("tag_name") or "").strip()
            title = str(release.get("name") or tag or repository).strip()
            body = _plain_text(str(release.get("body") or ""), limit=1200)
            stars = int(repo.get("stargazers_count", 0) or 0)
            forks = int(repo.get("forks_count", 0) or 0)
            issues = int(repo.get("open_issues_count", 0) or 0)
            momentum_score = min(4, max(0, round(stars / 25_000 + forks / 10_000)))
            html_url = str(release.get("html_url") or f"https://github.com/{repository}/releases")
            canon_url = canonical_source_url(html_url)
            items.append(
                SourceItem(
                    id=f"github-{_identifier(canon_url)}",
                    title=f"{repository} {title}",
                    url=html_url,
                    source="GitHub releases",
                    published_at=published.isoformat().replace("+00:00", "Z"),
                    summary=body or f"GitHub release {tag} for {repository}.",
                    authority="primary",
                    organization=repository.split("/", 1)[0],
                    category="developer tools",
                    metadata={
                        "canonical_url": canon_url,
                        "source_kind": "release_feed",
                        "signal_key": f"github:{repository}",
                        "community_signal_candidate": True,
                        "community_signal_type": "github",
                        "momentum": True,
                        "momentum_score": momentum_score,
                        "repository_stars": stars,
                        "repository_forks": forks,
                        "repository_open_issues": issues,
                        "score": 60 + momentum_score * 4,
                        **({"product_family": product_family} if product_family else {}),
                    },
                )
            )
    return items


_SKIP_DESCRIPTION_HOSTS = {
    "youtube.com", "youtu.be", "x.com", "twitter.com", "linkedin.com", "instagram.com",
    "tiktok.com", "facebook.com", "discord.gg", "discord.com", "t.co", "bit.ly",
    "open.spotify.com", "podcasts.apple.com", "lu.ma",
}
_SKIP_RESOLVED_X_HOSTS = {
    "x.com", "twitter.com", "pic.x.com", "pbs.twimg.com", "video.twimg.com",
    "t.co", "instagram.com", "tiktok.com", "facebook.com",
}


def _clean_vtt_transcript(raw_vtt: str, *, max_chars: int = 40_000) -> str:
    """Strip WebVTT headers, timestamps, tags, and rolling caption duplicates into clean prose."""
    lines: list[str] = []
    last = ""
    for raw_line in raw_vtt.splitlines():
        line = raw_line.strip()
        if (
            not line
            or line.startswith("WEBVTT")
            or line.startswith("Kind:")
            or line.startswith("Language:")
            or line.startswith("NOTE")
            or "-->" in line
            or line.isdigit()
        ):
            continue
        cleaned = re.sub(r"<[^>]+>", "", line)
        cleaned = html.unescape(re.sub(r"\s+", " ", cleaned)).strip()
        if not cleaned or cleaned == last:
            continue
        if last and cleaned.startswith(last):
            lines[-1] = cleaned
        else:
            lines.append(cleaned)
        last = cleaned
        if sum(len(part) + 1 for part in lines) >= max_chars:
            break
    return " ".join(lines)[:max_chars].strip()


def _extract_description_artifact_urls(description: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r"https?://[^\s\)\]>\"']+", description or ""):
        raw_url = match.group(0).rstrip(".,;:!)")
        parsed = urlparse(raw_url)
        host = parsed.netloc.lower().removeprefix("www.")
        if not host or any(host == skip or host.endswith(f".{skip}") for skip in _SKIP_DESCRIPTION_HOSTS):
            continue
        canon = canonical_source_url(raw_url)
        if canon and canon not in seen:
            seen.add(canon)
            urls.append(raw_url)
        if len(urls) >= 6:
            break
    return urls


def _fetch_youtube_transcript_fast(
    video_id: str,
    *,
    timeout_seconds: int = 15,
    max_transcript_chars: int = 40_000,
) -> str:
    """Fetch clean, deduplicated English/Chinese transcript via youtube_transcript_api (~1.5s, avoids yt-dlp 429)."""
    executable = shutil.which("youtube_transcript_api")
    if not executable or not video_id:
        return ""
    try:
        proc = subprocess.run(
            [executable, "--languages", "en", "en-US", "en-GB", "zh-Hans", "zh", "--format", "text", "--", video_id],
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        cleaned = re.sub(r"\s+", " ", proc.stdout).strip()
        return cleaned[:max_transcript_chars]
    except Exception as error:
        log.warning("youtube_transcript_api failed for %s: %s", video_id, error)
        return ""


def fetch_youtube_transcript_and_meta(
    video_url: str,
    *,
    include_transcript: bool = True,
    timeout_seconds: int = 25,
    max_transcript_chars: int = 40_000,
) -> dict[str, Any]:
    """Fetch YouTube metadata/links (yt-dlp --skip-download) and clean transcript (youtube_transcript_api first)."""
    canonical = canonical_source_url(video_url) or video_url
    vid = extract_youtube_video_id(canonical)
    executable = shutil.which("yt-dlp")
    info: dict[str, Any] = {}
    if executable:
        try:
            meta_proc = subprocess.run(
                [executable, "--dump-single-json", "--skip-download", "--no-warnings", canonical],
                check=True,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
            info = json.loads(meta_proc.stdout)
        except Exception as error:
            log.warning("yt-dlp metadata fetch failed for %s: %s", canonical, error)

    vid = str(info.get("id") or vid)
    description = str(info.get("description") or "").strip()
    linked_urls = _extract_description_artifact_urls(description)
    transcript_text = ""
    if include_transcript and vid:
        transcript_text = _fetch_youtube_transcript_fast(
            vid,
            timeout_seconds=min(15, timeout_seconds),
            max_transcript_chars=max_transcript_chars,
        )
    has_en_subs = bool(
        (info.get("subtitles") or {}).get("en")
        or (info.get("automatic_captions") or {}).get("en")
        or any(k.startswith("en-") for k in (info.get("subtitles") or {}))
        or any(k.startswith("en-") for k in (info.get("automatic_captions") or {}))
    )
    if include_transcript and not transcript_text and executable and has_en_subs:
        try:
            with tempfile.TemporaryDirectory(prefix="abb-yt-") as tmpdir:
                out_tpl = str(Path(tmpdir) / "video.%(ext)s")
                subprocess.run(
                    [
                        executable,
                        "--skip-download",
                        "--no-warnings",
                        "--write-sub",
                        "--write-auto-sub",
                        "--sub-lang",
                        "en.*",
                        "--sub-format",
                        "vtt",
                        "-o",
                        out_tpl,
                        canonical,
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                )
                vtt_files = sorted(Path(tmpdir).glob("*.vtt"))
                if vtt_files:
                    transcript_text = _clean_vtt_transcript(
                        vtt_files[0].read_text(encoding="utf-8", errors="replace"),
                        max_chars=max_transcript_chars,
                    )
        except Exception as error:
            log.warning("yt-dlp caption fallback failed for %s: %s", canonical, error)

    if not info and not transcript_text:
        return {}

    upload_raw = str(info.get("upload_date") or "").strip()
    published_at = ""
    if len(upload_raw) == 8 and upload_raw.isdigit():
        published_at = f"{upload_raw[:4]}-{upload_raw[4:6]}-{upload_raw[6:8]}T12:00:00Z"

    return {
        "video_id": vid,
        "title": str(info.get("title") or "").strip(),
        "channel": str(info.get("channel") or info.get("uploader") or "").strip(),
        "published_at": published_at,
        "view_count": int(info.get("view_count") or 0),
        "like_count": int(info.get("like_count") or 0),
        "comment_count": int(info.get("comment_count") or 0),
        "duration": int(info.get("duration") or 0),
        "description": description,
        "linked_urls": linked_urls,
        "transcript_text": transcript_text,
    }


def collect_youtube_channels(
    channels: list[Any],
    *,
    start: datetime,
    end: datetime,
    opener: Callable[..., Any] = urlopen,
    enrich_videos: bool | None = None,
    video_enricher: Callable[[str], dict[str, Any]] | None = None,
) -> list[SourceItem]:
    """Collect high-signal AI builder YouTube talks via zero-API-key Atom RSS + velocity filter."""
    if enrich_videos is None:
        enrich_videos = opener is urlopen
    if video_enricher is None:
        video_enricher = fetch_youtube_transcript_and_meta

    ns = {
        "a": "http://www.w3.org/2005/Atom",
        "yt": "http://www.youtube.com/xml/schemas/2015",
        "media": "http://search.yahoo.com/mrss/",
    }
    candidates: list[dict[str, Any]] = []
    for cfg in channels:
        if not isinstance(cfg, dict) or not cfg.get("channel_id"):
            continue
        channel_id = str(cfg["channel_id"]).strip()
        channel_name = str(cfg.get("name") or channel_id).strip()
        feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        try:
            root = ET.fromstring(_request(feed_url, opener=opener, timeout=15))
        except Exception as error:
            log.warning("source failed: YouTube %s: %s", channel_name, error)
            continue

        entries = root.findall("a:entry", ns)
        all_views: list[int] = []
        parsed_entries: list[dict[str, Any]] = []
        for entry in entries:
            vid = (entry.findtext("yt:videoId", "", ns) or "").strip()
            title = _plain_text(entry.findtext("a:title", "", ns) or "", limit=300)
            published = _parse_datetime(entry.findtext("a:published", "", ns) or "")
            desc = _plain_text(entry.findtext("media:group/media:description", "", ns) or "", limit=900)
            stat_el = entry.find("media:group/media:community/media:statistics", ns)
            views = int(stat_el.get("views", "0") or 0) if stat_el is not None else 0
            if not vid or not title or published is None:
                continue
            if views > 0:
                all_views.append(views)
            parsed_entries.append({
                "video_id": vid,
                "title": title,
                "published": published,
                "description": desc,
                "views": views,
            })

        median_views = int(statistics.median(all_views)) if all_views else 0
        min_views = int(cfg.get("min_views", 10_000))
        breakout_views = int(cfg.get("breakout_views", max(50_000, min_views * 3)))
        breakout_days = int(cfg.get("breakout_days", 7))
        breakout_start = end - timedelta(days=breakout_days)
        topic_filter = str(cfg.get("topic_filter") or "")
        title_only_topic = bool(cfg.get("title_topic_filter"))

        for entry in parsed_entries:
            published = entry["published"]
            views = entry["views"]
            in_primary_window = start <= published <= end
            in_breakout_window = (
                breakout_start <= published < start
                and views >= breakout_views
            )
            if not (in_primary_window or in_breakout_window):
                continue
            if re.search(r"\b(?:the\s+making\s+of|company\s+documentary|official\s+commercial)\b", entry["title"], re.I):
                continue
            if not _matches_topic_filter(
                entry["title"],
                "" if title_only_topic else entry["description"],
                topic_filter,
            ):
                continue
            if _builder_adjustment(entry["title"], entry["description"]) <= -35:
                continue
            age_hours = max(6.0, (end - published).total_seconds() / 3600.0)
            views_per_hour = views / age_hours
            relative_lift = views / max(1_000.0, float(median_views)) if median_views else 1.0
            floor_views = max(3_000, min_views // 3)
            if not (
                views >= min_views
                or (relative_lift >= 2.0 and views >= floor_views)
                or (bool(cfg.get("always_include_in_window")) and in_primary_window)
            ):
                continue
            candidates.append({
                **entry,
                "cfg": cfg,
                "channel_name": channel_name,
                "views_per_hour": views_per_hour,
                "relative_lift": relative_lift,
                "breakout": in_breakout_window,
            })

    candidates.sort(key=lambda c: (-c["views_per_hour"], -c["views"], c["video_id"]))
    enriched_by_id: dict[str, dict[str, Any]] = {}
    if enrich_videos and candidates:
        to_enrich = candidates[:6]
        if video_enricher is fetch_youtube_transcript_and_meta and len(to_enrich) > 1:
            with ThreadPoolExecutor(max_workers=4, thread_name_prefix="yt-meta") as pool:
                fetched = list(
                    pool.map(
                        lambda c: (c["video_id"], video_enricher(f"https://www.youtube.com/watch?v={c['video_id']}")),
                        to_enrich,
                    )
                )
            enriched_by_id = dict(fetched)
        else:
            enriched_by_id = {
                c["video_id"]: video_enricher(f"https://www.youtube.com/watch?v={c['video_id']}")
                for c in to_enrich
            }

    items: list[SourceItem] = []
    for cand in candidates:
        cfg = cand["cfg"]
        vid = cand["video_id"]
        meta = enriched_by_id.get(vid) or {}
        duration = int(meta.get("duration") or 0)
        min_duration = int(cfg.get("min_duration_seconds", 180))
        if duration > 0 and duration < min_duration and not re.search(r"\b(?:introducing|launch|release|v\d+\.\d+|api|sdk|mcp)\b", cand["title"], re.I):
            continue
        views = max(cand["views"], int(meta.get("view_count") or 0))
        likes = int(meta.get("like_count") or 0)
        linked_urls = list(meta.get("linked_urls") or _extract_description_artifact_urls(cand["description"]))
        transcript = str(meta.get("transcript_text") or "").strip()
        url = f"https://www.youtube.com/watch?v={vid}"
        dur_label = f"{duration // 60}m" if duration >= 60 else ""
        summary_parts = [cand["description"][:420]] if cand["description"] else []
        if transcript:
            summary_parts.append(f"Transcript excerpt: {transcript[:520]}")
        if linked_urls:
            summary_parts.append(f"Linked artifacts: {', '.join(linked_urls[:3])}")
        stats_tail = f"YouTube ({cand['channel_name']}): {views:,} views"
        if likes:
            stats_tail += f", {likes:,} likes"
        if dur_label:
            stats_tail += f", {dur_label}"
        summary_parts.append(stats_tail + ".")
        summary = " ".join(p for p in summary_parts if p)[:1200]

        momentum = min(
            4,
            int(views >= 10_000)
            + int(views >= 40_000 or cand["views_per_hour"] >= 800)
            + int(views >= 100_000 or cand["views_per_hour"] >= 2_500)
            + int(views >= 250_000),
        )
        base_score = float(cfg.get("score", 82))
        score = min(96.0, base_score + math.log1p(max(1, views)) * 1.1 + (4.0 if linked_urls else 0.0))
        items.append(
            SourceItem(
                id=f"yt-{vid}",
                title=cand["title"],
                url=url,
                source=f"YouTube ({cand['channel_name']})",
                published_at=cand["published"].isoformat().replace("+00:00", "Z"),
                summary=summary,
                authority=str(cfg.get("authority", "independent")),
                organization=str(cfg.get("organization") or cand["channel_name"]),
                category=str(cfg.get("category", "developer tools")),
                metadata={
                    "canonical_url": url,
                    "source_kind": "builder_video",
                    "community_signal": True,
                    "community_signal_type": "youtube",
                    "youtube_video_id": vid,
                    "youtube_channel": cand["channel_name"],
                    "youtube_views": views,
                    "youtube_likes": likes,
                    "youtube_duration_s": duration,
                    "youtube_views_per_hour": round(cand["views_per_hour"], 1),
                    "linked_urls": linked_urls,
                    "transcript_excerpt": transcript[:1200] if transcript else "",
                    "momentum_score": momentum,
                    "score": round(score, 2),
                    "selection_reason": (
                        "Breakout high-velocity builder talk on YouTube"
                        if cand["breakout"]
                        else "High-signal builder talk or demo on YouTube"
                    ),
                },
            )
        )
    return items


def _resolve_redirect_url(raw_url: str, *, timeout: int = 6) -> str:
    """Resolve t.co or short redirect URLs to their canonical destination without reading body."""
    request = Request(
        raw_url,
        method="HEAD",
        headers={"User-Agent": USER_AGENT},
    )
    try:
        with urlopen(request, timeout=timeout, context=SSL_CONTEXT) as resp:
            return str(resp.geturl() or raw_url)
    except Exception:
        try:
            get_req = Request(raw_url, headers={"User-Agent": USER_AGENT})
            with urlopen(get_req, timeout=timeout, context=SSL_CONTEXT) as resp:
                return str(resp.geturl() or raw_url)
        except Exception:
            return ""


def collect_x_panel(
    accounts: list[Any],
    *,
    start: datetime,
    end: datetime,
    topic_filter: str = "",
    fetcher: Callable[[str], list[dict[str, Any]]] | None = None,
    resolve_links: bool | None = None,
    url_resolver: Callable[[str], str] | None = None,
    video_enricher: Callable[[str], dict[str, Any]] | None = None,
    meta_fetcher: Callable[[str], str] | None = None,
) -> XPanelResult:
    """Collect approved-account observations with bounded retries and builder recommendation expansion."""
    if fetcher is None:
        fetcher = _twitter_cli_fetcher
    if resolve_links is None:
        resolve_links = fetcher is _twitter_cli_fetcher
    if url_resolver is None:
        url_resolver = _resolve_redirect_url
    if video_enricher is None:
        video_enricher = fetch_youtube_transcript_and_meta
    if meta_fetcher is None:
        meta_fetcher = _fetch_linked_meta_summary

    items: list[SourceItem] = []
    successful_accounts = 0
    failed_accounts: list[str] = []
    normalized_accounts: list[tuple[str, str, bool, str]] = []
    for configured in accounts:
        if isinstance(configured, dict):
            account = str(configured.get("account") or "").lstrip("@").strip()
            organization = str(configured.get("organization") or account).strip()
            required = bool(configured.get("required", True))
            tier = str(configured.get("tier") or "").strip()
        else:
            account = str(configured).lstrip("@").strip()
            organization = account
            required = True
            tier = ""
        if account:
            normalized_accounts.append((account, organization, required, tier))
    required_configured = sum(required for _, _, required, _ in normalized_accounts)
    required_successful = 0

    def fetch_account(entry: tuple[str, str, bool, str]) -> tuple[str, str, bool, str, list[dict[str, Any]] | None]:
        account, organization, required, tier = entry
        posts: list[dict[str, Any]] | None = None
        for attempt in range(2):
            try:
                if fetcher is _twitter_cli_fetcher:
                    payload = _twitter_cli_fetcher(account, timeout_seconds=30)
                else:
                    payload = fetcher(account)
                if not isinstance(payload, list):
                    raise ValueError("X account payload is not a list")
                posts = payload
                break
            except Exception:
                if attempt == 1:
                    log.warning("X panel account failed after retry: %s", account)
        return account, organization, required, tier, posts

    if fetcher is _twitter_cli_fetcher:
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="x-panel") as pool:
            fetched = list(pool.map(fetch_account, normalized_accounts))
    else:
        fetched = [fetch_account(entry) for entry in normalized_accounts]

    outbound_candidates: list[dict[str, Any]] = []
    for account, organization, required, tier, posts in fetched:
        if posts is None:
            failed_accounts.append(account)
            continue
        successful_accounts += 1
        if required:
            required_successful += 1
        for post in posts:
            published = _parse_datetime(str(post.get("published_at") or ""))
            if published is None:
                raw_time = str(post.get("published_at") or "").strip()
                try:
                    published = datetime.strptime(f"{end.year} {raw_time}", "%Y %b %d %H:%M").replace(tzinfo=UTC)
                except ValueError:
                    published = None
            if published is None or not start <= published <= end:
                continue
            url = str(post.get("url") or "").strip()
            text = _plain_text(str(post.get("text") or ""), limit=1200)
            if not url or not text:
                continue
            likes = max(0, int(post.get("likes", 0) or 0))
            retweets = max(0, int(post.get("retweets", post.get("rts", 0)) or 0))
            engagement = likes + 2 * retweets
            raw_links = re.findall(r"https?://[^\s\)\]>\"']+", text)
            if resolve_links and raw_links and (
                likes >= 250
                or retweets >= 40
                or (tier == "hands_on_builder" and likes >= 100)
            ):
                for raw_link in raw_links[:2]:
                    outbound_candidates.append({
                        "raw_url": raw_link.rstrip(".,;:!)"),
                        "account": account,
                        "tier": tier,
                        "post_url": url,
                        "text": text,
                        "published": published,
                        "likes": likes,
                        "retweets": retweets,
                        "engagement": engagement,
                    })
            if not _matches_topic_filter(text, "", topic_filter):
                continue
            momentum_score = min(
                4,
                int(engagement >= 10)
                + int(engagement >= 100)
                + int(engagement >= 500)
                + int(engagement >= 2_000),
            )
            items.append(
                SourceItem(
                    id=f"x-{_identifier(url)}",
                    title=f"@{account}: {text[:100]}",
                    url=url,
                    source="Approved X panel",
                    published_at=published.isoformat().replace("+00:00", "Z"),
                    summary=text,
                    authority="analysis",
                    organization=organization,
                    category="expert analysis",
                    metadata={
                        "canonical_url": url,
                        "source_kind": "x_post",
                        "affiliated_organization": organization,
                        "kind": "expert_analysis",
                        "community_signal": True,
                        "community_signal_type": "x",
                        "x_account": account,
                        **({"x_tier": tier} if tier else {}),
                        "x_likes": likes,
                        "x_retweets": retweets,
                        "x_engagement": engagement,
                        "momentum_score": momentum_score,
                        "score": 35 + momentum_score * 8 + (4 if tier == "hands_on_builder" else 0),
                    },
                )
            )

    if resolve_links and outbound_candidates:
        outbound_candidates.sort(key=lambda c: (-c["engagement"], c["raw_url"]))
        unique_raw = list(dict.fromkeys(c["raw_url"] for c in outbound_candidates))[:20]
        if url_resolver is _resolve_redirect_url and len(unique_raw) > 1:
            with ThreadPoolExecutor(max_workers=6, thread_name_prefix="x-link") as pool:
                resolved_map = dict(pool.map(lambda u: (u, url_resolver(u)), unique_raw))
        else:
            resolved_map = {u: url_resolver(u) for u in unique_raw}

        grouped_targets: dict[str, dict[str, Any]] = {}
        for cand in outbound_candidates:
            dest = resolved_map.get(cand["raw_url"], "")
            if not dest:
                continue
            dest_host = urlparse(dest).netloc.lower().removeprefix("www.")
            if not dest_host or any(dest_host == s or dest_host.endswith(f".{s}") for s in _SKIP_RESOLVED_X_HOSTS):
                continue
            if re.search(r"\.(?:png|jpe?g|gif|webp|mp4)(?:\?|$)", dest, re.I):
                continue
            canon = canonical_source_url(dest)
            if not canon:
                continue
            bucket = grouped_targets.setdefault(canon, {
                "url": canon,
                "recommenders": [],
                "likes": 0,
                "retweets": 0,
                "engagement": 0,
                "published": cand["published"],
                "contexts": [],
            })
            handle = f"@{cand['account']}"
            if handle not in bucket["recommenders"]:
                bucket["recommenders"].append(handle)
            bucket["likes"] += cand["likes"]
            bucket["retweets"] += cand["retweets"]
            bucket["engagement"] += cand["engagement"]
            if cand["published"] > bucket["published"]:
                bucket["published"] = cand["published"]
            bucket["contexts"].append(f"{handle}: {cand['text'][:180]}")

        for canon, info in sorted(grouped_targets.items(), key=lambda kv: (-kv[1]["engagement"], kv[0]))[:10]:
            yt_id = extract_youtube_video_id(canon)
            rec_label = ", ".join(info["recommenders"][:4])
            if yt_id:
                meta = video_enricher(canon) or {}
                title = str(meta.get("title") or f"Builder-recommended YouTube video ({yt_id})").strip()
                channel = str(meta.get("channel") or "YouTube").strip()
                views = int(meta.get("view_count") or 0)
                duration = int(meta.get("duration") or 0)
                linked_urls = list(meta.get("linked_urls") or [])
                transcript = str(meta.get("transcript_text") or "").strip()
                desc = _plain_text(str(meta.get("description") or ""), limit=420)
                if not _matches_topic_filter(f"{title} {desc} {' '.join(info['contexts'])}", "", topic_filter):
                    continue
                summary_bits = [f"Recommended by {rec_label} ({info['likes']:,} X likes)."]
                if desc:
                    summary_bits.append(desc)
                if transcript:
                    summary_bits.append(f"Transcript excerpt: {transcript[:480]}")
                if linked_urls:
                    summary_bits.append(f"Linked artifacts: {', '.join(linked_urls[:3])}")
                if views:
                    summary_bits.append(f"YouTube ({channel}): {views:,} views.")
                momentum = min(4, 2 + int(info["engagement"] >= 1_000 or views >= 50_000) + int(len(info["recommenders"]) >= 2 or views >= 150_000))
                score = min(96.0, 82.0 + math.log1p(max(1, info["engagement"])) * 1.4 + len(info["recommenders"]) * 3.0)
                items.append(
                    SourceItem(
                        id=f"yt-{yt_id}",
                        title=title,
                        url=canon,
                        source=f"Builder-recommended YouTube ({channel})",
                        published_at=info["published"].isoformat().replace("+00:00", "Z"),
                        summary=" ".join(summary_bits)[:1200],
                        authority="independent",
                        organization=channel,
                        category="developer tools",
                        metadata={
                            "canonical_url": canon,
                            "source_kind": "builder_video",
                            "community_signal": True,
                            "community_signal_type": "youtube",
                            "youtube_video_id": yt_id,
                            "youtube_channel": channel,
                            "youtube_views": views,
                            "youtube_duration_s": duration,
                            "linked_urls": linked_urls,
                            "transcript_excerpt": transcript[:1200] if transcript else "",
                            "recommended_by": info["recommenders"],
                            "x_likes": info["likes"],
                            "x_retweets": info["retweets"],
                            "x_engagement": info["engagement"],
                            "momentum_score": momentum,
                            "score": round(score, 2),
                            "selection_reason": f"YouTube talk recommended by {rec_label}",
                        },
                    )
                )
            else:
                extracted = meta_fetcher(canon) or ""
                authority, category = _classify_hn_target_authority(canon, extracted)
                if authority == "signal":
                    continue
                context_blob = " ".join(info["contexts"])
                if not _matches_topic_filter(f"{extracted} {context_blob}", urlparse(canon).netloc, topic_filter):
                    continue
                if _builder_adjustment(extracted or context_blob, context_blob) <= -35:
                    continue
                title_seed = extracted.split(".", 1)[0].strip()[:110] if extracted else info["contexts"][0][:110]
                summary = (
                    f"Recommended by {rec_label} ({info['likes']:,} X likes). "
                    f"{extracted or context_blob}"
                )[:1200]
                momentum = min(4, 1 + int(info["engagement"] >= 500) + int(info["engagement"] >= 2_000) + int(len(info["recommenders"]) >= 2))
                score = min(94.0, 74.0 + math.log1p(max(1, info["engagement"])) * 1.5 + len(info["recommenders"]) * 4.0)
                items.append(
                    SourceItem(
                        id=f"xrec-{_identifier(canon)}",
                        title=title_seed,
                        url=canon,
                        source="Builder-recommended artifact",
                        published_at=info["published"].isoformat().replace("+00:00", "Z"),
                        summary=summary,
                        authority=authority,
                        organization=urlparse(canon).netloc.removeprefix("www."),
                        category=_infer_category(category, title_seed, summary),
                        metadata={
                            "canonical_url": canon,
                            "source_kind": "builder_recommendation",
                            "community_signal": True,
                            "community_signal_type": "x",
                            "recommended_by": info["recommenders"],
                            "x_likes": info["likes"],
                            "x_retweets": info["retweets"],
                            "x_engagement": info["engagement"],
                            "momentum_score": momentum,
                            "score": round(score, 2),
                            "selection_reason": f"Technical artifact recommended by {rec_label}",
                        },
                    )
                )

    return XPanelResult(
        items=tuple(items),
        health=XPanelHealth(
            configured_accounts=len(normalized_accounts),
            attempted_accounts=len(normalized_accounts),
            successful_accounts=successful_accounts,
            in_window_posts=len(items),
            failed_accounts=tuple(failed_accounts),
            required_configured_accounts=required_configured,
            required_successful_accounts=required_successful,
        ),
    )


def _twitter_cli_fetcher(account: str, *, timeout_seconds: int = 45) -> list[dict[str, Any]]:
    """Read approved-account posts through the installed read-only X CLI."""
    executable = shutil.which("twitter")
    if not executable:
        raise RuntimeError("twitter CLI is not installed")
    result = subprocess.run(
        [executable, "--compact", "user-posts", account, "--max", "20", "--json"],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
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
        posts.append(
            {
                "published_at": raw_time,
                "url": f"https://x.com/{account}/status/{tweet_id}",
                "text": str(post.get("text") or ""),
                "likes": int(post.get("likes", 0) or 0),
                "retweets": int(post.get("rts", post.get("retweets", 0)) or 0),
            }
        )
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
