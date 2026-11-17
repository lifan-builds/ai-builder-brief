"""Deterministic editorial preprocessing, review contracts, and selection."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable

from castforge.models import SourceItem, StoryCluster

ROUTINE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ci", re.compile(r"\bci\b", re.IGNORECASE)),
    ("chore", re.compile(r"\bchore\b", re.IGNORECASE)),
    ("continuous integration", re.compile(r"\bcontinuous\s+integration\b", re.IGNORECASE)),
    ("bump version", re.compile(r"\bbump\s+version\b", re.IGNORECASE)),
    ("dependency update", re.compile(r"\bdependency\s+update\b", re.IGNORECASE)),
    ("release candidate", re.compile(r"\brelease\s+candidate\b|\brc\.?\d+\b", re.IGNORECASE)),
    ("roundup", re.compile(r"\b(?:weekly\s+)?roundup\b|^\[?ainews\]?\b", re.IGNORECASE)),
    ("cloud wrapper", re.compile(r"\bcloud\s+wrapper\b", re.IGNORECASE)),
    ("marketing", re.compile(r"\bmarketing\s+campaign\b|\bad\s+campaign\b", re.IGNORECASE)),
    ("sponsored", re.compile(r"\bsponsored\b", re.IGNORECASE)),
    ("launch recap", re.compile(r"\blaunch\s+recap\b", re.IGNORECASE)),
    ("minor fix", re.compile(r"\bminor\s+fix\b", re.IGNORECASE)),
    ("documentation only", re.compile(r"\bdocumentation\s+only\b", re.IGNORECASE)),
)

LOW_VALUE_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "cloud availability without a new builder capability": (
        re.compile(r"\bavailable\s+(?:on|in)\s+(?:aws|azure|google\s+cloud)\b", re.IGNORECASE),
        re.compile(r"\b(?:now\s+|generally\s+)?available\s+in\s+github\s+copilot\b", re.IGNORECASE),
        re.compile(r"\bpurchasing\s+channel\b", re.IGNORECASE),
    ),
    "narrow autonomous-driving research": (
        re.compile(r"\b(?:autonomous|vehicle-to-vehicle|cooperative)\s+driving\b", re.IGNORECASE),
    ),
    "corporate fundraising, IPO, or promo fluff": (
        re.compile(r"\braises\s+\$\d|\bseed\s+round\b|\bseries\s+[a-f]\b|\btargets\s+\$\d[\w.]*\s+in\s+(?:annual\s+)?revenue\b", re.IGNORECASE),
        re.compile(r"\bfiles\s+s-1\b|\binitial\s+public\s+offering\b|\bipo\s+filing\b", re.IGNORECASE),
        re.compile(r"\btechcrunch\s+disrupt\b|\$\d+\s+deal\s+for\s+your\b", re.IGNORECASE),
        re.compile(r"\bbefore\s+anyone\s+asks\b|\bleads\s+in\s+legal\s+intelligence\b", re.IGNORECASE),
    ),
    "generic index or landing page": (
        re.compile(r"^research\s+reports$", re.IGNORECASE),
    ),
}

PRACTICAL_TERMS = {
    "agent", "api", "available", "benchmark", "code", "compiler", "context",
    "developer", "eval", "harness", "inference", "kernel", "latency", "license",
    "migration", "open source", "quantization", "repository", "sdk", "security",
    "training", "verification", "weights",
}
BUILDER_ACTIONS = frozenset({"use", "build", "test", "monitor", "reconsider"})
EDITORIAL_FAILURE_TYPES = frozenset({"timeout", "proxy", "invalid_response"})
EDITORIAL_FAILURE_STAGES = frozenset({
    "proxy_config", "editorial_request", "editorial_response",
    "editorial_coverage", "editorial_input", "editorial_batch", "editorial_validation",
})
EDITORIAL_CLASS_CAPS = {"maintenance_release": 2, "research": 2}
MAX_PREPROCESS_PER_ORG = 3


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
    title_clean = item.title.strip()
    for label, pattern in ROUTINE_PATTERNS:
        if pattern.search(text):
            return f"routine or excluded pattern: {label}"
    for reason, patterns in LOW_VALUE_PATTERNS.items():
        target = title_clean if reason == "generic index or landing page" else text
        if any(pattern.search(target) for pattern in patterns):
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
    if value <= 4:
        return max(0, min(4, int(round(value))))
    return max(0, min(4, int(round(value / 25))))


def _is_community_signal(item: SourceItem) -> bool:
    return bool(item.metadata.get("community_signal"))


def _has_qualifying_evidence(items: Iterable[SourceItem]) -> bool:
    sources = list(items)
    if any(item.authority == "primary" for item in sources):
        return True
    distinct_independent = {
        item.source.strip().casefold()
        for item in sources
        if item.authority == "independent" and item.source.strip()
    }
    return len(distinct_independent) >= 2


def _editorial_class(items: Iterable[SourceItem]) -> str:
    sources = list(items)
    source_kinds = {str(item.metadata.get("source_kind") or "") for item in sources}
    if "daily_paper" in source_kinds or any(item.category == "research" for item in sources):
        return "research"
    release_sources = [
        item for item in sources
        if item.metadata.get("source_kind") == "release_feed"
        or item.source.casefold().endswith(" releases")
    ]
    substantive_non_release = any(
        item.authority in {"primary", "independent"} and item not in release_sources
        for item in sources
    )
    if release_sources and not substantive_non_release:
        return "maintenance_release"
    if not _has_qualifying_evidence(sources):
        return "community_theme"
    return "major_development"


def _admission_adjustment(editorial_class: str) -> int:
    return {
        "maintenance_release": -25,
        "research": -12,
    }.get(editorial_class, 0)


def _cluster_primary_org(items: list[SourceItem]) -> str:
    for item in items:
        if item.authority == "primary" and item.organization.strip():
            return item.organization.strip().casefold()
    subjects = [
        str(org).strip().casefold()
        for item in items
        for org in item.metadata.get("subject_organizations", [])
        if str(org).strip()
    ]
    return subjects[0] if len(set(subjects)) == 1 and subjects else ""


def preprocess(
    items: Iterable[SourceItem],
    *,
    limit: int = 24,
    as_of: datetime | None = None,
) -> tuple[list[SourceItem], list[dict[str, Any]]]:
    prepared: list[tuple[str, list[SourceItem], bool, str, float, str]] = []
    decisions_by_id: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[SourceItem]] = {}
    for item in items:
        grouped.setdefault(str(item.metadata.get("cluster_id") or item.id), []).append(item)
    for cluster_id, group in sorted(grouped.items()):
        kept: list[SourceItem] = []
        rejected_reasons: list[str] = []
        for item in sorted(group, key=lambda value: (-float(value.metadata.get("score", 0)), value.id)):
            reason = reject_reason(item)
            if reason == "signal-only evidence cannot qualify a story" and _is_community_signal(item):
                reason = None
            if reason:
                rejected_reasons.append(f"{item.id}: {reason}")
                continue
            metadata = dict(item.metadata)
            metadata.update({
                "preprocessed": True,
                "builder_impact": min(
                    4,
                    1 + sum(1 for term in ("available", "api", "weights", "security", "inference", "sdk", "benchmark") if term in _text(item)),
                ),
                "momentum_score": _momentum_score(item),
                "recency_score": _recency_score(item.published_at, as_of),
            })
            kept.append(replace(item, metadata=metadata))
        if not kept:
            decisions_by_id[cluster_id] = {
                "cluster_id": cluster_id,
                "decision": "reject",
                "rationale": "; ".join(rejected_reasons) or "deterministically rejected",
                "source_ids": [item.id for item in group],
            }
            continue
        community_led = any(_is_community_signal(item) for item in kept)
        editorial_class = _editorial_class(kept)
        signal_types = sorted({
            str(item.metadata.get("community_signal_type"))
            for item in kept if item.metadata.get("community_signal_type")
        })
        qualifying_evidence = _has_qualifying_evidence(kept)
        enriched = [
            replace(item, metadata={
                **item.metadata,
                "community_led": community_led,
                "community_signal_types": signal_types,
                "qualifying_evidence": qualifying_evidence,
                "editorial_class": editorial_class,
            })
            for item in kept
        ]
        corroboration_bonus = 6.0 if community_led and qualifying_evidence and len(enriched) >= 2 else 0.0
        admission_score = (
            max(float(item.metadata.get("score", 0)) for item in enriched)
            + max(_momentum_score(item) for item in enriched) * 5
            + corroboration_bonus
            + _admission_adjustment(editorial_class)
        )
        prepared.append((cluster_id, enriched, community_led, editorial_class, admission_score, _cluster_primary_org(enriched)))

    community_slots = limit // 2
    primary_slots = limit - community_slots
    community_ranked = sorted(
        (record for record in prepared if record[2]),
        key=lambda record: (-record[4], record[0]),
    )
    primary_ranked = sorted(
        (record for record in prepared if not record[2]),
        key=lambda record: (-record[4], record[0]),
    )
    class_counts: dict[str, int] = {}
    org_counts: dict[str, int] = {}
    admitted_ids: set[str] = set()

    def admit_balanced(
        ranked: list[tuple[str, list[SourceItem], bool, str, float, str]],
        slots: int,
        *,
        enforce_org_cap: bool = True,
    ) -> list[tuple[str, list[SourceItem], bool, str, float, str]]:
        admitted: list[tuple[str, list[SourceItem], bool, str, float, str]] = []
        for record in ranked:
            if record[0] in admitted_ids:
                continue
            editorial_class = record[3]
            cap = EDITORIAL_CLASS_CAPS.get(editorial_class)
            if cap is not None and class_counts.get(editorial_class, 0) >= cap:
                continue
            org = record[5]
            if enforce_org_cap and org and org_counts.get(org, 0) >= MAX_PREPROCESS_PER_ORG:
                continue
            admitted.append(record)
            admitted_ids.add(record[0])
            class_counts[editorial_class] = class_counts.get(editorial_class, 0) + 1
            if org:
                org_counts[org] = org_counts.get(org, 0) + 1
            if len(admitted) >= slots:
                break
        return admitted

    community = admit_balanced(community_ranked, community_slots)
    primary = admit_balanced(primary_ranked, primary_slots)

    # Spillover backfill: if either pool has unused slots, fill from remaining candidates
    remaining_slots = limit - (len(community) + len(primary))
    spillover: list[tuple[str, list[SourceItem], bool, str, float, str]] = []
    if remaining_slots > 0:
        combined_rest = sorted(
            (r for r in prepared if r[0] not in admitted_ids),
            key=lambda r: (-r[4], r[0]),
        )
        spillover = admit_balanced(combined_rest, remaining_slots, enforce_org_cap=True)
        if len(community) + len(primary) + len(spillover) < limit:
            spillover.extend(
                admit_balanced(
                    combined_rest,
                    limit - (len(community) + len(primary) + len(spillover)),
                    enforce_org_cap=False,
                )
            )

    all_admitted = (*community, *primary, *spillover)
    selected_ids = {record[0] for record in all_admitted}
    accepted = [item for record in all_admitted for item in record[1]]

    for cluster_id, kept, community_led, editorial_class, _, _ in prepared:
        admitted = cluster_id in selected_ids
        kept_ids = {item.id for item in kept}
        decisions_by_id[cluster_id] = {
            "cluster_id": cluster_id,
            "decision": "candidate" if admitted else "reject",
            "rationale": (
                "admitted to balanced community editorial pool"
                if admitted and community_led
                else "admitted to balanced primary editorial pool"
                if admitted
                else "excluded by balanced editorial pool quota"
            ),
            "community_led": community_led,
            "editorial_class": editorial_class,
            "source_ids": [item.id for item in kept],
            "rejected_source_ids": [item.id for item in grouped[cluster_id] if item.id not in kept_ids],
        }
    decisions = [decisions_by_id[cluster_id] for cluster_id in sorted(decisions_by_id)]
    return accepted, decisions


def score_candidates_deterministically(packets: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Deterministically score candidate packets into strict `EDITORIAL_SCHEMA` decisions.

    Used by `--dry-run` and offline benchmarks so full selection/review artifacts
    can be generated and evaluated without invoking external LLM endpoints.
    """
    decisions: list[dict[str, Any]] = []
    deep_assigned = False
    for packet in packets:
        cluster_id = str(packet["cluster_id"])
        title = str(packet.get("title") or "")
        summary = str(packet.get("summary") or "")
        url = str(packet.get("url") or "")
        text = f"{title} {summary}".casefold()
        editorial_class = str(packet.get("editorial_class") or "major_development")
        qualifying = bool(packet.get("qualifying_evidence"))
        authority = str(packet.get("authority") or "signal")
        hn_points = int(packet.get("hn_points") or 0)
        momentum = int(packet.get("momentum_score") or 0)

        artifact_hits = sum(
            1
            for term in ("api", "sdk", "weights", "open source", "benchmark", "eval", "inference", "cli", "mcp", "security", "vulnerability", "release", "v1.", "v0.")
            if term in text
        )
        has_quant = bool(re.search(r"\b\d+(?:\.\d+)?(?:\s*(?:%|×|x\b|ms|gb|gib|mtok)|\.\d+)\b|\$\d+", f"{title} {summary}", re.I))
        is_major_release = editorial_class == "major_development" or bool(re.search(r"\bv1\.0(?:\.0)?\b", title, re.I))

        impact = min(4, max(2, 2 + int(artifact_hits >= 2 or hn_points >= 250) + int((artifact_hits >= 4 or hn_points >= 600 or has_quant) and (is_major_release or hn_points >= 400))))
        actionability = min(4, max(2, 2 + int(any(t in text for t in ("api", "sdk", "cli", "weights", "release", "migration", "benchmark", "verification"))) + int("github.com/" in url.lower() or has_quant)))
        novelty = min(4, max(2, 2 + int(hn_points >= 150 or momentum >= 3 or qualifying) + int((hn_points >= 500 or ("arxiv.org" in url.lower() and has_quant)) and editorial_class != "maintenance_release")))
        evidence = 4 if (qualifying and has_quant) else (3 if qualifying else (3 if authority == "independent" and len(summary.split()) >= 20 else 2))
        audience_breadth = min(4, max(2, 2 + int(hn_points >= 120 or momentum >= 2 or authority == "primary") + int(hn_points >= 400 or any(t in text for t in ("openai", "anthropic", "claude", "gemini", "qwen", "deepseek", "vllm", "llama.cpp", "openrouter", "coding agent")))))

        actions: list[str] = []
        if any(t in text for t in ("release", "sdk", "api", "cli", "weights")):
            actions.append("use")
        if any(t in text for t in ("benchmark", "eval", "latency", "speed", "accuracy", "verification")):
            actions.append("test")
        if any(t in text for t in ("breaking", "vulnerability", "telemetry", "backdoor", "leak", "regression", "removes")):
            actions.append("reconsider")
        if not actions:
            actions.append("monitor")

        accept = qualifying and impact >= 3 and evidence >= 3 and editorial_class != "maintenance_release"
        if editorial_class == "research" and not (impact == 4 and novelty >= 3 and has_quant):
            accept = False
        depth = "brief"
        if accept and not deep_assigned and impact == 4 and evidence == 4:
            depth = "deep"
            deep_assigned = True

        decisions.append({
            "cluster_id": cluster_id,
            "decision": "accept" if accept else "reject",
            "impact": impact,
            "actionability": actionability,
            "novelty": novelty,
            "evidence": evidence,
            "audience_breadth": audience_breadth,
            "builder_actions": sorted(set(actions))[:3],
            "why_now": f"Published in rolling window ({packet.get('published_at', '')}) with momentum={momentum} and hn_points={hn_points}.",
            "rationale": f"Deterministic builder triage ({editorial_class}, authority={authority}, artifact_signals={artifact_hits}).",
            "caveats": "Verify benchmark scope, licensing, and workload fit against primary documentation before production rollout.",
            "depth_recommendation": depth,
            "source_ids": list(packet.get("source_ids") or [cluster_id]),
        })
    return {"decisions": decisions}


