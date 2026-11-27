"""Strict mechanical Source-vs-Claim Fact-Checker backed by Quotaswitch Responses API."""

from __future__ import annotations

import gzip
import html
import json
import os
import re
import socket
import ssl
from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlsplit
from urllib.request import Request, urlopen

import certifi

from ai_builder_brief.benchmark import _extract_raw_top_and_watchlist
from ai_builder_brief.collectors import extract_youtube_video_id, fetch_youtube_transcript_and_meta

DEFAULT_BASE_URL = "http://127.0.0.1:18455/v1"
DEFAULT_API_KEY = "quotaswitch-local"
DEFAULT_FACTCHECK_MODEL = "gpt-6.2-astra"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

VERDICTS = frozenset({"supported", "nuance_drift", "unsupported"})
ITEM_VERDICTS = frozenset({"supported", "nuance_drift", "unsupported", "unfetched"})
FAILURE_TYPES = frozenset({"timeout", "proxy", "invalid_response"})

SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
USER_AGENT = "AIBuilderBriefFactCheck/1.0 (+https://github.com/lifan-builds/ai-builder-brief)"
MAX_SOURCE_CHARS = 12_000
MAX_TOTAL_SOURCE_CHARS = 16_000
MAX_RESPONSE_BYTES = 4_000_000

_BOT_CHALLENGE_MARKERS = (
    "just a moment...",
    "enable javascript and cookies to continue",
    "attention required! | cloudflare",
    "performing security verification",
    "verify you are human",
    "checking if the site connection is secure",
)

FACTCHECK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["item_id", "claims"],
    "properties": {
        "item_id": {"type": "string"},
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "claim",
                    "verdict",
                    "source_url",
                    "evidence_quote",
                    "reason",
                ],
                "properties": {
                    "claim": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["supported", "nuance_drift", "unsupported"]},
                    "source_url": {"type": "string"},
                    "evidence_quote": {"type": "string"},
                    "reason": {"type": "string"},
                },
            },
        },
    },
}

FACTCHECK_SYSTEM_PROMPT = (
    "You are a strict mechanical Source-vs-Claim Fact-Checker for AI Builder Brief. "
    "Verify every concrete factual claim in the brief item strictly against the provided fetched primary source text(s). "
    "NEVER rewrite prose, suggest wording improvements, critique tone or voice, or comment on style. "
    "Group the item's concrete factual assertions into concise, non-overlapping verifiable claims (typically 4-8 claims "
    "covering metrics, benchmark scores, versions, dates, model/package identifiers, pricing, hardware requirements, "
    "license limits, technical mechanisms, and explicit caveats) — or verify the explicitly provided `claims` list if non-empty. "
    "Ignore ephemeral discovery-panel metadata explicitly labeled as Hacker News points/comments/surges, "
    "X likes/retweets, or Hugging Face download/upvote/Daily-Paper rank counts unless that HN/X/HF-stats page itself "
    "is included in `sources`. "
    "Classify each claim strictly as:\n"
    "- `supported`: directly backed by the fetched source text with matching figures, dates, identifiers, and scope.\n"
    "- `nuance_drift`: the core fact appears in the source, but the claim subtly shifts a figure, omits a material "
    "scope/condition/qualifier, conflates variants or settings, or overstates a provisional/bounded finding.\n"
    "- `unsupported`: directly contradicted by the source text, fabricated, or absent from all fetched source texts "
    "(if a companion source URL failed to fetch, note that in `reason` rather than guessing its contents).\n"
    "For `source_url`, return the exact `url` from `sources` that supports, qualifies, or contradicts the claim "
    "(or empty string \"\" if absent from every fetched source). "
    "For `evidence_quote`, copy a short exact verbatim substring from the fetched source text (empty string \"\" only if absent). "
    "For `reason`, state only the concise mechanical factual delta or confirmation in 1 sentence. "
    "Return strict JSON matching the schema."
)


class FactCheckError(RuntimeError):
    """Machine-readable fact-check client/validation failure without raw upstream secrets."""

    def __init__(self, category: str, *, stage: str, item_id: str | None = None) -> None:
        self.category = category if category in FAILURE_TYPES else "proxy"
        self.stage = stage
        self.item_id = item_id
        message = f"factcheck {self.category} at {stage}"
        if item_id:
            message += f" ({item_id})"
        super().__init__(message)

    def with_item(self, item_id: str) -> "FactCheckError":
        return FactCheckError(self.category, stage=self.stage, item_id=item_id)

    def to_metadata(self) -> dict[str, str]:
        metadata = {"failure_type": self.category, "stage": self.stage}
        if self.item_id:
            metadata["item_id"] = self.item_id
        return metadata


