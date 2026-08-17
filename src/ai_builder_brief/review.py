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
    community_target: int = 6,
) -> list[dict[str, Any]]:
    """Select a diverse 6/4 community/primary review and join its evidence."""

    representatives_by_id = {
        str(item.metadata.get("cluster_id") or item.id): item
        for item in representatives
    }
    sources_by_id: dict[str, list[SourceItem]] = {}
    for item in sources:
        cluster_id = str(item.metadata.get("cluster_id") or item.id)
        sources_by_id.setdefault(cluster_id, []).append(item)

    ranked = sorted(decisions, key=lambda item: (-item.score, item.cluster_id))
    community = [
        decision for decision in ranked
        if bool(representatives_by_id[decision.cluster_id].metadata.get("community_led"))
    ]
    primary = [
        decision for decision in ranked
        if not bool(representatives_by_id[decision.cluster_id].metadata.get("community_led"))
    ]
    selected: list[EditorialDecision] = []
    used_organizations: set[str] = set()
    used_products: set[str] = set()

    def admit(pool: list[EditorialDecision], target: int) -> None:
        for decision in pool:
            if sum(
                bool(representatives_by_id[item.cluster_id].metadata.get("community_led"))
                == bool(representatives_by_id[decision.cluster_id].metadata.get("community_led"))
                for item in selected
            ) >= target:
                break
            representative = representatives_by_id[decision.cluster_id]
            organization = representative.organization.strip().casefold()
            product = str(representative.metadata.get("product_family") or "").strip().casefold()
            if organization and organization in used_organizations:
                continue
            if product and product in used_products:
                continue
            selected.append(decision)
            if organization:
                used_organizations.add(organization)
            if product:
                used_products.add(product)

    admit(community, min(community_target, limit))
    admit(primary, max(0, limit - community_target))
    selected.sort(key=lambda item: (-item.score, item.cluster_id))
    items: list[dict[str, Any]] = []
    for rank, decision in enumerate(selected, 1):
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
                "community_led": bool(representative.metadata.get("community_led")),
                "community_signal_types": list(representative.metadata.get("community_signal_types", [])),
                "product_family": str(representative.metadata.get("product_family", "")),
                "published_at": representative.published_at,
                "decision": decision.decision,
                "podcast_ready": is_podcast_ready(decision, evidence),
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
                        "signals": {
                            key: source.metadata[key]
                            for key in (
                                "x_account", "x_likes", "x_retweets", "x_engagement",
                                "hn_points", "hn_comments", "hf_upvotes", "hf_likes",
                                "hf_downloads", "hf_trending_score", "repository_stars",
                                "repository_forks", "repository_open_issues", "delta_24h",
                                "delta_7d", "momentum_score",
                            )
                            if key in source.metadata
                        },
                    }
                    for source in evidence
                ],
            }
        )
    return items


def render_review_markdown(
    review_date: date,
    items: Iterable[dict[str, Any]],
    *,
    source_health: dict[str, Any] | None = None,
) -> str:
    """Render the structured review records without adding uncited claims."""

    records = list(items)
    ready_count = sum(bool(item["podcast_ready"]) for item in records)
    community_count = sum(bool(item["community_led"]) for item in records)
    primary_count = len(records) - community_count
    lines = [
        f"# AI Builder Brief editorial review — {review_date.isoformat()}",
        "",
        f"Reviewed candidates shown: {len(records)}. Community-led: {community_count}/6. Major primary: {primary_count}/4. Podcast-ready: {ready_count}.",
        "",
        "This review includes accepted and rejected candidates. Podcast-ready items pass the unchanged evidence and score gate.",
        "",
    ]
    if source_health:
        x_health = source_health.get("x_panel", {})
        lines.extend([
            "## Source health",
            "",
            f"X panel healthy: {'yes' if x_health.get('healthy') else 'no'}; accounts: {x_health.get('successful_accounts', 0)}/{x_health.get('configured_accounts', 0)}; in-window posts: {x_health.get('in_window_posts', 0)}.",
            "",
        ])
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
                f"**Review class:** {'community-led' if item['community_led'] else 'major primary'}",
                f"**Community signals:** {', '.join(item['community_signal_types']) or 'none'}",
                f"**Product family:** {item['product_family'] or 'not specified'}",
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
    *,
    source_health: dict[str, Any] | None = None,
) -> tuple[Path, Path]:
    """Write matching top-10 JSON and Markdown review artifacts."""

    items = build_review_items(representatives, sources, decisions)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{review_date.isoformat()}.json"
    markdown_path = output_dir / f"{review_date.isoformat()}.md"
    community_count = sum(bool(item["community_led"]) for item in items)
    primary_count = len(items) - community_count
    body = {
        "schema_version": 1,
        "review_date": review_date.isoformat(),
        "status": "reviewed",
        "candidate_count": len(items),
        "podcast_ready_count": sum(bool(item["podcast_ready"]) for item in items),
        "target_mix": {"community_led": 6, "major_primary": 4},
        "actual_mix": {"community_led": community_count, "major_primary": primary_count},
        "mix_shortfall": {
            "community_led": max(0, 6 - community_count),
            "major_primary": max(0, 4 - primary_count),
        },
        **({"source_health": source_health} if source_health else {}),
        "candidates": items,
    }
    json_path.write_text(
        json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(
        render_review_markdown(review_date, items, source_health=source_health),
        encoding="utf-8",
    )
    return json_path, markdown_path
