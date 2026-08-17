from __future__ import annotations

import json
from datetime import date

from castforge.models import SourceItem

from ai_builder_brief.editorial import EditorialDecision
from ai_builder_brief.review import build_review_items, review_priority, write_review_artifacts


def _source(index: int) -> SourceItem:
    cluster_id = f"candidate-{index:02d}"
    return SourceItem(
        id=f"source-{index:02d}",
        title=f"Candidate {index:02d}",
        url=f"https://example.com/{index}",
        source="Example",
        published_at="2026-08-17T12:00:00Z",
        summary=f"Source-linked summary {index} — café.",
        authority="primary",
        organization=f"Organization {index:02d}",
        category="developer tools",
        metadata={
            "cluster_id": cluster_id,
            "kind": "development",
            "community_led": index < 6,
            "community_signal_types": ["x"] if index < 6 else [],
        },
    )


def _decision(index: int) -> EditorialDecision:
    return EditorialDecision(
        cluster_id=f"candidate-{index:02d}",
        decision="reject" if index == 0 else "accept",
        impact=4,
        actionability=4,
        novelty=4,
        evidence=4,
        audience_breadth=4,
        builder_actions=("use",),
        why_now="Current primary-source release.",
        rationale="Review rationale.",
        caveats="Verify the measured impact.",
        source_ids=(f"source-{index:02d}",),
    )


def test_review_items_include_rejects_and_cap_deterministic_ties() -> None:
    sources = [_source(index) for index in range(12)]
    items = build_review_items(sources, sources, [_decision(index) for index in range(12)])

    assert len(items) == 10
    assert [item["cluster_id"] for item in items] == [
        f"candidate-{index:02d}" for index in range(10)
    ]
    assert items[0]["decision"] == "reject"
    assert items[0]["podcast_ready"] is False
    assert items[1]["podcast_ready"] is True
    assert items[0]["sources"][0]["url"] == "https://example.com/0"
    assert sum(item["community_led"] for item in items) == 6
    assert sum(not item["community_led"] for item in items) == 4


def test_json_and_markdown_render_the_same_top_ten(tmp_path) -> None:
    sources = [_source(index) for index in range(12)]
    json_path, markdown_path = write_review_artifacts(
        date(2026, 8, 17),
        sources,
        sources,
        [_decision(index) for index in range(12)],
        tmp_path,
        window_start="2026-08-14T13:00:00Z",
    )

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    markdown = markdown_path.read_text(encoding="utf-8")
    assert payload["candidate_count"] == 10
    assert payload["podcast_ready_count"] == 9
    assert payload["actual_mix"] == {"community_led": 6, "major_primary": 4}
    assert payload["mix_shortfall"] == {"community_led": 0, "major_primary": 0}
    assert payload["window_start"] == "2026-08-14T13:00:00Z"
    assert payload["quality_exclusion_count"] == 0
    assert markdown.count("\n## ") == 10
    for item in payload["candidates"]:
        assert item["title"] in markdown
        assert item["cluster_id"] in markdown
        assert item["sources"][0]["url"] in markdown
    assert "café" in markdown
    assert "Strict review window starts: 2026-08-14T13:00:00Z" in markdown


def test_review_selection_enforces_organization_and_product_diversity() -> None:
    sources = [_source(index) for index in range(12)]
    sources[1] = SourceItem.from_dict({
        **sources[1].to_dict(),
        "organization": sources[0].organization,
        "metadata": {**sources[1].metadata, "product_family": "shared-product"},
    })
    sources[0] = SourceItem.from_dict({
        **sources[0].to_dict(),
        "metadata": {**sources[0].metadata, "product_family": "shared-product"},
    })

    items = build_review_items(sources, sources, [_decision(index) for index in range(12)])

    organizations = [item["organization"].casefold() for item in items]
    products = [item["product_family"].casefold() for item in items if item["product_family"]]
    assert len(organizations) == len(set(organizations))
    assert len(products) == len(set(products))
    assert "candidate-01" not in {item["cluster_id"] for item in items}


def test_routine_release_cannot_outrank_consequential_development() -> None:
    routine = SourceItem.from_dict({
        **_source(0).to_dict(),
        "metadata": {**_source(0).metadata, "editorial_class": "maintenance_release"},
    })
    consequential = SourceItem.from_dict({
        **_source(1).to_dict(),
        "metadata": {**_source(1).metadata, "editorial_class": "major_development"},
    })
    routine_decision = EditorialDecision(
        cluster_id="candidate-00", decision="accept", impact=4, actionability=4,
        novelty=3, evidence=4, audience_breadth=4, builder_actions=("use",),
    )
    consequential_decision = EditorialDecision(
        cluster_id="candidate-01", decision="accept", impact=3, actionability=3,
        novelty=3, evidence=3, audience_breadth=4, builder_actions=("reconsider",),
    )

    items = build_review_items(
        [routine, consequential], [routine, consequential],
        [routine_decision, consequential_decision],
    )

    assert [item["cluster_id"] for item in items] == ["candidate-01"]
    assert review_priority(routine_decision, routine)[0] is False


def test_exceptional_release_keeps_its_priority_penalty() -> None:
    release = SourceItem.from_dict({
        **_source(0).to_dict(),
        "metadata": {**_source(0).metadata, "editorial_class": "maintenance_release"},
    })
    development = SourceItem.from_dict({
        **_source(1).to_dict(),
        "metadata": {**_source(1).metadata, "editorial_class": "major_development"},
    })
    release_decision = EditorialDecision(
        cluster_id="candidate-00", decision="accept", impact=4, actionability=4,
        novelty=4, evidence=4, audience_breadth=4, builder_actions=("use",),
    )
    development_decision = EditorialDecision(
        cluster_id="candidate-01", decision="accept", impact=4, actionability=4,
        novelty=4, evidence=3, audience_breadth=4, builder_actions=("reconsider",),
    )

    items = build_review_items(
        [release, development], [release, development],
        [release_decision, development_decision],
    )

    assert [item["cluster_id"] for item in items] == ["candidate-01", "candidate-00"]


def test_research_requires_exceptional_editorial_judgment() -> None:
    paper = SourceItem.from_dict({
        **_source(2).to_dict(),
        "category": "research",
        "metadata": {**_source(2).metadata, "editorial_class": "research"},
    })
    ordinary = EditorialDecision(
        cluster_id="candidate-02", decision="accept", impact=3, actionability=3,
        novelty=4, evidence=3, audience_breadth=3, builder_actions=("test",),
    )
    exceptional = EditorialDecision(
        cluster_id="candidate-02", decision="accept", impact=4, actionability=4,
        novelty=4, evidence=4, audience_breadth=4, builder_actions=("test",),
    )

    assert review_priority(ordinary, paper)[0] is False
    assert review_priority(exceptional, paper)[0] is True


def test_review_artifact_reports_quality_exclusions(tmp_path) -> None:
    paper = SourceItem.from_dict({
        **_source(2).to_dict(),
        "category": "research",
        "metadata": {**_source(2).metadata, "editorial_class": "research"},
    })
    ordinary = EditorialDecision(
        cluster_id="candidate-02", decision="accept", impact=3, actionability=3,
        novelty=4, evidence=3, audience_breadth=3, builder_actions=("test",),
    )

    json_path, _ = write_review_artifacts(
        date(2026, 8, 17), [paper], [paper], [ordinary], tmp_path,
    )
    payload = json.loads(json_path.read_text(encoding="utf-8"))

    assert payload["candidate_count"] == 0
    assert payload["quality_exclusion_count"] == 1
    assert payload["quality_exclusions"][0]["cluster_id"] == "candidate-02"
