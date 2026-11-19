"""Deterministic evaluation and benchmark harness for AI Builder Brief.

Scores both written editions (`history/YYYY-MM-DD.md`) and collector/editorial
pipeline candidate selections (`SourceItem` shortlist + watchlist) on three axes:
1. Must-catch builder recall (against Algolia HN high-engagement AI builder ground truth)
2. Signal-to-noise & deduplication (penalizing PR/funding/IPO fluff, repeats, org domination)
3. Editorial specificity & anti-slop quality (concrete metrics, primary artifacts, takeaways, zero filler)
"""

from __future__ import annotations

import gzip
import json
import math
import re
import ssl
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

import certifi
from castforge.models import SourceItem

from ai_builder_brief.journal import _parse_ranked_items

SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
USER_AGENT = "AIBuilderBriefBenchmark/1.0 (+https://github.com/lifan-builds/ai-builder-brief)"

STRICT_AI_BUILDER_RE = re.compile(
    r"(?:"
    r"\b(?:llm|llms|gpt-\d[\w.-]*|chatgpt|claude(?:\s+code)?|anthropic|openai|deepmind|gemini\s*\d*|qwen\d*|deepseek[\w-]*|mistral|mixtral|llama(?:\.cpp|\s*\d)|kimi\s*k?\d*|minimax|grok)\b|"
    r"\b(?:vllm|sglang|ollama|ggml|gguf|mlx(?:-lm)?|huggingface|hugging\s+face|openrouter|litellm|langchain|llamaindex|pydantic-?ai|cursor|codex|aider|cline|opencode|swe-bench)\b|"
    r"\b(?:ai\s+agents?|coding\s+agents?|agentic|multi-agent|agent\s+skills?|mcp\s+servers?|model\s+context\s+protocol)\b|"
    r"\b(?:machine\s+learning|deep\s+learning|neural\s+networks?|transformers?|diffusion\s+models?|language\s+models?|open-?weights?|model\s+weights?|safetensors)\b|"
    r"\b(?:fine-tun(?:e|ing)|lora|qlora|rlhf|grpo|quantiz(?:e|ation)|kv[- ]cache|speculative\s+decoding|flashattention|cuda|triton|pytorch|jax|npu|tpu|blackwell|b200|h100)\b|"
    r"\b(?:prompt\s+injection|(?:llm|ai|model|prompt)\s+jailbreak\w*|hallucinat\w*|rag\b|embeddings?|vector\s+search|ai[- ]generated|ai\s+coding|ai\s+benchmarks?|ai\s+evals?|ai\s+safety)\b"
    r")",
    re.IGNORECASE,
)

NON_AI_FALSE_POSITIVE_RE = re.compile(
    r"\b(?:ice\s+agent|fbi\s+agent|border\s+agent|real\s+estate\s+agent|secret\s+agent|user-agent|tesla\s+model|car's\s+navigation|iphone\s+jailbreak|ps[45]\s+jailbreak)\b",
    re.IGNORECASE,
)

PR_FLUFF_HEADLINE_RE = re.compile(
    r"(?:"
    r"\b(?:raises\s+\$?\d|series\s+[a-f]\b|seed\s+round|valuation\s+of|targets\s+\$\d[\w.]*\s+in\s+(?:annual\s+)?revenue)\b|"
    r"\b(?:ipo\s+filing|files\s+s-1|amended\s+filing|initial\s+public\s+offering|s-1\b)\b|"
    r"\b(?:memorandum\s+of\s+understanding|\bmou\b|pitches\s+federal|equity\s+fund|public-benefit\s+fund|biosecurity\s+lab\s+pledge)\b|"
    r"\b(?:replaces\s+.+\s+as\s+(?:interim\s+)?ceo|interim\s+ceo|steps\s+down\s+as\s+ceo|board\s+appoints)\b|"
    r"\b(?:purchasing\s+channel|techcrunch\s+disrupt|\$\d+\s+deal\s+for\s+your|local\s+voters\s+back|ballot\s+returns)\b|"
    r"\b(?:chatgpt\s+wallet|shopping\s+rollout|external\s+trainium\s+sales\s+as\s+ai\s+spending)\b"
    r")",
    re.IGNORECASE,
)

VAPOURWARE_BODY_RE = re.compile(
    r"\b(?:waitlist\s+only|contact\s+sales|account-team-gated|months\s+away|planned\s+for\s+late\s+202\d|no\s+api,\s*weights,\s*or\s*eval)\b",
    re.IGNORECASE,
)

TECHNICAL_ARTIFACT_OVERRIDE_RE = re.compile(
    r"(?:github\.com/[^/\s]+/[^/\s]+|arxiv\.org/(?:abs|html|pdf)/\d{4}\.\d{4,5}|huggingface\.co/[^/\s]+/[^/\s]+|\bapache[- ]2\.0\b|\bmit\s+license\b|\bsafetensors\b|\bpip\s+install\b|\bcargo\s+install\b|\bnpm\s+install\b)",
    re.IGNORECASE,
)

WEAK_DISCOVERY_URL_RE = re.compile(
    r"^(?:news\.ycombinator\.com|github\.com/trending|x\.com|twitter\.com|reddit\.com|news\.google\.com|openrouter\.ai/rankings|latent\.space/p/ainews-|[^/]+/?$)",
    re.IGNORECASE,
)

QUANT_METRIC_RE = re.compile(
    r"(?:"
    r"\b\d+(?:\.\d+)?\s*(?:%|×|(?:x|pp|ms|s|gb|gib|tb|mb|mib|tok/s|tokens/s|mtok|ktok|fps|ghz|tflops)\b)|"
    r"\$\d+(?:\.\d+)?(?:\s*/\s*(?:m|million|1m|hr|hour|sec|second))?|"
    r"\bv\d+\.\d+(?:\.\d+)?\b|"
    r"\b\d+(?:\.\d+)?[bmk]\s+(?:parameters?|tokens?|active|context|weights?)\b|"
    r"\b\d+/\d+\b"
    r")",
    re.IGNORECASE,
)

