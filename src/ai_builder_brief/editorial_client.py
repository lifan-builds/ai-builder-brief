"""Localhost-only CLIProxyAPI client for strict editorial review."""

from __future__ import annotations

import json
import os
import subprocess
import shlex
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen


EDITORIAL_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["decisions"],
    "properties": {"decisions": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["cluster_id", "decision", "impact", "actionability", "novelty", "evidence", "audience_breadth", "builder_actions", "why_now", "rationale", "caveats", "depth_recommendation", "source_ids"],
        "properties": {
            "cluster_id": {"type": "string"}, "decision": {"enum": ["accept", "reject"]},
            **{name: {"type": "integer", "minimum": 0, "maximum": 4} for name in ("impact", "actionability", "novelty", "evidence", "audience_breadth")},
            "builder_actions": {"type": "array", "items": {"enum": ["use", "build", "test", "monitor", "reconsider"]}},
            "why_now": {"type": "string"}, "rationale": {"type": "string"}, "caveats": {"type": "string"},
            "depth_recommendation": {"enum": ["brief", "deep"]}, "source_ids": {"type": "array", "items": {"type": "string"}},
        },
    }}}
}


def _usage_gate() -> None:
    command = os.environ.get("CLIPROXYAPI_USAGE_GATE_COMMAND", "").strip()
    if not command:
        raise RuntimeError("CLIProxyAPI usage-gate command is required")
    try:
        argv = shlex.split(command)
    except ValueError as error:
        raise RuntimeError("invalid CLIProxyAPI usage-gate command") from error
    if not argv:
        raise RuntimeError("CLIProxyAPI usage-gate command is required")
    result = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError("CLIProxyAPI usage gate is closed")


def _client_key() -> str:
    key = os.environ.get("CLIPROXYAPI_API_KEY", "").strip()
    if key:
        return key
    config = Path(os.environ.get("CLIPROXYAPI_CONFIG", Path.home() / ".cli-proxy-api" / "config.yaml"))
    if not config.is_file():
        raise RuntimeError("CLIProxyAPI client key is required")
    import yaml

    raw = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    keys = raw.get("api-keys") if isinstance(raw, dict) else None
    if not isinstance(keys, list) or not keys or not str(keys[0]).strip():
        raise RuntimeError("CLIProxyAPI client key is required")
    return str(keys[0]).strip()


def review_candidates(candidates: list[dict], *, opener=urlopen) -> dict:
    _usage_gate()
    base = os.environ.get("CLIPROXYAPI_URL", "http://127.0.0.1:8317").rstrip("/")
    if not base.startswith(("http://127.0.0.1", "http://localhost")):
        raise RuntimeError("CLIPROXYAPI_URL must point to localhost")
    model = os.environ.get("EDITORIAL_MODEL", "gpt-5.6-terra").strip() or "gpt-5.6-terra"
    key = _client_key()
    payload = {
        "model": model, "temperature": 0, "response_format": {"type": "json_schema", "json_schema": {"name": "editorial_review", "strict": True, "schema": EDITORIAL_SCHEMA}},
        "messages": [
            {"role": "system", "content": "Review candidates for developments that change what AI builders should use, build, test, monitor, or reconsider. Expert posts may qualify as attributed analysis, but any technical claim must be backed by an attached primary artifact or two independent credible reports. Reject filler, routine releases, cloud availability wrappers, roundups, weak model cards, and research without a practical changed assumption. Return strict JSON matching the schema."},
            {"role": "user", "content": json.dumps(candidates, ensure_ascii=False, sort_keys=True)},
        ],
    }
    request = Request(f"{base}/v1/chat/completions", data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"}, method="POST")
    try:
        with opener(request, timeout=60) as response:
            raw = json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, ValueError, KeyError) as error:
        raise RuntimeError("CLIProxyAPI editorial review failed") from error
    try:
        content = raw["choices"][0]["message"]["content"]
        return json.loads(content) if isinstance(content, str) else content
    except (KeyError, IndexError, TypeError, ValueError) as error:
        raise ValueError("CLIProxyAPI returned invalid editorial JSON") from error
