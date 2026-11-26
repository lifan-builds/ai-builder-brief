from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

from castforge.models import SourceItem

from ai_builder_brief.benchmark import (
    GroundTruthStory,
    canonicalize_url,
    evaluate_brief_markdown,
    evaluate_history_corpus,
    evaluate_pipeline_candidates,
    extract_artifact_keys,
    fetch_algolia_hn_ground_truth,
    fetch_youtube_builder_ground_truth,
    load_hn_ground_truth,
    parse_coverage_window,
    render_benchmark_markdown,
)


def _gt_story(
    hn_id: str,
    title: str,
    url: str,
    points: int,
    published_at: str = "2026-11-16T12:00:00Z",
    tier: str = "artifact",
) -> GroundTruthStory:
    dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    return GroundTruthStory(
        hn_id=hn_id,
        title=title,
        url=url,
        canonical_url=canonicalize_url(url),
        points=points,
        comments=points // 3,
        published_at=published_at,
        created_at_i=int(dt.timestamp()),
        tier=tier,
    )


def test_canonicalize_url_unifies_arxiv_and_hf_paper_mirrors() -> None:
    assert canonicalize_url("https://arxiv.org/html/2611.21315v1") == "arxiv.org/abs/2611.21315"
    assert canonicalize_url("https://arxiv.org/pdf/2611.21315.pdf") == "arxiv.org/abs/2611.21315"
    assert canonicalize_url("https://huggingface.co/papers/2611.21315") == "arxiv.org/abs/2611.21315"
    assert canonicalize_url("https://qwen.ai/blog?id=qwen4-max&utm_source=x") == "qwen.ai/blog?id=qwen4-max"

    keys = extract_artifact_keys(
        ["https://arxiv.org/html/2611.21315v1", "https://github.com/GAIR-NLP/Benchleaking/releases/tag/v1.0"]
    )
    assert "arxiv:2611.21315" in keys
    assert "gh:gair-nlp/benchleaking" in keys


def test_parse_coverage_window_extracts_explicit_utc_timestamps() -> None:
    md = (
        "# AI Builder Brief — November 17, 2026\n\n"
        "Coverage window: November 14, 18:23 UTC–November 17, 18:23 UTC, 2026 (rolling 72 hours).\n"
    )
    start, end = parse_coverage_window(md, date(2026, 11, 17))
    assert start == datetime(2026, 11, 14, 18, 23, tzinfo=UTC)
    assert end == datetime(2026, 11, 17, 18, 23, tzinfo=UTC)


def test_evaluate_brief_markdown_scores_recall_snr_and_specificity(tmp_path: Path) -> None:
    ground_truth = [
        _gt_story("1", "Benchleaking audits open-model test contamination", "https://arxiv.org/abs/2611.21315", 620),
        _gt_story("2", "llama.cpp v1.0.0 C ABI stability contract", "https://github.com/ggml-org/llama.cpp/releases/tag/v1.0.0", 540),
        _gt_story("3", "Windows 11 coding agent telemetry analysis", "https://xusheng.dev/posts/windows-coding-agent-telemetry/", 700),
    ]
    strong_brief = tmp_path / "2026-11-17.md"
    strong_brief.write_text(
        "# AI Builder Brief — November 17, 2026\n\n"
        "Coverage window: November 14, 18:00 UTC–November 17, 18:00 UTC, 2026.\n\n"
        "Read on the day: Benchmark contamination audits and C-ABI version contracts reshape local and hosted agent pipelines.\n\n"
        "## Top 10\n\n"
        "1. **Benchleaking exposes memorized benchmark answers across open weights** — **November 16, 2026 paper**. "
        "Across four base models and 20 standard benchmarks, ERNIE-5.5-Flash reproduces 96.3% of HumanEval prompts verbatim and drops from 85.17 to 68.55 when leaked items receive zero credit. "
        "Audit contamination disclosures and run private held-out task evaluations before selecting base checkpoints, while noting clean retraining performance remains untested. "
        "[Paper](https://arxiv.org/html/2611.21315v1), [code](https://github.com/GAIR-NLP/Benchleaking).\n\n"
        "2. **llama.cpp v1.0.0 locks public C ABI and removes 35 deprecated calls** — **November 16, 2026 release**. "
        "ggml-org adopts semantic major-version bumps for incompatible public C API and ABI changes while hardening HTTP server defaults in v1.0.0 across CPU, Metal, and CUDA inference backends. "
        "Pin compatible library versions and run downstream language-binding integration checks before upgrading production servers, however single-stream inference throughput remains unchanged from prior builds. "
        "[Release](https://github.com/ggml-org/llama.cpp/releases/tag/v1.0.0).\n\n"
        "## Watchlist\n\n"
        "- **Windows 11 coding-agent telemetry — November 16:** collects active process metadata across CLI agents. [Analysis](https://xusheng.dev/posts/windows-coding-agent-telemetry/).\n",
        encoding="utf-8",
    )
    strong_eval = evaluate_brief_markdown(strong_brief, ground_truth)
    assert strong_eval.must_catch_recall > 80.0
    assert strong_eval.signal_to_noise_dedup >= 90.0
    assert strong_eval.editorial_specificity >= 95.0
    assert strong_eval.fluff_items == ()
    assert strong_eval.slop_flags == ()

    slop_brief = tmp_path / "2026-11-18.md"
    slop_brief.write_text(
        "# AI Builder Brief — November 18, 2026\n\n"
        "Coverage window: November 15, 18:00 UTC–November 18, 18:00 UTC, 2026.\n\n"
        "Read on the day: The strongest developments concern new enterprise deals, while labs file paperwork.\n\n"
        "## Top 10\n\n"
        "1. **Pedestal AI raises $4.7 million seed round for enterprise workflows** — **November 16, 2026**. "
        "This game-changing paradigm shift will revolutionize supply chains. Author-run results apply. [News](https://news.ycombinator.com).\n\n"
        "2. **Anthropic files S-1 for initial public offering** — **November 16, 2026**. "
        "An unprecedented watershed moment for capital markets. Author-run estimates apply. [Trending](https://github.com/trending).\n\n"
        "3. **Benchleaking repeats yesterday's exact paper without update** — **November 16, 2026**. "
        "Author-run numbers repeated verbatim from yesterday. [Paper](https://arxiv.org/abs/2611.21315).\n\n"
        "## Watchlist\n\n"
        "- **Duplicate Benchleaking entry:** same link in watchlist. [Paper](https://arxiv.org/abs/2611.21315).\n",
        encoding="utf-8",
    )
    slop_eval = evaluate_brief_markdown(slop_brief, ground_truth, prior_briefs=[strong_brief])
    assert slop_eval.signal_to_noise_dedup < 55.0
    assert len(slop_eval.fluff_items) >= 2
    assert len(slop_eval.duplicate_items) >= 2
    assert len(slop_eval.weak_source_items) >= 2
    assert "madlib_read_on_the_day" in slop_eval.slop_flags
    assert any(flag.startswith("repetitive_phrase:author-run") for flag in slop_eval.slop_flags)
    assert strong_eval.composite_score > slop_eval.composite_score + 30.0


