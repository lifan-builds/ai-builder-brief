from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from castforge.models import SourceItem, StoryCluster
from ai_builder_brief.editorial import EditorialDecision, is_podcast_ready, preprocess, select_clusters, validate_review, write_ledger
from ai_builder_brief import editorial_client
from ai_builder_brief.editorial_client import EDITORIAL_SCHEMA, EditorialReviewError, _client_key, review_candidates_batched
from ai_builder_brief.pipeline import _editorial_packet


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


def test_preprocess_reserves_twelve_community_and_twelve_primary_clusters() -> None:
    items = []
    for index in range(14):
        items.append(_item(
            f"community-{index:02d}",
            f"Builder evaluation pattern {index}",
            authority="analysis",
            metadata={
                "cluster_id": f"community-{index:02d}",
                "score": 30 + index,
                "community_signal": True,
                "community_signal_type": "x",
                "momentum_score": index % 5,
            },
        ))
        items.append(_item(
            f"primary-{index:02d}",
            f"Builder API capability {index}",
            metadata={"cluster_id": f"primary-{index:02d}", "score": 80 + index},
        ))

    candidates, _ = preprocess(items)
    cluster_ids = {str(item.metadata["cluster_id"]) for item in candidates}

    assert len(cluster_ids) == 24
    assert sum(cluster_id.startswith("community-") for cluster_id in cluster_ids) == 12
    assert sum(cluster_id.startswith("primary-") for cluster_id in cluster_ids) == 12


def test_signal_only_candidate_is_never_podcast_ready() -> None:
    signal = _item(
        "community",
        "Builders discuss a new evaluation pattern",
        authority="analysis",
        metadata={"community_signal": True, "community_signal_type": "x"},
    )
    decision = EditorialDecision(
        cluster_id="community", decision="accept", impact=4, actionability=4,
        novelty=4, evidence=4, audience_breadth=4, builder_actions=("test",),
    )

    assert is_podcast_ready(decision, [signal]) is False
    assert is_podcast_ready(decision, [_item("primary", "Primary API evidence")]) is True


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


def test_strict_schema_requires_every_declared_decision_property() -> None:
    decision = EDITORIAL_SCHEMA["properties"]["decisions"]["items"]
    assert set(decision["required"]) == set(decision["properties"])


def test_editorial_packet_is_compact_and_auditable() -> None:
    item = _item(
        "candidate",
        "A consequential API release",
        metadata={
            "cluster_id": "candidate", "source_ids": ["a", "b"],
            "momentum_score": 3, "recency_score": 4, "community_led": True,
            "community_signal_types": ["x"],
            "community_context": [{
                "source_id": "b", "source": "Approved X panel", "account": "builder",
                "summary": "Builders are changing how they evaluate agents.",
                "likes": 200, "retweets": 20, "hn_points": 0, "hn_comments": 0,
            }],
            "qualifying_evidence": True,
            "qualifying_source_ids": ["a"],
        },
        summary="x" * 1000,
    )
    packet = _editorial_packet([item])[0]
    assert packet["cluster_id"] == "candidate"
    assert packet["source_ids"] == ["a", "b"]
    assert packet["community_led"] is True
    assert packet["community_context"][0]["likes"] == 200
    assert packet["qualifying_source_ids"] == ["a"]
    assert len(packet["summary"]) == 700
    assert "metadata" not in packet


def _review_decision(cluster_id: str) -> dict:
    return {
        "cluster_id": cluster_id,
        "decision": "accept",
        "impact": 4,
        "actionability": 4,
        "novelty": 3,
        "evidence": 4,
        "audience_breadth": 3,
        "builder_actions": ["use"],
        "why_now": "now",
        "rationale": "reason",
        "caveats": "none",
        "depth_recommendation": "brief",
        "source_ids": [cluster_id],
    }