ACTION_TAKEAWAY_RE = re.compile(
    r"\b(?:test|benchmark|audit|pin|upgrade|migrate|verify|measure|evaluate|check|configure|enable|restrict|isolate|separate|require|treat|compare|rerun|track|replace|inspect|use|build|monitor|reconsider)\b",
    re.IGNORECASE,
)

CAVEAT_MARKER_RE = re.compile(
    r"\b(?:however|but|caveat|limit\w*|unverified|unproven|untested|remains?\s+(?:unverified|private|planned|internal|limited|uncertain|untested|unchanged)|does\s+not\s+establish|not\s+(?:yet\s+)?(?:general\s+availability|proof|replicated|independent)|author-run|vendor-reported|self-reported|preview\s+only|bounded\s+tasks?)\b",
    re.IGNORECASE,
)

SLOP_CLICHE_RE = re.compile(
    r"\b(?:delve\w*|game-?chang\w*|paradigm\s+shift|testament\s+to|revolutioniz\w*|unprecedented|watershed\s+moment|seismic\s+shift|tapestry|synergy|unlocking\s+the\s+future)\b",
    re.IGNORECASE,
)

MADLIB_LEDE_RE = re.compile(
    r"^the\s+(?:strongest|most\s+useful|most\s+consequential|clearest)\s+(?:technical\s+|new\s+)?(?:developments?|advances?|work|stories)\s+(?:concern|show|focus|center)\b",
    re.IGNORECASE,
)

REPETITIVE_BOILERPLATE_PHRASES = (
    "author-run",
    "without verified independent replication",
    "not an inferred popularity trend",
    "total stars do not establish",
    "vendor-reported",
)

BUILDER_ARTIFACT_DOMAINS = (
    "github.com/",
    "arxiv.org/",
    "huggingface.co/",
    "simonwillison.net/",
    "claude.dev/",
    "anthropic.com/research",
    "anthropic.com/engineering",
    "openai.com/index/",
    "deploymentsafety.openai.com",
    "alignment.openai.com",
    "deepmind.google/",
    "research.google/",
    "blog.cloudflare.com/",
    "together.ai/blog",
    "epoch.ai/",
    "metr.org/",
    "apolloresearch.ai/",
)

MATCH_STOPWORDS = {
    "about", "after", "agent", "agents", "almost", "also", "amid", "among",
    "analysis", "announces", "artificial", "available", "based", "benchmark",
    "benchmarks", "better", "blog", "build", "builder", "builders", "building",
    "change", "changes", "code", "coding", "company", "context", "could",
    "data", "developer", "developers", "does", "early", "engineering", "eval",
    "evals", "evaluation", "every", "faster", "find", "finds", "first", "from",
    "full", "general", "good", "guide", "have", "here", "high", "into",
    "introducing", "intelligence", "just", "large", "latest", "launch",
    "launches", "learning", "less", "live", "local", "long", "make", "makes",
    "making", "model", "models", "more", "most", "much", "need", "needs",
    "news", "next", "only", "open", "opens", "over", "paper", "part", "post",
    "preview", "production", "public", "release", "released", "releases",
    "releasing", "report", "reports", "research", "results", "review", "runs",
    "safety", "says", "scale", "security", "server", "show", "shows", "small",
    "software", "some", "source", "state", "still", "study", "support",
    "system", "systems", "task", "tasks", "team", "teams", "tech", "technical",
    "test", "testing", "tests", "than", "that", "their", "them", "there",
    "these", "they", "this", "through", "time", "today", "token", "tokens",
    "tool", "tools", "training", "turn", "turns", "under", "update", "updates",
    "usage", "used", "user", "users", "using", "version", "versus", "what",
    "when", "where", "which", "while", "with", "without", "work", "workflow",
    "workflows", "working", "world", "year", "years", "your",
}

ORG_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("openai", re.compile(r"\b(?:openai|chatgpt|gpt-\d|codex|astra\b|sol\b|dall-e|sora)\b", re.I)),
    ("anthropic", re.compile(r"\b(?:anthropic|claude(?:\s+code)?|sonnet|opus|haiku|fable|mythos)\b", re.I)),
    ("google", re.compile(r"\b(?:google|deepmind|gemini|alphaproof|genie\s+\d|antigravity)\b", re.I)),
    ("meta", re.compile(r"\b(?:meta\s+ai|llama\s+[345]|fair\b)\b", re.I)),
    ("mistral", re.compile(r"\bmistral|mixtral|codestral\b", re.I)),
    ("alibaba", re.compile(r"\b(?:alibaba|qwen\d*|modelscope)\b", re.I)),
    ("deepseek", re.compile(r"\bdeepseek\b", re.I)),
    ("moonshot", re.compile(r"\b(?:moonshot|kimi\s+k?\d*)\b", re.I)),
    ("nvidia", re.compile(r"\b(?:nvidia|blackwell|nemotron|tensorrt|cuda)\b", re.I)),
    ("huggingface", re.compile(r"\b(?:hugging\s*face|transformers\b)\b", re.I)),
    ("microsoft", re.compile(r"\b(?:microsoft|windows\s+11|azure\s+ai|github\s+copilot)\b", re.I)),
    ("cloudflare", re.compile(r"\bcloudflare\b", re.I)),
    ("cursor", re.compile(r"\bcursor\b", re.I)),
    ("together", re.compile(r"\btogether\s+(?:ai|compiler)\b", re.I)),
    ("tencent", re.compile(r"\b(?:tencent|hunyuan|hy\d)\b", re.I)),
)


