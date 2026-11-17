"""Loopback OpenAI-compatible client for strict editorial review."""

from __future__ import annotations

import json
import os
import socket
from typing import Any
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

DEFAULT_BASE_URL = "http://127.0.0.1:18455/v1"
DEFAULT_API_KEY = "quotaswitch-local"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
BUILDER_ACTIONS = frozenset({"use", "build", "test", "monitor", "reconsider"})
EDITORIAL_BATCH_SIZE = 6
EDITORIAL_FAILURE_TYPES = frozenset({"timeout", "proxy", "invalid_response"})


class EditorialReviewError(RuntimeError):
    """A safe, machine-readable editorial review failure.

    Underlying proxy responses and raw exceptions are omitted from the message
    so only controlled fields cross into the unpublished ledger.
    """

    def __init__(
        self,
        category: str,
        *,
        stage: str,
        batch_index: int | None = None,
    ) -> None:
        self.category = category if category in EDITORIAL_FAILURE_TYPES else "proxy"
        self.stage = stage
        self.batch_index = batch_index
        message = f"editorial review {self.category} at {stage}"
        if batch_index is not None:
            message += f" (batch {batch_index})"
        super().__init__(message)

    def with_batch(self, batch_index: int) -> "EditorialReviewError":
        return EditorialReviewError(self.category, stage=self.stage, batch_index=batch_index)

    def to_metadata(self) -> dict[str, str | int]:
        metadata: dict[str, str | int] = {"failure_type": self.category, "stage": self.stage}
        if self.batch_index is not None:
            metadata["batch_index"] = self.batch_index
        return metadata


def _is_timeout(error: BaseException) -> bool:
    if isinstance(error, (TimeoutError, socket.timeout)):
        return True
    if isinstance(error, URLError) and isinstance(error.reason, (TimeoutError, socket.timeout)):
        return True
    text = str(error).casefold()
    return "timed out" in text or "timeout" in text


def _failure_from_exception(error: BaseException, *, stage: str) -> EditorialReviewError:
    if isinstance(error, EditorialReviewError):
        return error
    if _is_timeout(error):
        return EditorialReviewError("timeout", stage=stage)
    if isinstance(error, (ValueError, TypeError, KeyError, UnicodeError, json.JSONDecodeError)):
        return EditorialReviewError("invalid_response", stage=stage)
    return EditorialReviewError("proxy", stage=stage)


EDITORIAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["decisions"],
    "properties": {
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "cluster_id",
                    "decision",
                    "impact",
                    "actionability",
                    "novelty",
                    "evidence",
                    "audience_breadth",
                    "builder_actions",
                    "why_now",
                    "rationale",
                    "caveats",
                    "depth_recommendation",
                    "source_ids",
                ],
                "properties": {
                    "cluster_id": {"type": "string"},
                    "decision": {"enum": ["accept", "reject"]},
                    **{
                        name: {"type": "integer", "minimum": 0, "maximum": 4}
                        for name in ("impact", "actionability", "novelty", "evidence", "audience_breadth")
                    },
                    "builder_actions": {"type": "array", "items": {"enum": sorted(BUILDER_ACTIONS)}},
                    "why_now": {"type": "string"},
                    "rationale": {"type": "string"},
                    "caveats": {"type": "string"},
                    "depth_recommendation": {"enum": ["brief", "deep"]},
                    "source_ids": {"type": "array", "items": {"type": "string"}},
                },
            },
        }
    },
}


