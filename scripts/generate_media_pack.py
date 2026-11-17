#!/usr/bin/env python3
"""Generate grounded multimodal distribution packs via the local QuotaSwitch proxy.

Supports two workflows:
1. Issue pack mode (`--issue` or `--latest-issue`): reads a verified AI Builder
   Brief Markdown issue and generates:
   - `hero-landscape.png` (`1536x1024` editorial technical illustration)
   - `xhs-card-1-cover.png`, `xhs-card-2-insight.png`, `xhs-card-3-takeaway.png`
     (`1024x1536` vertical RedNote / Xiaohongshu visual story cards)
   - `briefing-script.txt` + `briefing.mp3` (~90-120s narrated builder briefing)
2. Custom prompt / manifest mode (`--prompt` or `--manifest`): generates single
   or batched visual/audio assets for brand explorations or future posts.

All API requests route strictly through a loopback OpenAI-compatible proxy
(`http://127.0.0.1:18455/v1` with `Bearer quotaswitch-local` by default).
"""

from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import struct
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import yaml

DEFAULT_BASE_URL = "http://127.0.0.1:18455/v1"
DEFAULT_API_TOKEN = "quotaswitch-local"
DEFAULT_IMAGE_MODEL = "gpt-image-2.6-sunburst"
DEFAULT_SCRIPT_MODEL = "gpt-6.1-sol"
DEFAULT_TTS_MODEL = "gpt-4o-mini-tts"
DEFAULT_TTS_VOICE = "ash"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
SUPPORTED_SIZES = frozenset({"1536x1024", "1024x1536", "1024x1024"})

_SECRET_PATTERN = re.compile(r"sk-[A-Za-z0-9_-]+")
_ISSUE_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")
_RANKED_ITEM_RE = re.compile(
    r"^(\d+)\.\s+\*\*(.+?)\*\*\s*(?:—|--|-)\s*\*\*(.+?)\*\*\.\s*(.+)$"
)
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")


@dataclass(frozen=True)
class IssueItem:
    rank: int
    headline: str
    date_label: str
    summary: str
    links: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ParsedIssue:
    path: Path
    slug: str
    title: str
    coverage_window: str
    read_on_the_day: str
    items: list[IssueItem]
    watchlist: list[str]
    raw_markdown: str


@dataclass(frozen=True)
class ImageTask:
    name: str
    filename: str
    prompt: str
    size: str = "1536x1024"
    quality: str = "high"
    model: str = DEFAULT_IMAGE_MODEL


@dataclass(frozen=True)
class ProxyResponse:
    status: int
    body: bytes
    content_type: str
    quotaswitch_account_id: str | None
    quotaswitch_provider: str | None
    request_id: str | None


def redact_secrets(text: str) -> str:
    """Strip any sk-* token pattern before surfacing error diagnostics."""
    return _SECRET_PATTERN.sub("[REDACTED_KEY]", text)


def validate_proxy_config(base_url: str, token: str) -> str:
    """Enforce loopback-only routing and refuse raw OpenAI secret keys."""
    parsed = urlsplit(base_url.strip())
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in LOOPBACK_HOSTS:
        raise ValueError(
            f"Refusing non-loopback base URL {base_url!r}; route through http://127.0.0.1:18455/v1."
        )
    cleaned_token = token.strip()
    if not cleaned_token:
        raise ValueError("Proxy bearer token must not be empty.")
    if cleaned_token.startswith("sk-"):
        raise ValueError("Refusing raw sk-* API key; use 'quotaswitch-local'.")
    return base_url.rstrip("/")


def inspect_png_dimensions(data: bytes) -> tuple[int, int]:
    """Verify PNG signature + IHDR chunk and return (width, height)."""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise ValueError("Generated image bytes do not have a valid PNG IHDR header.")
    width, height = struct.unpack(">II", data[16:24])
    return int(width), int(height)


