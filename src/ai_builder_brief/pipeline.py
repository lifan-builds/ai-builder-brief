"""Show-owned orchestration around CastForge's reusable stages."""

from __future__ import annotations

import json
import re
import shutil
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from castforge.audio import format_duration, probe_audio_duration
from castforge.config import AudioConfig, OutputConfig, PublicationConfig, load_config
from castforge.models import EpisodeManifest, SourceItem, StoryCluster
from castforge.publishers.r2 import R2Publisher
from castforge.rss import write_episode
from castforge.runner import run_episode

from ai_builder_brief.benchmark import canonicalize_url
from ai_builder_brief.collectors import CollectionResult, collect_sources, read_sources, write_sources
from ai_builder_brief.editorial import (
    preprocess,
    score_candidates_deterministically,
    select_clusters,
    validate_review,
    write_ledger,
)
from ai_builder_brief.editorial_client import EditorialReviewError, review_candidates_batched
from ai_builder_brief.journal import _parse_ranked_items
from ai_builder_brief.review import write_review_artifacts
from ai_builder_brief.site import render_site
from ai_builder_brief.transcription import load_fixture, transcribe, write_chapters, write_vtt


def is_published(feed_path: Path, episode_id: str) -> bool:
    if not feed_path.is_file():
        return False
    try:
        root = ET.parse(feed_path).getroot()
    except ET.ParseError:
        return False
    for item in root.findall("./channel/item"):
        if item.findtext("guid") != episode_id:
            continue
        enclosure = item.find("enclosure")
        if enclosure is None or not enclosure.get("url"):
            return False
        try:
            return int(enclosure.get("length", "0")) > 0
        except ValueError:
            return False
    return False


def _fixture_config(config, root: Path):
    fixture_audio = root / "fixtures" / "audio.mp3"
    fixture_root = root / "build" / "fixture"
    return replace(
        config,
        source=replace(config.source, fixture=root / "fixtures" / "sources.json"),
        selection=replace(config.selection, recent_days=0, min_stories=1),
        audio=AudioConfig(
            provider="fixture",
            output_dir=fixture_root / "audio",
            duration=config.audio.duration,
            public_url_template="https://audio.example/episodes/{filename}",
            fixture_length_bytes=fixture_audio.stat().st_size,
            language="en",
            audio_length="default",
        ),
        publication=PublicationConfig(provider="fixture"),
        outputs=OutputConfig(
            root=fixture_root,
            feed=fixture_root / "feed.xml",
            manifests=fixture_root / "manifests",
            sources=fixture_root / "sources",
        ),
    )


def _working_config(config, root: Path, episode_date: date):
    work = root / "build" / "work" / episode_date.isoformat()
    manifests = work / "manifests"
    if config.outputs.manifests.is_dir():
        manifests.mkdir(parents=True, exist_ok=True)
        for prior in config.outputs.manifests.glob("*.json"):
            shutil.copy2(prior, manifests / prior.name)
    return replace(
        config,
        audio=replace(config.audio, output_dir=work / "audio"),
        outputs=OutputConfig(
            root=work,
            feed=work / "feed.xml",
            manifests=manifests,
            sources=work / "sources",
        ),
    )


def _cluster(items):
    grouped: dict[str, list[SourceItem]] = {}
    for item in items:
        grouped.setdefault(str(item.metadata.get("cluster_id") or item.id), []).append(item)
    clusters = []
    for cluster_id, sources in grouped.items():
        lead = min(
            sources,
            key=lambda item: (
                {"primary": 0, "independent": 1, "analysis": 2, "signal": 3}[item.authority],
                -float(item.metadata.get("score", 0)),
                item.id,
            ),
        )
        cluster = StoryCluster(
            id=cluster_id,
            title=lead.title,
            summary=lead.summary,
            category=lead.category,
            organization=lead.organization,
            sources=tuple(sources),
            selection_reason=str(lead.metadata.get("selection_reason") or "Editorial review"),
            kind=str(lead.metadata.get("kind", "development") or "development"),
            metadata=dict(lead.metadata),
        )
        if cluster.is_qualified():
            clusters.append(cluster)
    return clusters