@dataclass(frozen=True, slots=True)
class GroundTruthStory:
    hn_id: str
    title: str
    url: str
    canonical_url: str
    points: int
    comments: int
    published_at: str
    created_at_i: int
    tier: str = "discussion"

    @property
    def published_dt(self) -> datetime:
        dt = datetime.fromisoformat(self.published_at.replace("Z", "+00:00"))
        return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)

    @property
    def weight(self) -> float:
        tier_mult = 1.25 if self.tier == "artifact" else 1.0
        return math.log1p(max(1, self.points) + 0.5 * max(0, self.comments)) * tier_mult


@dataclass(frozen=True, slots=True)
class BriefEvaluation:
    brief_date: str
    must_catch_recall: float
    same_day_timeliness: float
    signal_to_noise_dedup: float
    editorial_specificity: float
    composite_score: float
    top_count: int
    watchlist_count: int
    ground_truth_in_window: int
    matched_ground_truth: tuple[str, ...]
    missed_ground_truth: tuple[str, ...]
    fluff_items: tuple[str, ...]
    duplicate_items: tuple[str, ...]
    weak_source_items: tuple[str, ...]
    slop_flags: tuple[str, ...]
    dominant_org: str
    dominant_org_count: int
    unique_domains: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "brief_date": self.brief_date,
            "must_catch_recall": round(self.must_catch_recall, 2),
            "same_day_timeliness": round(self.same_day_timeliness, 2),
            "signal_to_noise_dedup": round(self.signal_to_noise_dedup, 2),
            "editorial_specificity": round(self.editorial_specificity, 2),
            "composite_score": round(self.composite_score, 2),
            "top_count": self.top_count,
            "watchlist_count": self.watchlist_count,
            "ground_truth_in_window": self.ground_truth_in_window,
            "matched_ground_truth": list(self.matched_ground_truth),
            "missed_ground_truth": list(self.missed_ground_truth),
            "fluff_items": list(self.fluff_items),
            "duplicate_items": list(self.duplicate_items),
            "weak_source_items": list(self.weak_source_items),
            "slop_flags": list(self.slop_flags),
            "dominant_org": self.dominant_org,
            "dominant_org_count": self.dominant_org_count,
            "unique_domains": self.unique_domains,
        }


def canonicalize_url(raw_url: str) -> str:
    """Normalize URLs across schemes, www, tracking params, and arXiv/HF paper mirrors."""
    text = (raw_url or "").strip().strip("<>.,;)").lower()
    if not text:
        return ""
    text = re.sub(r"^https?://(?:www\.)?", "", text)
    text = text.split("#", 1)[0]
    if "qwen.ai/blog?id=" in text:
        return text.split("&", 1)[0].rstrip("/")
    text = text.split("?", 1)[0].rstrip("/")
    arxiv_match = re.match(
        r"^(?:arxiv\.org/(?:abs|html|pdf)|huggingface\.co/papers)/(\d{4}\.\d{4,5})(?:v\d+)?(?:\.pdf)?$",
        text,
    )
    if arxiv_match:
        return f"arxiv.org/abs/{arxiv_match.group(1)}"
    return text


def extract_artifact_keys(urls: Iterable[str], text: str = "") -> set[str]:
    """Return canonical artifact keys (`url:...`, `arxiv:...`, `gh:...`) for cross-source matching."""
    keys: set[str] = set()
    all_urls = list(urls)
    all_urls.extend(re.findall(r"https?://[^\s\)\]>]+", text))
    for raw in all_urls:
        canon = canonicalize_url(raw)
        if not canon or WEAK_DISCOVERY_URL_RE.match(canon):
            continue
        keys.add(f"url:{canon}")
        arxiv_m = re.match(r"^arxiv\.org/abs/(\d{4}\.\d{4,5})$", canon)
        if arxiv_m:
            keys.add(f"arxiv:{arxiv_m.group(1)}")
        gh_m = re.match(r"^github\.com/([a-z0-9_.-]+/[a-z0-9_.-]+)", canon)
        if gh_m and gh_m.group(1).split("/")[0] not in {"trending", "topics", "orgs", "features"}:
            keys.add(f"gh:{gh_m.group(1)}")
    return keys


def _distinctive_tokens(text: str) -> set[str]:
    tokens: set[str] = set()
    for raw in re.findall(r"[a-z0-9][a-z0-9.+-]{2,}", text.casefold()):
        tok = raw.strip(".-+")
        if len(tok) >= 3 and tok not in MATCH_STOPWORDS and not tok.isdigit():
            tokens.add(tok)
    return tokens


def _story_matches_entry(
    gt: GroundTruthStory,
    entry_keys: set[str],
    entry_title: str,
    entry_body: str,
) -> bool:
    gt_keys = extract_artifact_keys([gt.url])
    if gt_keys & entry_keys:
        return True
    gt_tokens = _distinctive_tokens(gt.title)
    if not gt_tokens:
        return False
    entry_tokens = _distinctive_tokens(f"{entry_title} {entry_body}")
    overlap = gt_tokens & entry_tokens
    if len(gt_tokens) <= 2:
        return gt_tokens.issubset(entry_tokens) and any(len(t) >= 5 for t in overlap)
    if len(overlap) >= 3 and len(overlap) / len(gt_tokens) >= 0.6:
        return True
    rare_overlap = {t for t in overlap if len(t) >= 6 or any(ch.isdigit() for ch in t)}
    return len(rare_overlap) >= 2 and len(overlap) >= 2 and len(overlap) / len(gt_tokens) >= 0.5


def infer_primary_org(headline: str, urls: Sequence[str]) -> str:
    """Infer the primary subject organization of a ranked headline for diversity scoring."""
    lead_clause = re.split(r"\b(?:versus|vs\.?|over|against|compared\s+with)\b", headline, maxsplit=1, flags=re.I)[0]
    for org, pattern in ORG_PATTERNS:
        if pattern.search(lead_clause):
            return org
    for raw in urls:
        canon = canonicalize_url(raw)
        for org, pattern in ORG_PATTERNS:
            if pattern.search(canon.split("/", 1)[0]):
                return org
    return ""


