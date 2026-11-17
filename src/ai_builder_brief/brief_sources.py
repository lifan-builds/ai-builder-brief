"""Validation for the daily brief source catalog."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

REQUIRED_LANES = {
    "models_and_apis",
    "developer_tools_and_agents",
    "infrastructure_and_economics",
    "research_and_evaluation",
    "security_and_policy",
    "builder_behavior",
}
ALLOWED_ROLES = {"primary", "independent", "adoption", "discovery"}


def load_manifest(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def validate_manifest(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if payload.get("version") != 1:
        errors.append("version must be 1")

    policy = payload.get("policy")
    if not isinstance(policy, dict):
        errors.append("policy must be a mapping")
    else:
        for key, expected in {
            "storage": "metadata_only",
            "reddit": "discovery_only",
            "social": "discovery_only",
            "newsletters": "discovery_only",
            "paywalls": "do_not_bypass",
        }.items():
            if policy.get(key) != expected:
                errors.append(f"policy.{key} must be {expected}")

    sources = payload.get("sources")
    if not isinstance(sources, list) or not sources:
        return [*errors, "sources must be a non-empty list"]
    by_id: dict[str, dict[str, Any]] = {}
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            errors.append(f"sources[{index}] must be a mapping")
            continue
        source_id = str(source.get("id") or "")
        if not source_id:
            errors.append(f"sources[{index}] is missing id")
            continue
        if source_id in by_id:
            errors.append(f"duplicate source id: {source_id}")
        by_id[source_id] = source
        if source.get("role") not in ALLOWED_ROLES:
            errors.append(f"{source_id} has an invalid role")
        if not source.get("language"):
            errors.append(f"{source_id} is missing language")
        if not source.get("url"):
            errors.append(f"{source_id} is missing url")
        if "reddit.com" in str(source.get("url", "")) and source.get("role") != "discovery":
            errors.append("Reddit sources must be discovery-only")

    lanes = payload.get("coverage_lanes")
    if not isinstance(lanes, dict):
        errors.append("coverage_lanes must be a mapping")
    else:
        missing = REQUIRED_LANES - set(lanes)
        errors.extend(f"missing coverage lane: {lane}" for lane in sorted(missing))
        for lane, references in lanes.items():
            if not isinstance(references, list) or not references:
                errors.append(f"coverage lane {lane} must have sources")
                continue
            for reference in references:
                if reference not in by_id:
                    errors.append(f"coverage lane {lane} references unknown source: {reference}")
    return errors


def validate_path(path: Path) -> list[str]:
    try:
        return validate_manifest(load_manifest(path))
    except (OSError, yaml.YAMLError) as error:
        return [f"could not read manifest: {error}"]