def _proxy_config() -> tuple[str, str]:
    base = os.environ.get("OPENAI_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in LOOPBACK_HOSTS:
        raise EditorialReviewError("proxy", stage="proxy_config")
    if not base.endswith("/v1"):
        base = f"{base}/v1"
    key = os.environ.get("OPENAI_API_KEY", DEFAULT_API_KEY).strip()
    if not key or key.startswith("sk-"):
        raise EditorialReviewError("proxy", stage="proxy_config")
    return base, key


def _validate_response_shape(payload: object) -> dict:
    """Validate the strict response schema before merging batches."""
    if not isinstance(payload, dict) or set(payload) != {"decisions"} or not isinstance(payload["decisions"], list):
        raise EditorialReviewError("invalid_response", stage="editorial_response")
    properties = set(EDITORIAL_SCHEMA["properties"]["decisions"]["items"]["properties"])
    for decision in payload["decisions"]:
        if not isinstance(decision, dict) or set(decision) != properties:
            raise EditorialReviewError("invalid_response", stage="editorial_response")
        if (
            not isinstance(decision["cluster_id"], str)
            or decision["decision"] not in {"accept", "reject"}
            or decision["depth_recommendation"] not in {"brief", "deep"}
            or any(not isinstance(decision[field], str) for field in ("why_now", "rationale", "caveats"))
        ):
            raise EditorialReviewError("invalid_response", stage="editorial_response")
        for field in ("impact", "actionability", "novelty", "evidence", "audience_breadth"):
            value = decision[field]
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 4:
                raise EditorialReviewError("invalid_response", stage="editorial_response")
        actions = decision["builder_actions"]
        sources = decision["source_ids"]
        if (
            not isinstance(actions, list)
            or any(not isinstance(a, str) or a not in BUILDER_ACTIONS for a in actions)
            or not isinstance(sources, list)
            or any(not isinstance(s, str) for s in sources)
        ):
            raise EditorialReviewError("invalid_response", stage="editorial_response")
    return payload


SYSTEM_PROMPT = (
    "You are choosing scarce space in a daily briefing for AI builders. Select only developments "
    "that are newly useful to know: a genuinely new shipped artifact, API/SDK capability, open-weight "
    "checkpoint, security vulnerability, engineering postmortem, or reproducible evaluation that changes "
    "a concrete builder decision. Reject corporate fundraising rounds, valuations, IPO/S-1 filings, "
    "executive appointments, government MOUs, customer case-study testimonials, cloud-marketplace "
    "reselling, consumer shopping features, and waitlist-only vapourware lacking runnable code, weights, "
    "APIs, or published evaluations. A new article, repost, or refreshed summary about an already-covered "
    "event is not new unless availability, pricing, weights, or technical evidence materially changed. "
    "Routine minor versions, compatibility patches, release candidates, and ordinary preprints should "
    "score low unless they overturn a wider engineering assumption. State the concrete builder action "
    "each accepted story enables and state specific methodological or deployment caveats without generic "
    "boilerplate. Treat X, Hacker News, Hugging Face, and GitHub activity as attention and momentum, "
    "never standalone proof of technical claims. Apply these principles impartially across all vendors "
    "and open-source maintainers. Return strict JSON matching the schema."
)


def review_candidates(candidates: list[dict], *, opener=urlopen) -> dict:
    """Review one bounded packet via the loopback OpenAI-compatible gateway."""
    base, key = _proxy_config()
    model = os.environ.get("EDITORIAL_MODEL", "gpt-6-luna").strip() or "gpt-6-luna"
    try:
        payload = {
            "model": model,
            "temperature": 0,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "editorial_review", "strict": True, "schema": EDITORIAL_SCHEMA},
            },
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(candidates, ensure_ascii=False, sort_keys=True)},
            ],
        }
        request = Request(
            f"{base}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
            method="POST",
        )
    except Exception as error:
        raise _failure_from_exception(error, stage="editorial_request") from None
    try:
        with opener(request, timeout=60) as response:
            body = response.read().decode("utf-8")
    except Exception as error:
        raise _failure_from_exception(error, stage="editorial_request") from None
    try:
        raw = json.loads(body)
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
            response = review_candidates(batch) if opener is None else review_candidates(batch, opener=opener)
            merged.extend(_validate_batch_coverage(response, expected_ids)["decisions"])
        except EditorialReviewError as error:
            raise error.with_batch(batch_index) from None
        except Exception as error:
            raise _failure_from_exception(error, stage="editorial_batch").with_batch(batch_index) from None
    return {"decisions": merged}
