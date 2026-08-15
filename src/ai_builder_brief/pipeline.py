"""Show-owned orchestration around CastForge's reusable stages."""

from __future__ import annotations

import shutil
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from castforge.audio import format_duration, probe_audio_duration
from castforge.config import AudioConfig, OutputConfig, PublicationConfig, load_config
from castforge.models import EpisodeManifest, StoryCluster
from castforge.publishers.r2 import R2Publisher
from castforge.rss import write_episode
from castforge.runner import run_episode

from ai_builder_brief.collectors import collect_sources, read_sources, write_sources
from ai_builder_brief.editorial import preprocess, select_clusters, validate_review, write_ledger
from ai_builder_brief.editorial_client import review_candidates
from ai_builder_brief.site import render_site
from ai_builder_brief.transcription import load_fixture, transcribe, write_chapters, write_vtt


def _episode_id(prefix: str, episode_date: date) -> str:
    return f"{prefix}-{episode_date.isoformat()}"


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
        # The bundled two-story fixture proves the mechanics; production keeps
        # the approved minimum of three decision-changing stories.
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
    grouped = {}
    for item in items:
        grouped.setdefault(str(item.metadata.get("cluster_id") or item.id), []).append(item)
    clusters = []
    for cluster_id, sources in grouped.items():
        lead = sorted(sources, key=lambda item: item.id)[0]
        cluster = StoryCluster(
            id=cluster_id, title=lead.title, summary=lead.summary, category=lead.category,
            organization=lead.organization, sources=tuple(sources),
            selection_reason=str(lead.metadata.get("selection_reason") or "Editorial review"),
            kind=str(lead.metadata.get("kind", "development") or "development"),
            metadata=dict(lead.metadata),
        )
        if cluster.is_qualified():
            clusters.append(cluster)
    return clusters


def _merge_editorial_ledger(deterministic, decisions):
    reviewed = {decision.cluster_id: decision.to_dict() for decision in decisions}
    return [reviewed.get(str(record.get("cluster_id", "")), record) for record in deterministic]


def _apply_snapshot_deltas(items):
    """Attach measured 24-hour/seven-day deltas to the latest signal item."""

    metric_names = (
        "repository_stars", "repository_forks", "repository_open_issues",
        "hf_likes", "hf_downloads", "hf_trending_score",
    )
    grouped = {}
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
        positive = sum(max(value, 0) for value in (*delta_24.values(), *delta_7.values()))
        measured_momentum = min(4, int(positive > 0) + int(positive >= 10) + int(positive >= 100) + int(positive >= 1000))
        enriched.append(replace(latest, metadata={
            **latest.metadata,
            "delta_24h": delta_24,
            "delta_7d": delta_7,
            "momentum_score": max(int(latest.metadata.get("momentum_score", 0) or 0), measured_momentum),
        }))
    return enriched


def _ledger_evidence(deterministic, candidates):
    grouped = {}
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
                        for key in ("delta_24h", "delta_7d", "momentum_score", "recency_score")
                        if key in item.metadata
                    },
                }
                for item in evidence
            ],
        })
    return records


def _represent_candidates(candidates):
    grouped = {}
    for item in candidates:
        grouped.setdefault(str(item.metadata.get("cluster_id") or item.id), []).append(item)
    representatives = []
    metadata = {}
    for cluster_id, group in grouped.items():
        lead = group[0]
        momentum = max(int(item.metadata.get("momentum_score", 0)) for item in group)
        recency = max(int(item.metadata.get("recency_score", 0)) for item in group)
        representatives.append(replace(lead, metadata={**lead.metadata, "source_ids": [item.id for item in group], "momentum_score": momentum, "recency_score": recency}))
        metadata[cluster_id] = {"momentum_score": momentum, "recency_score": recency}
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
            "published_at": item.published_at,
            "source_ids": list(item.metadata.get("source_ids", [item.id])),
            "momentum_score": int(item.metadata.get("momentum_score", 0)),
            "recency_score": int(item.metadata.get("recency_score", 0)),
        }
        for item in representatives
    ]