@dataclass(frozen=True, slots=True)
class FetchedSource:
    """Ephemeral in-memory primary source text fetched for verification only."""

    url: str
    fetched_url: str
    status: str  # fetched | blocked | unreadable_binary | fetch_error
    text: str = ""
    error: str = ""
    truncated: bool = False

    def to_metadata(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "fetched_url": self.fetched_url,
            "status": self.status,
            "chars": len(self.text),
            "truncated": self.truncated,
            **({"error": self.error} if self.error else {}),
        }


@dataclass(frozen=True, slots=True)
class BriefItem:
    """One ranked brief or review item to verify against its linked sources."""

    item_id: str
    section: str
    rank: int
    headline: str
    claim_text: str
    source_urls: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ClaimVerdict:
    """Verification verdict for a single atomic factual claim."""

    claim: str
    verdict: str
    source_url: str
    evidence_quote: str
    reason: str
    quote_verified: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "claim": self.claim,
            "verdict": self.verdict,
            "source_url": self.source_url,
            "evidence_quote": self.evidence_quote,
            "reason": self.reason,
            "quote_verified": self.quote_verified,
        }


@dataclass(frozen=True, slots=True)
class ItemFactCheck:
    """Verification rollup for one brief item."""

    item_id: str
    section: str
    rank: int
    headline: str
    verdict: str
    sources: tuple[FetchedSource, ...]
    claims: tuple[ClaimVerdict, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "section": self.section,
            "rank": self.rank,
            "headline": self.headline,
            "verdict": self.verdict,
            "sources": [source.to_metadata() for source in self.sources],
            "claim_counts": {
                "total": len(self.claims),
                "supported": sum(c.verdict == "supported" for c in self.claims),
                "nuance_drift": sum(c.verdict == "nuance_drift" for c in self.claims),
                "unsupported": sum(c.verdict == "unsupported" for c in self.claims),
            },
            "claims": [claim.to_dict() for claim in self.claims],
        }


@dataclass(frozen=True, slots=True)
class FactCheckReport:
    """Full mechanical Source-vs-Claim verification report for a brief or review artifact."""

    target: str
    model: str
    items: tuple[ItemFactCheck, ...]

    @property
    def summary(self) -> dict[str, int]:
        all_claims = [claim for item in self.items for claim in item.claims]
        return {
            "items_total": len(self.items),
            "items_supported": sum(item.verdict == "supported" for item in self.items),
            "items_nuance_drift": sum(item.verdict == "nuance_drift" for item in self.items),
            "items_unsupported": sum(item.verdict == "unsupported" for item in self.items),
            "items_unfetched": sum(item.verdict == "unfetched" for item in self.items),
            "claims_total": len(all_claims),
            "claims_supported": sum(claim.verdict == "supported" for claim in all_claims),
            "claims_nuance_drift": sum(claim.verdict == "nuance_drift" for claim in all_claims),
            "claims_unsupported": sum(claim.verdict == "unsupported" for claim in all_claims),
            "quotes_verified": sum(claim.quote_verified for claim in all_claims if claim.evidence_quote),
            "quotes_total": sum(bool(claim.evidence_quote) for claim in all_claims),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "target": self.target,
            "model": self.model,
            "summary": self.summary,
            "items": [item.to_dict() for item in self.items],
        }


def _is_timeout(error: BaseException) -> bool:
    if isinstance(error, (TimeoutError, socket.timeout)):
        return True
    if isinstance(error, URLError) and isinstance(error.reason, (TimeoutError, socket.timeout)):
        return True
    text = str(error).casefold()
    return "timed out" in text or "timeout" in text


def _failure_from_exception(error: BaseException, *, stage: str) -> FactCheckError:
    if isinstance(error, FactCheckError):
        return error
    if _is_timeout(error):
        return FactCheckError("timeout", stage=stage)
    if isinstance(error, (ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError)):
        return FactCheckError("invalid_response", stage=stage)
    return FactCheckError("proxy", stage=stage)


