from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

import pytest

from ai_builder_brief import cli, factcheck
from ai_builder_brief.factcheck import (
    DEFAULT_FACTCHECK_MODEL,
    FACTCHECK_SCHEMA,
    QUOTASWITCH_AUTH_HEADER,
    QUOTASWITCH_AUTH_VALUE,
    ClaimVerdict,
    FactCheckError,
    FactCheckReport,
    FetchedSource,
    ItemFactCheck,
    _candidate_fetch_urls,
    _proxy_config,
    extract_text_from_html,
    factcheck_brief,
    fetch_source_text,
    parse_brief_items,
    render_factcheck_markdown,
    rollup_item_verdict,
    should_fail_gate,
    verify_item_claims,
)


class _FakeHTTPResponse:
    def __init__(self, body: bytes | str, *, content_type: str = "application/json") -> None:
        self._body = body.encode("utf-8") if isinstance(body, str) else body
        self.headers = {"Content-Type": content_type}

    def __enter__(self) -> "_FakeHTTPResponse":
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def read(self, size: int = -1) -> bytes:
        return self._body if size < 0 else self._body[:size]


def _responses_envelope(payload: dict, *, status: str = "completed") -> str:
    return json.dumps({
        "id": "resp_test",
        "object": "response",
        "status": status,
        "error": None,
        "output": [
            {"id": "rs_1", "type": "reasoning", "summary": []},
            {
                "id": "msg_1",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(payload, ensure_ascii=False),
                    }
                ],
            },
        ],
    })


def test_factcheck_schema_is_strict_and_complete() -> None:
    assert FACTCHECK_SCHEMA["additionalProperties"] is False
    assert set(FACTCHECK_SCHEMA["required"]) == set(FACTCHECK_SCHEMA["properties"])
    claim_schema = FACTCHECK_SCHEMA["properties"]["claims"]["items"]
    assert claim_schema["additionalProperties"] is False
    assert set(claim_schema["required"]) == set(claim_schema["properties"])
    assert claim_schema["properties"]["verdict"]["enum"] == [
        "supported",
        "nuance_drift",
        "unsupported",
    ]


