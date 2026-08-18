"""Human- and machine-readable artifacts for daily editorial review."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

from castforge.models import SourceItem

from ai_builder_brief.editorial import EditorialDecision, is_podcast_ready


def review_priority(
    decision: EditorialDecision,
    representative: SourceItem,
) -> tuple[bool, float, int, str]:
    """Apply source-class opportunity cost after model review."""

    editorial_class = str(representative.metadata.get("editorial_class", "major_development"))
    if editorial_class == "maintenance_release":
        exceptional = (
            decision.decision == "accept"
            and decision.score >= 80
            and decision.impact == 4
            and decision.actionability >= 3
            and decision.novelty == 4
            and decision.audience_breadth == 4
        )
        return (
            exceptional,
            decision.score - 10,
            -10,
            "exceptional broad release" if exceptional else "maintenance release did not change a broad builder assumption",
        )
    if editorial_class == "research":
        exceptional = (
            decision.decision == "accept"
            and decision.score >= 80
            and decision.impact == 4
            and decision.actionability >= 3
            and decision.novelty == 4
            and decision.evidence >= 3
            and decision.audience_breadth >= 3
        )
        return (
            exceptional,
            decision.score - 5,
            -5,
            "exceptional practical research" if exceptional else "research did not clear the exceptional-news threshold",
        )
    if editorial_class == "community_theme":
        eligible = decision.score >= 60 and decision.impact >= 3 and decision.audience_breadth >= 3
        return (
            eligible,
            decision.score,
            0,
            "consequential community theme" if eligible else "community theme did not clear the consequence floor",
        )
    eligible = decision.score >= 65 and decision.impact >= 3 and decision.audience_breadth >= 2
    return (
        eligible,
        decision.score,
        0,
        "consequential major development" if eligible else "development did not clear the consequence floor",
    )


def _review_item(
    decision: EditorialDecision,
    *,
    rank: int,
    tier: str,
    representative: SourceItem,
    evidence: list[SourceItem],
    priority_record: tuple[bool, float, int, str],
) -> dict[str, Any]:
    _, priority, adjustment, quality_reason = priority_record
    return {
        "rank": rank,
        "tier": tier,
        "cluster_id": decision.cluster_id,
        "title": representative.title,
        "summary": representative.summary,
        "organization": representative.organization,
        "category": representative.category,
        "kind": str(representative.metadata.get("kind", "development")),
        "editorial_class": str(representative.metadata.get("editorial_class", "major_development")),
        "theme_key": str(representative.metadata.get("theme_key") or decision.cluster_id),
        "community_led": bool(representative.metadata.get("community_led")),
        "community_signal_types": list(representative.metadata.get("community_signal_types", [])),
        "product_family": str(representative.metadata.get("product_family", "")),
        "published_at": representative.published_at,
        "decision": decision.decision,
        "podcast_ready": is_podcast_ready(decision, evidence),
        "score": decision.score,
        "review_priority": priority,
        "priority_adjustment": adjustment,
        "quality_reason": quality_reason,
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


def build_review_items(
    representatives: Iterable[SourceItem],
    sources: Iterable[SourceItem],
    decisions: Iterable[EditorialDecision],
    *,
    limit: int = 10,
    community_target: int = 6,
) -> list[dict[str, Any]]:
    """Select a diverse 6/4 community/primary review and join its evidence."""

    representatives = list(representatives)
    sources = list(sources)
    decisions = list(decisions)
    representatives_by_id = {
        str(item.metadata.get("cluster_id") or item.id): item
        for item in representatives
    }
    sources_by_id: dict[str, list[SourceItem]] = {}
    for item in sources:
        cluster_id = str(item.metadata.get("cluster_id") or item.id)
        sources_by_id.setdefault(cluster_id, []).append(item)

    priority_by_id = {
        decision.cluster_id: review_priority(
            decision, representatives_by_id[decision.cluster_id],
        )
        for decision in decisions
    }
    ranked = sorted(
        (
            decision for decision in decisions
            if priority_by_id[decision.cluster_id][0]
        ),
        key=lambda item: (-priority_by_id[item.cluster_id][1], item.cluster_id),
    )
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
    used_themes: set[str] = set()

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
            theme = str(representative.metadata.get("theme_key") or decision.cluster_id).strip().casefold()
            if organization and organization in used_organizations:
                continue
            if product and product in used_products:
                continue
            if theme and theme in used_themes:
                continue
            selected.append(decision)
            if organization:
                used_organizations.add(organization)
            if product:
                used_products.add(product)
            if theme:
                used_themes.add(theme)

    admit(community, min(community_target, limit))
    admit(primary, max(0, limit - community_target))
    selected.sort(
        key=lambda item: (-priority_by_id[item.cluster_id][1], item.cluster_id),
    )
    items: list[dict[str, Any]] = []
    for rank, decision in enumerate(selected, 1):
        representative = representatives_by_id[decision.cluster_id]
        evidence = sorted(
            sources_by_id.get(decision.cluster_id, [representative]),
            key=lambda item: (item.id, item.url),
        )
        items.append(_review_item(
            decision,
            rank=rank,
            tier="shortlist",
            representative=representative,
            evidence=evidence,
            priority_record=priority_by_id[decision.cluster_id],
        ))
    return items


def build_watchlist_items(
    representatives: Iterable[SourceItem],
    sources: Iterable[SourceItem],
    decisions: Iterable[EditorialDecision],
    *,
    exclude_cluster_ids: set[str],
    limit: int,
) -> list[dict[str, Any]]:
    """Add the next useful themes without weakening the quality shortlist."""

    if limit <= 0:
        return []
    representatives_by_id = {
        str(item.metadata.get("cluster_id") or item.id): item
        for item in representatives
    }
    sources_by_id: dict[str, list[SourceItem]] = {}
    for item in sources:
        cluster_id = str(item.metadata.get("cluster_id") or item.id)
        sources_by_id.setdefault(cluster_id, []).append(item)
    priority_by_id = {
        decision.cluster_id: review_priority(
            decision, representatives_by_id[decision.cluster_id],
        )
        for decision in decisions
    }
    ranked = sorted(
        (
            decision for decision in decisions
            if decision.cluster_id not in exclude_cluster_ids
        ),
        key=lambda item: (-priority_by_id[item.cluster_id][1], item.cluster_id),
    )
    used_organizations = {
        representatives_by_id[cluster_id].organization.strip().casefold()
        for cluster_id in exclude_cluster_ids
        if representatives_by_id[cluster_id].organization.strip()
    }
    used_products = {
        str(representatives_by_id[cluster_id].metadata.get("product_family") or "").strip().casefold()
        for cluster_id in exclude_cluster_ids
        if representatives_by_id[cluster_id].metadata.get("product_family")
    }
    used_themes = {
        str(representatives_by_id[cluster_id].metadata.get("theme_key") or cluster_id).strip().casefold()
        for cluster_id in exclude_cluster_ids
    }
    selected: list[EditorialDecision] = []
    for decision in ranked:
        eligible = priority_by_id[decision.cluster_id][0]
        representative = representatives_by_id[decision.cluster_id]
        editorial_class = str(representative.metadata.get("editorial_class", "major_development"))
        if not eligible and editorial_class in {"maintenance_release", "research"}:
            continue
        organization = representative.organization.strip().casefold()
        product = str(representative.metadata.get("product_family") or "").strip().casefold()
        theme = str(representative.metadata.get("theme_key") or decision.cluster_id).strip().casefold()
        if organization and organization in used_organizations:
            continue
        if product and product in used_products:
            continue
        if theme in used_themes:
            continue
        selected.append(decision)
        if organization:
            used_organizations.add(organization)
        if product:
            used_products.add(product)
        used_themes.add(theme)
        if len(selected) >= limit:
            break

    items = []
    for rank, decision in enumerate(selected, 1):
        representative = representatives_by_id[decision.cluster_id]
        evidence = sorted(
            sources_by_id.get(decision.cluster_id, [representative]),
            key=lambda item: (item.id, item.url),
        )
        items.append(_review_item(
            decision,
            rank=rank,
            tier="watchlist",
            representative=representative,
            evidence=evidence,
            priority_record=priority_by_id[decision.cluster_id],
        ))
    return items


def _render_review_record(item: dict[str, Any], heading: str) -> list[str]:
    editorial = item["editorial"]
    actions = ", ".join(editorial["builder_actions"]) or "not specified"
    readiness = "yes" if item["podcast_ready"] else "no"
    lines = [
        heading,
        "",
        f"**Cluster ID:** {item['cluster_id']}",
        f"**Decision:** {item['decision']}",
        f"**Podcast-ready:** {readiness}",
        f"**Editorial score:** {item['score']:.2f}",
        f"**Review priority:** {item['review_priority']:.2f}",
        f"**Editorial class:** {item['editorial_class']}",
        f"**Quality rationale:** {item['quality_reason']}",
        f"**Organization / Category:** {item['organization'] or 'unresolved'} / {item['category']}",
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
    lines.extend(
        f"- [{source['source']}]({source['url']}) — {source['authority']}: {source['summary']}"
        for source in item["sources"]
    )
    lines.append("")
    return lines


def render_review_markdown(
    review_date: date,
    items: Iterable[dict[str, Any]],
    *,
    watchlist: Iterable[dict[str, Any]] = (),
    source_health: dict[str, Any] | None = None,
    window_start: str | None = None,
) -> str:
    """Render the structured review records without adding uncited claims."""

    records = list(items)
    watch_records = list(watchlist)
    ready_count = sum(bool(item["podcast_ready"]) for item in records)
    community_count = sum(bool(item["community_led"]) for item in records)
    primary_count = len(records) - community_count
    lines = [
        f"# AI Builder Brief editorial review — {review_date.isoformat()}",
        "",
        f"Quality shortlist: {len(records)}. Additional watchlist: {len(watch_records)}. Total shown: {len(records) + len(watch_records)}.",
        f"Shortlist mix — community-led: {community_count}/6; major primary: {primary_count}/4; podcast-ready: {ready_count}.",
        "",
        "The shortlist keeps the strict editorial floor. The watchlist adds the next useful non-maintenance, non-routine-research themes for human review; it does not weaken podcast readiness.",
        "",
    ]
    if window_start:
        lines.extend([f"Strict review window starts: {window_start}.", ""])
    if source_health:
        x_health = source_health.get("x_panel", {})
        lines.extend([
            "## Source health",
            "",
            f"X panel healthy: {'yes' if x_health.get('healthy') else 'no'}; accounts: {x_health.get('successful_accounts', 0)}/{x_health.get('configured_accounts', 0)}; in-window posts: {x_health.get('in_window_posts', 0)}.",
            "",
        ])
    for item in records:
        lines.extend(_render_review_record(item, f"## {item['rank']}. {item['title']}"))
    if watch_records:
        lines.extend([
            "## Additional watchlist",
            "",
            "These themes are included for broader review and are not part of the quality shortlist.",
            "",
        ])
        for item in watch_records:
            lines.extend(_render_review_record(item, f"### W{item['rank']}. {item['title']}"))
    return "\n".join(lines).rstrip() + "\n"


def write_review_artifacts(
    review_date: date,
    representatives: Iterable[SourceItem],
    sources: Iterable[SourceItem],
    decisions: Iterable[EditorialDecision],
    output_dir: Path,
    *,
    source_health: dict[str, Any] | None = None,
    window_start: str | None = None,
) -> tuple[Path, Path]:
    """Write matching top-10 JSON and Markdown review artifacts."""

    representatives = list(representatives)
    sources = list(sources)
    decisions = list(decisions)
    items = build_review_items(representatives, sources, decisions)
    shortlist_ids = {str(item["cluster_id"]) for item in items}
    watchlist = build_watchlist_items(
        representatives,
        sources,
        decisions,
        exclude_cluster_ids=shortlist_ids,
        limit=max(0, 10 - len(items)),
    )
    representatives_by_id = {
        str(item.metadata.get("cluster_id") or item.id): item
        for item in representatives
    }
    quality_exclusions = []
    for decision in decisions:
        eligible, priority, adjustment, reason = review_priority(
            decision, representatives_by_id[decision.cluster_id],
        )
        if not eligible:
            quality_exclusions.append({
                "cluster_id": decision.cluster_id,
                "editorial_class": str(
                    representatives_by_id[decision.cluster_id].metadata.get(
                        "editorial_class", "major_development",
                    )
                ),
                "score": decision.score,
                "review_priority": priority,
                "priority_adjustment": adjustment,
                "reason": reason,
            })
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{review_date.isoformat()}.json"
    markdown_path = output_dir / f"{review_date.isoformat()}.md"
    community_count = sum(bool(item["community_led"]) for item in items)
    primary_count = len(items) - community_count
    body = {
        "schema_version": 1,
        "review_date": review_date.isoformat(),
        "status": "reviewed",
        **({"window_start": window_start} if window_start else {}),
        "candidate_count": len(items),
        "watchlist_count": len(watchlist),
        "displayed_count": len(items) + len(watchlist),
        "podcast_ready_count": sum(bool(item["podcast_ready"]) for item in items),
        "target_mix": {"community_led": 6, "major_primary": 4},
        "actual_mix": {"community_led": community_count, "major_primary": primary_count},
        "mix_shortfall": {
            "community_led": max(0, 6 - community_count),
            "major_primary": max(0, 4 - primary_count),
        },
        "quality_exclusion_count": len(quality_exclusions),
        "quality_exclusions": quality_exclusions,
        **({"source_health": source_health} if source_health else {}),
        "candidates": items,
        "watchlist": watchlist,
    }
    json_path.write_text(
        json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(
        render_review_markdown(
            review_date,
            items,
            watchlist=watchlist,
            source_health=source_health,
            window_start=window_start,
        ),
        encoding="utf-8",
    )
    return json_path, markdown_path