def _header_get(headers: Any, name: str) -> str | None:
    target = name.lower()
    for key, val in headers.items():
        if key.lower() == target and val:
            return str(val).strip()
    return None


def post_proxy_json(
    *,
    base_url: str,
    token: str,
    endpoint: str,
    payload: dict[str, Any],
    timeout: float = 240.0,
) -> ProxyResponse:
    """Send a JSON POST to the local QuotaSwitch proxy and capture routing headers."""
    normalized_base = validate_proxy_config(base_url, token)
    url = f"{normalized_base}/{endpoint.lstrip('/')}"
    req = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token.strip()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return ProxyResponse(
                status=int(resp.status),
                body=body,
                content_type=_header_get(resp.headers, "Content-Type") or "",
                quotaswitch_account_id=_header_get(resp.headers, "X-Quotaswitch-Account-ID"),
                quotaswitch_provider=_header_get(resp.headers, "X-Quotaswitch-Provider"),
                request_id=_header_get(resp.headers, "X-Request-ID"),
            )
    except HTTPError as exc:
        err_body = redact_secrets(exc.read().decode("utf-8", errors="replace")[:600])
        acct = _header_get(exc.headers, "X-Quotaswitch-Account-ID")
        raise RuntimeError(
            f"Proxy HTTP {exc.code} on /{endpoint.lstrip('/')} "
            f"(account_id={acct or 'none'}): {err_body}"
        ) from None
    except URLError as exc:
        raise RuntimeError(
            f"Proxy connection error on /{endpoint.lstrip('/')}: {redact_secrets(str(exc.reason))}"
        ) from None


def parse_issue_markdown(path: Path) -> ParsedIssue:
    """Parse a verified AI Builder Brief Markdown issue into structured fields."""
    raw = path.read_text(encoding="utf-8")
    lines = raw.splitlines()
    title = path.stem
    coverage_window = ""
    read_on_the_day = ""
    items: list[IssueItem] = []
    watchlist: list[str] = []
    section = "header"

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("# ") and title == path.stem:
            title = line[2:].strip()
            continue
        if line.startswith("## "):
            heading = line[3:].strip().lower()
            if heading.startswith("top"):
                section = "top"
            elif heading.startswith("watchlist"):
                section = "watchlist"
            else:
                section = heading
            continue

        if section == "header":
            if line.lower().startswith("coverage window:"):
                coverage_window = line.split(":", 1)[1].strip()
            elif line.lower().startswith("read on the day:"):
                read_on_the_day = line.split(":", 1)[1].strip()
        elif section == "top":
            match = _RANKED_ITEM_RE.match(line)
            if match:
                rank = int(match.group(1))
                headline = match.group(2).strip()
                date_label = match.group(3).strip()
                body_with_links = match.group(4).strip()
                links = [m.group(2) for m in _MD_LINK_RE.finditer(body_with_links)]
                clean_summary = _MD_LINK_RE.sub(r"\1", body_with_links)
                items.append(
                    IssueItem(
                        rank=rank,
                        headline=headline,
                        date_label=date_label,
                        summary=clean_summary,
                        links=links,
                    )
                )
        elif section == "watchlist" and line.startswith("- "):
            watchlist.append(_MD_LINK_RE.sub(r"\1", line[2:].strip()))

    if not items:
        from ai_builder_brief.journal import _parse_ranked_items

        for rank_idx, (story_title, story_date, summary, link_pairs) in enumerate(_parse_ranked_items(raw), start=1):
            items.append(
                IssueItem(
                    rank=rank_idx,
                    headline=story_title,
                    date_label=story_date,
                    summary=summary,
                    links=[u for _, u in link_pairs],
                )
            )
    if not items:
        raise ValueError(f"No ranked Top 10 items found in issue file {path}")

    slug_match = re.search(r"\d{4}-\d{2}-\d{2}", path.stem)
    slug = slug_match.group(0) if slug_match else path.stem
    return ParsedIssue(
        path=path,
        slug=slug,
        title=title,
        coverage_window=coverage_window,
        read_on_the_day=read_on_the_day,
        items=items,
        watchlist=watchlist,
        raw_markdown=raw,
    )


