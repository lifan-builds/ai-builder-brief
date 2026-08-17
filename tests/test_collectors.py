from __future__ import annotations

from datetime import UTC, datetime
from email.message import Message

from castforge.models import SourceItem

from ai_builder_brief.collectors import assign_story_clusters, collect_feed, collect_x_panel


class Response:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.headers = Message()

    def read(self) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


def test_feed_collector_keeps_only_rolling_window() -> None:
    xml = b"""<rss><channel>
      <item><title>Fresh release</title><link>https://example.com/fresh</link><pubDate>Tue, 11 Aug 2026 12:00:00 GMT</pubDate><description>Documented facts.</description></item>
      <item><title>Old release</title><link>https://example.com/old</link><pubDate>Mon, 10 Aug 2026 01:00:00 GMT</pubDate><description>Old facts.</description></item>
    </channel></rss>"""
    items = collect_feed(
        {"name": "Example", "url": "https://example.com/feed", "authority": "primary", "organization": "Example", "category": "models", "score": 90},
        start=datetime(2026, 8, 10, 13, tzinfo=UTC),
        end=datetime(2026, 8, 11, 13, tzinfo=UTC),
        opener=lambda request, timeout: Response(xml),
    )
    assert [item.title for item in items] == ["Fresh release"]
    assert items[0].authority == "primary"


def test_feed_topic_filter_rejects_general_technology_news() -> None:
    xml = b"""<rss><channel>
      <item><title>Mac screen sharing vulnerability</title><link>https://example.com/mac</link><pubDate>Tue, 11 Aug 2026 12:00:00 GMT</pubDate><description>Attackers can access a workstation.</description></item>
      <item><title>New AI model security guidance</title><link>https://example.com/ai</link><pubDate>Tue, 11 Aug 2026 12:00:00 GMT</pubDate><description>Threat modeling for agent deployments.</description></item>
    </channel></rss>"""
    items = collect_feed(
        {
            "name": "Broad technology feed", "url": "https://example.com/feed",
            "authority": "independent", "organization": "Example",
            "topic_filter": "ai", "category": "industry", "score": 75,
        },
        start=datetime(2026, 8, 10, 13, tzinfo=UTC),
        end=datetime(2026, 8, 11, 13, tzinfo=UTC),
        opener=lambda request, timeout: Response(xml),
    )

    assert [item.title for item in items] == ["New AI model security guidance"]


def _item(item_id: str, title: str, url: str, source: str) -> SourceItem:
    return SourceItem(
        id=item_id,
        title=title,
        url=url,
        source=source,
        published_at="2026-08-11T12:00:00Z",
        summary="Corroborated facts.",
        authority="independent",
        organization=source,
        category="industry",
        metadata={"score": 70},
    )


def test_headline_overlap_assigns_same_conservative_cluster() -> None:
    clustered = assign_story_clusters(
        [
            _item("a", "Acme Builder Model 2 reaches public preview", "https://a.example/1", "A"),
            _item("b", "Public preview opens for Acme Builder Model 2", "https://b.example/2", "B"),
        ]
    )
    assert clustered[0].metadata["cluster_id"] == clustered[1].metadata["cluster_id"]


def test_x_panel_retries_and_preserves_engagement_and_health() -> None:
    calls = 0

    def fetcher(account):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("transient")
        return [{
            "published_at": "2026-08-11T12:00:00Z",
            "url": "https://x.com/builder/status/1",
            "text": "A practical attributed observation about evaluation design.",
            "likes": 420,
            "rts": 40,
        }]

    result = collect_x_panel(
        ["builder"],
        start=datetime(2026, 8, 10, tzinfo=UTC),
        end=datetime(2026, 8, 12, tzinfo=UTC),
        fetcher=fetcher,
    )
    items = result.items
    assert calls == 2
    assert len(items) == 1
    assert items[0].authority == "analysis"
    assert items[0].metadata["kind"] == "expert_analysis"
    assert items[0].metadata["x_likes"] == 420
    assert items[0].metadata["x_retweets"] == 40
    assert items[0].metadata["momentum_score"] == 3
    assert result.health.healthy is True
    assert result.health.successful_accounts == 1


def test_x_panel_health_fails_below_coverage_without_raw_errors() -> None:
    result = collect_x_panel(
        ["one", "two"],
        start=datetime(2026, 8, 10, tzinfo=UTC),
        end=datetime(2026, 8, 12, tzinfo=UTC),
        fetcher=lambda account: (_ for _ in ()).throw(RuntimeError("secret response")),
    )

    assert result.items == ()
    assert result.health.healthy is False
    assert result.health.failed_accounts == ("one", "two")
    assert "secret" not in str(result.health.to_dict())