def _merge_editorial_ledger(deterministic, decisions):
    reviewed = {decision.cluster_id: decision.to_dict() for decision in decisions}
    return [
        {**record, **reviewed.get(str(record.get("cluster_id", "")), {})}
        for record in deterministic
    ]


def _apply_snapshot_deltas(items):
    """Attach measured 24-hour/seven-day deltas to the latest signal item."""
    metric_names = (
        "repository_stars", "repository_forks", "repository_open_issues",
        "hf_likes", "hf_downloads", "hf_trending_score",
    )
    adoption_metrics = {
        "repository_stars", "repository_forks",
        "hf_likes", "hf_downloads", "hf_trending_score",
    }
    grouped: dict[str, list[SourceItem]] = {}
    passthrough = []
    for item in items:
        key = str(item.metadata.get("signal_key") or "")
        snapshot_date = str(item.metadata.get("snapshot_date") or "")
        if not key or not snapshot_date:
            passthrough.append(item)
            continue
        grouped.setdefault(key, []).append(item)

    enriched = list(passthrough)
    for group in grouped.values():
        ordered = sorted(group, key=lambda item: (str(item.metadata["snapshot_date"]), item.id))
        latest = ordered[-1]
        latest_date = date.fromisoformat(str(latest.metadata["snapshot_date"]))
        prior_24 = [item for item in ordered if date.fromisoformat(str(item.metadata["snapshot_date"])) <= latest_date - timedelta(days=1)]
        baseline_24 = prior_24[-1] if prior_24 else latest
        baseline_7 = ordered[0]

        def deltas(baseline):
            return {
                name: float(latest.metadata.get(name, 0) or 0) - float(baseline.metadata.get(name, 0) or 0)
                for name in metric_names
                if name in latest.metadata and name in baseline.metadata
            }

        delta_24 = deltas(baseline_24)
        delta_7 = deltas(baseline_7)
        pos_24 = sum(max(val, 0.0) for name, val in delta_24.items() if name in adoption_metrics)
        pos_7 = sum(max(val, 0.0) for name, val in delta_7.items() if name in adoption_metrics)
        positive = max(pos_24, pos_7)
        measured_momentum = min(4, int(positive > 0) + int(positive >= 10) + int(positive >= 100) + int(positive >= 1000))
        is_github = str(latest.metadata.get("signal_key", "")).startswith("github:")
        has_prior_snapshot = len(ordered) > 1
        enriched.append(replace(latest, metadata={
            **latest.metadata,
            "delta_24h": delta_24,
            "delta_7d": delta_7,
            "momentum_score": (
                measured_momentum
                if (is_github and has_prior_snapshot)
                else max(int(latest.metadata.get("momentum_score", 0) or 0), measured_momentum)
            ),
            "community_signal": (
                measured_momentum > 0
                if (is_github and has_prior_snapshot)
                else bool(latest.metadata.get("community_signal"))
            ),
        }))
    return enriched


def _eligible_editorial_items(items, *, start: datetime, end: datetime):
    """Keep one current copy of each source inside the strict review window."""
    eligible: dict[str, SourceItem] = {}
    for item in items:
        try:
            published = datetime.fromisoformat(item.published_at.replace("Z", "+00:00"))
        except ValueError:
            continue
        if published.tzinfo is None:
            published = published.replace(tzinfo=UTC)
        if not start <= published.astimezone(UTC) <= end:
            continue
        existing = eligible.get(item.id)
        if existing is None or str(item.metadata.get("snapshot_date", "")) > str(existing.metadata.get("snapshot_date", "")):
            eligible[item.id] = item
    return list(eligible.values())


