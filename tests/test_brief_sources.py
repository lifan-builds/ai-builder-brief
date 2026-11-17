from pathlib import Path

from ai_builder_brief.brief_sources import load_manifest, validate_manifest


def test_daily_brief_source_manifest_is_valid() -> None:
    payload = load_manifest(Path(__file__).resolve().parents[1] / "brief_sources.yaml")
    assert validate_manifest(payload) == []


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

