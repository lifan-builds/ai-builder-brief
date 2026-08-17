from __future__ import annotations

import json
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime
from pathlib import Path

from castforge.config import load_config
from castforge.models import EpisodeManifest

from ai_builder_brief.collectors import CollectionResult, XPanelHealth
from ai_builder_brief.pipeline import (
    _apply_snapshot_deltas,
    _eligible_editorial_items,
    _represent_candidates,
    is_published,
    run_daily,
)
from castforge.models import SourceItem


def test_production_config_caps_r2_below_free_allowance() -> None:
    config = load_config(Path(__file__).resolve().parents[1] / "podcast.yaml")
    assert config.publication.max_bucket_bytes == 9_000_000_000
    assert config.selection.recent_days == 3


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
    assert (output / "editorial" / "2026-08-11.json").is_file()
    assert manifest.duration == "00:00:01"
    assert manifest.metadata["editorial_ledger"] == "editorial/2026-08-11.json"

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


def test_review_only_writes_review_and_stops_before_episode(show_project, monkeypatch) -> None:
    def fail_if_called(*args, **kwargs):
        raise AssertionError("review-only mode reached episode generation")

    monkeypatch.setattr("ai_builder_brief.pipeline.run_episode", fail_if_called)
    status = run_daily(
        config_path=show_project / "podcast.yaml",
        sources_path=show_project / "sources.yaml",
        episode_date=date(2026, 8, 11),
        fixture=True,
        review_only=True,
    )

    assert status == "review-ready"
    review_root = show_project / "build" / "review"
    review = json.loads((review_root / "2026-08-11.json").read_text(encoding="utf-8"))
    assert review["candidate_count"] == 2
    assert review["podcast_ready_count"] == 2
    assert [item["rank"] for item in review["candidates"]] == [1, 2]
    assert (review_root / "2026-08-11.md").is_file()
    assert (show_project / "build" / "editorial" / "2026-08-11.json").is_file()
    assert not (show_project / "build" / "fixture" / "manifests").exists()
    assert not (show_project / "build" / "fixture" / "feed.xml").exists()
    assert not (show_project / "docs").exists()


def test_zero_length_placeholder_is_not_considered_published(tmp_path) -> None:
    feed = tmp_path / "feed.xml"
    feed.write_text(
        """<rss><channel><item><guid>ai-builder-brief-2026-08-11</guid><enclosure url="https://example.com/a.mp3" length="0" type="audio/mpeg" /></item></channel></rss>""",
        encoding="utf-8",
    )
    assert not is_published(feed, "ai-builder-brief-2026-08-11")