def validate_review(
    payload: Any,
    candidate_ids: set[str],
    candidate_metadata: dict[str, dict[str, Any]] | None = None,
) -> list[EditorialDecision]:
    if not isinstance(payload, dict) or not isinstance(payload.get("decisions"), list):
        raise ValueError("editorial response must be an object with decisions")
    decisions: list[EditorialDecision] = []
    seen: set[str] = set()
    required = {
        "cluster_id", "decision", "impact", "actionability", "novelty",
        "evidence", "audience_breadth", "builder_actions", "why_now",
        "rationale", "caveats", "depth_recommendation", "source_ids",
    }
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
            impact=int(raw["impact"]),
            actionability=int(raw["actionability"]),
            novelty=int(raw["novelty"]),
            evidence=int(raw["evidence"]),
            audience_breadth=int(raw["audience_breadth"]),
            builder_actions=tuple(str(item) for item in raw["builder_actions"]),
            why_now=str(raw["why_now"]),
            rationale=str(raw["rationale"]),
            caveats=str(raw["caveats"]),
            depth_recommendation=str(raw["depth_recommendation"]),
            source_ids=tuple(str(item) for item in raw.get("source_ids", [])),
            momentum=int(metadata.get("momentum_score", 0)),
            recency=int(metadata.get("recency_score", 0)),
        ))
    if seen != candidate_ids:
        raise ValueError("editorial response must cover every candidate exactly once")
    return decisions


