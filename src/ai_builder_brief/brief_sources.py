"""Validation and live health probing for the daily brief source catalog."""

from __future__ import annotations

import gzip
import ssl
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import certifi
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
LIVE_SKIP_DOMAINS = {"x.com", "www.reddit.com", "reddit.com"}
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
USER_AGENT = "AIBuilderBriefValidator/1.0 (+https://github.com/lifan-builds/ai-builder-brief)"


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
        weight = source.get("weight")
        if weight is not None and (isinstance(weight, bool) or not isinstance(weight, int) or not 1 <= weight <= 100):
            errors.append(f"{source_id} weight must be an integer between 1 and 100")
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


def check_live_sources(
    payload: dict[str, Any],
    *,
    opener: Callable[..., Any] = urlopen,
    timeout: float = 15.0,
) -> list[str]:
    """Probe configured URLs and return error strings for HTTP failures or challenge shells."""
    sources = [s for s in payload.get("sources", []) if isinstance(s, dict) and s.get("url")]

    def probe_one(source: dict[str, Any]) -> str | None:
        source_id = str(source.get("id") or "unknown")
        url = str(source["url"])
        if any(f"://{dom}/" in url or url.endswith(f"://{dom}") for dom in LIVE_SKIP_DOMAINS):
            return None
        req = Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/atom+xml,application/rss+xml,application/json,text/xml,text/html,*/*",
            },
        )
        kwargs: dict[str, Any] = {"timeout": timeout}
        if opener is urlopen:
            kwargs["context"] = SSL_CONTEXT
        try:
            with opener(req, **kwargs) as resp:
                status = getattr(resp, "status", 200)
                raw = resp.read(256_000)
                if raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
                if status >= 400:
                    return f"{source_id} ({url}): HTTP {status}"
                if len(raw) < 32:
                    return f"{source_id} ({url}): empty response ({len(raw)} bytes)"
                sample = raw[:4096].decode("utf-8", errors="replace").lower()
                if "just a moment..." in sample and "cf-browser-verification" in sample:
                    return f"{source_id} ({url}): blocked by Cloudflare challenge"
        except HTTPError as err:
            return f"{source_id} ({url}): HTTP {err.code}"
        except URLError as err:
            return f"{source_id} ({url}): network error ({err.reason})"
        except Exception as err:
            return f"{source_id} ({url}): {err}"
        return None

    if opener is urlopen and len(sources) > 1:
        with ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(probe_one, sources))
    else:
        results = [probe_one(s) for s in sources]
    return [msg for msg in results if msg is not None]


def validate_path(path: Path, *, check_live: bool = False) -> list[str]:
    try:
        manifest = load_manifest(path)
    except (OSError, yaml.YAMLError) as error:
        return [f"could not read manifest: {error}"]
    errors = validate_manifest(manifest)
    if not errors and check_live:
        errors.extend(check_live_sources(manifest))
    return errors
