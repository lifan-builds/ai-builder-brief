from __future__ import annotations

from datetime import UTC, datetime

import pytest

from castforge.models import SourceItem, StoryCluster
from ai_builder_brief.editorial import EditorialDecision, preprocess, select_clusters, validate_review
from ai_builder_brief.editorial_client import _client_key


def _item(item_id: str, title: str, *, authority: str = "primary", category: str = "models", metadata=None, published_at: str = "2026-08-11T12:00:00Z", summary: str = "Documented API and inference details for builders.") -> SourceItem:
    return SourceItem(
        id=item_id,
        title=title,
        url=f"https://example.com/{item_id}",
        source=item_id,
        published_at=published_at,
        summary=summary,
        authority=authority,
        organization=item_id,
        category=category,
        metadata=metadata or {},
    )


def test_historical_low_value_examples_are_rejected() -> None:
    items = [
        _item("ci", "Routine CI dependency update", metadata={"score": 100}),
        _item("rumor", "Unverified launch rumor", authority="signal", metadata={"score": 99}),
        _item("card", "Weak model card", metadata={"score": 98}, summary="A model card with no actionable details."),
        _item("aws", "Daybreak models are now available on AWS", metadata={"score": 97}, summary="The same model is now available through an AWS cloud service."),
        _item("roundup", "The latest AI news announced in July roundup", metadata={"score": 96}),
        _item("rc", "Ollama v0.32.6 release candidate", metadata={"score": 95}),
        _item("patch", "Ollama v0.32.8 minor fix", metadata={"score": 94}),
        _item("build", "llama.cpp ROCm CI build update", metadata={"score": 93}),
        _item("driving", "Cooperative multi-agent autonomous driving paper", category="research", metadata={"score": 92}, summary="A vehicle-to-vehicle driving paper."),
    ]
    candidates, decisions = preprocess(items)
    assert candidates == []
    assert all(item["decision"] == "reject" for item in decisions)


def test_strict_review_requires_exact_candidate_coverage() -> None:
    with pytest.raises(ValueError, match="cover every candidate"):
        validate_review({"decisions": []}, {"candidate"})


def test_momentum_and_recency_are_carried_into_score() -> None:
    item = _item("release", "Builder API release", metadata={"momentum_score": 4})
    candidates, _ = preprocess([item], as_of=datetime(2026, 8, 11, 13, tzinfo=UTC))
    candidate = candidates[0]
    decision = validate_review(
        {"decisions": [{
            "cluster_id": "release", "decision": "accept", "impact": 4,
            "actionability": 4, "novelty": 4, "evidence": 4,
            "audience_breadth": 4, "builder_actions": ["use"],
            "why_now": "now", "rationale": "reason", "caveats": "none",
            "depth_recommendation": "brief", "source_ids": ["release"],
        }]},
        {"release"},
        {"release": candidate.metadata},
    )[0]
    assert decision.momentum == 4
    assert decision.recency == 4
    assert decision.score == 100


def test_selection_applies_diversity_penalty_iteratively_and_one_deep() -> None:
    source = _item("one", "One", metadata={"score": 90})
    clusters = tuple(
        StoryCluster(
            id=str(index), title=str(index), summary="summary", category="models",
            organization="same" if index < 3 else "other", sources=(source,),
            selection_reason="reason", metadata={},
        )
        for index in range(4)
    )
    decisions = tuple(
        EditorialDecision(
            cluster_id=str(index), decision="accept", impact=4,
            actionability=4, novelty=4, evidence=4, audience_breadth=4,
            builder_actions=("use",), why_now="now", rationale="reason",
            depth_recommendation="deep" if index < 2 else "brief",
        )
        for index in range(4)
    )
    selected = select_clusters(clusters, decisions, maximum=3)
    assert len(selected) == 3
    assert sum(story.metadata["editorial"]["depth_recommendation"] == "deep" for story in selected) == 1


def test_proxy_key_can_be_loaded_from_runner_local_config(tmp_path, monkeypatch) -> None:
    config = tmp_path / "config.yaml"
    config.write_text('api-keys:\n  - "local-test-key"\n', encoding="utf-8")
    monkeypatch.delenv("CLIPROXYAPI_API_KEY", raising=False)
    monkeypatch.setenv("CLIPROXYAPI_CONFIG", str(config))
    assert _client_key() == "local-test-key"
