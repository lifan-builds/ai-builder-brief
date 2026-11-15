from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path
import struct
import sys
import pytest

_SCRIPT = Path(__file__).parents[1] / "scripts" / "generate_media_pack.py"
_SPEC = importlib.util.spec_from_file_location("generate_media_pack", _SCRIPT)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules["generate_media_pack"] = _MODULE
_SPEC.loader.exec_module(_MODULE)


def _fake_png(width: int, height: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + ihdr + b"\x00\x00\x00\x00"


def test_validate_proxy_config_enforces_loopback_and_blocks_sk_keys() -> None:
    assert (
        _MODULE.validate_proxy_config("http://127.0.0.1:18455/v1/", "quotaswitch-local")
        == "http://127.0.0.1:18455/v1"
    )
    with pytest.raises(ValueError, match="non-loopback"):
        _MODULE.validate_proxy_config("https://api.openai.com/v1", "quotaswitch-local")
    with pytest.raises(ValueError, match="Refusing raw sk-"):
        _MODULE.validate_proxy_config("http://127.0.0.1:18455/v1", "sk-test-placeholder")


def test_redact_secrets_scrubs_sk_tokens() -> None:
    assert _MODULE.redact_secrets("bad token sk-test-abc123XYZ") == "bad token [REDACTED_KEY]"


def test_parse_issue_markdown_extracts_ranked_stories_and_thesis(tmp_path: Path) -> None:
    sample = tmp_path / "2026-11-14.md"
    sample.write_text(
        "# AI Builder Brief — November 14, 2026\n\n"
        "Coverage window: Nov 11–Nov 14 UTC.\n\n"
        "Read on the day: Eval boundaries and explicit control flow matter.\n\n"
        "## Top 10\n\n"
        "1. **Anthropic shows why safety scores change** — **November 13, 2026 analysis**. "
        "API vs product setups differed by 34 percentage points. [Guide](https://example.com/1).\n\n"
        "2. **Singularity-Bench tests AI engineering** — **November 12, 2026 paper**. "
        "Breakwater isolates verification phases. [Paper](https://example.com/2).\n\n"
        "## Watchlist\n\n"
        "- **FBI advisory — November 13:** consolidated guidance. [PDF](https://example.com/w).\n",
        encoding="utf-8",
    )
    parsed = _MODULE.parse_issue_markdown(sample)
    assert parsed.slug == "2026-11-14"
    assert parsed.title == "AI Builder Brief — November 14, 2026"
    assert "Eval boundaries" in parsed.read_on_the_day
    assert len(parsed.items) == 2
    assert parsed.items[0].rank == 1
    assert parsed.items[0].links == ["https://example.com/1"]
    assert "Guide" in parsed.items[0].summary
    assert "https://example.com/1" not in parsed.items[0].summary

    tasks = _MODULE.build_issue_image_tasks(parsed)
    assert [t.filename for t in tasks] == [
        "hero-landscape.png",
        "xhs-card-1-cover.png",
        "xhs-card-2-insight.png",
        "xhs-card-3-takeaway.png",
    ]
    assert tasks[0].size == "1536x1024"
    assert all(t.size == "1024x1536" for t in tasks[1:])


def test_generate_image_decodes_b64_and_records_quotaswitch_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    png_bytes = _fake_png(1536, 1024)

    def fake_post(**kwargs):
        assert kwargs["endpoint"] == "images/generations"
        return _MODULE.ProxyResponse(
            status=200,
            body=json.dumps({"data": [{"b64_json": base64.b64encode(png_bytes).decode()}]}).encode(),
            content_type="application/json",
            quotaswitch_account_id="api-acct-test",
            quotaswitch_provider="openai-api-key",
            request_id="req_123",
        )

    monkeypatch.setattr(_MODULE, "post_proxy_json", fake_post)
    task = _MODULE.ImageTask(
        name="hero",
        filename="hero.png",
        prompt="Test editorial illustration",
        size="1536x1024",
    )
    meta = _MODULE.generate_image(task, output_dir=tmp_path)
    assert meta["width"] == 1536
    assert meta["height"] == 1024
    assert meta["quotaswitch_account_id"] == "api-acct-test"
    assert (tmp_path / "hero.png").read_bytes() == png_bytes


def test_load_manifest_tasks_parses_json_manifest(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "images": [
                    {"name": "b1", "filename": "b1.png", "prompt": "Banner 1", "size": "1536x1024"}
                ]
            }
        ),
        encoding="utf-8",
    )
    tasks, _ = _MODULE.load_manifest_tasks(manifest)
    assert len(tasks) == 1
    assert tasks[0].filename == "b1.png"
    assert tasks[0].model == _MODULE.DEFAULT_IMAGE_MODEL