def find_latest_issue(repo_root: Path) -> Path:
    """Locate the latest dated YYYY-MM-DD.md issue in history/."""
    history_dir = repo_root / "history"
    candidates = sorted(
        p for p in history_dir.iterdir() if p.is_file() and _ISSUE_DATE_RE.match(p.name)
    )
    if not candidates:
        raise FileNotFoundError(f"No dated issue Markdown files found in {history_dir}")
    return candidates[-1]


def build_issue_image_tasks(
    issue: ParsedIssue,
    *,
    image_model: str = DEFAULT_IMAGE_MODEL,
    quality: str = "high",
) -> list[ImageTask]:
    """Construct grounded editorial + RedNote visual prompts from verified issue items."""
    top = issue.items
    lead = top[0]
    second = top[1] if len(top) > 1 else lead
    third = top[2] if len(top) > 2 else lead
    fourth = top[3] if len(top) > 3 else second
    sixth = next((i for i in top if i.rank == 6), third)
    seventh = next((i for i in top if i.rank == 7), third)
    eighth = next((i for i in top if i.rank == 8), fourth)
    tenth = next((i for i in top if i.rank == 10), fourth)

    thesis = issue.read_on_the_day or f"Key developments led by {lead.headline}."
    story_context = " | ".join(f"#{item.rank}: {item.headline}" for item in top[:8])

    def short_tag(headline: str, max_words: int = 4) -> str:
        words = re.findall(r"[A-Za-z0-9.+-]+", headline)
        return " ".join(words[:max_words]).upper() or "BUILDER SIGNAL"

    tag1 = short_tag(lead.headline)
    tag2 = short_tag(second.headline)
    tag3 = short_tag(third.headline)
    tag4 = short_tag(fourth.headline)
    tag6 = short_tag(sixth.headline)
    tag7 = short_tag(seventh.headline)
    tag8 = short_tag(eighth.headline)
    tag10 = short_tag(tenth.headline)

    hero_prompt = (
        f"Wide 3:2 editorial technical illustration for '{issue.title}'. "
        "Visual style: ultra-crisp dark-slate architectural schematic and flagship engineering magazine hero artwork "
        "on deep midnight slate (#0c1720) with luminous emerald-mint (#34d399), sky-cyan (#7cc4fa), and warm amber (#f7c873) signal lines. "
        f"Semantic editorial context (for visual iconography only — do NOT paste raw headlines verbatim or invent unverified percentages): {thesis} ({story_context}). "
        "Composition: Top-left minimal monospace pill reading 'AI BUILDER BRIEF · "
        f"{issue.slug}', bold clean headline '{tag1} & {tag2}', and three interconnected isometric engineering chambers left-to-right: "
        f"(Left chamber) 'LEAD EVIDENCE' — split comparison diagram for '{tag1}' with verification checkmarks; "
        f"(Center chamber) 'SYSTEMS & HARNESS' — isolated verification sandbox above a clean workflow DAG for '{tag2}' and '{tag3}'; "
        f"(Right chamber) 'RUNTIME & API STACK' — clean modular stack icons with complete short labels ONLY: '{tag4}', '{tag6}', '{tag7}', '{tag8}', and '{tag10}'. "
        "Every visible label must be complete, grammatical, and razor-sharp; zero truncated sentences, zero invented numbers."
    )

    xhs_cover_prompt = (
        f"Vertical 2:3 Xiaohongshu (RedNote) tech editorial cover poster for '{issue.title}'. "
        "Visual aesthetic: flagship indie-engineering magazine cover on deep midnight navy-slate (#0b131b) with subtle architectural grid, "
        "emerald (#34d399), electric cyan (#66a6ff), and warm amber (#fbbf24) glowing accents. "
        f"Top header pill badge: 'AI BUILDER BRIEF · {issue.slug}'. "
        f"Bold, ultra-clean poster title at upper center: '{tag1}'. "
        "Below the title, three crisp glassmorphic highlight cards with icons and COMPLETE short titles ONLY (no cut-off sentences): "
        f"Card 1: '{tag1}', "
        f"Card 2: '{tag2}', "
        f"Card 3: '{tag3}'. "
        "Lower centerpiece visual: striking isometric 3D technical diagram of an AI agent signal passing through a glowing emerald 'VERIFICATION GATE' into a structured 'BUILDER WORKFLOW' node cluster. "
        "Bottom clean footer strip: '10 SOURCE-LINKED STORIES · EVIDENCE FIRST'. "
        "Crisp typography, complete phrases only, balanced margins for mobile 2:3 feed."
    )

    xhs_insight_prompt = (
        f"Vertical 2:3 Xiaohongshu (RedNote) technical insight infographic card for AI Builder Brief ({issue.slug}). "
        "Style: high-craft dark-slate (#0d1821) architectural schematic with crisp mint-emerald (#34d399), amber (#f59e0b), and ice-blue (#7dd3fc) diagrams. "
        f"Top header bar ONLY: 'AI BUILDER BRIEF | {issue.slug}' above banner 'CORE INSIGHT · {tag1}' (do NOT add a bullet or summary text strip under the banner; go straight into the two diagram panels). "
        f"Semantic reference (do NOT render these sentences verbatim): {lead.headline}; {second.headline}; {sixth.headline}. "
        "Two spacious visual diagram panels stacked vertically filling the card: "
        f"Upper panel — '{tag1}': clean engineering comparison diagram paired with a verification loop 'BENCHMARK -> CALIBRATE -> VERIFY'. "
        f"Lower panel — '{tag2} & {tag6}': fortified sandbox and system boundary schematic separating verified execution from untrusted inputs. "
        "Every visible label must be complete, concise, and razor-sharp."
    )

    xhs_takeaway_prompt = (
        f"Vertical 2:3 Xiaohongshu (RedNote) builder action blueprint card for AI Builder Brief ({issue.slug}). "
        "Style: refined dark-mode systems engineering card (#0b141d) with crisp borders (#243849), emerald checkmarks (#34d399), cyan workflow nodes (#66a6ff), and amber highlights (#fbbf24). "
        f"Top header bar ONLY: 'BUILDER MOVES · SHIP WITH EVIDENCE' and subheader 'AI BUILDER BRIEF | {issue.slug}' (do NOT render a checklist strip above Module 01; go straight from the header into the three spacious bento modules). "
        f"Semantic reference (do NOT render verbatim): {fourth.headline}; {seventh.headline}; {eighth.headline}; {tenth.headline}. "
        "Three clean horizontal bento modules stacked vertically using ONLY complete, self-contained labels: "
        f"Module 01 — '01 · {tag4}': workflow icon + 'PLAN -> TEST -> VERIFY' DAG. "
        f"Module 02 — '02 · {tag7}': runtime & memory contract schematic + '{tag8}'. "
        f"Module 03 — '03 · {tag10}': explicit permission boundary & production check badge. "
        "Bottom sign-off pill: 'CLIFAN.COM/AI-BUILDER-BRIEF'. "
        "Ultra-clean layout, complete grammatical labels, generous vertical spacing."
    )

    return [
        ImageTask(
            name="hero-landscape",
            filename="hero-landscape.png",
            prompt=hero_prompt,
            size="1536x1024",
            quality=quality,
            model=image_model,
        ),
        ImageTask(
            name="xhs-card-1-cover",
            filename="xhs-card-1-cover.png",
            prompt=xhs_cover_prompt,
            size="1024x1536",
            quality=quality,
            model=image_model,
        ),
        ImageTask(
            name="xhs-card-2-insight",
            filename="xhs-card-2-insight.png",
            prompt=xhs_insight_prompt,
            size="1024x1536",
            quality=quality,
            model=image_model,
        ),
        ImageTask(
            name="xhs-card-3-takeaway",
            filename="xhs-card-3-takeaway.png",
            prompt=xhs_takeaway_prompt,
            size="1024x1536",
            quality=quality,
            model=image_model,
        ),
    ]


