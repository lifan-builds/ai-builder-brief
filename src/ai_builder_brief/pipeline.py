"""Show-owned orchestration around CastForge's reusable stages."""

from __future__ import annotations

import json
import shutil
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from castforge.config import AudioConfig, OutputConfig, PublicationConfig, load_config
from castforge.models import EpisodeManifest
from castforge.publishers.r2 import R2Publisher
from castforge.rss import write_episode
from castforge.runner import run_episode

from ai_builder_brief.collectors import collect_sources, write_sources
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
        selection=replace(config.selection, recent_days=0),
        audio=AudioConfig(
            provider="fixture",
            output_dir=fixture_root / "audio",
            duration=config.audio.duration,
            public_url_template="https://audio.example/episodes/{filename}",
            fixture_length_bytes=fixture_audio.stat().st_size,
            language="en",
            audio_length="short",
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
        items = collect_sources(sources_path, start=end - timedelta(hours=24), end=end)
        write_sources(items, public_config.source.fixture)
    else:
        end = datetime(
            episode_date.year,
            episode_date.month,
            episode_date.day,
            public_config.show.publication_hour,
            tzinfo=ZoneInfo(public_config.show.timezone),
        ).astimezone(UTC)
        items = collect_sources(sources_path, start=end - timedelta(hours=24), end=end)
        write_sources(items, public_config.source.fixture)

    work_config = _working_config(public_config, root, episode_date)
    manifest = run_episode(work_config, episode_date, shadow=True)
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
    base_url = public_config.show.site_url.rstrip("/")
    manifest = replace(
        manifest,
        source_document=source_final.relative_to(root).as_posix(),
        audio_url=audio_url,
        duration=public_config.audio.duration,
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
        duration=public_config.audio.duration,
    )

    source_stage = work_config.outputs.sources / f"{episode_date.isoformat()}.md"
    _move(transcript_stage, transcript_final)
    _move(chapters_stage, chapters_final)
    _move(source_stage, source_final)
    _move(manifest_stage, manifest_final)
    _move(site_stage, site_final)
    _move(feed_stage, feed_final)  # Feed moves last: it is the public commit point.
    return "shadow" if shadow else "published"