def test_evaluate_pipeline_candidates_penalizes_ci_false_rejects_and_stub_summaries() -> None:
    gt = [
        _gt_story("10", "GPT-6.2 Astra and Sol API release", "https://openai.com/index/introducing-gpt-6-2-astra-sol/", 950),
    ]
    win_start = datetime(2026, 11, 14, 18, tzinfo=UTC)
    win_end = datetime(2026, 11, 17, 22, tzinfo=UTC)

    stub_item = SourceItem(
        id="hn-10",
        title="GPT-6.2 Astra and Sol",
        url="https://openai.com/index/introducing-gpt-6-2-astra-sol/",
        source="Hacker News",
        published_at="2026-11-17T16:00:00Z",
        summary="Hacker News signal: 950 points and 400 comments.",
        authority="signal",
        organization="openai.com",
        category="trend",
        metadata={"qualifying_evidence": False},
    )
    bad_decisions = [
        {"cluster_id": "artificial-intelligence-pricing-update", "decision": "reject", "rationale": "x: routine or excluded pattern: ci"}
        for _ in range(10)
    ]
    before = evaluate_pipeline_candidates(
        [stub_item],
        [],
        gt,
        window_start=win_start,
        window_end=win_end,
        decisions=bad_decisions,
    )

    rich_item = SourceItem(
        id="primary-10",
        title="OpenAI releases GPT-6.2 Astra and Sol in the API",
        url="https://openai.com/index/introducing-gpt-6-2-astra-sol/",
        source="OpenAI",
        published_at="2026-11-17T16:00:00Z",
        summary="Astra ($8/$40 per MTok) and Sol ($2/$10 per MTok) ship in the Responses API and Codex CLI with 20% lower token pricing.",
        authority="primary",
        organization="OpenAI",
        category="models",
        metadata={"qualifying_evidence": True},
    )
    after = evaluate_pipeline_candidates(
        [rich_item],
        [],
        gt,
        window_start=win_start,
        window_end=win_end,
        decisions=[],
    )
    assert before["false_ci_reject_count"] == 10
    assert before["stub_summary_count"] == 1
    assert after["false_ci_reject_count"] == 0
    assert after["stub_summary_count"] == 0
    assert after["editorial_specificity"] > before["editorial_specificity"] + 40.0
    assert after["signal_to_noise_dedup"] > before["signal_to_noise_dedup"] + 20.0