def _historical_story_keys(root: Path, *, episode_date: date, lookback_days: int = 14) -> tuple[set[str], set[str]]:
    """Return `(story_slugs, canonical_urls)` covered in the prior `lookback_days`."""
    slugs: set[str] = set()
    urls: set[str] = set()
    start = episode_date - timedelta(days=lookback_days)
    for path in (root / "build" / "editorial").glob("*.json"):
        try:
            day = date.fromisoformat(path.stem)
            if day < start or day >= episode_date:
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        records: list[dict] = []
        if isinstance(payload, list):
            records = [r for r in payload if isinstance(r, dict)]
        elif isinstance(payload, dict):
            records.extend(r for r in payload.get("candidates", []) if isinstance(r, dict))
            records.extend(
                r for r in payload.get("decisions", [])
                if isinstance(r, dict) and r.get("decision") in {"accept", "candidate"}
            )
        for record in records:
            key = str(record.get("theme_key") or record.get("cluster_id") or "").strip().casefold()
            if key:
                slugs.add(key)
            evidence = record.get("evidence")
            if isinstance(evidence, list):
                for ev in evidence:
                    if isinstance(ev, dict) and ev.get("url"):
                        cu = canonicalize_url(str(ev["url"]))
                        if cu:
                            urls.add(cu)

    for path in (root / "history").glob("*.md"):
        if "-x-" in path.name or not re.match(r"^\d{4}-\d{2}-\d{2}\.md$", path.name):
            continue
        try:
            day = date.fromisoformat(path.stem)
            if day < start or day >= episode_date:
                continue
            md_text = path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            continue
        top_section = re.split(
            r"^(?:##\s+|\*\*)(?:Watchlist|Source panel|What changed)",
            md_text,
            maxsplit=1,
            flags=re.M | re.I,
        )[0]
        for raw_url in re.findall(r"https?://[^\s\)\]>]+", top_section):
            cu = canonicalize_url(raw_url)
            if cu and "/" in cu:
                urls.add(cu)
        for story_title, _, _, _ in _parse_ranked_items(md_text):
            slug = re.sub(r"[^a-z0-9]+", "-", story_title.casefold()).strip("-")[:80]
            if len(slug) >= 12:
                slugs.add(slug)
    return slugs, urls


def _load_event_registry(root: Path) -> set[str]:
    path = root / "history" / "event-registry.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    return {str(item).strip().casefold() for item in payload.get("events", []) if str(item).strip()}


def _suppress_historical_repeats(items: list[SourceItem], *, root: Path, episode_date: date) -> list[SourceItem]:
    """Drop exact URL or canonical event repeats from prior 14 days unless marked material_update."""
    prior_slugs, prior_urls = _historical_story_keys(root, episode_date=episode_date)
    prior_slugs |= _load_event_registry(root)
    if not prior_slugs and not prior_urls:
        return items
    kept: list[SourceItem] = []
    for item in items:
        if bool(item.metadata.get("material_update")):
            kept.append(item)
            continue
        key = str(item.metadata.get("theme_key") or item.metadata.get("cluster_id") or item.id).strip().casefold()
        canon_url = canonicalize_url(str(item.metadata.get("canonical_url") or item.url))
        if (key and key in prior_slugs) or (canon_url and canon_url in prior_urls):
            continue
        kept.append(item)
    return kept


def _ledger_evidence(deterministic, candidates):
    grouped: dict[str, list[SourceItem]] = {}
    for item in candidates:
        grouped.setdefault(str(item.metadata.get("cluster_id") or item.id), []).append(item)
    records = []
    for record in deterministic:
        cluster_id = str(record.get("cluster_id", ""))
        evidence = grouped.get(cluster_id, [])
        records.append({
            **record,
            "evidence": [
                {
                    "id": item.id,
                    "title": item.title,
                    "url": item.url,
                    "authority": item.authority,
                    "published_at": item.published_at,
                    "signals": {
                        key: item.metadata[key]
                        for key in (
                            "delta_24h", "delta_7d", "momentum_score", "recency_score",
                            "x_likes", "x_retweets", "x_engagement", "hn_points",
                            "hn_comments", "hf_upvotes", "hf_likes", "hf_downloads",
                            "hf_trending_score", "repository_stars", "repository_forks",
                            "repository_open_issues",
                        )
                        if key in item.metadata
                    },
                }
                for item in evidence
            ],
        })
    return records