def _move(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.replace(destination)


def run_daily(
    *,
    config_path: Path,
    sources_path: Path,
    episode_date: date,
    fixture: bool = False,
    shadow: bool = False,
) -> str:
    root = Path(config_path).resolve().parent
    public_config = load_config(config_path)
    episode_id = _episode_id(public_config.show.episode_guid_prefix, episode_date)
    if not fixture and not shadow and is_published(public_config.outputs.feed, episode_id):
        return "already-published"

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
        try:
            items = collect_sources(sources_path, start=end - timedelta(days=7), end=end)
            write_sources(items, public_config.source.fixture)
        except Exception:
            write_ledger([], root / "build" / "editorial" / f"{episode_date.isoformat()}.json", episode_date=episode_date.isoformat(), status="no-episode-collector-failure")
            return "no-episode"
    else:
        end = datetime(
            episode_date.year,
            episode_date.month,
            episode_date.day,
            public_config.show.publication_hour,
            tzinfo=ZoneInfo(public_config.show.timezone),
        ).astimezone(UTC)
        try:
            items = collect_sources(sources_path, start=end - timedelta(days=7), end=end)
            write_sources(items, public_config.source.fixture)
        except Exception:
            write_ledger([], root / "build" / "editorial" / f"{episode_date.isoformat()}.json", episode_date=episode_date.isoformat(), status="no-episode-collector-failure")
            return "no-episode"

    from castforge.models import SourceItem
    normalized = read_sources(public_config.source.fixture)
    if not fixture:
        snapshot_dir = root / "build" / "snapshots"
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        write_sources(normalized, snapshot_dir / f"{episode_date.isoformat()}.json")
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
        normalized = _apply_snapshot_deltas(historical)
    end = datetime(
        episode_date.year, episode_date.month, episode_date.day,
        public_config.show.publication_hour,
        tzinfo=ZoneInfo(public_config.show.timezone),
    ).astimezone(UTC)
    candidates, deterministic = preprocess(normalized, as_of=end)
    deterministic = _ledger_evidence(deterministic, candidates)
    representatives, candidate_metadata = _represent_candidates(candidates)
    clusters = _cluster(candidates)
    ledger_path = root / "build" / "editorial" / f"{episode_date.isoformat()}.json"
    try:
        response = {"decisions": [
            {"cluster_id": str(item.metadata.get("cluster_id") or item.id), "decision": "accept", "impact": 4, "actionability": 4, "novelty": 3, "evidence": 4, "audience_breadth": 3, "builder_actions": ["use"], "why_now": "fixture candidate", "rationale": "fixture candidate", "caveats": "", "depth_recommendation": "brief", "source_ids": list(item.metadata.get("source_ids", [item.id]))}
            for item in representatives
        ]} if fixture else review_candidates(_editorial_packet(representatives))
        decisions = validate_review(
            response,
            set(candidate_metadata),
            candidate_metadata,
        )
    except Exception:
        write_ledger(deterministic, ledger_path, episode_date=episode_date.isoformat(), status="no-episode-editorial-failure")
        return "no-episode"
    write_ledger(_merge_editorial_ledger(deterministic, decisions), ledger_path, episode_date=episode_date.isoformat())
    selected = select_clusters(clusters, decisions, minimum=public_config.selection.min_stories, maximum=public_config.selection.max_stories)
    if len(selected) < public_config.selection.min_stories:
        write_ledger(_merge_editorial_ledger(deterministic, decisions), ledger_path, episode_date=episode_date.isoformat(), status="no-episode")
        return "no-episode"
    editorial_source = root / "build" / "editorial-input" / f"{episode_date.isoformat()}.json"
    selected_sources = []
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
        duration=manifest.duration,
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
    _move(feed_stage, feed_final)  # Feed moves last: it is the public commit point.
    return "shadow" if shadow else "published"