def generate_image(
    task: ImageTask,
    *,
    output_dir: Path,
    base_url: str = DEFAULT_BASE_URL,
    token: str = DEFAULT_API_TOKEN,
) -> dict[str, Any]:
    """Generate a single PNG image via /v1/images/generations and verify its header."""
    if task.size not in SUPPORTED_SIZES:
        raise ValueError(f"Unsupported image size {task.size!r}; choose from {sorted(SUPPORTED_SIZES)}")

    payload: dict[str, Any] = {
        "model": task.model,
        "prompt": task.prompt,
        "size": task.size,
        "n": 1,
    }
    if task.quality and task.quality != "default":
        payload["quality"] = task.quality

    try:
        resp = post_proxy_json(
            base_url=base_url,
            token=token,
            endpoint="images/generations",
            payload=payload,
            timeout=300.0,
        )
    except RuntimeError as first_err:
        if "quality" in payload and "HTTP 400" in str(first_err):
            fallback_payload = {k: v for k, v in payload.items() if k != "quality"}
            resp = post_proxy_json(
                base_url=base_url,
                token=token,
                endpoint="images/generations",
                payload=fallback_payload,
                timeout=300.0,
            )
        else:
            raise

    parsed = json.loads(resp.body.decode("utf-8"))
    items = parsed.get("data") or []
    if not items or not isinstance(items[0], dict):
        raise RuntimeError(f"Unexpected images/generations payload for {task.name}: missing data[0]")

    first = items[0]
    if first.get("b64_json"):
        img_bytes = base64.b64decode(first["b64_json"])
    elif first.get("url"):
        with urlopen(str(first["url"]), timeout=120.0) as dl:
            img_bytes = dl.read()
    else:
        raise RuntimeError(f"Image response for {task.name} contained neither b64_json nor url")

    width, height = inspect_png_dimensions(img_bytes)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / task.filename
    out_path.write_bytes(img_bytes)

    return {
        "name": task.name,
        "path": str(out_path.resolve()),
        "model": task.model,
        "requested_size": task.size,
        "width": width,
        "height": height,
        "bytes": len(img_bytes),
        "quotaswitch_account_id": resp.quotaswitch_account_id,
        "quotaswitch_provider": resp.quotaswitch_provider,
    }