def select_clusters(
    clusters: Iterable[StoryCluster],
    decisions: Iterable[EditorialDecision],
    *,
    minimum: int = 3,
    maximum: int = 6,
) -> tuple[StoryCluster, ...]:
    by_id = {decision.cluster_id: decision for decision in decisions}
    ranked: list[tuple[float, StoryCluster, EditorialDecision]] = []
    for cluster in clusters:
        decision = by_id.get(cluster.id)
        if not decision or not is_podcast_ready(decision, cluster.sources):
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
        selected.append(replace(
            cluster,
            metadata={
                **cluster.metadata,
                "editorial": {
                    **decision.to_dict(),
                    "diversity_penalty": penalty,
                    "adjusted_score": adjusted,
                },
            },
        ))
    return tuple(selected)


def is_podcast_ready(
    decision: EditorialDecision,
    sources: Iterable[SourceItem] | None = None,
) -> bool:
    """Return whether a reviewed candidate passes the episode evidence gate."""
    return (
        decision.decision == "accept"
        and decision.score >= 70
        and decision.impact >= 3
        and decision.evidence >= 3
        and sources is not None
        and _has_qualifying_evidence(sources)
    )


def write_ledger(
    decisions: Iterable[EditorialDecision | dict[str, Any]],
    path: Path,
    *,
    episode_date: str,
    status: str = "reviewed",
    metadata: dict[str, Any] | None = None,
) -> Path:
    records = [item.to_dict() if isinstance(item, EditorialDecision) else item for item in decisions]
    body: dict[str, Any] = {
        "schema_version": 1,
        "episode_date": episode_date,
        "status": status,
        "decisions": records,
    }
    if metadata:
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