def _represent_candidates(candidates):
    grouped: dict[str, list[SourceItem]] = {}
    for item in candidates:
        grouped.setdefault(str(item.metadata.get("cluster_id") or item.id), []).append(item)
    representatives = []
    metadata = {}
    for cluster_id, group in grouped.items():
        lead = min(
            group,
            key=lambda item: (
                {"primary": 0, "independent": 1, "analysis": 2, "signal": 3}[item.authority],
                -float(item.metadata.get("score", 0)),
                item.id,
            ),
        )
        momentum = max(int(item.metadata.get("momentum_score", 0)) for item in group)
        recency = max(int(item.metadata.get("recency_score", 0)) for item in group)
        community_led = any(bool(item.metadata.get("community_led")) for item in group)
        signal_types = sorted({
            str(signal_type)
            for item in group
            for signal_type in item.metadata.get("community_signal_types", [])
        })
        product_family = next(
            (str(item.metadata["product_family"]) for item in group if item.metadata.get("product_family")),
            "",
        )
        subject_sets = [
            {
                str(organization)
                for organization in item.metadata.get("subject_organizations", [])
                if str(organization).strip()
            }
            for item in group
        ]
        nonempty_subject_sets = [subjects for subjects in subject_sets if subjects]
        common_subjects = (
            set.intersection(*nonempty_subject_sets)
            if nonempty_subject_sets
            else set()
        )
        subject_organizations = sorted(set.union(*nonempty_subject_sets)) if nonempty_subject_sets else []
        story_organization = (
            next(iter(common_subjects))
            if len(common_subjects) == 1
            else (
                lead.organization
                if lead.authority == "primary" and "." not in lead.organization
                else ""
            )
        )
        community_sources = sorted(
            (item for item in group if item.metadata.get("community_signal")),
            key=lambda item: (-int(item.metadata.get("momentum_score", 0) or 0), item.id),
        )
        aggregate = {
            "source_ids": [item.id for item in group],
            "cluster_urls": sorted({item.url for item in group if item.url}),
            "source_types": sorted({item.source for item in group}),
            "source_authorities": sorted({item.authority for item in group}),
            "story_organization": story_organization,
            "subject_organizations": subject_organizations,
            "community_led": community_led,
            "editorial_class": str(lead.metadata.get("editorial_class", "major_development")),
            "theme_key": str(lead.metadata.get("theme_key") or cluster_id),
            "community_signal_types": signal_types,
            "community_signal_count": sum(bool(item.metadata.get("community_signal")) for item in group),
            "x_post_count": sum(item.metadata.get("community_signal_type") == "x" for item in group),
            "x_likes": sum(int(item.metadata.get("x_likes", 0) or 0) for item in group),
            "x_retweets": sum(int(item.metadata.get("x_retweets", 0) or 0) for item in group),
            "hn_points": sum(int(item.metadata.get("hn_points", 0) or 0) for item in group),
            "hn_comments": sum(int(item.metadata.get("hn_comments", 0) or 0) for item in group),
            "momentum_score": momentum,
            "recency_score": recency,
            "qualifying_evidence": any(bool(item.metadata.get("qualifying_evidence")) for item in group),
            "qualifying_source_ids": [
                item.id for item in group if item.authority in {"primary", "independent"}
            ],
            "community_context": [
                {
                    "source_id": item.id,
                    "source": item.source,
                    "account": str(item.metadata.get("x_account", "")),
                    "summary": item.summary[:280],
                    "likes": int(item.metadata.get("x_likes", 0) or 0),
                    "retweets": int(item.metadata.get("x_retweets", 0) or 0),
                    "hn_points": int(item.metadata.get("hn_points", 0) or 0),
                    "hn_comments": int(item.metadata.get("hn_comments", 0) or 0),
                }
                for item in community_sources[:3]
            ],
            **({"product_family": product_family} if product_family else {}),
        }
        representatives.append(replace(
            lead,
            organization=story_organization,
            metadata={**lead.metadata, **aggregate},
        ))
        metadata[cluster_id] = aggregate
    return representatives, metadata


