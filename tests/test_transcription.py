from __future__ import annotations

import json

from castforge.models import EpisodeManifest, SourceItem, StoryCluster

from ai_builder_brief.transcription import Segment, write_chapters, write_vtt


def manifest() -> EpisodeManifest:
    source = SourceItem(
        id="source",
        title="Release",
        url="https://example.com/release",
        source="Example",
        published_at="2026-08-11T12:00:00Z",
        summary="A release happened.",
        authority="primary",
    )
    story = StoryCluster(
        id="story",
        title="Release",
        summary="A release happened.",
        category="models",
        organization="Example",
        sources=(source,),
        selection_reason="Primary source",
    )
    return EpisodeManifest(
        show_slug="show",
        episode_id="show-2026-08-11",
        episode_date="2026-08-11",
        title="Show",
        created_at="2026-08-11T13:00:00Z",
        stories=(story,),
        source_document="source.md",
        pipeline_version="0.1.0",
    )


def test_vtt_and_chapters_are_podcast_ready(tmp_path) -> None:
    segments = [Segment(0, 3.5, "Opening"), Segment(3.5, 10, "Story")]
    vtt = write_vtt(segments, tmp_path / "transcript.vtt")
    assert vtt.read_text(encoding="utf-8").startswith("WEBVTT\n")
    chapters = json.loads(write_chapters(manifest(), segments, tmp_path / "chapters.json").read_text())
    assert chapters["version"] == "1.2.0"
    assert chapters["chapters"][0]["startTime"] == 0.0