def distill_briefing_script(
    issue: ParsedIssue,
    *,
    base_url: str = DEFAULT_BASE_URL,
    token: str = DEFAULT_API_TOKEN,
    script_model: str = DEFAULT_SCRIPT_MODEL,
) -> tuple[str, dict[str, Any]]:
    """Distill a verified issue Markdown file into a ~90-120s spoken briefing script."""
    issue_facts_lines = [
        f"Title: {issue.title}",
        f"Coverage window: {issue.coverage_window}",
        f"Read on the day: {issue.read_on_the_day}",
        "",
        "Verified Ranked Developments:",
    ]
    for item in issue.items:
        issue_facts_lines.append(
            f"{item.rank}. {item.headline} ({item.date_label}): {item.summary}"
        )
    verified_context = "\n".join(issue_facts_lines)

    system_prompt = (
        "You write concise, factual morning audio scripts for AI Builder Brief. "
        "STRICT GROUNDING RULES:\n"
        "1. Use ONLY facts, dates, metrics, model/project names, builder implications, and caveats explicitly present in the provided issue text.\n"
        "2. Never invent numbers, benchmarks, URLs, quotes, or unverified claims. Preserve essential caveats (e.g., internal examples vs independent replication, bounded tasks, exact VRAM plus host RAM figures, ABI breaking policy, or limited rollout).\n"
        "3. Write natural, crisp spoken English prose ready for direct text-to-speech synthesis: no Markdown formatting, no bullet lists, no URLs, no speaker labels, and no stage directions.\n"
        "4. Target strictly 205 to 230 words so the narrated audio runs approximately 95 to 115 seconds (inside the 90-120s window) at a crisp morning-standup pace.\n"
        "5. Open with the date and the issue's overarching read on the day, cover the top decision-changing developments and concrete builder takeaways cleanly, and close with a one-sentence sign-off pointing listeners to the source manifest in AI Builder Brief."
    )

    payload = {
        "model": script_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": verified_context},
        ],
        "temperature": 0.2,
        "max_completion_tokens": 900,
    }
    resp = post_proxy_json(
        base_url=base_url,
        token=token,
        endpoint="chat/completions",
        payload=payload,
        timeout=120.0,
    )
    data = json.loads(resp.body.decode("utf-8"))
    choices = data.get("choices") or []
    if not choices:
        raise RuntimeError("chat/completions returned no choices for briefing script")
    content = str(choices[0].get("message", {}).get("content") or "").strip()
    if not content:
        raise RuntimeError("chat/completions returned empty briefing script content")

    word_count = len(content.split())
    meta = {
        "model": script_model,
        "words": word_count,
        "chars": len(content),
        "quotaswitch_account_id": resp.quotaswitch_account_id,
        "quotaswitch_provider": resp.quotaswitch_provider,
    }
    return content, meta