def _editorial_packet(representatives):
    """Keep model review bounded while retaining the evidence needed to judge impact."""
    return [
        {
            "cluster_id": str(item.metadata.get("cluster_id") or item.id),
            "title": item.title,
            "summary": item.summary[:700],
            "url": item.url,
            "authority": item.authority,
            "organization": item.organization,
            "category": item.category,
            "kind": str(item.metadata.get("kind", "development")),
            "product_family": str(item.metadata.get("product_family", "")),
            "published_at": item.published_at,
            "source_ids": list(item.metadata.get("source_ids", [item.id])),
            "source_types": list(item.metadata.get("source_types", [item.source])),
            "source_authorities": list(item.metadata.get("source_authorities", [item.authority])),
            "community_led": bool(item.metadata.get("community_led")),
            "editorial_class": str(item.metadata.get("editorial_class", "major_development")),
            "theme_key": str(item.metadata.get("theme_key") or item.metadata.get("cluster_id") or item.id),
            "community_signal_types": list(item.metadata.get("community_signal_types", [])),
            "community_signal_count": int(item.metadata.get("community_signal_count", 0)),
            "x_post_count": int(item.metadata.get("x_post_count", 0)),
            "x_likes": int(item.metadata.get("x_likes", 0)),
            "x_retweets": int(item.metadata.get("x_retweets", 0)),
            "hn_points": int(item.metadata.get("hn_points", 0)),
            "hn_comments": int(item.metadata.get("hn_comments", 0)),
            "qualifying_evidence": bool(item.metadata.get("qualifying_evidence")),
            "qualifying_source_ids": list(item.metadata.get("qualifying_source_ids", [])),
            "community_context": list(item.metadata.get("community_context", [])),
            "momentum_score": int(item.metadata.get("momentum_score", 0)),
            "recency_score": int(item.metadata.get("recency_score", 0)),
        }
        for item in representatives
    ]


def _move(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.replace(destination)


def _write_source_health(root: Path, episode_date: date, result: CollectionResult) -> Path:
    path = root / "build" / "source-health" / f"{episode_date.isoformat()}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "review_date": episode_date.isoformat(),
                **result.health_dict(),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ) + "\n",
        encoding="utf-8",
    )
    return path


