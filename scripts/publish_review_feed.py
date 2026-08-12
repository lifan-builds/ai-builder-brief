"""Publish previously reviewed shadows after their R2 audio is public."""

from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path
from urllib.request import Request, urlopen

from castforge.config import load_config
from castforge.models import EpisodeManifest
from castforge.publishers.r2 import validate_public_audio
from castforge.rss import write_episode

from ai_builder_brief.site import render_site

REVIEW_DATES = ("2026-08-05", "2026-08-11")
ACTUAL_DURATIONS = {"2026-08-05": "00:05:10", "2026-08-11": "00:06:31"}


def _public_length(url: str) -> int:
    with urlopen(Request(url, method="HEAD", headers={"User-Agent": "AIBuilderBrief/0.1"})) as response:
        return int(response.headers.get("Content-Length", "0") or 0)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    config = load_config(root / "podcast.yaml")
    stage = root / "build" / "review-feed-stage"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    feed = stage / "feed.xml"

    for episode_date in REVIEW_DATES:
        shadow = root / "build" / "shadow" / episode_date
        filename = f"{config.show.episode_file_prefix}_{episode_date}.mp3"
        origin_url = f"{config.publication.public_base_url}/episodes/{filename}"
        length = _public_length(origin_url)
        validate_public_audio(origin_url, expected_length=length)

        transcript = root / "docs" / "transcripts" / f"{episode_date}.vtt"
        chapters = root / "docs" / "chapters" / f"{episode_date}.json"
        source = root / "docs" / "sources" / f"{episode_date}.md"
        manifest_path = root / "docs" / "manifests" / f"{episode_date}.json"
        for source_path, destination in (
            (shadow / "transcripts" / transcript.name, transcript),
            (shadow / "chapters" / chapters.name, chapters),
            (shadow / "sources" / source.name, source),
        ):
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, destination)

        manifest = EpisodeManifest.read(shadow / "manifests" / manifest_path.name)
        site = config.show.site_url.rstrip("/")
        manifest = replace(
            manifest,
            source_document=source.relative_to(root).as_posix(),
            audio_url=f"{config.publication.download_url_prefix}{origin_url}",
            duration=ACTUAL_DURATIONS[episode_date],
            transcript_url=f"{site}/transcripts/{transcript.name}",
            chapters_url=f"{site}/chapters/{chapters.name}",
        )
        manifest.write(manifest_path)
        write_episode(
            feed,
            show=config.show,
            manifest=manifest,
            audio_url=manifest.audio_url,
            audio_length=length,
            duration=manifest.duration,
        )

    render_site(root / "docs" / "index.html")
    feed.replace(config.outputs.feed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
