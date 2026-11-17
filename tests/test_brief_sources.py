from email.message import Message
from pathlib import Path

from ai_builder_brief.brief_sources import check_live_sources, load_manifest, validate_manifest


def test_daily_brief_source_manifest_is_valid() -> None:
    payload = load_manifest(Path(__file__).resolve().parents[1] / "brief_sources.yaml")
    assert validate_manifest(payload) == []


def test_live_source_probe_flags_http_blocks_and_validates_weights() -> None:
    class MockResp:
        def __init__(self, status: int, body: bytes) -> None:
            self.status = status
            self._body = body
            self.headers = Message()

        def read(self, _n: int = -1) -> bytes:
            return self._body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

    bad_weight = {
        "version": 1,
        "policy": {
            "storage": "metadata_only",
            "reddit": "discovery_only",
            "social": "discovery_only",
            "newsletters": "discovery_only",
            "paywalls": "do_not_bypass",
        },
        "sources": [{"id": "s1", "role": "primary", "language": "en", "weight": 150, "url": "https://example.com/feed"}],
        "coverage_lanes": {
            lane: ["s1"]
            for lane in (
                "models_and_apis",
                "developer_tools_and_agents",
                "infrastructure_and_economics",
                "research_and_evaluation",
                "security_and_policy",
                "builder_behavior",
            )
        },
    }
    assert "s1 weight must be an integer between 1 and 100" in validate_manifest(bad_weight)

    probe_errors = check_live_sources(
        {"sources": [{"id": "blocked", "url": "https://example.com/news"}, {"id": "ok", "url": "https://example.com/rss"}]},
        opener=lambda req, timeout: MockResp(403, b"forbidden") if "news" in req.full_url else MockResp(200, b"<rss>" + b"x" * 64 + b"</rss>"),
    )
    assert len(probe_errors) == 1
    assert "blocked" in probe_errors[0] and "403" in probe_errors[0]


def test_reddit_cannot_become_factual_evidence() -> None:
    payload = {
        "version": 1,
        "policy": {
            "storage": "metadata_only",
            "reddit": "discovery_only",
            "social": "discovery_only",
            "newsletters": "discovery_only",
            "paywalls": "do_not_bypass",
        },
        "sources": [{"id": "reddit", "role": "primary", "language": "en", "url": "https://reddit.com/r/LocalLLaMA"}],
        "coverage_lanes": {lane: ["reddit"] for lane in {
            "models_and_apis",
            "developer_tools_and_agents",
            "infrastructure_and_economics",
            "research_and_evaluation",
            "security_and_policy",
            "builder_behavior",
        }},
    }
    errors = validate_manifest(payload)
    assert "Reddit sources must be discovery-only" in errors