def run_daily(
    *,
    config_path: Path,
    sources_path: Path,
    episode_date: date,
    fixture: bool = False,
    shadow: bool = False,
    review_only: bool = False,
    dry_run: bool = False,
) -> str:
    root = Path(config_path).resolve().parent
    public_config = load_config(config_path)
    episode_id = f"{public_config.show.episode_guid_prefix}-{episode_date.isoformat()}"
    if review_only or dry_run:
        for suffix in ("json", "md"):
            (root / "build" / "review" / f"{episode_date.isoformat()}.{suffix}").unlink(missing_ok=True)
    if not fixture and not shadow and not review_only and not dry_run and is_published(public_config.outputs.feed, episode_id):
        return "already-published"

    collection_result: CollectionResult | None = None
    if fixture:
        public_config = _fixture_config(public_config, root)
    elif shadow:
        shadow_root = root / "build" / "shadow" / episode_date.isoformat()
        public_config = replace(
            public_config,
            publication=PublicationConfig(provider="fixture"),
            outputs=OutputConfig(
                root=shadow_root,
                feed=shadow_root / "feed.xml",
                manifests=shadow_root / "manifests",
                sources=shadow_root / "sources",
            ),
        )

    end = datetime(
        episode_date.year,
        episode_date.month,
        episode_date.day,
        public_config.show.publication_hour,
        tzinfo=ZoneInfo(public_config.show.timezone),
    ).astimezone(UTC)
    if dry_run and episode_date == date.today():
        end = max(end, datetime.now(UTC))

    if not fixture:
        try:
            collection_result = collect_sources(
                sources_path,
                start=end - timedelta(days=public_config.selection.recent_days),
                end=end,
            )
            write_sources(list(collection_result.items), public_config.source.fixture)
            _write_source_health(root, episode_date, collection_result)
        except Exception:
            write_ledger([], root / "build" / "editorial" / f"{episode_date.isoformat()}.json", episode_date=episode_date.isoformat(), status="no-episode-collector-failure")
            return "no-episode"

    normalized = read_sources(public_config.source.fixture)
    if not fixture:
        snapshot_dir = root / "build" / "snapshots"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        write_sources(normalized, snapshot_dir / f"{episode_date.isoformat()}.json")
        if (review_only or dry_run) and collection_result is not None and not collection_result.healthy:
            write_ledger(
                [],
                root / "build" / "editorial" / f"{episode_date.isoformat()}.json",
                episode_date=episode_date.isoformat(),
                status="no-review-source-failure",
            )
            return "no-episode"
        historical: list[SourceItem] = []
        for snapshot in sorted(snapshot_dir.glob("*.json")):
            try:
                snapshot_date = date.fromisoformat(snapshot.stem)
            except ValueError:
                continue
            if episode_date - timedelta(days=6) <= snapshot_date <= episode_date:
                historical.extend(
                    replace(item, metadata={**item.metadata, "snapshot_date": snapshot_date.isoformat()})
                    for item in read_sources(snapshot)
                )
        normalized = _eligible_editorial_items(
            _apply_snapshot_deltas(historical),
            start=end - timedelta(days=public_config.selection.recent_days),
            end=end,
        )
        normalized = _suppress_historical_repeats(
            normalized,
            root=root,
            episode_date=episode_date,
        )
    candidates, deterministic = preprocess(normalized, as_of=end)
    deterministic = _ledger_evidence(deterministic, candidates)
    representatives, candidate_metadata = _represent_candidates(candidates)
    clusters = _cluster(candidates)
    ledger_path = root / "build" / "editorial" / f"{episode_date.isoformat()}.json"
    try:
        if fixture:
            response: dict = {
                "decisions": [
                    {
                        "cluster_id": str(item.metadata.get("cluster_id") or item.id),
                        "decision": "accept",
                        "impact": 4,
                        "actionability": 4,
                        "novelty": 3,
                        "evidence": 4,
                        "audience_breadth": 3,
                        "builder_actions": ["use"],
                        "why_now": "fixture candidate",
                        "rationale": "fixture candidate",
                        "caveats": "",
                        "depth_recommendation": "brief",
                        "source_ids": list(item.metadata.get("source_ids", [item.id])),
                    }
                    for item in representatives
                ]
            }
        elif dry_run:
            response = score_candidates_deterministically(_editorial_packet(representatives))
        else:
            response = review_candidates_batched(_editorial_packet(representatives))
        decisions = validate_review(
            response,
            set(candidate_metadata),
            candidate_metadata,
        )
    except EditorialReviewError as error:
        write_ledger(
            deterministic,
            ledger_path,
            episode_date=episode_date.isoformat(),
            status="no-episode-editorial-failure",
            metadata=error.to_metadata(),
        )
        return "no-episode"
    except Exception:
        write_ledger(
            deterministic,
            ledger_path,
            episode_date=episode_date.isoformat(),
            status="no-episode-editorial-failure",
            metadata={"failure_type": "invalid_response", "stage": "editorial_validation"},
        )
        return "no-episode"
    write_ledger(_merge_editorial_ledger(deterministic, decisions), ledger_path, episode_date=episode_date.isoformat())
    if review_only or dry_run:
        write_review_artifacts(
            episode_date,
            representatives,
            candidates,
            decisions,
            root / "build" / "review",
            window_start=(end - timedelta(days=public_config.selection.recent_days)).isoformat().replace("+00:00", "Z"),
            source_health=(
                collection_result.health_dict()
                if collection_result is not None
                else {"healthy": True, "mode": "fixture" if fixture else "dry-run"}
            ),
        )
        return "dry-run-ready" if dry_run else "review-ready"
    selected = select_clusters(clusters, decisions, minimum=public_config.selection.min_stories, maximum=public_config.selection.max_stories)
    if len(selected) < public_config.selection.min_stories:
        write_ledger(_merge_editorial_ledger(deterministic, decisions), ledger_path, episode_date=episode_date.isoformat(), status="no-episode")
        return "no-episode"
    editorial_source = root / "build" / "editorial-input" / f"{episode_date.isoformat()}.json"
    selected_sources: list[SourceItem] = []
    for cluster in selected:
        editorial = cluster.metadata.get("editorial", {})
        selected_sources.extend(replace(source, metadata={**source.metadata, "editorial": editorial}) for source in cluster.sources)
    write_sources(selected_sources, editorial_source)
    public_config = replace(public_config, source=replace(public_config.source, fixture=editorial_source))

    work_config = _working_config(public_config, root, episode_date)
    manifest = run_episode(work_config, episode_date, shadow=True)
    if not isinstance(manifest, EpisodeManifest):
        return "no-episode"
    filename = f"{public_config.show.episode_file_prefix}_{episode_date.isoformat()}.mp3"
    if fixture:
        audio_path = root / "fixtures" / "audio.mp3"
        segments = load_fixture(root / "fixtures" / "transcript.json")
    else:
        audio_path = work_config.audio.output_dir / filename
        segments = transcribe(audio_path)
    audio_length = audio_path.stat().st_size
    if audio_length < 1:
        raise RuntimeError("audio output is empty")
    if fixture:
        measured = probe_audio_duration(audio_path)
        manifest = replace(manifest, duration=format_duration(measured), metadata={**manifest.metadata, "duration_seconds": measured})

    stage = work_config.outputs.root / "public-stage"
    transcript_stage = write_vtt(segments, stage / "transcripts" / f"{episode_date.isoformat()}.vtt")
    chapters_stage = write_chapters(manifest, segments, stage / "chapters" / f"{episode_date.isoformat()}.json")
    site_stage = render_site(stage / "index.html")

    if shadow or fixture:
        audio_url = public_config.audio.public_url_template.format(date=episode_date.isoformat(), filename=filename)
    else:
        if "ACCOUNT_ID" in public_config.publication.endpoint_url:
            raise RuntimeError("Replace ACCOUNT_ID in podcast.yaml before R2 publication")
        publisher = R2Publisher.from_env(
            bucket=public_config.publication.bucket,
            endpoint_url=public_config.publication.endpoint_url,
            public_base_url=public_config.publication.public_base_url,
            access_key_env=public_config.publication.access_key_env,
            secret_key_env=public_config.publication.secret_key_env,
            max_bucket_bytes=public_config.publication.max_bucket_bytes,
        )
        origin_url = publisher.publish(audio_path, f"episodes/{filename}")
        prefix = public_config.publication.download_url_prefix
        audio_url = f"{prefix}{origin_url}" if prefix else origin_url

    public_root = public_config.outputs.feed.parent
    transcript_final = public_root / "transcripts" / transcript_stage.name
    chapters_final = public_root / "chapters" / chapters_stage.name
    source_final = public_config.outputs.sources / f"{episode_date.isoformat()}.md"
    manifest_final = public_config.outputs.manifests / f"{episode_date.isoformat()}.json"
    feed_final = public_config.outputs.feed
    site_final = public_root / "index.html"
    ledger_final = public_root / "editorial" / ledger_path.name
    base_url = public_config.show.site_url.rstrip("/")
    manifest = replace(
        manifest,
        source_document=source_final.relative_to(root).as_posix(),
        audio_url=audio_url,
        metadata={**manifest.metadata, "editorial_ledger": f"editorial/{ledger_path.name}"},
        transcript_url=f"{base_url}/transcripts/{transcript_stage.name}",
        chapters_url=f"{base_url}/chapters/{chapters_stage.name}",
    )
    manifest_stage = manifest.write(stage / "manifests" / manifest_final.name)
    feed_stage = stage / "feed.xml"
    if feed_final.is_file():
        shutil.copy2(feed_final, feed_stage)
    write_episode(
        feed_stage,
        show=public_config.show,
        manifest=manifest,
        audio_url=audio_url,
        audio_length=audio_length,
        duration=manifest.duration,
    )

    source_stage = work_config.outputs.sources / f"{episode_date.isoformat()}.md"
    _move(transcript_stage, transcript_final)
    _move(chapters_stage, chapters_final)
    _move(source_stage, source_final)
    _move(manifest_stage, manifest_final)
    _move(site_stage, site_final)
    _move(ledger_path, ledger_final)
    _move(feed_stage, feed_final)
    return "shadow" if shadow else "published"