@pytest.mark.parametrize(
    ("candidate_count", "expected_batch_sizes"),
    [(23, [6, 6, 6, 5]), (24, [6, 6, 6, 6])],
)
def test_editorial_batches_are_bounded_and_merged_in_input_order(
    monkeypatch,
    candidate_count,
    expected_batch_sizes,
) -> None:
    calls: list[list[str]] = []

    def fake_review(packet):
        ids = [item["cluster_id"] for item in packet]
        calls.append(ids)
        return {"decisions": [_review_decision(cluster_id) for cluster_id in reversed(ids)]}

    monkeypatch.setattr(editorial_client, "review_candidates", fake_review)
    candidates = [{"cluster_id": str(index)} for index in range(candidate_count)]
    response = review_candidates_batched(candidates)

    assert [len(call) for call in calls] == expected_batch_sizes
    assert [item["cluster_id"] for item in response["decisions"]] == [
        str(index) for index in range(candidate_count)
    ]


def test_editorial_batch_rejects_partial_coverage(monkeypatch) -> None:
    def fake_review(packet):
        return {"decisions": [_review_decision(packet[0]["cluster_id"])]}

    monkeypatch.setattr(editorial_client, "review_candidates", fake_review)
    with pytest.raises(EditorialReviewError) as caught:
        review_candidates_batched([{"cluster_id": "one"}, {"cluster_id": "two"}])

    assert caught.value.to_metadata() == {
        "failure_type": "invalid_response",
        "stage": "editorial_coverage",
        "batch_index": 1,
    }


def test_editorial_request_timeout_is_safely_classified(monkeypatch) -> None:
    monkeypatch.setattr(editorial_client, "_usage_gate", lambda: None)
    monkeypatch.setattr(editorial_client, "_client_key", lambda: "local-test-key")

    def timeout_opener(request, timeout):
        assert timeout == 60
        raise TimeoutError("secret upstream details")

    with pytest.raises(EditorialReviewError) as caught:
        editorial_client.review_candidates(
            [{"cluster_id": "candidate"}],
            opener=timeout_opener,
        )

    assert caught.value.to_metadata() == {
        "failure_type": "timeout",
        "stage": "editorial_request",
    }
    assert "secret upstream details" not in str(caught.value)


def test_editorial_response_value_type_is_strictly_validated(monkeypatch) -> None:
    monkeypatch.setattr(editorial_client, "_usage_gate", lambda: None)
    monkeypatch.setattr(editorial_client, "_client_key", lambda: "local-test-key")

    def invalid_opener(request, timeout):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                body = {"choices": [{"message": {"content": json.dumps({"decisions": [{**_review_decision("candidate"), "impact": "4"}]})}}]}
                return json.dumps(body).encode("utf-8")

        return Response()

    with pytest.raises(EditorialReviewError) as caught:
        editorial_client.review_candidates(
            [{"cluster_id": "candidate"}],
            opener=invalid_opener,
        )

    assert caught.value.to_metadata() == {
        "failure_type": "invalid_response",
        "stage": "editorial_response",
    }


def test_editorial_batch_failure_is_indexed_without_raw_details(monkeypatch) -> None:
    def fake_review(packet):
        if packet[0]["cluster_id"] == "6":
            raise RuntimeError("https://localhost/proxy?api_key=secret-response")
        return {"decisions": [_review_decision(item["cluster_id"]) for item in packet]}

    monkeypatch.setattr(editorial_client, "review_candidates", fake_review)
    with pytest.raises(EditorialReviewError) as caught:
        review_candidates_batched([{"cluster_id": str(index)} for index in range(8)])

    error = caught.value
    assert error.category == "proxy"
    assert error.batch_index == 2
    assert "secret-response" not in str(error)
    assert error.to_metadata() == {
        "failure_type": "proxy",
        "stage": "editorial_batch",
        "batch_index": 2,
    }


def test_editorial_failure_ledger_metadata_is_allowlisted(tmp_path) -> None:
    path = write_ledger(
        [],
        tmp_path / "ledger.json",
        episode_date="2026-08-16",
        status="no-episode-editorial-failure",
        metadata={
            "failure_type": "timeout",
            "stage": "editorial_request",
            "batch_index": 2,
            "response_body": "Bearer super-secret",
            "url": "http://localhost/?key=secret",
        },
    )
    body = json.loads(path.read_text(encoding="utf-8"))
    assert body["metadata"] == {
        "failure_type": "timeout",
        "stage": "editorial_request",
        "batch_index": 2,
    }
    assert "secret" not in path.read_text(encoding="utf-8")
