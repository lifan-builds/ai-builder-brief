from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import date

from castforge.models import EpisodeManifest

from ai_builder_brief.pipeline import is_published, run_daily


def test_fixture_pipeline_builds_complete_public_artifacts(show_project) -> None:
    status = run_daily(
        config_path=show_project / "podcast.yaml",
        sources_path=show_project / "sources.yaml",
        episode_date=date(2026, 8, 11),
        fixture=True,
    )
    assert status == "published"
    output = show_project / "build" / "fixture"
    manifest = EpisodeManifest.read(output / "manifests" / "2026-08-11.json")
    assert len(manifest.stories) == 2
    assert all(story.is_qualified() for story in manifest.stories)
    assert (output / "sources" / "2026-08-11.md").is_file()
    assert (output / "transcripts" / "2026-08-11.vtt").is_file()
    chapters = json.loads((output / "chapters" / "2026-08-11.json").read_text(encoding="utf-8"))
    assert len(chapters["chapters"]) == 2
    assert (output / "index.html").is_file()

    root = ET.parse(output / "feed.xml").getroot()
    items = root.findall("./channel/item")
    assert len(items) == 1
    assert items[0].find("{https://podcastindex.org/namespace/1.0}transcript") is not None
    assert items[0].find("{https://podcastindex.org/namespace/1.0}chapters") is not None


def test_fixture_rerun_is_idempotent(show_project) -> None:
    kwargs = {
        "config_path": show_project / "podcast.yaml",
        "sources_path": show_project / "sources.yaml",
        "episode_date": date(2026, 8, 11),
        "fixture": True,
    }
    run_daily(**kwargs)
    run_daily(**kwargs)
    root = ET.parse(show_project / "build" / "fixture" / "feed.xml").getroot()
    assert len(root.findall("./channel/item")) == 1


def test_fixture_shadow_never_mutates_docs(show_project) -> None:
    docs = show_project / "docs"
    docs.mkdir()
    (docs / "feed.xml").write_text("public-feed", encoding="utf-8")
    (docs / "index.html").write_text("public-site", encoding="utf-8")
    assert run_daily(
        config_path=show_project / "podcast.yaml",
        sources_path=show_project / "sources.yaml",
        episode_date=date(2026, 8, 11),
        fixture=True,
        shadow=True,
    ) == "shadow"
    assert (docs / "feed.xml").read_text(encoding="utf-8") == "public-feed"
    assert (docs / "index.html").read_text(encoding="utf-8") == "public-site"


def test_zero_length_placeholder_is_not_considered_published(tmp_path) -> None:
    feed = tmp_path / "feed.xml"
    feed.write_text(
        """<rss><channel><item><guid>ai-builder-brief-2026-08-11</guid><enclosure url="https://example.com/a.mp3" length="0" type="audio/mpeg" /></item></channel></rss>""",
        encoding="utf-8",
    )
    assert not is_published(feed, "ai-builder-brief-2026-08-11")