def synthesize_audio(
    script_text: str,
    *,
    output_path: Path,
    base_url: str = DEFAULT_BASE_URL,
    token: str = DEFAULT_API_TOKEN,
    tts_model: str = DEFAULT_TTS_MODEL,
    voice: str = DEFAULT_TTS_VOICE,
    speed: float = 1.0,
) -> dict[str, Any]:
    """Synthesize MP3 narration from spoken script text via /v1/audio/speech."""
    cleaned = script_text.strip()
    if not cleaned:
        raise ValueError("Cannot synthesize empty script text.")
    if len(cleaned) > 4096:
        raise ValueError(
            f"Script length ({len(cleaned)} chars) exceeds single-request 4096-char TTS limit."
        )

    payload: dict[str, Any] = {
        "model": tts_model,
        "voice": voice,
        "input": cleaned,
        "response_format": "mp3",
        "speed": speed,
    }
    if tts_model.startswith("gpt-4o"):
        payload["instructions"] = (
            "Speak as a crisp, natural, well-paced staff engineer delivering a two-minute morning "
            "technical briefing before stand-up. Maintain an energetic, clear broadcast tempo (~130 wpm) "
            "with clean transitions and sharp articulation on metrics and caveats."
        )

    try:
        resp = post_proxy_json(
            base_url=base_url,
            token=token,
            endpoint="audio/speech",
            payload=payload,
            timeout=180.0,
        )
    except RuntimeError as first_err:
        if "instructions" in payload and "HTTP 400" in str(first_err):
            fallback = {k: v for k, v in payload.items() if k != "instructions"}
            resp = post_proxy_json(
                base_url=base_url,
                token=token,
                endpoint="audio/speech",
                payload=fallback,
                timeout=180.0,
            )
        else:
            raise

    if len(resp.body) < 1024:
        raise RuntimeError(f"Audio response too small ({len(resp.body)} bytes) to be valid MP3")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(resp.body)
    return {
        "path": str(output_path.resolve()),
        "model": tts_model,
        "voice": voice,
        "speed": speed,
        "bytes": len(resp.body),
        "quotaswitch_account_id": resp.quotaswitch_account_id,
        "quotaswitch_provider": resp.quotaswitch_provider,
    }


