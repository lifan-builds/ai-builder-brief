"""Human- and machine-readable artifacts for daily editorial review."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

from castforge.models import SourceItem

from ai_builder_brief.editorial import EditorialDecision, is_podcast_ready


def build_review_items(
    representatives: Iterable[SourceItem],
    sources: Iterable[SourceItem],
    decisions: Iterable[EditorialDecision],
    *,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Join editorial decisions to source evidence and rank them for review."""

    representatives_by_id = {
        str(item.metadata.get("cluster_id") or item.id): item
        for item in representatives
    }
    sources_by_id: dict[str, list[SourceItem]] = {}
    for item in sources:
        cluster_id = str(item.metadata.get("cluster_id") or item.id)
        sources_by_id.setdefault(cluster_id, []).append(item)

    ranked = sorted(decisions, key=lambda item: (-item.score, item.cluster_id))[:limit]
    items: list[dict[str, Any]] = []
    for rank, decision in enumerate(ranked, 1):
        representative = representatives_by_id[decision.cluster_id]
        evidence = sorted(
            sources_by_id.get(decision.cluster_id, [representative]),
            key=lambda item: (item.id, item.url),
        )
        items.append(
            {
                "rank": rank,
                "cluster_id": decision.cluster_id,
                "title": representative.title,
                "summary": representative.summary,
                "organization": representative.organization,
                "category": representative.category,
                "kind": str(representative.metadata.get("kind", "development")),
                "published_at": representative.published_at,
                "decision": decision.decision,
                "podcast_ready": is_podcast_ready(decision),
                "score": decision.score,
                "editorial": decision.to_dict(),
                "sources": [
                    {
                        "id": source.id,
                        "title": source.title,
                        "url": source.url,
                        "source": source.source,
                        "authority": source.authority,
                        "organization": source.organization,
                        "category": source.category,
                        "published_at": source.published_at,
                        "summary": source.summary,
                    }
                    for source in evidence
                ],
            }
        )
    return items


def render_review_markdown(review_date: date, items: Iterable[dict[str, Any]]) -> str:
    """Render the structured review records without adding uncited claims."""

    records = list(items)
    ready_count = sum(bool(item["podcast_ready"]) for item in records)
    lines = [
        f"# AI Builder Brief editorial review — {review_date.isoformat()}",
        "",
        f"Reviewed candidates shown: {len(records)}. Podcast-ready: {ready_count}.",
        "",
        "This review includes accepted and rejected candidates. Podcast-ready items pass the unchanged evidence and score gate.",
        "",
    ]
    for item in records:
        editorial = item["editorial"]
        actions = ", ".join(editorial["builder_actions"]) or "not specified"
        readiness = "yes" if item["podcast_ready"] else "no"
        lines.extend(
            [
                f"## {item['rank']}. {item['title']}",
                "",
                f"**Cluster ID:** {item['cluster_id']}",
                f"**Decision:** {item['decision']}",
                f"**Podcast-ready:** {readiness}",
                f"**Editorial score:** {item['score']:.2f}",
                f"**Organization / Category:** {item['organization']} / {item['category']}",
                f"**Published:** {item['published_at']}",
                f"**What happened:** {item['summary']}",
                f"**Why now:** {editorial['why_now']}",
                f"**Editorial rationale:** {editorial['rationale']}",
                f"**Builder actions:** {actions}",
                f"**Caveats / Unknowns:** {editorial['caveats'] or 'not specified'}",
                "**Sources:**",
            ]
        )
        lines.extend(
            f"- [{source['source']}]({source['url']}) — {source['authority']}: {source['summary']}"
            for source in item["sources"]
        )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_review_artifacts(
    review_date: date,
    representatives: Iterable[SourceItem],
    sources: Iterable[SourceItem],
    decisions: Iterable[EditorialDecision],
    output_dir: Path,
) -> tuple[Path, Path]:
    """Write matching top-10 JSON and Markdown review artifacts."""

    items = build_review_items(representatives, sources, decisions)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{review_date.isoformat()}.json"
    markdown_path = output_dir / f"{review_date.isoformat()}.md"
    body = {
        "schema_version": 1,
        "review_date": review_date.isoformat(),
        "status": "reviewed",
        "candidate_count": len(items),
        "podcast_ready_count": sum(bool(item["podcast_ready"]) for item in items),
        "candidates": items,
    }
    json_path.write_text(
        json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(
        render_review_markdown(review_date, items),
        encoding="utf-8",
    )
    return json_path, markdown_path