def load_hn_ground_truth(path: Path) -> list[GroundTruthStory]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    records = payload.get("stories", []) if isinstance(payload, dict) else payload
    stories: list[GroundTruthStory] = []
    for item in records:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        stories.append(
            GroundTruthStory(
                hn_id=str(item.get("hn_id") or item.get("objectID") or ""),
                title=str(item.get("title") or "").strip(),
                url=url,
                canonical_url=str(item.get("canonical_url") or canonicalize_url(url)),
                points=int(item.get("points") or 0),
                comments=int(item.get("comments") or item.get("num_comments") or 0),
                published_at=str(item.get("published_at") or item.get("created_at") or ""),
                created_at_i=int(item.get("created_at_i") or 0),
                tier=str(item.get("tier") or "discussion"),
            )
        )
    return stories


def fetch_algolia_hn_ground_truth(
    *,
    start: datetime,
    end: datetime,
    min_points: int = 120,
    opener: Callable[..., Any] = urlopen,
) -> list[GroundTruthStory]:
    """Fetch and deduplicate high-engagement AI builder threads from Algolia HN API."""
    deduped: dict[str, GroundTruthStory] = {}
    cur = start.astimezone(UTC)
    stop = end.astimezone(UTC)
    while cur < stop:
        nxt = min(cur + timedelta(days=5), stop)
        for page in range(4):
            params = urlencode(
                {
                    "tags": "story",
                    "numericFilters": f"created_at_i>={int(cur.timestamp())},created_at_i<={int(nxt.timestamp())},points>={min(100, min_points)}",
                    "hitsPerPage": 100,
                    "page": page,
                }
            )
            req = Request(
                f"https://hn.algolia.com/api/v1/search?{params}",
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            )
            kwargs: dict[str, Any] = {"timeout": 20}
            if opener is urlopen:
                kwargs["context"] = SSL_CONTEXT
            raw_bytes = None
            for attempt in range(3):
                try:
                    with opener(req, **kwargs) as resp:
                        raw_bytes = resp.read()
                        break
                except Exception:
                    if attempt == 2:
                        raise
            if raw_bytes is None:
                continue
            if raw_bytes[:2] == b"\x1f\x8b":
                raw_bytes = gzip.decompress(raw_bytes)
            payload = json.loads(raw_bytes.decode("utf-8"))
            for hit in payload.get("hits", []):
                title = str(hit.get("title") or "").strip()
                url = str(hit.get("url") or "").strip()
                if not title or not url:
                    continue
                if NON_AI_FALSE_POSITIVE_RE.search(title) or PR_FLUFF_HEADLINE_RE.search(title):
                    continue
                low_url = url.lower()
                if not (
                    STRICT_AI_BUILDER_RE.search(title)
                    or any(d in low_url for d in BUILDER_ARTIFACT_DOMAINS)
                ):
                    continue
                points = int(hit.get("points") or 0)
                comments = int(hit.get("num_comments") or 0)
                is_artifact = any(d in low_url for d in BUILDER_ARTIFACT_DOMAINS) or bool(
                    re.search(r"\b(?:show hn|release|benchmark|eval|open[- ]source|weights|paper|sdk|v\d+\.\d+)\b", title, re.I)
                )
                if points < min_points and not (points >= 100 and is_artifact):
                    continue
                canon = canonicalize_url(url)
                candidate = GroundTruthStory(
                    hn_id=str(hit.get("objectID") or ""),
                    title=title,
                    url=url,
                    canonical_url=canon,
                    points=points,
                    comments=comments,
                    published_at=str(hit.get("created_at") or ""),
                    created_at_i=int(hit.get("created_at_i") or 0),
                    tier="artifact" if is_artifact else "discussion",
                )
                prev = deduped.get(canon)
                if prev is None or candidate.points > prev.points:
                    deduped[canon] = candidate
            if page + 1 >= int(payload.get("nbPages") or 0):
                break
        cur = nxt
    return sorted(deduped.values(), key=lambda s: (-s.created_at_i, -s.points))


_MONTH_MAP = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}


def parse_coverage_window(markdown: str, brief_day: date) -> tuple[datetime, datetime]:
    """Parse explicit UTC rolling window from `Coverage window:` line or default to 72h."""
    for line in markdown.splitlines()[:12]:
        if not line.lower().startswith("coverage window:"):
            continue
        matches = re.findall(
            r"([A-Za-z]+)\s+(\d{1,2})(?:,\s*(\d{4}))?,?\s+(\d{1,2}):(\d{2})\s*UTC",
            line,
        )
        if len(matches) >= 2:
            year_hint = brief_day.year
            y_m = re.search(r"\b(202\d)\b", line)
            if y_m:
                year_hint = int(y_m.group(1))
            parsed_dts: list[datetime] = []
            for mon_s, day_s, yr_s, hr_s, min_s in matches[:2]:
                mon = _MONTH_MAP.get(mon_s.lower())
                if not mon:
                    break
                yr = int(yr_s) if yr_s else year_hint
                parsed_dts.append(datetime(yr, mon, int(day_s), int(hr_s), int(min_s), tzinfo=UTC))
            if len(parsed_dts) == 2 and parsed_dts[0] < parsed_dts[1]:
                return parsed_dts[0], parsed_dts[1]
    end = datetime(brief_day.year, brief_day.month, brief_day.day, 18, 0, tzinfo=UTC)
    return end - timedelta(hours=72), end