def test_committed_ground_truth_fixture_loads_and_renders_corpus_report() -> None:
    repo = Path(__file__).resolve().parents[1]
    gt = load_hn_ground_truth(repo / "fixtures" / "benchmark_hn_ground_truth.json")
    assert len(gt) >= 80
    report = evaluate_history_corpus(
        repo / "history",
        gt,
        start_date=date(2026, 11, 14),
        end_date=date(2026, 11, 17),
    )
    assert report["edition_count"] == 3
    md = render_benchmark_markdown(report)
    assert "# AI Builder Brief Benchmark Report" in md
    assert "2026-11-17" in md
    assert json.loads(json.dumps(report))["averages"]["composite_score"] > 0


def test_benchmark_filters_non_ai_github_repos_canonicalizes_youtube_and_skips_retired_top10(tmp_path: Path) -> None:
    class _Resp:
        def __init__(self, body: bytes) -> None:
            self._body = body
        def read(self) -> bytes:
            return self._body
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False

    hn_payload = json.dumps({
        "nbPages": 1,
        "hits": [
            {
                "objectID": "1",
                "title": "jQuery 4.1.0",
                "url": "https://github.com/jquery/jquery/releases/tag/4.1.0",
                "points": 381,
                "num_comments": 120,
                "created_at": "2026-11-24T12:00:00Z",
                "created_at_i": int(datetime(2026, 11, 24, 12, tzinfo=UTC).timestamp()),
            },
            {
                "objectID": "2",
                "title": "GLM-5.4 open-weights coding model",
                "url": "https://github.com/zai-org/GLM-5.4",
                "points": 1100,
                "num_comments": 400,
                "created_at": "2026-11-24T13:00:00Z",
                "created_at_i": int(datetime(2026, 11, 24, 13, tzinfo=UTC).timestamp()),
            },
        ],
    }).encode("utf-8")
    gt = fetch_algolia_hn_ground_truth(
        start=datetime(2026, 11, 23, tzinfo=UTC),
        end=datetime(2026, 11, 25, tzinfo=UTC),
        opener=lambda req, timeout=20: _Resp(hn_payload),
    )
    assert [s.title for s in gt] == ["GLM-5.4 open-weights coding model"]
    assert canonicalize_url("https://youtu.be/O_zcc6Xl12g?t=10") == "youtube.com/watch?v=o_zcc6xl12g"
    assert "yt:o_zcc6xl12g" in extract_artifact_keys(["https://www.youtube.com/watch?v=O_zcc6Xl12g"])

    sources_yaml = tmp_path / "sources.yaml"
    sources_yaml.write_text(
        "youtube_channels:\n  - {name: AI Engineer, channel_id: UC123, min_views: 15000}\n",
        encoding="utf-8",
    )
    yt_xml = b"""<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/">
      <entry>
        <yt:videoId>O_zcc6Xl12g</yt:videoId>
        <title>All Code Is Slop (Except Mine) - Lauren Tan</title>
        <published>2026-11-24T14:00:00+00:00</published>
        <media:group><media:community><media:statistics views="250000"/></media:community></media:group>
      </entry>
    </feed>"""
    yt_gt = fetch_youtube_builder_ground_truth(
        sources_yaml,
        start=datetime(2026, 11, 23, tzinfo=UTC),
        end=datetime(2026, 11, 25, tzinfo=UTC),
        opener=lambda req, timeout=15: _Resp(yt_xml),
    )
    assert len(yt_gt) == 1
    assert yt_gt[0].hn_id == "yt-O_zcc6Xl12g"

    day1 = tmp_path / "2026-11-24.md"
    day1.write_text(
        "# AI Builder Brief — November 24, 2026\n\n"
        "Coverage window: November 22, 18:00 UTC–November 24, 18:00 UTC, 2026.\n\n"
        "Read on the day: Open-weight coding loops advance.\n\n"
        "## Top 10\n\n"
        "1. **GLM-5.4 open-weights coding model launches with 100K context** — **November 24, 2026**. "
        "Benchmarks show 82.1% pass rate across coding tasks; test locally, however 8-GPU serving is needed. [Repo](https://github.com/zai-org/GLM-5.4).\n\n"
        "## Watchlist\n\n- None.\n",
        encoding="utf-8",
    )
    day2 = tmp_path / "2026-11-25.md"
    day2.write_text(
        "# AI Builder Brief — November 25, 2026\n\n"
        "Coverage window: November 23, 18:00 UTC–November 25, 18:00 UTC, 2026.\n\n"
        "Read on the day: Deterministic agent guardrails move mainstream.\n\n"
        "## Top 10\n\n"
        "1. **Lauren Tan details deterministic coding-agent verification loops in AI Engineer talk** — **November 24, 2026**. "
        "Over 250K views on converting review comments into repeatable harness checks; audit PR loops today, though setup cost is nontrivial. [Talk](https://youtu.be/O_zcc6Xl12g).\n\n"
        "## Watchlist\n\n- None.\n",
        encoding="utf-8",
    )
    eval_day2 = evaluate_brief_markdown(day2, [*gt, *yt_gt], prior_briefs=[day1])
    assert eval_day2.must_catch_recall == 100.0
    assert eval_day2.missed_ground_truth == ()
