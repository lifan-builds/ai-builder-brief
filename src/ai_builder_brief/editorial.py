"""Deterministic editorial preprocessing, review contracts, and selection."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from castforge.models import SourceItem, StoryCluster

ROUTINE_TERMS = {
    "chore", "ci", "continuous integration", "bump version", "dependency update",
    "release candidate", "rc.", "roundup", "weekly roundup", "cloud wrapper",
    "marketing", "sponsored", "launch recap", "minor fix", "documentation only",
}
LOW_VALUE_PATTERNS = {
    "cloud availability without a new builder capability": (
        "available on aws", "available in aws", "available on azure", "available in azure",
        "available on google cloud", "available in google cloud",
    ),
    "narrow autonomous-driving research": (
        "autonomous driving", "vehicle-to-vehicle driving", "cooperative driving",
    ),
}
PRACTICAL_TERMS = {
    "api", "sdk", "inference", "weights", "model", "benchmark", "security",
    "latency", "context", "training", "open source", "repository", "agent",
    "developer", "migration", "license", "available", "research",
}
BUILDER_ACTIONS = frozenset({"use", "build", "test", "monitor", "reconsider"})
EDITORIAL_FAILURE_TYPES = frozenset({"usage_gate", "timeout", "proxy", "invalid_response"})
EDITORIAL_FAILURE_STAGES = frozenset({
    "usage_gate", "proxy_config", "editorial_request", "editorial_response",
    "editorial_coverage", "editorial_input", "editorial_batch", "editorial_validation",
})


@dataclass(frozen=True, slots=True)
class EditorialDecision:
    """Strict review output for one candidate cluster."""

    cluster_id: str
    decision: str
    impact: int
    actionability: int
    novelty: int
    evidence: int
    audience_breadth: int
    builder_actions: tuple[str, ...] = ()
    why_now: str = ""
    rationale: str = ""
    caveats: str = ""
    depth_recommendation: str = "brief"
    source_ids: tuple[str, ...] = ()
    momentum: int = 0
    recency: int = 0

    def __post_init__(self) -> None:
        if self.decision not in {"accept", "reject"}:
            raise ValueError("decision must be accept or reject")
        for name in ("impact", "actionability", "novelty", "evidence", "audience_breadth", "momentum", "recency"):
            value = int(getattr(self, name))
            if value < 0 or value > 4:
                raise ValueError(f"{name} must be an integer from 0 to 4")
        if any(action not in BUILDER_ACTIONS for action in self.builder_actions):
            raise ValueError("builder_actions contains an unsupported action")
        if self.depth_recommendation not in {"brief", "deep"}:
            raise ValueError("depth_recommendation must be brief or deep")

    @property
    def score(self) -> float:
        # Weights are the approved 35/20/15/10/10/5/5 rubric. Values are
        # normalized from the strict 0–4 review scale to 100 points.
        points = (
            self.impact * 35
            + self.actionability * 20
            + self.novelty * 15
            + self.evidence * 10
            + self.audience_breadth * 10
            + self.momentum * 5
            + self.recency * 5
        ) / 4
        return points

    def to_dict(self) -> dict[str, Any]:
        return {
            "cluster_id": self.cluster_id,
            "decision": self.decision,
            "impact": self.impact,
            "actionability": self.actionability,
            "novelty": self.novelty,
            "evidence": self.evidence,
            "audience_breadth": self.audience_breadth,
            "builder_actions": list(self.builder_actions),
            "why_now": self.why_now,
            "rationale": self.rationale,
            "caveats": self.caveats,
            "depth_recommendation": self.depth_recommendation,
            "source_ids": list(self.source_ids),
            "momentum": self.momentum,
            "recency": self.recency,
            "score": self.score,
        }


def _text(item: SourceItem) -> str:
    return f"{item.title} {item.summary}".casefold()


def reject_reason(item: SourceItem) -> str | None:
    text = _text(item)
    for term in ROUTINE_TERMS:
        if term in text:
            return f"routine or excluded pattern: {term}"
    for reason, patterns in LOW_VALUE_PATTERNS.items():
        if any(pattern in text for pattern in patterns):
            return reason
    if item.authority == "signal":
        return "signal-only evidence cannot qualify a story"
    if item.category == "research" and not any(term in text for term in PRACTICAL_TERMS):
        return "research lacks a practical builder implication"
    if "model card" in text and not any(term in text for term in ("weights", "inference", "license", "benchmark")):
        return "weak model card without actionable details"
    return None


def _recency_score(published_at: str, as_of: datetime | None) -> int:
    if as_of is None:
        return 0
    try:
        published = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if published.tzinfo is None:
        published = published.replace(tzinfo=UTC)
    age_hours = max(0.0, (as_of.astimezone(UTC) - published.astimezone(UTC)).total_seconds() / 3600)
    if age_hours <= 24:
        return 4
    if age_hours <= 48:
        return 3
    if age_hours <= 96:
        return 2
    if age_hours <= 168:
        return 1
    return 0


def _momentum_score(item: SourceItem) -> int:
    raw = item.metadata.get("momentum_score", item.metadata.get("momentum", 0))
    if isinstance(raw, bool):
        return 2 if raw else 0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0
    # Collectors may provide either the normalized 0–4 value or a raw signal.
    if value <= 4:
        return max(0, min(4, int(round(value))))
    return max(0, min(4, int(round(value / 25))))


def preprocess(
    items: Iterable[SourceItem], *, limit: int = 24, as_of: datetime | None = None,
) -> tuple[list[SourceItem], list[dict[str, Any]]]:
    accepted: list[SourceItem] = []
    decisions: list[dict[str, Any]] = []
    grouped: dict[str, list[SourceItem]] = {}
    for item in items:
        grouped.setdefault(str(item.metadata.get("cluster_id") or item.id), []).append(item)
    ordered_groups = sorted(
        grouped.items(),
        key=lambda pair: (-max(float(value.metadata.get("score", 0)) for value in pair[1]), pair[0]),
    )
    for cluster_id, group in ordered_groups:
        kept: list[SourceItem] = []
        rejected_reasons: list[str] = []
        for item in sorted(group, key=lambda value: (-float(value.metadata.get("score", 0)), value.id)):
            reason = reject_reason(item)
            if reason:
                rejected_reasons.append(f"{item.id}: {reason}")
                continue
            metadata = dict(item.metadata)
            metadata.update({
                "preprocessed": True,
                "builder_impact": min(4, 1 + sum(1 for term in ("available", "api", "weights", "security", "inference") if term in _text(item))),
                "momentum_score": _momentum_score(item),
                "recency_score": _recency_score(item.published_at, as_of),
            })
            kept.append(replace(item, metadata=metadata))
        if not kept:
            decisions.append({"cluster_id": cluster_id, "decision": "reject", "rationale": "; ".join(rejected_reasons) or "deterministically rejected", "source_ids": [item.id for item in group]})
            continue
        accepted.extend(kept)
        decisions.append({"cluster_id": cluster_id, "decision": "candidate", "rationale": "passed deterministic preprocessing", "source_ids": [item.id for item in kept], "rejected_source_ids": [item.id for item in group if item not in kept]})
        if len({str(item.metadata.get("cluster_id") or item.id) for item in accepted}) >= limit:
            break
    return accepted, decisions


def validate_review(
    payload: Any,
    candidate_ids: set[str],
    candidate_metadata: dict[str, dict[str, Any]] | None = None,
) -> list[EditorialDecision]:
    if not isinstance(payload, dict) or not isinstance(payload.get("decisions"), list):
        raise ValueError("editorial response must be an object with decisions")
    decisions: list[EditorialDecision] = []
    seen: set[str] = set()
    required = {"cluster_id", "decision", "impact", "actionability", "novelty", "evidence", "audience_breadth", "builder_actions", "why_now", "rationale", "caveats", "depth_recommendation", "source_ids"}
    for raw in payload["decisions"]:
        if not isinstance(raw, dict) or set(raw) != required:
            raise ValueError("editorial decision is missing strict-schema fields")
        cluster_id = str(raw["cluster_id"])
        if cluster_id not in candidate_ids or cluster_id in seen:
            raise ValueError(f"editorial decision does not exactly cover candidates: {cluster_id}")
        seen.add(cluster_id)
        metadata = (candidate_metadata or {}).get(cluster_id, {})
        decisions.append(EditorialDecision(
            cluster_id=cluster_id,
            decision=str(raw["decision"]),
            impact=int(raw["impact"]), actionability=int(raw["actionability"]), novelty=int(raw["novelty"]),
            evidence=int(raw["evidence"]), audience_breadth=int(raw["audience_breadth"]),
            builder_actions=tuple(str(item) for item in raw["builder_actions"]),
            why_now=str(raw["why_now"]), rationale=str(raw["rationale"]), caveats=str(raw["caveats"]),
            depth_recommendation=str(raw["depth_recommendation"]), source_ids=tuple(str(item) for item in raw.get("source_ids", [])),
            momentum=int(metadata.get("momentum_score", 0)), recency=int(metadata.get("recency_score", 0)),
        ))
    if seen != candidate_ids:
        raise ValueError("editorial response must cover every candidate exactly once")
    return decisions


def select_clusters(clusters: Iterable[StoryCluster], decisions: Iterable[EditorialDecision], *, minimum: int = 3, maximum: int = 6) -> tuple[StoryCluster, ...]:
    by_id = {decision.cluster_id: decision for decision in decisions}
    ranked: list[tuple[float, StoryCluster, EditorialDecision]] = []
    for cluster in clusters:
        decision = by_id.get(cluster.id)
        if not decision or decision.decision != "accept" or decision.score < 70 or decision.impact < 3 or decision.evidence < 3:
            continue
        ranked.append((decision.score, cluster, decision))
    selected: list[StoryCluster] = []
    deep_claimed = False
    while ranked and len(selected) < maximum:
        scored: list[tuple[float, str, StoryCluster, EditorialDecision, int]] = []
        for base_score, cluster, decision in ranked:
            org_penalty = 12 if any(existing.organization.casefold() == cluster.organization.casefold() for existing in selected) else 0
            lens_penalty = 8 if any(existing.category.casefold() == cluster.category.casefold() for existing in selected) else 0
            expert_penalty = 10 if cluster.kind == "expert_analysis" and any(existing.kind == "expert_analysis" for existing in selected) else 0
            penalty = org_penalty + lens_penalty + expert_penalty
            scored.append((base_score - penalty, cluster.id, cluster, decision, penalty))
        _, _, cluster, decision, penalty = min(scored, key=lambda item: (-item[0], item[1]))
        ranked = [item for item in ranked if item[1].id != cluster.id]
        adjusted = decision.score - penalty
        if decision.depth_recommendation == "deep":
            if deep_claimed:
                decision = replace(decision, depth_recommendation="brief")
            else:
                deep_claimed = True
        selected.append(replace(cluster, metadata={**cluster.metadata, "editorial": {**decision.to_dict(), "diversity_penalty": penalty, "adjusted_score": adjusted}}))
    return tuple(selected)


def write_ledger(
    decisions: Iterable[EditorialDecision | dict[str, Any]],
    path: Path,
    *,
    episode_date: str,
    status: str = "reviewed",
    metadata: dict[str, Any] | None = None,
) -> Path:
    records = [item.to_dict() if isinstance(item, EditorialDecision) else item for item in decisions]
    body = {"schema_version": 1, "episode_date": episode_date, "status": status, "decisions": records}
    if metadata:
        # Failure metadata is an operational diagnostic, not a second channel
        # for proxy output.  Copy only the three controlled scalar fields.
        safe: dict[str, str | int] = {}
        failure_type = metadata.get("failure_type")
        if isinstance(failure_type, str) and failure_type in EDITORIAL_FAILURE_TYPES:
            safe["failure_type"] = failure_type
        stage = metadata.get("stage")
        if isinstance(stage, str) and stage in EDITORIAL_FAILURE_STAGES:
            safe["stage"] = stage
        batch_index = metadata.get("batch_index")
        if isinstance(batch_index, int) and not isinstance(batch_index, bool) and batch_index > 0:
            safe["batch_index"] = batch_index
        if safe:
            body["metadata"] = safe
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