def load_manifest_tasks(
    manifest_path: Path,
    *,
    default_image_model: str = DEFAULT_IMAGE_MODEL,
    default_quality: str = "high",
) -> tuple[list[ImageTask], dict[str, Any]]:
    """Load custom image tasks from a JSON or YAML manifest."""
    raw = manifest_path.read_text(encoding="utf-8")
    if manifest_path.suffix.lower() in {".yaml", ".yml"}:
        doc = yaml.safe_load(raw)
    else:
        doc = json.loads(raw)

    if not isinstance(doc, dict) or not isinstance(doc.get("images"), list):
        raise ValueError(f"Manifest {manifest_path} must be an object with an 'images' list.")

    tasks: list[ImageTask] = []
    for idx, entry in enumerate(doc["images"], 1):
        if not isinstance(entry, dict):
            raise ValueError(f"Manifest image entry #{idx} must be an object.")
        name = str(entry.get("name") or f"asset-{idx}").strip()
        filename = str(entry.get("filename") or f"{name}.png").strip()
        prompt = str(entry.get("prompt") or "").strip()
        if not prompt:
            raise ValueError(f"Manifest image entry {name!r} is missing 'prompt'.")
        tasks.append(
            ImageTask(
                name=name,
                filename=filename,
                prompt=prompt,
                size=str(entry.get("size") or "1536x1024"),
                quality=str(entry.get("quality") or default_quality),
                model=str(entry.get("model") or default_image_model),
            )
        )
    return tasks, doc