def test_collector_failure_returns_no_episode_without_feed_mutation(show_project, monkeypatch) -> None:
    feed = show_project / "docs" / "feed.xml"
    feed.parent.mkdir(parents=True, exist_ok=True)
    feed.write_text("existing-feed", encoding="utf-8")
    monkeypatch.setattr("ai_builder_brief.pipeline.collect_sources", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")))
    status = run_daily(
        config_path=show_project / "podcast.yaml",
        sources_path=show_project / "sources.yaml",
        episode_date=date(2026, 8, 11),
    )
    assert status == "no-episode"
    assert feed.read_text(encoding="utf-8") == "existing-feed"


def test_review_only_collector_failure_keeps_ledger_without_partial_review(show_project, monkeypatch) -> None:
    monkeypatch.setattr(
        "ai_builder_brief.pipeline.collect_sources",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    status = run_daily(
        config_path=show_project / "podcast.yaml",
        sources_path=show_project / "sources.yaml",
        episode_date=date(2026, 8, 17),
        review_only=True,
    )

    assert status == "no-episode"
    ledger_path = show_project / "build" / "editorial" / "2026-08-17.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert ledger["status"] == "no-episode-collector-failure"
    assert not (show_project / "build" / "review" / "2026-08-17.json").exists()
    assert not (show_project / "build" / "review" / "2026-08-17.md").exists()


def test_unhealthy_x_retains_snapshot_and_health_but_emits_no_review(show_project, monkeypatch) -> None:
    source = SourceItem(
        id="primary", title="Primary builder API", url="https://example.com/primary",
        source="Example", published_at="2026-08-17T12:00:00Z",
        summary="Documented API and inference details.", authority="primary",
        organization="Example", category="developer tools",
        metadata={"cluster_id": "primary", "score": 90},
    )
    monkeypatch.setattr(
        "ai_builder_brief.pipeline.collect_sources",
        lambda *args, **kwargs: CollectionResult(
            items=(source,),
            x_panel=XPanelHealth(
                configured_accounts=19,
                attempted_accounts=19,
                successful_accounts=14,
                in_window_posts=0,
                failed_accounts=("one", "two", "three", "four", "five"),
            ),
        ),
    )

    status = run_daily(
        config_path=show_project / "podcast.yaml",
        sources_path=show_project / "sources.yaml",
        episode_date=date(2026, 8, 17),
        review_only=True,
    )

    assert status == "no-episode"
    health = json.loads((show_project / "build" / "source-health" / "2026-08-17.json").read_text(encoding="utf-8"))
    assert health["healthy"] is False
    assert health["x_panel"]["successful_accounts"] == 14
    assert (show_project / "build" / "snapshots" / "2026-08-17.json").is_file()
    ledger = json.loads((show_project / "build" / "editorial" / "2026-08-17.json").read_text(encoding="utf-8"))
    assert ledger["status"] == "no-review-source-failure"
    assert not (show_project / "build" / "review" / "2026-08-17.json").exists()
    assert not (show_project / "build" / "review" / "2026-08-17.md").exists()


def test_snapshot_deltas_measure_change_instead_of_lifetime_totals() -> None:
    def snapshot(day: str, stars: int) -> SourceItem:
        return SourceItem(
            id="release", title="Release", url="https://github.com/acme/tool/releases/1",
            source="GitHub", published_at="2026-08-11T12:00:00Z", summary="Release notes.",
            authority="primary", organization="acme", category="developer tools",
            metadata={"signal_key": "github:acme/tool", "snapshot_date": day, "repository_stars": stars},
        )

    enriched = _apply_snapshot_deltas([
        snapshot("2026-08-04", 100), snapshot("2026-08-10", 125), snapshot("2026-08-11", 130),
    ])
    assert enriched[0].metadata["delta_24h"]["repository_stars"] == 5
    assert enriched[0].metadata["delta_7d"]["repository_stars"] == 30
    assert enriched[0].metadata["momentum_score"] == 2
    assert enriched[0].metadata["community_signal"] is True


def test_editorial_window_is_strict_and_deduplicates_historical_snapshots() -> None:
    def source(item_id: str, published_at: str, snapshot_date: str) -> SourceItem:
        return SourceItem(
            id=item_id, title=item_id, url=f"https://example.com/{item_id}",
            source="Example", published_at=published_at, summary="Builder impact.",
            authority="primary", organization="Example", category="developer tools",
            metadata={"snapshot_date": snapshot_date},
        )

    start = datetime(2026, 8, 14, 13, tzinfo=UTC)
    end = datetime(2026, 8, 17, 13, tzinfo=UTC)
    eligible = _eligible_editorial_items([
        source("boundary", "2026-08-14T13:00:00Z", "2026-08-16"),
        source("duplicate", "2026-08-16T12:00:00Z", "2026-08-16"),
        source("duplicate", "2026-08-16T12:00:00Z", "2026-08-17"),
        source("stale", "2026-08-14T12:59:59Z", "2026-08-17"),
    ], start=start, end=end)

    assert {item.id for item in eligible} == {"boundary", "duplicate"}
    assert next(item for item in eligible if item.id == "duplicate").metadata["snapshot_date"] == "2026-08-17"


def test_representative_uses_resolved_subject_instead_of_publisher() -> None:
    report = SourceItem(
        id="report", title="Acme changes AI output policy",
        url="https://press.example/acme", source="Press",
        published_at="2026-08-17T12:00:00Z", summary="Acme policy details.",
        authority="independent", organization="Press", category="models",
        metadata={
            "cluster_id": "acme-policy", "score": 80,
            "subject_organizations": ["Acme"],
        },
    )

    representatives, _ = _represent_candidates([report])

    assert representatives[0].organization == "Acme"
    assert representatives[0].metadata["story_organization"] == "Acme"


def test_representative_does_not_invent_subject_for_ambiguous_resolution() -> None:
    report = SourceItem(
        id="report", title="Acme and Beta discuss AI policy",
        url="https://press.example/policy", source="Press",
        published_at="2026-08-17T12:00:00Z", summary="Both organizations publish policy details.",
        authority="independent", organization="Press", category="models",
        metadata={
            "cluster_id": "ambiguous-policy", "score": 80,
            "subject_organizations": ["Acme", "Beta"],
        },
    )

    representatives, _ = _represent_candidates([report])

    assert representatives[0].organization == ""
    assert representatives[0].metadata["story_organization"] == ""


def test_representative_uses_subject_common_to_all_resolved_sources() -> None:
    first = SourceItem(
        id="first", title="Acme changes AI output policy",
        url="https://press.example/acme", source="Press",
        published_at="2026-08-17T12:00:00Z", summary="Acme policy details.",
        authority="independent", organization="Press", category="models",
        metadata={
            "cluster_id": "acme-policy", "score": 80,
            "subject_organizations": ["Acme"],
        },
    )
    second = SourceItem(
        id="second", title="Acme uses Beta watermark technology",
        url="https://another.example/acme", source="Another Press",
        published_at="2026-08-17T11:00:00Z", summary="Acme and Beta implementation details.",
        authority="independent", organization="Another Press", category="models",
        metadata={
            "cluster_id": "acme-policy", "score": 75,
            "subject_organizations": ["Acme", "Beta"],
        },
    )

    representatives, _ = _represent_candidates([first, second])

    assert representatives[0].organization == "Acme"
    assert representatives[0].metadata["subject_organizations"] == ["Acme", "Beta"]
