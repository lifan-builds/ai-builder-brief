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