def _proxy_config() -> tuple[str, str]:
    base = os.environ.get("OPENAI_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in LOOPBACK_HOSTS:
        raise FactCheckError("proxy", stage="proxy_config")
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    key = os.environ.get("OPENAI_API_KEY", DEFAULT_API_KEY).strip()
    if not key or key.startswith("sk-"):
        raise FactCheckError("proxy", stage="proxy_config")
    return base, key


class _HTMLTextExtractor(HTMLParser):
    """Minimal stdlib HTML-to-text extractor preserving headings, lists, and tables."""

    _SKIP_TAGS = frozenset({"script", "style", "noscript", "svg", "template", "iframe"})
    _BLOCK_TAGS = frozenset({
        "p", "div", "section", "article", "main", "li", "tr", "blockquote", "pre",
        "h1", "h2", "h3", "h4", "h5", "h6", "br", "hr", "dt", "dd",
    })
    _CELL_TAGS = frozenset({"td", "th"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        low = tag.lower()
        if low in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if low in self._BLOCK_TAGS:
            self._parts.append("\n")
        elif low in self._CELL_TAGS:
            self._parts.append(" | ")

    def handle_endtag(self, tag: str) -> None:
        low = tag.lower()
        if low in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if low in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            self._parts.append(data)

    def text(self) -> str:
        raw = html.unescape("".join(self._parts))
        lines = [re.sub(r"[ \t]+", " ", line).strip() for line in raw.splitlines()]
        compact = "\n".join(line for line in lines if line)
        return re.sub(r"\n{3,}", "\n\n", compact).strip()


def extract_text_from_html(markup: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(markup)
    parser.close()
    return parser.text()


def _candidate_fetch_urls(url: str) -> list[tuple[str, str]]:
    """Return ordered `(fetch_url, kind)` candidates for primary source artifacts."""
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.")
    parts = [p for p in parsed.path.split("/") if p]

    if host == "github.com" and len(parts) >= 2:
        owner, repo = parts[0], parts[1]
        if len(parts) >= 5 and parts[2] == "blob":
            ref = parts[3]
            file_path = "/".join(parts[4:])
            return [
                (f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{file_path}", "text"),
                (url, "html"),
            ]
        if len(parts) >= 5 and parts[2] == "tree":
            ref = parts[3]
            sub_path = "/".join(parts[4:])
            return [
                (f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{sub_path}/README.md", "text"),
                (f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{sub_path}/readme.md", "text"),
                (url, "html"),
            ]
        if len(parts) >= 5 and parts[2] == "releases" and parts[3] == "tag":
            tag = "/".join(parts[4:])
            return [
                (f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}", "github_release"),
                (url, "html"),
            ]
        if len(parts) == 2:
            return [
                (f"https://raw.githubusercontent.com/{owner}/{repo}/HEAD/README.md", "text"),
                (f"https://raw.githubusercontent.com/{owner}/{repo}/main/README.md", "text"),
                (f"https://raw.githubusercontent.com/{owner}/{repo}/master/README.md", "text"),
                (url, "html"),
            ]

    if host == "huggingface.co" and len(parts) == 2 and parts[0] not in {
        "blog", "papers", "spaces", "docs", "datasets", "models",
    }:
        owner, repo = parts[0], parts[1]
        return [
            (f"https://huggingface.co/{owner}/{repo}/raw/main/README.md", "text"),
            (url, "html"),
        ]

    if host == "arxiv.org" and len(parts) >= 2 and parts[0] in {"abs", "pdf", "html"}:
        paper_id = parts[1].removesuffix(".pdf")
        return [
            (f"https://arxiv.org/html/{paper_id}", "html"),
            (f"https://arxiv.org/abs/{paper_id}", "html"),
        ]

    return [(url, "html")]


def _decode_bytes(raw: bytes, content_type: str) -> str:
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    charset_m = re.search(r"charset=([a-zA-Z0-9._-]+)", content_type, re.I)
    encoding = charset_m.group(1) if charset_m else "utf-8"
    try:
        return raw.decode(encoding, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def _looks_like_bot_challenge(text: str) -> bool:
    low = text[:3000].casefold()
    return any(marker in low for marker in _BOT_CHALLENGE_MARKERS)


def fetch_source_text(
    url: str,
    *,
    opener: Callable[..., Any] = urlopen,
    timeout: int = 20,
    max_chars: int = 120_000,
    youtube_fetcher: Callable[[str], dict[str, Any]] | None = None,
) -> FetchedSource:
    """Fetch primary source text ephemerally in memory for citation checking."""
    yt_id = extract_youtube_video_id(url)
    if yt_id and (youtube_fetcher is not None or opener is urlopen):
        fetcher_fn = youtube_fetcher or fetch_youtube_transcript_and_meta
        meta = fetcher_fn(url) or {}
        parts = [
            f"YouTube Video: {meta.get('title') or ''}",
            f"Channel: {meta.get('channel') or ''}",
            f"Published: {meta.get('published_at') or ''}",
            f"Views: {meta.get('view_count') or 0}",
            "",
            str(meta.get("description") or ""),
            "",
            ("Transcript:\n" + str(meta.get("transcript_text") or "")) if meta.get("transcript_text") else "",
        ]
        yt_text = "\n".join(p for p in parts if p is not None).strip()
        if len(yt_text) >= 80:
            return FetchedSource(
                url=url,
                fetched_url=f"https://www.youtube.com/watch?v={yt_id}",
                status="fetched",
                text=yt_text[:max_chars],
                truncated=len(yt_text) > max_chars,
            )

    last_status = "fetch_error"
    last_error = "no fetch candidates"
    last_url = url

    for candidate_url, kind in _candidate_fetch_urls(url):
        last_url = candidate_url
        headers = {
            "User-Agent": USER_AGENT,
            "Accept": (
                "application/vnd.github+json"
                if kind == "github_release"
                else "text/markdown, text/plain, text/html;q=0.9, */*;q=0.8"
            ),
            "Accept-Encoding": "gzip",
        }
        request = Request(candidate_url, headers=headers)
        kwargs: dict[str, Any] = {"timeout": timeout}
        if opener is urlopen:
            kwargs["context"] = SSL_CONTEXT
        try:
            with opener(request, **kwargs) as response:
                resp_headers = getattr(response, "headers", {}) or {}
                content_type = str(
                    resp_headers.get("Content-Type", "")
                    if hasattr(resp_headers, "get")
                    else ""
                )
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as error:
            last_status = "blocked" if error.code in {401, 403, 429} else "fetch_error"
            last_error = f"HTTP {error.code}"
            continue
        except Exception as error:
            last_status = "fetch_error"
            last_error = type(error).__name__
            continue

        if "application/pdf" in content_type.casefold() or candidate_url.casefold().endswith(".pdf"):
            last_status = "unreadable_binary"
            last_error = "PDF binary extraction unavailable"
            continue

        decoded = _decode_bytes(raw[:MAX_RESPONSE_BYTES], content_type)
        if kind == "github_release":
            try:
                release = json.loads(decoded)
                parts = [
                    f"Release: {release.get('name') or release.get('tag_name') or ''}",
                    f"Tag: {release.get('tag_name') or ''}",
                    f"Published: {release.get('published_at') or ''}",
                    "",
                    str(release.get("body") or ""),
                ]
                text = "\n".join(parts).strip()
            except json.JSONDecodeError:
                last_status = "fetch_error"
                last_error = "invalid GitHub release JSON"
                continue
        elif kind == "text" or "text/plain" in content_type.casefold() or "text/markdown" in content_type.casefold():
            text = decoded.strip()
        else:
            text = extract_text_from_html(decoded)

        if _looks_like_bot_challenge(text):
            last_status = "blocked"
            last_error = "bot challenge page"
            continue
        if len(text) < 80:
            last_status = "blocked"
            last_error = "empty or minimal response body"
            continue

        truncated = len(text) > max_chars
        return FetchedSource(
            url=url,
            fetched_url=candidate_url,
            status="fetched",
            text=text[:max_chars],
            truncated=truncated,
        )

    return FetchedSource(
        url=url,
        fetched_url=last_url,
        status=last_status,
        error=last_error,
    )


def _normalize_ws(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _quote_in_source(quote: str, source_text: str) -> bool:
    if not quote.strip() or not source_text:
        return False
    if quote in source_text:
        return True
    norm_quote = _normalize_ws(quote)
    if norm_quote and norm_quote in _normalize_ws(source_text):
        return True
    stripped_quote = re.sub(r"[`*_]", "", norm_quote)
    stripped_source = re.sub(r"[`*_]", "", _normalize_ws(source_text))
    return bool(stripped_quote) and stripped_quote in stripped_source


def _extract_responses_text(raw: dict[str, Any]) -> str:
    if raw.get("error"):
        raise FactCheckError("proxy", stage="factcheck_response")
    status = raw.get("status")
    if status is not None and status != "completed":
        raise FactCheckError("invalid_response", stage="factcheck_response")

    chunks: list[str] = []
    output = raw.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str):
                    chunks.append(part["text"])
    if chunks:
        return "".join(chunks)
    if isinstance(raw.get("output_text"), str) and raw["output_text"].strip():
        return raw["output_text"]
    raise FactCheckError("invalid_response", stage="factcheck_response")


def _validate_factcheck_payload(
    payload: object,
    *,
    expected_item_id: str,
    allowed_urls: dict[str, str],
    source_texts_by_url: dict[str, str],
) -> tuple[ClaimVerdict, ...]:
    if not isinstance(payload, dict) or set(payload) != {"item_id", "claims"}:
        raise FactCheckError("invalid_response", stage="factcheck_schema")
    if payload["item_id"] != expected_item_id or not isinstance(payload["claims"], list):
        raise FactCheckError("invalid_response", stage="factcheck_schema")

    claim_props = set(FACTCHECK_SCHEMA["properties"]["claims"]["items"]["properties"])
    verdicts: list[ClaimVerdict] = []
    for entry in payload["claims"]:
        if not isinstance(entry, dict) or set(entry) != claim_props:
            raise FactCheckError("invalid_response", stage="factcheck_schema")
        if any(not isinstance(entry[field], str) for field in claim_props):
            raise FactCheckError("invalid_response", stage="factcheck_schema")
        verdict = entry["verdict"]
        if verdict not in VERDICTS:
            raise FactCheckError("invalid_response", stage="factcheck_schema")
        claim_text = entry["claim"].strip()
        if not claim_text:
            raise FactCheckError("invalid_response", stage="factcheck_schema")
        raw_url = entry["source_url"].strip()
        if raw_url:
            if raw_url not in allowed_urls:
                raise FactCheckError("invalid_response", stage="factcheck_schema")
            canonical_url = allowed_urls[raw_url]
        else:
            canonical_url = ""
        quote = entry["evidence_quote"].strip()
        reason = entry["reason"].strip()
        source_text = source_texts_by_url.get(canonical_url, "")
        quote_ok = (
            _quote_in_source(quote, source_text)
            if quote and canonical_url
            else (any(_quote_in_source(quote, txt) for txt in source_texts_by_url.values()) if quote else False)
        )
        verdicts.append(
            ClaimVerdict(
                claim=claim_text,
                verdict=verdict,
                source_url=canonical_url,
                evidence_quote=quote,
                reason=reason,
                quote_verified=quote_ok,
            )
        )
    return tuple(verdicts)


def rollup_item_verdict(claims: Sequence[ClaimVerdict], *, has_fetched_source: bool) -> str:
    """Deterministically compute item-level verdict from atomic claim verdicts."""
    if not has_fetched_source:
        return "unfetched"
    if any(claim.verdict == "unsupported" for claim in claims):
        return "unsupported"
    if any(claim.verdict == "nuance_drift" for claim in claims):
        return "nuance_drift"
    return "supported"


def _parse_responses_body(body: str) -> str:
    """Extract JSON payload text from either a plain JSON response or an SSE stream."""
    stripped = body.lstrip()
    if stripped.startswith("{"):
        raw = json.loads(stripped)
        if not isinstance(raw, dict):
            raise FactCheckError("invalid_response", stage="factcheck_response")
        return _extract_responses_text(raw)

    deltas: list[str] = []
    completed_response: dict[str, Any] | None = None
    saw_event = False
    for line in body.splitlines():
        text_line = line.strip()
        if not text_line.startswith("data:"):
            continue
        payload_str = text_line[5:].strip()
        if not payload_str or payload_str == "[DONE]":
            continue
        saw_event = True
        event = json.loads(payload_str)
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        if event_type in {"error", "response.failed"}:
            raise FactCheckError("proxy", stage="factcheck_response")
        if event_type == "response.output_text.delta" and isinstance(event.get("delta"), str):
            deltas.append(event["delta"])
        elif event_type == "response.output_text.done" and isinstance(event.get("text"), str) and not deltas:
            deltas.append(event["text"])
        elif event_type == "response.completed" and isinstance(event.get("response"), dict):
            completed_response = event["response"]

    if completed_response is not None:
        try:
            return _extract_responses_text(completed_response)
        except FactCheckError:
            if deltas:
                return "".join(deltas)
            raise
    if deltas:
        return "".join(deltas)
    if saw_event:
        raise FactCheckError("proxy", stage="factcheck_response")
    raise FactCheckError("invalid_response", stage="factcheck_response")


def _claim_anchors(text: str) -> tuple[set[str], set[str]]:
    numbers = set(re.findall(r"\b\d+(?:[.,:/-]\d+)*(?:%|x|×|gb|mb|tb|ms|s|b|m|k)?\b", text.casefold()))
    words = {
        tok
        for tok in re.findall(r"[a-z0-9][a-z0-9._+-]{3,}", text.casefold())
        if not tok.isdigit()
    }
    return numbers, words


def _select_source_excerpt(source_text: str, claim_text: str, max_chars: int) -> str:
    """Keep opening context plus claim-relevant paragraphs in original order when truncating."""
    if len(source_text) <= max_chars:
        return source_text
    if max_chars <= 0:
        return ""

    lead_budget = int(max_chars * 0.35)
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", source_text) if p.strip()]
    if len(paragraphs) <= 2:
        paragraphs = [p.strip() for p in source_text.splitlines() if p.strip()]
    if len(paragraphs) <= 1:
        return source_text[:max_chars]

    selected_indices: set[int] = set()
    used = 0
    for idx, para in enumerate(paragraphs):
        if used + len(para) > lead_budget and selected_indices:
            break
        selected_indices.add(idx)
        used += len(para) + 2

    numbers, words = _claim_anchors(claim_text)
    scored: list[tuple[int, int]] = []
    for idx, para in enumerate(paragraphs):
        if idx in selected_indices:
            continue
        low = para.casefold()
        score = sum(3 for num in numbers if num in low) + sum(1 for word in words if word in low)
        if score > 0:
            scored.append((score, idx))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))

    for _, idx in scored:
        para_len = len(paragraphs[idx]) + 7
        if used + para_len > max_chars:
            continue
        selected_indices.add(idx)
        used += para_len

    # Fill any remaining budget sequentially from the top.
    for idx, para in enumerate(paragraphs):
        if idx in selected_indices:
            continue
        if used + len(para) + 2 > max_chars:
            break
        selected_indices.add(idx)
        used += len(para) + 2

    ordered = sorted(selected_indices)
    parts: list[str] = []
    prev = -1
    for idx in ordered:
        if prev >= 0 and idx > prev + 1:
            parts.append("[...]")
        parts.append(paragraphs[idx])
        prev = idx
    return "\n\n".join(parts)[:max_chars]


def _format_verification_input(
    *,
    item_id: str,
    headline: str,
    claim_text: str,
    claims: Sequence[str],
    source_packets: Sequence[dict[str, str]],
) -> str:
    lines = [
        f"ITEM_ID: {item_id}",
        f"HEADLINE: {headline}",
        "",
        "BRIEF_ITEM_TEXT:",
        claim_text,
    ]
    if claims:
        lines.extend(["", "EXPLICIT_CLAIMS:"])
        for idx, claim in enumerate(claims, start=1):
            lines.append(f"{idx}. {claim}")
    lines.extend(["", "SOURCES:"])
    for idx, src in enumerate(source_packets, start=1):
        lines.append(f"=== SOURCE {idx} ===")
        lines.append(f"URL: {src['url']}")
        lines.append(f"STATUS: {src['status']}")
        if src.get("error"):
            lines.append(f"ERROR: {src['error']}")
        if src.get("text"):
            lines.append("TEXT:")
            lines.append(src["text"])
        lines.append("")
    return "\n".join(lines).rstrip()


def verify_item_claims(
    *,
    item_id: str,
    headline: str,
    claim_text: str,
    sources: Sequence[FetchedSource],
    claims: Sequence[str] = (),
    model: str | None = None,
    reasoning_effort: str = "medium",
    opener: Callable[..., Any] = urlopen,
    timeout: int = 540,
    max_attempts: int = 3,
) -> tuple[ClaimVerdict, ...]:
    """Verify one item's factual claims against fetched primary source text via Responses API."""
    fetched_sources = [src for src in sources if src.status == "fetched" and src.text]
    if not fetched_sources:
        return ()

    base, key = _proxy_config()
    selected_model = (model or os.environ.get("FACTCHECK_MODEL", DEFAULT_FACTCHECK_MODEL)).strip() or DEFAULT_FACTCHECK_MODEL

    per_source_cap = min(MAX_SOURCE_CHARS, max(6_000, MAX_TOTAL_SOURCE_CHARS // len(fetched_sources)))
    budget = MAX_TOTAL_SOURCE_CHARS
    anchor_text = f"{headline}\n{claim_text}\n" + "\n".join(claims)
    source_packets: list[dict[str, str]] = []
    allowed_urls: dict[str, str] = {}
    source_texts_by_url: dict[str, str] = {}
    for src in sources:
        allowed_urls[src.url] = src.url
        allowed_urls[src.fetched_url] = src.url
        if src.status == "fetched" and src.text and budget > 0:
            limit = min(per_source_cap, budget)
            snippet = _select_source_excerpt(src.text, anchor_text, limit)
            budget -= len(snippet)
            source_texts_by_url[src.url] = snippet
            source_packets.append({
                "url": src.url,
                "status": src.status,
                "text": snippet,
            })
        else:
            source_packets.append({
                "url": src.url,
                "status": src.status,
                "error": src.error,
                "text": "",
            })

    formatted_input = _format_verification_input(
        item_id=item_id,
        headline=headline,
        claim_text=claim_text,
        claims=claims,
        source_packets=source_packets,
    )
    try:
        request_body = {
            "model": selected_model,
            "stream": True,
            "instructions": FACTCHECK_SYSTEM_PROMPT,
            "input": [{"role": "user", "content": formatted_input}],
            "reasoning": {"effort": reasoning_effort},
            "text": {
                "verbosity": "low",
                "format": {
                    "type": "json_schema",
                    "name": "citation_factcheck",
                    "strict": True,
                    "schema": FACTCHECK_SCHEMA,
                },
            },
        }
        request = Request(
            f"{base}/responses",
            data=json.dumps(request_body).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            },
            method="POST",
        )
    except Exception as error:
        raise _failure_from_exception(error, stage="factcheck_request").with_item(item_id) from None

    attempts = max(1, max_attempts)
    last_error: FactCheckError | None = None
    for attempt in range(attempts):
        try:
            with opener(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
        except Exception as error:
            last_error = _failure_from_exception(error, stage="factcheck_request").with_item(item_id)
            if attempt + 1 < attempts:
                continue
            raise last_error from None

        try:
            content = _parse_responses_body(body)
            parsed = json.loads(content)
        except FactCheckError as error:
            last_error = error.with_item(item_id)
            if last_error.category == "proxy" and attempt + 1 < attempts:
                continue
            raise last_error from None
        except Exception:
            raise FactCheckError("invalid_response", stage="factcheck_response", item_id=item_id) from None

        try:
            return _validate_factcheck_payload(
                parsed,
                expected_item_id=item_id,
                allowed_urls=allowed_urls,
                source_texts_by_url=source_texts_by_url,
            )
        except FactCheckError as error:
            raise error.with_item(item_id) from None

    raise last_error or FactCheckError("proxy", stage="factcheck_request", item_id=item_id)


def _dedupe_urls(urls: Iterable[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in urls:
        clean = raw.strip().rstrip(".,;)")
        if clean and clean not in seen:
            seen.add(clean)
            ordered.append(clean)
    return tuple(ordered)


def parse_brief_items(path: Path, *, include_watchlist: bool = False) -> list[BriefItem]:
    """Parse verifiable items from either a written Markdown brief or a review JSON artifact."""
    file_path = Path(path)
    if file_path.suffix.casefold() == ".json":
        payload = json.loads(file_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"Review artifact must be a JSON object: {file_path}")
        items: list[BriefItem] = []
        sections: list[tuple[str, str]] = [("top", "candidates")]
        if include_watchlist:
            sections.append(("watchlist", "watchlist"))
        for section, key in sections:
            for idx, record in enumerate(payload.get(key, []), start=1):
                if not isinstance(record, dict):
                    continue
                editorial = record.get("editorial") if isinstance(record.get("editorial"), dict) else {}
                parts = [
                    str(record.get("title") or "").strip(),
                    str(record.get("summary") or "").strip(),
                    str(editorial.get("why_now") or "").strip(),
                    str(editorial.get("rationale") or "").strip(),
                    str(editorial.get("caveats") or "").strip(),
                ]
                urls = [
                    str(src.get("url") or "").strip()
                    for src in record.get("sources", [])
                    if isinstance(src, dict) and src.get("url")
                ]
                items.append(
                    BriefItem(
                        item_id=f"{section}-{idx:02d}",
                        section=section,
                        rank=int(record.get("rank") or idx),
                        headline=str(record.get("title") or f"Item {idx}").strip(),
                        claim_text="\n".join(part for part in parts if part),
                        source_urls=_dedupe_urls(urls),
                    )
                )
        return items

    markdown = file_path.read_text(encoding="utf-8")
    top_entries, watch_entries = _extract_raw_top_and_watchlist(markdown)
    parsed_items: list[BriefItem] = []
    for rank, (headline, body, urls) in enumerate(top_entries, start=1):
        parsed_items.append(
            BriefItem(
                item_id=f"top-{rank:02d}",
                section="top",
                rank=rank,
                headline=headline,
                claim_text=body,
                source_urls=_dedupe_urls(urls),
            )
        )
    if include_watchlist:
        for rank, (headline, body, urls) in enumerate(watch_entries, start=1):
            parsed_items.append(
                BriefItem(
                    item_id=f"watchlist-{rank:02d}",
                    section="watchlist",
                    rank=rank,
                    headline=headline,
                    claim_text=body,
                    source_urls=_dedupe_urls(urls),
                )
            )
    return parsed_items


def factcheck_brief(
    path: Path,
    *,
    include_watchlist: bool = False,
    model: str | None = None,
    max_workers: int = 10,
    fetcher: Callable[[str], FetchedSource] | None = None,
    opener: Callable[..., Any] = urlopen,
) -> FactCheckReport:
    """Fetch linked primary sources and run strict Source-vs-Claim verification on a brief."""
    selected_model = (model or os.environ.get("FACTCHECK_MODEL", DEFAULT_FACTCHECK_MODEL)).strip() or DEFAULT_FACTCHECK_MODEL
    brief_items = parse_brief_items(path, include_watchlist=include_watchlist)
    fetch_fn = fetcher or fetch_source_text

    unique_urls = _dedupe_urls(url for item in brief_items for url in item.source_urls)
    fetched_by_url: dict[str, FetchedSource] = {}
    if unique_urls:
        if max_workers > 1 and len(unique_urls) > 1:
            with ThreadPoolExecutor(max_workers=min(max_workers + 2, len(unique_urls))) as pool:
                for src in pool.map(fetch_fn, unique_urls):
                    fetched_by_url[src.url] = src
        else:
            for url in unique_urls:
                fetched_by_url[url] = fetch_fn(url)

    def _verify_one(item: BriefItem) -> ItemFactCheck:
        item_sources = tuple(fetched_by_url[url] for url in item.source_urls if url in fetched_by_url)
        has_fetched = any(src.status == "fetched" and src.text for src in item_sources)
        claims = (
            verify_item_claims(
                item_id=item.item_id,
                headline=item.headline,
                claim_text=item.claim_text,
                sources=item_sources,
                model=selected_model,
                opener=opener,
            )
            if has_fetched
            else ()
        )
        return ItemFactCheck(
            item_id=item.item_id,
            section=item.section,
            rank=item.rank,
            headline=item.headline,
            verdict=rollup_item_verdict(claims, has_fetched_source=has_fetched),
            sources=item_sources,
            claims=claims,
        )

    if max_workers > 1 and len(brief_items) > 1:
        with ThreadPoolExecutor(max_workers=min(max_workers, len(brief_items))) as pool:
            checked_items = tuple(pool.map(_verify_one, brief_items))
    else:
        checked_items = tuple(_verify_one(item) for item in brief_items)

    return FactCheckReport(
        target=str(path),
        model=selected_model,
        items=checked_items,
    )


def should_fail_gate(report: FactCheckReport, *, fail_on: str = "unsupported") -> bool:
    """Return True when the report violates the requested mechanical citation gate."""
    if fail_on == "none":
        return False
    summary = report.summary
    if fail_on == "unsupported":
        return summary["claims_unsupported"] > 0
    if fail_on == "nuance_drift":
        return summary["claims_unsupported"] > 0 or summary["claims_nuance_drift"] > 0
    raise ValueError(f"unsupported fail_on gate: {fail_on}")


def render_factcheck_markdown(report: FactCheckReport) -> str:
    """Render a scannable mechanical verification report with verbatim evidence quotes."""
    s = report.summary
    lines = [
        f"# Source-vs-Claim Fact-Check — {Path(report.target).name}",
        "",
        f"Model: `{report.model}` · Target: `{report.target}`",
        "",
        "| Metric | Count |",
        "| --- | ---: |",
        f"| Items checked | {s['items_total']} |",
        f"| Items supported | {s['items_supported']} |",
        f"| Items nuance_drift | {s['items_nuance_drift']} |",
        f"| Items unsupported | {s['items_unsupported']} |",
        f"| Items unfetched | {s['items_unfetched']} |",
        f"| Claims supported | {s['claims_supported']} |",
        f"| Claims nuance_drift | {s['claims_nuance_drift']} |",
        f"| Claims unsupported | {s['claims_unsupported']} |",
        f"| Verbatim quotes verified | {s['quotes_verified']}/{s['quotes_total']} |",
        "",
    ]
    for item in report.items:
        lines.append(f"## [{item.verdict.upper()}] {item.item_id} — {item.headline}")
        lines.append("")
        for src in item.sources:
            status_note = f"{src.status} ({len(src.text)} chars)" if src.status == "fetched" else f"{src.status}: {src.error}"
            lines.append(f"- Source: {src.url} — `{status_note}`")
        lines.append("")
        if not item.claims:
            lines.append("_No claims verified (source text unavailable)._")
            lines.append("")
            continue
        for claim in item.claims:
            quote_tag = "verified-quote" if claim.quote_verified else "unverified-quote"
            lines.append(f"- **`{claim.verdict}`** — {claim.claim}")
            lines.append(f"  - Reason: {claim.reason}")
            if claim.evidence_quote:
                lines.append(f"  - Quote (`{quote_tag}`, {claim.source_url or 'source'}): \"{claim.evidence_quote}\"")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