def test_proxy_config_enforces_loopback_and_blocks_sk_keys(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert _proxy_config() == ("http://127.0.0.1:18455/v1", "quotaswitch-local")

    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    with pytest.raises(FactCheckError) as remote:
        _proxy_config()
    assert remote.value.to_metadata() == {"failure_type": "proxy", "stage": "proxy_config"}

    monkeypatch.setenv("OPENAI_BASE_URL", "http://127.0.0.1:18455/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-live-secret")
    with pytest.raises(FactCheckError) as raw_key:
        _proxy_config()
    assert raw_key.value.to_metadata() == {"failure_type": "proxy", "stage": "proxy_config"}


def test_verify_item_claims_uses_responses_api_header_and_verifies_quotes(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("FACTCHECK_MODEL", raising=False)

    captured: dict[str, object] = {}
    source_url = "https://example.org/release"
    source_text = (
        "Model X ships at 5.93 GB in Q2_0 format under Apache-2.0 and reaches 204 tok/s decode on RTX 4090. "
        "Terminal-Bench 2.1 scores 60.1 versus 70.8 for FP16."
    )

    def fake_opener(request, timeout):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _FakeHTTPResponse(
            _responses_envelope({
                "item_id": "top-01",
                "claims": [
                    {
                        "claim": "Model X ships at 5.93 GB under Apache-2.0",
                        "verdict": "supported",
                        "source_url": source_url,
                        "evidence_quote": "Model X ships at 5.93 GB in Q2_0 format under Apache-2.0",
                        "reason": "Exact size and license match the release post.",
                    },
                    {
                        "claim": "Model X matches FP16 on Terminal-Bench 2.1",
                        "verdict": "unsupported",
                        "source_url": source_url,
                        "evidence_quote": "Terminal-Bench 2.1 scores 60.1 versus 70.8 for FP16.",
                        "reason": "Source reports 60.1 vs 70.8 FP16, a 10.7-point gap.",
                    },
                ],
            })
        )

    verdicts = verify_item_claims(
        item_id="top-01",
        headline="Model X ships at 5.93 GB",
        claim_text="Model X ships at 5.93 GB under Apache-2.0 and matches FP16 on Terminal-Bench 2.1.",
        sources=[FetchedSource(url=source_url, fetched_url=source_url, status="fetched", text=source_text)],
        opener=fake_opener,
    )

    assert captured["url"] == "http://127.0.0.1:18455/v1/responses"
    headers = {k.lower(): v for k, v in captured["headers"].items()}  # type: ignore[union-attr]
    assert headers[QUOTASWITCH_AUTH_HEADER.lower()] == QUOTASWITCH_AUTH_VALUE
    assert headers["authorization"] == "Bearer quotaswitch-local"
    body = captured["body"]
    assert body["model"] == DEFAULT_FACTCHECK_MODEL  # type: ignore[index]
    assert body["text"]["format"]["type"] == "json_schema"  # type: ignore[index]
    assert body["text"]["format"]["strict"] is True  # type: ignore[index]
    assert body["text"]["format"]["schema"] == FACTCHECK_SCHEMA  # type: ignore[index]

    assert len(verdicts) == 2
    assert verdicts[0].verdict == "supported"
    assert verdicts[0].quote_verified is True
    assert verdicts[1].verdict == "unsupported"
    assert verdicts[1].quote_verified is True
    assert rollup_item_verdict(verdicts, has_fetched_source=True) == "unsupported"


@pytest.mark.parametrize(
    ("bad_payload", "expected_stage"),
    [
        ({"item_id": "wrong-id", "claims": []}, "factcheck_schema"),
        (
            {
                "item_id": "top-01",
                "claims": [
                    {
                        "claim": "x",
                        "verdict": "partially_supported",
                        "source_url": "https://example.org/a",
                        "evidence_quote": "x",
                        "reason": "y",
                    }
                ],
            },
            "factcheck_schema",
        ),
        (
            {
                "item_id": "top-01",
                "claims": [
                    {
                        "claim": "x",
                        "verdict": "supported",
                        "source_url": "https://hallucinated.example/other",
                        "evidence_quote": "x",
                        "reason": "y",
                    }
                ],
            },
            "factcheck_schema",
        ),
        (
            {
                "item_id": "top-01",
                "claims": [
                    {
                        "claim": "x",
                        "verdict": "supported",
                        "source_url": "https://example.org/a",
                        "evidence_quote": "x",
                        "reason": "y",
                        "rewrite_suggestion": "not allowed",
                    }
                ],
            },
            "factcheck_schema",
        ),
    ],
)
def test_verify_item_claims_rejects_schema_and_url_violations(monkeypatch, bad_payload, expected_stage) -> None:
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def bad_opener(request, timeout):
        return _FakeHTTPResponse(_responses_envelope(bad_payload))

    with pytest.raises(FactCheckError) as caught:
        verify_item_claims(
            item_id="top-01",
            headline="Headline",
            claim_text="Claim text",
            sources=[FetchedSource(url="https://example.org/a", fetched_url="https://example.org/a", status="fetched", text="Source text " * 20)],
            opener=bad_opener,
        )
    assert caught.value.to_metadata() == {
        "failure_type": "invalid_response",
        "stage": expected_stage,
        "item_id": "top-01",
    }


def test_verify_item_claims_classifies_timeouts_without_leaking_secrets(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def timeout_opener(request, timeout):
        raise TimeoutError("upstream token=super-secret-value")

    with pytest.raises(FactCheckError) as caught:
        verify_item_claims(
            item_id="top-02",
            headline="Headline",
            claim_text="Claim text",
            sources=[FetchedSource(url="https://example.org/a", fetched_url="https://example.org/a", status="fetched", text="Source text " * 20)],
            opener=timeout_opener,
            max_attempts=1,
        )
    assert caught.value.to_metadata() == {
        "failure_type": "timeout",
        "stage": "factcheck_request",
        "item_id": "top-02",
    }
    assert "super-secret-value" not in str(caught.value)


def test_verify_item_claims_parses_sse_stream_and_retries_transient_failure(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    calls = 0
    payload = {
        "item_id": "top-01",
        "claims": [{
            "claim": "Ships v2.0",
            "verdict": "supported",
            "source_url": "https://example.org/a",
            "evidence_quote": "Ships v2.0 today",
            "reason": "Confirmed verbatim.",
        }],
    }
    sse_body = (
        "event: response.created\n"
        'data: {"type":"response.created","response":{"id":"r1","status":"in_progress"}}\n\n'
        "event: keepalive\n"
        'data: {"type":"keepalive"}\n\n'
        "event: response.completed\n"
        f'data: {{"type":"response.completed","response":{_responses_envelope(payload)}}}\n\n'
    )

    def flaky_sse_opener(request, timeout):
        nonlocal calls
        calls += 1
        assert json.loads(request.data.decode("utf-8"))["stream"] is True
        if calls == 1:
            return _FakeHTTPResponse(
                'event: response.failed\ndata: {"type":"response.failed","response":{"status":"failed"}}\n\n',
                content_type="text/event-stream",
            )
        return _FakeHTTPResponse(sse_body, content_type="text/event-stream")

    verdicts = verify_item_claims(
        item_id="top-01",
        headline="Headline",
        claim_text="Ships v2.0.",
        sources=[FetchedSource(url="https://example.org/a", fetched_url="https://example.org/a", status="fetched", text="Ships v2.0 today. " * 10)],
        opener=flaky_sse_opener,
        max_attempts=2,
    )
    assert calls == 2
    assert len(verdicts) == 1
    assert verdicts[0].verdict == "supported"
    assert verdicts[0].quote_verified is True


def test_verify_item_claims_retries_truncated_sse_stream(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    calls = 0
    payload = {
        "item_id": "top-01",
        "claims": [{
            "claim": "Ships v2.0",
            "verdict": "supported",
            "source_url": "https://example.org/a",
            "evidence_quote": "Ships v2.0 today",
            "reason": "Confirmed.",
        }],
    }
    truncated_sse = (
        "event: response.created\n"
        'data: {"type":"response.created","response":{"id":"r1","status":"in_progress"}}\n\n'
        "event: keepalive\n"
        'data: {"type":"keepalive"}\n\n'
    )
    complete_sse = (
        "event: response.completed\n"
        f'data: {{"type":"response.completed","response":{_responses_envelope(payload)}}}\n\n'
    )

    def drop_then_ok(request, timeout):
        nonlocal calls
        calls += 1
        return _FakeHTTPResponse(truncated_sse if calls == 1 else complete_sse, content_type="text/event-stream")

    verdicts = verify_item_claims(
        item_id="top-01",
        headline="Headline",
        claim_text="Ships v2.0.",
        sources=[FetchedSource(url="https://example.org/a", fetched_url="https://example.org/a", status="fetched", text="Ships v2.0 today. " * 10)],
        opener=drop_then_ok,
        max_attempts=2,
    )
    assert calls == 2
    assert len(verdicts) == 1
    assert verdicts[0].verdict == "supported"


def test_candidate_fetch_urls_resolve_github_hf_and_arxiv_artifacts() -> None:
    assert _candidate_fetch_urls("https://github.com/openai/math/blob/main/lean/docs/107.md")[0] == (
        "https://raw.githubusercontent.com/openai/math/main/lean/docs/107.md",
        "text",
    )
    assert _candidate_fetch_urls("https://github.com/anthropics/formal-math/tree/main/vp-vnp")[0] == (
        "https://raw.githubusercontent.com/anthropics/formal-math/main/vp-vnp/README.md",
        "text",
    )
    assert _candidate_fetch_urls("https://github.com/unslothai/unsloth/releases/tag/v0.1.924-beta")[0] == (
        "https://api.github.com/repos/unslothai/unsloth/releases/tags/v0.1.924-beta",
        "github_release",
    )
    assert _candidate_fetch_urls("https://huggingface.co/llamaindex/OCRia-1")[0] == (
        "https://huggingface.co/llamaindex/OCRia-1/raw/main/README.md",
        "text",
    )
    assert _candidate_fetch_urls("https://arxiv.org/abs/2611.32884") == [
        ("https://arxiv.org/html/2611.32884", "html"),
        ("https://arxiv.org/abs/2611.32884", "html"),
    ]


def test_fetch_source_text_strips_html_detects_bot_walls_and_handles_pdf() -> None:
    html_doc = (
        "<html><head><style>.x{color:red}</style><script>alert(1)</script></head>"
        "<body><article><h1>Benchmark Release</h1><table><tr><th>Model</th><th>Score</th></tr>"
        "<tr><td>OCRia-1</td><td>85.2</td></tr></table>"
        "<p>Fine-tuned from Qwen3.5-4B with single-pass document parsing across tables, charts, and equations.</p>"
        "</article></body></html>"
    )
    assert "alert(1)" not in extract_text_from_html(html_doc)
    assert "OCRia-1 | 85.2" in extract_text_from_html(html_doc)

    fetched = fetch_source_text(
        "https://example.org/post",
        opener=lambda req, timeout: _FakeHTTPResponse(html_doc, content_type="text/html; charset=utf-8"),
    )
    assert fetched.status == "fetched"
    assert "85.2" in fetched.text

    blocked = fetch_source_text(
        "https://example.org/cf",
        opener=lambda req, timeout: _FakeHTTPResponse(
            "<html><title>Just a moment...</title><p>Enable JavaScript and cookies to continue</p></html>",
            content_type="text/html",
        ),
    )
    assert blocked.status == "blocked"

    pdf = fetch_source_text(
        "https://example.org/paper.pdf",
        opener=lambda req, timeout: _FakeHTTPResponse(b"%PDF-1.7 binary", content_type="application/pdf"),
    )
    assert pdf.status == "unreadable_binary"

    def http_403(req, timeout):
        raise HTTPError(req.full_url, 403, "Forbidden", hdrs=None, fp=BytesIO(b""))

    forbidden = fetch_source_text("https://example.org/private", opener=http_403)
    assert forbidden.status == "blocked"
    assert forbidden.error == "HTTP 403"


def test_parse_brief_items_supports_markdown_and_review_json(tmp_path: Path) -> None:
    md = tmp_path / "2026-11-25.md"
    md.write_text(
        "# AI Builder Brief — November 25, 2026\n\n"
        "## Top 10\n\n"
        "1. **Item One ships v1.2** — **November 24, 2026**. Ships 50% faster decode. [Post](https://example.org/1).\n\n"
        "## Watchlist\n\n"
        "- **Watch Item:** Early preview. [Repo](https://github.com/org/repo).\n",
        encoding="utf-8",
    )
    top_only = parse_brief_items(md)
    assert [i.item_id for i in top_only] == ["top-01"]
    assert top_only[0].source_urls == ("https://example.org/1",)

    with_wl = parse_brief_items(md, include_watchlist=True)
    assert [i.item_id for i in with_wl] == ["top-01", "watchlist-01"]

    review_json = tmp_path / "2026-11-25.json"
    review_json.write_text(
        json.dumps({
            "candidates": [
                {
                    "rank": 1,
                    "title": "Review Candidate",
                    "summary": "Ships v2.0 API.",
                    "editorial": {"why_now": "GA today", "rationale": "Cuts latency", "caveats": "US only"},
                    "sources": [{"url": "https://example.org/review"}],
                }
            ],
            "watchlist": [],
        }),
        encoding="utf-8",
    )
    review_items = parse_brief_items(review_json)
    assert len(review_items) == 1
    assert review_items[0].source_urls == ("https://example.org/review",)
    assert "US only" in review_items[0].claim_text


def test_factcheck_brief_gate_and_cli_exit_codes(tmp_path: Path, monkeypatch, capsys) -> None:
    brief = tmp_path / "2026-11-25.md"
    brief.write_text(
        "# AI Builder Brief — November 25, 2026\n\n"
        "## Top 10\n\n"
        "1. **Supported item** — **November 24, 2026**. Ships v2.0 with 10x speedup. [Post](https://example.org/ok).\n\n"
        "2. **Drift item** — **November 24, 2026**. Cuts latency 50% everywhere. [Post](https://example.org/drift).\n\n"
        "3. **Unfetched item** — **November 24, 2026**. Private page. [Post](https://example.org/blocked).\n",
        encoding="utf-8",
    )

    def fake_fetch(url: str) -> FetchedSource:
        if url.endswith("/blocked"):
            return FetchedSource(url=url, fetched_url=url, status="blocked", error="HTTP 403")
        if url.endswith("/drift"):
            return FetchedSource(
                url=url,
                fetched_url=url,
                status="fetched",
                text="Cuts latency 50% on H100 batch size 8 only, not batch size 1. " * 4,
            )
        return FetchedSource(
            url=url,
            fetched_url=url,
            status="fetched",
            text="Ships v2.0 with 10x speedup across supported kernels. " * 4,
        )

    def fake_llm_opener(request, timeout):
        req_body = json.loads(request.data.decode("utf-8"))
        assert req_body["text"]["verbosity"] == "low"
        prompt_text = req_body["input"][0]["content"]
        item_id = prompt_text.splitlines()[0].split(":", 1)[1].strip()
        if item_id == "top-01":
            payload = {
                "item_id": "top-01",
                "claims": [{
                    "claim": "Ships v2.0 with 10x speedup",
                    "verdict": "supported",
                    "source_url": "https://example.org/ok",
                    "evidence_quote": "Ships v2.0 with 10x speedup",
                    "reason": "Exact match.",
                }],
            }
        else:
            payload = {
                "item_id": "top-02",
                "claims": [{
                    "claim": "Cuts latency 50% everywhere",
                    "verdict": "nuance_drift",
                    "source_url": "https://example.org/drift",
                    "evidence_quote": "Cuts latency 50% on H100 batch size 8 only",
                    "reason": "Source restricts 50% gain to H100 batch size 8.",
                }],
            }
        return _FakeHTTPResponse(_responses_envelope(payload))

    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    report = factcheck_brief(brief, fetcher=fake_fetch, opener=fake_llm_opener, max_workers=1)
    assert report.summary == {
        "items_total": 3,
        "items_supported": 1,
        "items_nuance_drift": 1,
        "items_unsupported": 0,
        "items_unfetched": 1,
        "claims_total": 2,
        "claims_supported": 1,
        "claims_nuance_drift": 1,
        "claims_unsupported": 0,
        "quotes_verified": 2,
        "quotes_total": 2,
    }
    assert should_fail_gate(report, fail_on="unsupported") is False
    assert should_fail_gate(report, fail_on="nuance_drift") is True
    assert should_fail_gate(report, fail_on="none") is False

    md_out = render_factcheck_markdown(report)
    assert "## [SUPPORTED] top-01" in md_out
    assert "## [NUANCE_DRIFT] top-02" in md_out
    assert "## [UNFETCHED] top-03" in md_out
    assert "verified-quote" in md_out

    monkeypatch.setattr(cli, "factcheck_brief", lambda *a, **kw: report)
    assert cli.main(["factcheck", "--brief", str(brief), "--fail-on", "unsupported", "--json"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["summary"]["claims_nuance_drift"] == 1
    assert cli.main(["factcheck", "--brief", str(brief), "--fail-on", "nuance_drift"]) == 1


def test_rollup_and_empty_report_gate() -> None:
    empty = FactCheckReport(target="x.md", model=DEFAULT_FACTCHECK_MODEL, items=())
    assert should_fail_gate(empty, fail_on="unsupported") is False
    assert rollup_item_verdict((), has_fetched_source=False) == "unfetched"
    assert rollup_item_verdict((), has_fetched_source=True) == "supported"
    drift_claim = ClaimVerdict("c", "nuance_drift", "https://e.org", "q", "r", True)
    unsup_claim = ClaimVerdict("c2", "unsupported", "", "", "missing", False)
    assert rollup_item_verdict([drift_claim], has_fetched_source=True) == "nuance_drift"
    assert rollup_item_verdict([drift_claim, unsup_claim], has_fetched_source=True) == "unsupported"
    item = ItemFactCheck("top-01", "top", 1, "H", "unsupported", (), (unsup_claim,))
    assert item.to_dict()["claim_counts"]["unsupported"] == 1


def test_select_source_excerpt_preserves_opening_and_claim_anchored_paragraphs() -> None:
    filler = "\n\n".join(f"Background paragraph {i} about unrelated history and setup." * 6 for i in range(40))
    needle = "ParseBench 2 reaches 85.2 and OpenRAIL-M Attachment A caps commercial revenue at $2M."
    doc = "Title: OCRia-1 release overview.\n\n" + filler + "\n\n" + needle + "\n\n" + filler
    excerpt = factcheck._select_source_excerpt(doc, "OCRia-1 hits 85.2 on ParseBench 2 with a $2M revenue cap.", 2000)
    assert len(excerpt) <= 2000
    assert "Title: OCRia-1 release overview." in excerpt
    assert "85.2" in excerpt
    assert "$2M" in excerpt
