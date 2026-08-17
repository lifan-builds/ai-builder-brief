"""Localhost-only CLIProxyAPI client for strict editorial review."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import shlex
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


EDITORIAL_BATCH_SIZE = 6
EDITORIAL_FAILURE_TYPES = frozenset({"usage_gate", "timeout", "proxy", "invalid_response"})


class EditorialReviewError(RuntimeError):
    """A safe, machine-readable editorial review failure.

    The underlying proxy response and exception are deliberately not retained in
    the public message.  This exception is used at the pipeline boundary where a
    failure is written to the unpublished ledger, so only its controlled fields
    can cross that boundary.
    """

    def __init__(
        self,
        category: str,
        *,
        stage: str,
        batch_index: int | None = None,
    ) -> None:
        if category not in EDITORIAL_FAILURE_TYPES:
            category = "proxy"
        self.category = category
        # ``failure_type`` is the ledger-facing name and keeps callers from
        # having to know whether the implementation calls this a category.
        self.failure_type = category
        self.stage = stage
        self.batch_index = batch_index
        message = f"editorial review {category} at {stage}"
        if batch_index is not None:
            message += f" (batch {batch_index})"
        super().__init__(message)

    def with_batch(self, batch_index: int) -> "EditorialReviewError":
        """Return the same safe failure annotated with a 1-based batch index."""

        return EditorialReviewError(
            self.category,
            stage=self.stage,
            batch_index=batch_index,
        )

    def to_metadata(self) -> dict[str, str | int]:
        metadata: dict[str, str | int] = {
            "failure_type": self.failure_type,
            "stage": self.stage,
        }
        if self.batch_index is not None:
            metadata["batch_index"] = self.batch_index
        return metadata


def _is_timeout(error: BaseException) -> bool:
    if isinstance(error, (TimeoutError, socket.timeout)):
        return True
    if isinstance(error, URLError):
        reason = error.reason
        if isinstance(reason, (TimeoutError, socket.timeout)):
            return True
    # urllib and local proxy implementations do not all use the same timeout
    # exception type.  This check is only for classification; the message is
    # never copied into a ledger or user-facing exception.
    return "timed out" in str(error).casefold() or "timeout" in str(error).casefold()


def _failure_from_exception(error: BaseException, *, stage: str) -> EditorialReviewError:
    if isinstance(error, EditorialReviewError):
        return error
    if _is_timeout(error):
        return EditorialReviewError("timeout", stage=stage)
    if isinstance(error, (ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError)):
        return EditorialReviewError("invalid_response", stage=stage)
    return EditorialReviewError("proxy", stage=stage)


EDITORIAL_SCHEMA: dict[str, Any] = {
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
        raise EditorialReviewError("usage_gate", stage="usage_gate")
    try:
        argv = shlex.split(command)
    except ValueError:
        raise EditorialReviewError("usage_gate", stage="usage_gate") from None
    if not argv:
        raise EditorialReviewError("usage_gate", stage="usage_gate")
    try:
        result = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=30)
    except Exception:
        raise EditorialReviewError("usage_gate", stage="usage_gate") from None
    if result.returncode != 0:
        raise EditorialReviewError("usage_gate", stage="usage_gate")


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


def _validate_response_shape(payload: object) -> dict:
    """Validate the portion of the strict schema needed before merging batches."""

    if not isinstance(payload, dict) or set(payload) != {"decisions"}:
        raise EditorialReviewError("invalid_response", stage="editorial_response")
    raw_decisions = payload.get("decisions")
    if not isinstance(raw_decisions, list):
        raise EditorialReviewError("invalid_response", stage="editorial_response")
    required = set(EDITORIAL_SCHEMA["properties"]["decisions"]["items"]["required"])
    properties = set(EDITORIAL_SCHEMA["properties"]["decisions"]["items"]["properties"])
    for decision in raw_decisions:
        if not isinstance(decision, dict) or set(decision) != properties or not required.issubset(decision):
            raise EditorialReviewError("invalid_response", stage="editorial_response")
        if not isinstance(decision["cluster_id"], str):
            raise EditorialReviewError("invalid_response", stage="editorial_response")
        if not isinstance(decision["decision"], str) or decision["decision"] not in {"accept", "reject"}:
            raise EditorialReviewError("invalid_response", stage="editorial_response")
        for field in ("impact", "actionability", "novelty", "evidence", "audience_breadth"):
            value = decision[field]
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 4:
                raise EditorialReviewError("invalid_response", stage="editorial_response")
        actions = decision["builder_actions"]
        if not isinstance(actions, list) or any(
            not isinstance(action, str)
            or action not in {"use", "build", "test", "monitor", "reconsider"}
            for action in actions
        ):
            raise EditorialReviewError("invalid_response", stage="editorial_response")
        if any(not isinstance(decision[field], str) for field in ("why_now", "rationale", "caveats")):
            raise EditorialReviewError("invalid_response", stage="editorial_response")
        if not isinstance(decision["depth_recommendation"], str) or decision["depth_recommendation"] not in {"brief", "deep"}:
            raise EditorialReviewError("invalid_response", stage="editorial_response")
        source_ids = decision["source_ids"]
        if not isinstance(source_ids, list) or any(not isinstance(source_id, str) for source_id in source_ids):
            raise EditorialReviewError("invalid_response", stage="editorial_response")
    return payload


def review_candidates(candidates: list[dict], *, opener=urlopen) -> dict:
    """Review one bounded packet with the strict local proxy contract."""

    try:
        _usage_gate()
    except EditorialReviewError:
        raise
    except Exception as error:
        raise _failure_from_exception(error, stage="usage_gate") from None
    base = os.environ.get("CLIPROXYAPI_URL", "http://127.0.0.1:8317").rstrip("/")
    try:
        parsed_base = urlsplit(base)
    except ValueError:
        raise EditorialReviewError("proxy", stage="proxy_config") from None
    if parsed_base.scheme != "http" or parsed_base.hostname not in {"127.0.0.1", "localhost"}:
        raise EditorialReviewError("proxy", stage="proxy_config")
    model = os.environ.get("EDITORIAL_MODEL", "gpt-5.6-terra").strip() or "gpt-5.6-terra"
    try:
        key = _client_key()
    except EditorialReviewError:
        raise
    except Exception:
        raise EditorialReviewError("proxy", stage="proxy_config") from None
    try:
        payload = {
            "model": model, "temperature": 0, "response_format": {"type": "json_schema", "json_schema": {"name": "editorial_review", "strict": True, "schema": EDITORIAL_SCHEMA}},
            "messages": [
                {"role": "system", "content": "You are choosing scarce space in a daily briefing for AI builders. Judge whether each candidate is consequential enough to displace other news: it should materially change a meaningful builder decision, affect a broad or strategically important audience, or reveal an important provider/community shift. A long changelog, many supported models, or relevance to existing users of one runtime is not broad impact by itself. Routine versions, compatibility work, patches, release candidates, and ordinary paper flow should score low unless they overturn a wider assumption. Research must be unusually well-supported and practically consequential. Treat X, Hacker News, Hugging Face, and GitHub activity as attention and momentum, never proof of technical claims. Expert posts may support attributed observations; technical claims still require an attached primary artifact or two independent credible reports. Apply these principles generally, without favoring named vendors. Return strict JSON matching the schema."},
                {"role": "user", "content": json.dumps(candidates, ensure_ascii=False, sort_keys=True)},
            ],
        }
        request = Request(f"{base}/v1/chat/completions", data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"}, method="POST")
    except Exception as error:
        raise _failure_from_exception(error, stage="editorial_request") from None
    try:
        with opener(request, timeout=60) as response:
            body = response.read().decode("utf-8")
    except Exception as error:
        raise _failure_from_exception(error, stage="editorial_request") from None
    try:
        raw = json.loads(body)
    except Exception:
        raise EditorialReviewError("invalid_response", stage="editorial_response") from None
    try:
        content = raw["choices"][0]["message"]["content"]
        parsed = json.loads(content) if isinstance(content, str) else content
    except Exception:
        raise EditorialReviewError("invalid_response", stage="editorial_response") from None
    return _validate_response_shape(parsed)


def _validate_batch_coverage(payload: dict, expected_ids: list[str]) -> dict:
    """Reject omissions, duplicates, and unknown candidates before merging."""

    expected = set(expected_ids)
    raw_decisions = payload["decisions"]
    actual = [str(item["cluster_id"]) for item in raw_decisions]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise EditorialReviewError("invalid_response", stage="editorial_coverage")
    by_id = {str(item["cluster_id"]): item for item in raw_decisions}
    return {"decisions": [by_id[cluster_id] for cluster_id in expected_ids]}


def review_candidates_batched(
    candidates: list[dict],
    *,
    batch_size: int = EDITORIAL_BATCH_SIZE,
    opener=None,
) -> dict:
    """Review a packet in deterministic bounded batches and merge in input order."""

    if batch_size < 1:
        raise ValueError("editorial batch size must be positive")
    packets = list(candidates)
    candidate_ids: list[str] = []
    for candidate in packets:
        if not isinstance(candidate, dict) or not str(candidate.get("cluster_id", "")).strip():
            raise EditorialReviewError("invalid_response", stage="editorial_input")
        candidate_ids.append(str(candidate["cluster_id"]))
    if len(candidate_ids) != len(set(candidate_ids)):
        raise EditorialReviewError("invalid_response", stage="editorial_input")

    merged: list[dict] = []
    for start in range(0, len(packets), batch_size):
        batch_index = (start // batch_size) + 1
        batch = packets[start : start + batch_size]
        expected_ids = candidate_ids[start : start + batch_size]
        try:
            if opener is None:
                response = review_candidates(batch)
            else:
                response = review_candidates(batch, opener=opener)
            merged.extend(_validate_batch_coverage(response, expected_ids)["decisions"])
        except EditorialReviewError as error:
            raise error.with_batch(batch_index) from None
        except Exception as error:
            raise _failure_from_exception(error, stage="editorial_batch").with_batch(batch_index) from None
    return {"decisions": merged}