def test_explicit_product_family_collapses_versioned_releases() -> None:
    first = _item("ollama-1", "Ollama v0.12.1", "https://github.com/ollama/ollama/releases/1", "Ollama")
    second = _item("ollama-2", "Ollama v0.12.2", "https://github.com/ollama/ollama/releases/2", "Ollama")
    first = SourceItem.from_dict({**first.to_dict(), "metadata": {**first.metadata, "product_family": "ollama"}})
    second = SourceItem.from_dict({**second.to_dict(), "metadata": {**second.metadata, "product_family": "ollama"}})

    clustered = assign_story_clusters([first, second])

    assert {item.metadata["cluster_id"] for item in clustered} == {"product-ollama"}


def test_controlled_product_family_collapses_cross_source_qwen_posts() -> None:
    announcement = _item(
        "qwen-announcement", "Qwen3.8 27B open weights released",
        "https://x.com/example/status/1", "Approved X panel",
    )
    local_test = _item(
        "qwen-test", "Testing Qwen 3.8 27B locally",
        "https://example.com/qwen-test", "Independent review",
    )

    clustered = assign_story_clusters([announcement, local_test])

    assert {item.metadata["cluster_id"] for item in clustered} == {"product-qwen"}


def test_affiliation_and_topic_overlap_consolidate_one_event() -> None:
    commentary = SourceItem(
        id="commentary", title="Acme's watermark policy draws developer concern",
        url="https://news.example/watermark", source="Community discussion",
        published_at="2026-08-17T12:00:00Z", summary="Builders debate Acme watermark behavior.",
        authority="signal", organization="news.example", category="trend",
        metadata={"score": 70, "community_signal": True},
    )
    representative = SourceItem(
        id="representative", title="FAQ about watermarking",
        url="https://x.com/representative/status/1", source="Approved X panel",
        published_at="2026-08-17T11:00:00Z", summary="We explain how watermarking works.",
        authority="analysis", organization="Acme", category="expert analysis",
        metadata={
            "score": 60, "community_signal": True,
            "affiliated_organization": "Acme",
        },
    )
    unrelated = SourceItem(
        id="unrelated", title="Acme changes API pricing",
        url="https://x.com/representative/status/2", source="Approved X panel",
        published_at="2026-08-17T10:00:00Z", summary="New token pricing begins today.",
        authority="analysis", organization="Acme", category="expert analysis",
        metadata={
            "score": 60, "community_signal": True,
            "affiliated_organization": "Acme",
        },
    )

    clustered = assign_story_clusters(
        [commentary, representative, unrelated], organizations=["Acme"],
    )
    by_id = {item.id: item for item in clustered}

    assert by_id["commentary"].metadata["cluster_id"] == by_id["representative"].metadata["cluster_id"]
    assert by_id["unrelated"].metadata["cluster_id"] != by_id["representative"].metadata["cluster_id"]


def test_affiliation_name_cannot_satisfy_topic_overlap() -> None:
    security = SourceItem(
        id="security", title="Acme publishes defender guidance",
        url="https://acme.example/security", source="Acme",
        published_at="2026-08-17T12:00:00Z", summary="Acme discusses cyber defense.",
        authority="primary", organization="Acme", category="security",
        metadata={"score": 90},
    )
    pricing = SourceItem(
        id="pricing", title="Acme changes API pricing",
        url="https://acme.example/pricing", source="Acme",
        published_at="2026-08-17T11:00:00Z", summary="Acme announces token prices.",
        authority="primary", organization="Acme", category="models",
        metadata={"score": 90},
    )

    clustered = assign_story_clusters([security, pricing], organizations=["Acme"])

    assert len({item.metadata["cluster_id"] for item in clustered}) == 2


def test_independent_publisher_is_not_treated_as_story_subject() -> None:
    first = SourceItem(
        id="first", title="Acme explains invisible AI watermarks",
        url="https://press.example/acme", source="Press",
        published_at="2026-08-17T12:00:00Z", summary="Acme describes watermark behavior.",
        authority="independent", organization="Press", category="models",
        metadata={"score": 80},
    )
    second = SourceItem(
        id="second", title="Beta removes visible AI watermarks",
        url="https://press.example/beta", source="Press",
        published_at="2026-08-17T11:00:00Z", summary="Beta changes watermark controls.",
        authority="independent", organization="Press", category="models",
        metadata={"score": 80},
    )

    clustered = assign_story_clusters(
        [first, second], organizations=["Acme", "Beta", "Press"],
    )

    assert len({item.metadata["cluster_id"] for item in clustered}) == 2


def test_x_account_affiliation_is_preserved_as_organization() -> None:
    result = collect_x_panel(
        [{"account": "representative", "organization": "Acme"}],
        start=datetime(2026, 8, 16, tzinfo=UTC),
        end=datetime(2026, 8, 18, tzinfo=UTC),
        fetcher=lambda account: [{
            "published_at": "2026-08-17T12:00:00Z",
            "url": "https://x.com/representative/status/1",
            "text": "A consequential provider policy update.",
        }],
    )

    assert result.items[0].organization == "Acme"
    assert result.items[0].metadata["x_account"] == "representative"
