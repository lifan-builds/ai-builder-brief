from __future__ import annotations

from datetime import UTC, datetime
from email.message import Message

from castforge.models import SourceItem

from ai_builder_brief.collectors import assign_story_clusters, collect_feed, collect_hacker_news, collect_x_panel


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


def test_optional_x_accounts_do_not_block_health() -> None:
    result = collect_x_panel(
        [{"account": "core", "required": True}, {"account": "interpreter", "required": False}],
        start=datetime(2026, 8, 10, tzinfo=UTC),
        end=datetime(2026, 8, 12, tzinfo=UTC),
        fetcher=lambda account: ([{
            "published_at": "Aug 11 12:00", "url": "https://x.test/core/1",
            "text": "AI model API release", "likes": 1, "retweets": 0,
        }] if account == "core" else (_ for _ in ()).throw(RuntimeError("optional"))),
    )
    assert result.health.healthy is True
    assert result.health.failed_accounts == ("interpreter",)
    assert result.health.required_configured_accounts == 1
    assert result.health.required_successful_accounts == 1


def test_x_panel_recognizes_builder_product_and_lab_names() -> None:
    result = collect_x_panel(
        ["launches"],
        start=datetime(2026, 8, 10, tzinfo=UTC),
        end=datetime(2026, 8, 12, tzinfo=UTC),
        topic_filter="ai",
        fetcher=lambda account: [
            {
                "published_at": "2026-08-11T12:00:00Z",
                "url": "https://x.com/launches/status/1",
                "text": "DeepSeek V4 API is live with a new plugin and skill workflow.",
            }
        ],
    )

    assert len(result.items) == 1


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


def test_same_organization_generic_summary_words_do_not_merge_events() -> None:
    voice = SourceItem(
        id="voice", title="Build natural voice experiences with GPT Live",
        url="https://openai.example/voice", source="OpenAI",
        published_at="2026-08-17T12:00:00Z",
        summary="The API brings stronger instruction following and telephony support.",
        authority="primary", organization="OpenAI", category="models",
        metadata={"score": 95},
    )
    government = SourceItem(
        id="government", title="Expanding AI access for governments",
        url="https://openai.example/government", source="OpenAI",
        published_at="2026-08-17T11:00:00Z",
        summary="Eligible agencies receive cyber defense support and usage access.",
        authority="primary", organization="OpenAI", category="policy",
        metadata={"score": 95},
    )
    policy = SourceItem(
        id="policy", title="The AI policy window is open",
        url="https://openai.example/policy", source="OpenAI",
        published_at="2026-08-17T10:00:00Z",
        summary="Durable safety standards require stronger evidence and action.",
        authority="primary", organization="OpenAI", category="policy",
        metadata={"score": 95},
    )

    clustered = assign_story_clusters(
        [voice, government, policy], organizations=["OpenAI"],
    )
    by_id = {item.id: item for item in clustered}

    assert len({item.metadata["cluster_id"] for item in clustered}) == 3
    assert by_id["voice"].metadata["cluster_id"] != by_id["government"].metadata["cluster_id"]
    assert by_id["voice"].metadata["cluster_id"] != by_id["policy"].metadata["cluster_id"]


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


def test_long_organization_names_and_baseline_mentions_do_not_false_merge() -> None:
    revenue = SourceItem(
        id="rev",
        title="Moonshot AI targets $2B in annual revenue",
        url="https://techcrunch.example/moonshot-revenue",
        source="TechCrunch AI",
        published_at="2026-09-11T19:00:00Z",
        summary="OpenRouter data shows strong Kimi usage across models.",
        authority="independent",
        organization="TechCrunch",
        category="industry",
        metadata={"score": 60},
    )
    distillation = SourceItem(
        id="distill",
        title="Anthropic details distillation campaigns from Moonshot AI",
        url="https://techcrunch.example/distillation",
        source="TechCrunch AI",
        published_at="2026-09-10T18:00:00Z",
        summary="Anthropic alleges model usage campaigns across providers.",
        authority="independent",
        organization="TechCrunch",
        category="security",
        metadata={"score": 60},
    )
    quail_paper = SourceItem(
        id="quail",
        title="Quail saves recurrent checkpoints alongside KV caches",
        url="https://fsdatalab.github.io/blog/quail",
        source="FSDataLab",
        published_at="2026-11-16T12:00:00Z",
        summary="On one H100 with Qwen3.5-4B, Quail achieves a 3.2x speedup over vLLM.",
        authority="independent",
        organization="FSDataLab",
        category="infrastructure",
        metadata={"score": 85},
    )
    qwen_release = SourceItem(
        id="qwen-ga",
        title="Qwen4-Max opens general API access",
        url="https://docs.qwencloud.com/changelog/models",
        source="QwenCloud",
        published_at="2026-11-16T10:00:00Z",
        summary="General API availability with 1M context.",
        authority="primary",
        organization="Qwen",
        category="models",
        metadata={"score": 90},
    )

    clustered = assign_story_clusters(
        [revenue, distillation, quail_paper, qwen_release],
        organizations=["Anthropic", "Moonshot AI", "Qwen", "vLLM"],
    )
    by_id = {item.id: item for item in clustered}
    assert by_id["rev"].metadata["cluster_id"] != by_id["distill"].metadata["cluster_id"]
    assert by_id["quail"].metadata["cluster_id"] != by_id["qwen-ga"].metadata["cluster_id"]
    assert not str(by_id["quail"].metadata["cluster_id"]).startswith("product-")


def test_algolia_hn_collector_enriches_linked_summary_and_filters_non_ai_agents() -> None:
    import json as _json

    algolia_payload = {
        "nbPages": 1,
        "hits": [
            {
                "objectID": "501",
                "title": "Windows 11 collects coding agent telemetry across CLI tools",
                "url": "https://xusheng.example/posts/windows-coding-agent-telemetry",
                "points": 704,
                "num_comments": 584,
                "created_at_i": int(datetime(2026, 11, 16, 22, 0, tzinfo=UTC).timestamp()),
            },
            {
                "objectID": "502",
                "title": "Police officer gave ICE agent husband access to camera system",
                "url": "https://localnews.example/ice-agent",
                "points": 650,
                "num_comments": 370,
                "created_at_i": int(datetime(2026, 11, 16, 20, 0, tzinfo=UTC).timestamp()),
            },
        ],
    }
    article_html = (
        b'<html><head><meta property="og:description" content="'
        b"Detailed packet capture and ETW trace analysis showing Windows 11 enumerating active coding agent processes and MCP configs."
        b'"></head></html>'
    )

    def opener(request, timeout=30):
        if "hn.algolia.com" in request.full_url:
            return Response(_json.dumps(algolia_payload).encode("utf-8"))
        return Response(article_html)

    items = collect_hacker_news(
        start=datetime(2026, 11, 15, tzinfo=UTC),
        end=datetime(2026, 11, 17, tzinfo=UTC),
        limit=20,
        topic_filter="ai",
        use_algolia=True,
        enrich_links=True,
        opener=opener,
    )
    assert len(items) == 1
    assert items[0].id == "hn-501"
    assert items[0].authority == "independent"
    assert "ETW trace analysis" in items[0].summary
    assert items[0].metadata["hn_points"] == 704