def _extract_raw_top_and_watchlist(markdown: str) -> tuple[list[tuple[str, str, list[str]]], list[tuple[str, str, list[str]]]]:
    """Return `(headline, full_text, urls)` tuples for Top 10 and Watchlist sections."""
    parsed_top = _parse_ranked_items(markdown)
    top_section = re.split(
        r"^(?:##\s+|\*\*)(?:Watchlist|Source panel|What changed)",
        markdown,
        maxsplit=1,
        flags=re.M | re.I,
    )[0]
    raw_blocks = re.findall(r"^\d+\.\s+.+?(?=^\d+\.\s+|\Z)", top_section, flags=re.M | re.S)
    top_entries: list[tuple[str, str, list[str]]] = []
    for idx, block in enumerate(raw_blocks[:10]):
        headline = parsed_top[idx][0] if idx < len(parsed_top) else block.splitlines()[0][:90]
        urls = [m.group(1) for m in re.finditer(r"(https?://[^\s\)\]>]+)", block)]
        top_entries.append((headline, re.sub(r"\s+", " ", block).strip(), urls))

    watch_entries: list[tuple[str, str, list[str]]] = []
    watch_match = re.search(
        r"^(?:##\s+Watchlist|\*\*Watchlist\*\*)\s*$(.+?)(?=^##\s+|\Z)",
        markdown,
        flags=re.M | re.S | re.I,
    )
    if watch_match:
        for line in watch_match.group(1).splitlines():
            stripped = line.strip()
            if not stripped.startswith("- "):
                continue
            urls = [m.group(1) for m in re.finditer(r"(https?://[^\s\)\]>]+)", stripped)]
            title_m = re.match(r"^-\s+\*\*(.+?)\*\*", stripped)
            w_title = title_m.group(1) if title_m else stripped[2:80]
            watch_entries.append((w_title, stripped[2:], urls))
    return top_entries, watch_entries