def run_image_batch(
    tasks: list[ImageTask],
    *,
    output_dir: Path,
    base_url: str,
    token: str,
    concurrency: int,
) -> list[dict[str, Any]]:
    """Execute ImageTasks concurrently while preserving manifest order."""
    if not tasks:
        return []
    workers = max(1, min(concurrency, len(tasks)))
    ordered: dict[int, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {
            pool.submit(
                generate_image,
                task,
                output_dir=output_dir,
                base_url=base_url,
                token=token,
            ): idx
            for idx, task in enumerate(tasks)
        }
        for fut in as_completed(future_map):
            idx = future_map[fut]
            ordered[idx] = fut.result()
    return [ordered[i] for i in range(len(tasks))]


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate grounded multimodal packs via local QuotaSwitch OpenAI proxy."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--issue",
        type=Path,
        help="Path to a verified issue Markdown file (e.g., history/2026-11-14.md).",
    )
    source.add_argument(
        "--latest-issue",
        action="store_true",
        help="Automatically select the latest dated YYYY-MM-DD.md in history/.",
    )
    source.add_argument(
        "--manifest",
        type=Path,
        help="Path to a JSON or YAML asset manifest for custom image packs.",
    )
    source.add_argument(
        "--prompt",
        type=str,
        help="Custom single-image prompt for ad-hoc visual generation.",
    )

    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        help="Target output directory (defaults to output/media-packs/<slug> for issues).",
    )
    parser.add_argument(
        "--filename",
        type=str,
        default="custom.png",
        help="Output filename when using --prompt (default: custom.png).",
    )
    parser.add_argument(
        "--size",
        type=str,
        default="1536x1024",
        choices=sorted(SUPPORTED_SIZES),
        help="Image size when using --prompt (default: 1536x1024).",
    )
    parser.add_argument(
        "--quality",
        type=str,
        default="high",
        help="Image quality setting (e.g., high, medium, low, default).",
    )
    parser.add_argument(
        "--only",
        choices=("all", "images", "audio"),
        default="all",
        help="Subset of assets to generate in issue mode (default: all).",
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default=DEFAULT_BASE_URL,
        help=f"Loopback proxy base URL (default: {DEFAULT_BASE_URL}).",
    )
    parser.add_argument(
        "--token",
        type=str,
        default=DEFAULT_API_TOKEN,
        help="Loopback proxy bearer token (default: quotaswitch-local).",
    )
    parser.add_argument(
        "--image-model",
        type=str,
        default=DEFAULT_IMAGE_MODEL,
        help=f"Image generation model (default: {DEFAULT_IMAGE_MODEL}).",
    )
    parser.add_argument(
        "--script-model",
        type=str,
        default=DEFAULT_SCRIPT_MODEL,
        help=f"Chat model for spoken script distillation (default: {DEFAULT_SCRIPT_MODEL}).",
    )
    parser.add_argument(
        "--tts-model",
        type=str,
        default=DEFAULT_TTS_MODEL,
        help=f"Speech synthesis model (default: {DEFAULT_TTS_MODEL}).",
    )
    parser.add_argument(
        "--voice",
        type=str,
        default=DEFAULT_TTS_VOICE,
        help=f"TTS voice (default: {DEFAULT_TTS_VOICE}).",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="TTS playback speed multiplier (default: 1.0).",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=3,
        help="Parallel worker count for image generation (default: 3).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[1]
    base_url = validate_proxy_config(args.base_url, args.token)

    receipt: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "proxy_base_url": base_url,
        "images": [],
        "audio": None,
        "script": None,
    }

    if args.issue or args.latest_issue:
        issue_path = find_latest_issue(repo_root) if args.latest_issue else args.issue.resolve()
        issue = parse_issue_markdown(issue_path)
        output_dir = (
            args.output_dir.resolve()
            if args.output_dir
            else (repo_root / "output" / "media-packs" / issue.slug)
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        receipt["mode"] = "issue"
        receipt["issue"] = {
            "path": str(issue.path),
            "slug": issue.slug,
            "title": issue.title,
            "item_count": len(issue.items),
        }

        if args.only in {"all", "images"}:
            tasks = build_issue_image_tasks(
                issue,
                image_model=args.image_model,
                quality=args.quality,
            )
            receipt["images"] = run_image_batch(
                tasks,
                output_dir=output_dir,
                base_url=base_url,
                token=args.token,
                concurrency=args.concurrency,
            )

        if args.only in {"all", "audio"}:
            script_text, script_meta = distill_briefing_script(
                issue,
                base_url=base_url,
                token=args.token,
                script_model=args.script_model,
            )
            script_path = output_dir / "briefing-script.txt"
            script_path.write_text(script_text + "\n", encoding="utf-8")
            script_meta["path"] = str(script_path.resolve())
            receipt["script"] = script_meta

            audio_meta = synthesize_audio(
                script_text,
                output_path=output_dir / "briefing.mp3",
                base_url=base_url,
                token=args.token,
                tts_model=args.tts_model,
                voice=args.voice,
                speed=args.speed,
            )
            receipt["audio"] = audio_meta

    elif args.manifest:
        manifest_path = args.manifest.resolve()
        tasks, doc = load_manifest_tasks(
            manifest_path,
            default_image_model=args.image_model,
            default_quality=args.quality,
        )
        default_out = doc.get("output_dir")
        if args.output_dir:
            output_dir = args.output_dir.resolve()
        elif default_out:
            output_dir = (manifest_path.parent / str(default_out)).resolve()
        else:
            output_dir = (repo_root / "output" / "custom-pack").resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        receipt["mode"] = "manifest"
        receipt["manifest_path"] = str(manifest_path)
        receipt["images"] = run_image_batch(
            tasks,
            output_dir=output_dir,
            base_url=base_url,
            token=args.token,
            concurrency=args.concurrency,
        )

    else:
        output_dir = (
            args.output_dir.resolve()
            if args.output_dir
            else (repo_root / "output" / "custom").resolve()
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        task = ImageTask(
            name=Path(args.filename).stem,
            filename=args.filename,
            prompt=args.prompt,
            size=args.size,
            quality=args.quality,
            model=args.image_model,
        )
        receipt["mode"] = "prompt"
        receipt["images"] = [
            generate_image(
                task,
                output_dir=output_dir,
                base_url=base_url,
                token=args.token,
            )
        ]

    manifest_out = output_dir / "media-pack-receipt.json"
    manifest_out.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    receipt["receipt_path"] = str(manifest_out)
    print(json.dumps(receipt, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