def evaluate_brief_markdown(
    path: Path,
    ground_truth: Sequence[GroundTruthStory],
    *,
    prior_briefs: Sequence[Path] = (),
    top_k_ground_truth: int = 12,
) -> BriefEvaluation:
    """Deterministically score a single `history/YYYY-MM-DD.md` issue on all 3 rubric axes."""
    date_m = re.search(r"(\d{4}-\d{2}-\d{2})", path.name)
    if not date_m:
        raise ValueError(f"Brief filename must contain YYYY-MM-DD: {path}")
    brief_day = date.fromisoformat(date_m.group(1))
    markdown = path.read_text(encoding="utf-8")
    win_start, win_end = parse_coverage_window(markdown, brief_day)
    top_entries, watch_entries = _extract_raw_top_and_watchlist(markdown)

    # 1. Must-catch builder recall
    in_window = [
        gt for gt in ground_truth
        if win_start <= gt.published_dt <= win_end
    ]
    in_window.sort(key=lambda g: (-g.weight, -g.points, g.hn_id))
    must_catch = in_window[:top_k_ground_truth]

    top_indexed = [(h, b, extract_artifact_keys(u, b)) for h, b, u in top_entries]
    watch_indexed = [(h, b, extract_artifact_keys(u, b)) for h, b, u in watch_entries]

    earned_weight = 0.0
    total_weight = sum(gt.weight for gt in must_catch)
    matched_labels: list[str] = []
    missed_labels: list[str] = []
    timely_hits = 0
    matched_count = 0

    for gt in must_catch:
        in_top = any(_story_matches_entry(gt, keys, h, b) for h, b, keys in top_indexed)
        in_watch = False if in_top else any(_story_matches_entry(gt, keys, h, b) for h, b, keys in watch_indexed)
        if in_top or in_watch:
            earned_weight += gt.weight * (1.0 if in_top else 0.5)
            matched_count += 1
            tier_tag = "Top10" if in_top else "Watchlist"
            matched_labels.append(f"[{gt.points}p {tier_tag}] {gt.title}")
            if (win_end - gt.published_dt) <= timedelta(hours=30):
                timely_hits += 1
        else:
            missed_labels.append(f"[{gt.points}p] {gt.title} ({gt.url})")

    recall_score = (100.0 * earned_weight / total_weight) if total_weight > 0 else 100.0
    timeliness_score = (100.0 * timely_hits / matched_count) if matched_count > 0 else (100.0 if not must_catch else 0.0)

    # 2. Signal-to-noise & deduplication
    prior_keys: set[str] = set()
    for prior_path in prior_briefs:
        if not prior_path.is_file():
            continue
        p_top, _ = _extract_raw_top_and_watchlist(prior_path.read_text(encoding="utf-8"))
        for _, p_body, p_urls in p_top:
            prior_keys.update(extract_artifact_keys(p_urls, p_body))

    snr_score = 100.0
    fluff_items: list[str] = []
    duplicate_items: list[str] = []
    weak_source_items: list[str] = []
    seen_intra_keys: set[str] = set()
    org_counts: Counter[str] = Counter()
    domains: set[str] = set()

    for rank_idx, (headline, body, urls) in enumerate(top_entries, start=1):
        item_keys = extract_artifact_keys(urls, body)
        # Fluff check
        is_fluff_headline = bool(PR_FLUFF_HEADLINE_RE.search(headline))
        is_vapourware = bool(VAPOURWARE_BODY_RE.search(body)) and not bool(TECHNICAL_ARTIFACT_OVERRIDE_RE.search(body))
        if (is_fluff_headline and not TECHNICAL_ARTIFACT_OVERRIDE_RE.search(" ".join(urls))) or is_vapourware:
            fluff_items.append(f"#{rank_idx} {headline}")
            snr_score -= 10.0

        # Intra-brief & cross-day duplicate checks
        if item_keys and (item_keys & seen_intra_keys):
            duplicate_items.append(f"intra:#{rank_idx} {headline}")
            snr_score -= 12.0
        elif item_keys and (item_keys & prior_keys):
            # Repeated exact URL/repo/paper from prior 14 days without any new primary URL
            if item_keys.issubset(prior_keys):
                duplicate_items.append(f"cross-day:#{rank_idx} {headline}")
                snr_score -= 8.0
        seen_intra_keys.update(item_keys)

        # Weak discovery/aggregator link check
        canon_urls = [canonicalize_url(u) for u in urls if canonicalize_url(u)]
        if not canon_urls or all(WEAK_DISCOVERY_URL_RE.match(cu) for cu in canon_urls):
            weak_source_items.append(f"#{rank_idx} {headline}")
            snr_score -= 6.0
        for cu in canon_urls:
            host = urlparse(f"https://{cu}").netloc
            if host:
                domains.add(host)

        org = infer_primary_org(headline, urls)
        if org:
            org_counts[org] += 1

    for w_headline, w_body, w_urls in watch_entries:
        w_keys = extract_artifact_keys(w_urls, w_body)
        if w_keys and (w_keys & seen_intra_keys):
            duplicate_items.append(f"watchlist-dup: {w_headline}")
            snr_score -= 8.0
        seen_intra_keys.update(w_keys)
        if PR_FLUFF_HEADLINE_RE.search(w_headline) and not TECHNICAL_ARTIFACT_OVERRIDE_RE.search(" ".join(w_urls)):
            fluff_items.append(f"WL: {w_headline}")
            snr_score -= 3.0

    for org, count in org_counts.items():
        if count > 2:
            snr_score -= 8.0 * (count - 2)
    if top_entries and len(domains) < min(6, len(top_entries)):
        snr_score -= 10.0

    snr_score = max(0.0, min(100.0, snr_score))
    dominant_org, dominant_org_count = org_counts.most_common(1)[0] if org_counts else ("none", 0)

    # 3. Editorial specificity & anti-slop quality
    item_spec_scores: list[float] = []
    slop_flags: list[str] = []

    for rank_idx, (headline, body, urls) in enumerate(top_entries, start=1):
        sub = 0.0
        # (a) Quantitative metric / concrete version / benchmark delta (25)
        if QUANT_METRIC_RE.search(body):
            sub += 25.0
        # (b) Concrete primary/technical artifact URL + non-thin body (25)
        canon_urls = [canonicalize_url(u) for u in urls if canonicalize_url(u)]
        has_deep_link = any(not WEAK_DISCOVERY_URL_RE.match(cu) and "/" in cu for cu in canon_urls)
        word_len = len(body.split())
        if has_deep_link and word_len >= 45:
            sub += 25.0
        elif has_deep_link:
            sub += 12.5
        # (c) Actionable builder takeaway + explicit caveat/scope (25)
        has_action = bool(ACTION_TAKEAWAY_RE.search(body))
        has_caveat = bool(CAVEAT_MARKER_RE.search(body))
        if has_action and has_caveat:
            sub += 25.0
        elif has_action or has_caveat:
            sub += 12.5
        # (d) Clean anti-slop prose & crisp headline (25)
        h_words = len(headline.split())
        has_cliche = bool(SLOP_CLICHE_RE.search(body))
        if not has_cliche and 5 <= h_words <= 18:
            sub += 25.0
        elif not has_cliche:
            sub += 15.0
        else:
            slop_flags.append(f"cliche:#{rank_idx}")
        item_spec_scores.append(sub)

    spec_score = sum(item_spec_scores) / len(item_spec_scores) if item_spec_scores else 0.0

    # Document-level structure & repetitive boilerplate checks
    if not re.search(r"^#\s+", markdown, flags=re.M):
        slop_flags.append("missing_h1")
        spec_score -= 5.0
    if "coverage window:" not in markdown.lower():
        slop_flags.append("missing_coverage_window")
        spec_score -= 4.0
    lede_m = re.search(r"^Read on the day:\s*(.+)$", markdown, flags=re.M | re.I)
    if not lede_m:
        slop_flags.append("missing_read_on_the_day")
        spec_score -= 4.0
    elif MADLIB_LEDE_RE.search(lede_m.group(1).strip()):
        slop_flags.append("madlib_read_on_the_day")
        spec_score -= 5.0
    if not re.search(r"^##\s+Top\s+\d+", markdown, flags=re.M | re.I):
        slop_flags.append("missing_top_h2")
        spec_score -= 4.0
    if not re.search(r"^##\s+Watchlist", markdown, flags=re.M | re.I):
        slop_flags.append("missing_watchlist_h2")
        spec_score -= 3.0

    top_blob_low = " ".join(b for _, b, _ in top_entries).lower()
    for phrase in REPETITIVE_BOILERPLATE_PHRASES:
        cnt = top_blob_low.count(phrase)
        if cnt >= 3:
            slop_flags.append(f"repetitive_phrase:{phrase}({cnt}x)")
            spec_score -= 4.0

    spec_score = max(0.0, min(100.0, spec_score))
    composite = 0.40 * recall_score + 0.35 * snr_score + 0.25 * spec_score

    return BriefEvaluation(
        brief_date=brief_day.isoformat(),
        must_catch_recall=recall_score,
        same_day_timeliness=timeliness_score,
        signal_to_noise_dedup=snr_score,
        editorial_specificity=spec_score,
        composite_score=composite,
        top_count=len(top_entries),
        watchlist_count=len(watch_entries),
        ground_truth_in_window=len(must_catch),
        matched_ground_truth=tuple(matched_labels),
        missed_ground_truth=tuple(missed_labels),
        fluff_items=tuple(fluff_items),
        duplicate_items=tuple(duplicate_items),
        weak_source_items=tuple(weak_source_items),
        slop_flags=tuple(slop_flags),
        dominant_org=dominant_org,
        dominant_org_count=dominant_org_count,
        unique_domains=len(domains),
    )


def evaluate_history_corpus(
    history_dir: Path,
    ground_truth: Sequence[GroundTruthStory],
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict[str, Any]:
    """Evaluate all dated `YYYY-MM-DD.md` briefs in `[start_date, end_date]` with rolling 14-day dedup."""
    all_dated: list[tuple[date, Path]] = []
    for path in sorted(Path(history_dir).glob("*.md")):
        if re.match(r"^\d{4}-\d{2}-\d{2}\.md$", path.name):
            all_dated.append((date.fromisoformat(path.stem), path))

    evaluations: list[BriefEvaluation] = []
    for idx, (day, path) in enumerate(all_dated):
        if start_date and day < start_date:
            continue
        if end_date and day > end_date:
            continue
        prior = [
            p for p_day, p in all_dated[:idx]
            if day - timedelta(days=14) <= p_day < day
        ]
        evaluations.append(evaluate_brief_markdown(path, ground_truth, prior_briefs=prior))

    count = len(evaluations)
    if count == 0:
        return {"edition_count": 0, "averages": {}, "editions": []}

    gt_evals = [e for e in evaluations if e.ground_truth_in_window > 0] or evaluations
    avg_recall = sum(e.must_catch_recall for e in gt_evals) / len(gt_evals)
    avg_timely = sum(e.same_day_timeliness for e in gt_evals) / len(gt_evals)
    avg_snr = sum(e.signal_to_noise_dedup for e in evaluations) / count
    avg_spec = sum(e.editorial_specificity for e in evaluations) / count
    avg_comp = 0.40 * avg_recall + 0.35 * avg_snr + 0.25 * avg_spec

    return {
        "edition_count": count,
        "date_range": [evaluations[0].brief_date, evaluations[-1].brief_date],
        "averages": {
            "must_catch_recall": round(avg_recall, 2),
            "same_day_timeliness": round(avg_timely, 2),
            "signal_to_noise_dedup": round(avg_snr, 2),
            "editorial_specificity": round(avg_spec, 2),
            "composite_score": round(avg_comp, 2),
        },
        "totals": {
            "fluff_items": sum(len(e.fluff_items) for e in evaluations),
            "duplicate_items": sum(len(e.duplicate_items) for e in evaluations),
            "weak_source_items": sum(len(e.weak_source_items) for e in evaluations),
            "missed_ground_truth": sum(len(e.missed_ground_truth) for e in evaluations),
            "editions_with_org_overload": sum(1 for e in evaluations if e.dominant_org_count > 2),
        },
        "editions": [e.to_dict() for e in evaluations],
    }


def evaluate_pipeline_candidates(
    shortlist: Sequence[SourceItem],
    watchlist: Sequence[SourceItem],
    ground_truth: Sequence[GroundTruthStory],
    *,
    window_start: datetime,
    window_end: datetime,
    all_collected: Sequence[SourceItem] = (),
    decisions: Sequence[dict[str, Any]] = (),
    prior_keys: set[str] | None = None,
    top_k_ground_truth: int = 12,
) -> dict[str, Any]:
    """Score pipeline candidate selection (`shortlist` Top-10 + `watchlist`) on the same 3 rubric axes."""
    in_window = [
        gt for gt in ground_truth
        if window_start <= gt.published_dt <= window_end
    ]
    in_window.sort(key=lambda g: (-g.weight, -g.points, g.hn_id))
    must_catch = in_window[:top_k_ground_truth]

    def item_urls(it: SourceItem) -> list[str]:
        extra = [str(u) for u in it.metadata.get("cluster_urls", []) if u]
        return [it.url, *extra]

    top_indexed = [(it.title, it.summary, extract_artifact_keys(item_urls(it), it.summary)) for it in shortlist[:10]]
    watch_indexed = [(it.title, it.summary, extract_artifact_keys(item_urls(it), it.summary)) for it in watchlist]
    coll_indexed = [(it.title, it.summary, extract_artifact_keys(item_urls(it), it.summary)) for it in all_collected]

    earned_weight = 0.0
    collected_weight = 0.0
    total_weight = sum(gt.weight for gt in must_catch)
    matched_labels: list[str] = []
    missed_labels: list[str] = []

    for gt in must_catch:
        if any(_story_matches_entry(gt, keys, h, b) for h, b, keys in coll_indexed or [*top_indexed, *watch_indexed]):
            collected_weight += gt.weight
        in_top = any(_story_matches_entry(gt, keys, h, b) for h, b, keys in top_indexed)
        in_watch = False if in_top else any(_story_matches_entry(gt, keys, h, b) for h, b, keys in watch_indexed)
        if in_top or in_watch:
            earned_weight += gt.weight * (1.0 if in_top else 0.5)
            matched_labels.append(f"[{gt.points}p {'Top10' if in_top else 'Pool'}] {gt.title}")
        else:
            missed_labels.append(f"[{gt.points}p] {gt.title} ({gt.url})")

    recall_score = (100.0 * earned_weight / total_weight) if total_weight > 0 else 100.0
    collector_recall = (100.0 * collected_weight / total_weight) if total_weight > 0 else 100.0

    # Signal-to-noise & deduplication
    snr_score = 100.0
    fluff_items: list[str] = []
    duplicate_items: list[str] = []
    false_ci_rejects = [
        str(d.get("cluster_id"))
        for d in decisions
        if "pattern: ci" in str(d.get("rationale", ""))
        and not re.search(r"\bci\b", str(d.get("cluster_id", "")).replace("-", " "))
    ]
    snr_score -= min(30.0, 3.0 * len(false_ci_rejects))

    seen_keys: set[str] = set()
    org_counts: Counter[str] = Counter()
    domains: set[str] = set()
    prior = prior_keys or set()

    for idx, it in enumerate(shortlist[:10], start=1):
        urls = item_urls(it)
        keys = extract_artifact_keys(urls, it.summary)
        if PR_FLUFF_HEADLINE_RE.search(it.title) and not TECHNICAL_ARTIFACT_OVERRIDE_RE.search(" ".join(urls)):
            fluff_items.append(f"#{idx} {it.title}")
            snr_score -= 10.0
        if keys and (keys & seen_keys):
            duplicate_items.append(f"intra:#{idx} {it.title}")
            snr_score -= 12.0
        elif keys and prior and keys.issubset(prior):
            duplicate_items.append(f"historical:#{idx} {it.title}")
            snr_score -= 8.0
        seen_keys.update(keys)
        canon = canonicalize_url(it.url)
        if canon:
            host = urlparse(f"https://{canon}").netloc
            if host:
                domains.add(host)
        org = infer_primary_org(it.title, urls) or (it.organization.strip().casefold() if it.organization else "")
        if org:
            org_counts[org] += 1

    for org, cnt in org_counts.items():
        if cnt > 2:
            snr_score -= 8.0 * (cnt - 2)
    if shortlist and len(domains) < min(6, len(shortlist[:10])):
        snr_score -= 10.0
    snr_score = max(0.0, min(100.0, snr_score))

    # Candidate specificity & metadata richness
    spec_scores: list[float] = []
    stub_summaries = 0
    unqualified_count = 0
    for it in shortlist[:10]:
        sub = 0.0
        is_stub = it.summary.startswith("Hacker News signal:") or len(it.summary.split()) < 14
        if is_stub:
            stub_summaries += 1
        else:
            sub += 30.0
        if QUANT_METRIC_RE.search(f"{it.title} {it.summary}"):
            sub += 25.0
        elif len(it.summary.split()) >= 25:
            sub += 12.0
        canon = canonicalize_url(it.url)
        if canon and not WEAK_DISCOVERY_URL_RE.match(canon):
            sub += 25.0
        qual = bool(it.metadata.get("qualifying_evidence")) or it.authority in {"primary", "independent"}
        if qual:
            sub += 20.0
        else:
            unqualified_count += 1
        spec_scores.append(sub)

    spec_score = (sum(spec_scores) / len(spec_scores)) if spec_scores else 0.0
    spec_score = max(0.0, min(100.0, spec_score))
    composite = 0.40 * recall_score + 0.35 * snr_score + 0.25 * spec_score

    return {
        "collector_recall": round(collector_recall, 2),
        "must_catch_recall": round(recall_score, 2),
        "signal_to_noise_dedup": round(snr_score, 2),
        "editorial_specificity": round(spec_score, 2),
        "composite_score": round(composite, 2),
        "shortlist_count": len(shortlist[:10]),
        "pool_count": len(shortlist) + len(watchlist),
        "ground_truth_in_window": len(must_catch),
        "matched_ground_truth": matched_labels,
        "missed_ground_truth": missed_labels,
        "fluff_items": fluff_items,
        "duplicate_items": duplicate_items,
        "false_ci_reject_count": len(false_ci_rejects),
        "stub_summary_count": stub_summaries,
        "unqualified_shortlist_count": unqualified_count,
        "unique_domains": len(domains),
        "org_distribution": dict(org_counts.most_common(5)),
    }


def render_benchmark_markdown(corpus_report: dict[str, Any], pipeline_report: dict[str, Any] | None = None) -> str:
    """Format corpus and pipeline benchmark results as a concise Markdown table."""
    avg = corpus_report.get("averages", {})
    totals = corpus_report.get("totals", {})
    lines = [
        "# AI Builder Brief Benchmark Report",
        "",
        f"- **Evaluated Historical Editions:** {corpus_report.get('edition_count', 0)} ({' .. '.join(corpus_report.get('date_range', []))})",
        f"- **Composite Score:** **{avg.get('composite_score', 0):.2f} / 100**",
        f"  - Must-Catch Builder Recall: `{avg.get('must_catch_recall', 0):.2f}` (Same-day timeliness: `{avg.get('same_day_timeliness', 0):.2f}%`)",
        f"  - Signal-to-Noise & Dedup: `{avg.get('signal_to_noise_dedup', 0):.2f}` (Fluff items: `{totals.get('fluff_items', 0)}`, Duplicates: `{totals.get('duplicate_items', 0)}`, Weak links: `{totals.get('weak_source_items', 0)}`, Org-overloaded issues: `{totals.get('editions_with_org_overload', 0)}`)",
        f"  - Editorial Specificity & Anti-Slop: `{avg.get('editorial_specificity', 0):.2f}`",
        "",
        "## Per-Edition Breakdown",
        "",
        "| Date | Composite | Recall | Timeliness | SNR & Dedup | Specificity | Dom Org | Fluff | Dups | Missed GT |",
        "| --- | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: | ---: |",
    ]
    for ed in corpus_report.get("editions", []):
        lines.append(
            f"| {ed['brief_date']} | **{ed['composite_score']:.1f}** | {ed['must_catch_recall']:.1f} | "
            f"{ed['same_day_timeliness']:.0f}% | {ed['signal_to_noise_dedup']:.1f} | {ed['editorial_specificity']:.1f} | "
            f"{ed['dominant_org']} ({ed['dominant_org_count']}) | {len(ed['fluff_items'])} | {len(ed['duplicate_items'])} | "
            f"{len(ed['missed_ground_truth'])}/{ed['ground_truth_in_window']} |"
        )
    if pipeline_report:
        lines.extend([
            "",
            "## Pipeline Candidate Benchmark",
            "",
            f"- **Collector Recall:** `{pipeline_report['collector_recall']:.2f}`",
            f"- **Shortlist/Pool Must-Catch Recall:** `{pipeline_report['must_catch_recall']:.2f}`",
            f"- **Signal-to-Noise & Dedup:** `{pipeline_report['signal_to_noise_dedup']:.2f}` (False `ci` rejects: `{pipeline_report['false_ci_reject_count']}`, Fluff: `{len(pipeline_report['fluff_items'])}`)",
            f"- **Candidate Specificity:** `{pipeline_report['editorial_specificity']:.2f}` (Stub summaries: `{pipeline_report['stub_summary_count']}`, Unqualified: `{pipeline_report['unqualified_shortlist_count']}`)",
            f"- **Composite Score:** **{pipeline_report['composite_score']:.2f} / 100**",
        ])
    return "\n".join(lines) + "\n"
