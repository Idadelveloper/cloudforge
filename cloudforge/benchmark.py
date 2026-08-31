"""Helpers for the pre-registered CloudForge benchmark suite.

The benchmark file is the source of truth for both the prompt text and its
pre-run complexity profile. Keeping those together prevents Chapter 4 metrics
from being reconstructed from generated output after an experiment has run.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
BENCHMARK_FILE = REPO_ROOT / "benchmark" / "specs.yaml"
COMPLEXITY_FIELDS = (
    "cloud_components",
    "aws_service_types",
    "api_operations",
    "domain_entities",
    "workflow_rules",
    "score",
)


def read_benchmark_document(path: Path = BENCHMARK_FILE) -> dict[str, Any]:
    """Read the benchmark YAML document or return an empty document."""
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text()) or {}


def read_benchmark_specs(path: Path = BENCHMARK_FILE) -> dict[str, dict[str, Any]]:
    """Load benchmark entries keyed by their stable pre-registered IDs."""
    document = read_benchmark_document(path)
    return {entry["id"]: entry for entry in document.get("specs", [])}


def complexity_score(profile: dict[str, Any]) -> int:
    """Calculate the declared additive Specification Complexity Profile score."""
    return sum(
        int(profile.get(field, 0))
        for field in ("cloud_components", "api_operations", "domain_entities", "workflow_rules")
    )


def validate_complexity_profile(entry: dict[str, Any]) -> list[str]:
    """Return human-readable profile errors for a benchmark entry."""
    profile = entry.get("complexity", {})
    missing = [field for field in COMPLEXITY_FIELDS if field not in profile]
    errors = [f"{entry.get('id', '<unknown>')}: missing complexity field {field}" for field in missing]
    if not missing and complexity_score(profile) != profile["score"]:
        errors.append(
            f"{entry.get('id', '<unknown>')}: score {profile['score']} does not match the declared formula"
        )
    return errors


def benchmark_fingerprint(entry: dict[str, Any] | None) -> str:
    """Hash the evaluation-relevant fields so a report identifies its prompt version."""
    if not entry:
        return ""
    payload = {
        "id": entry.get("id", ""),
        "tier": entry.get("tier", ""),
        "spec": entry.get("spec", ""),
        "complexity": entry.get("complexity", {}),
        "congruence_checklist": entry.get("congruence_checklist", []),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def evaluation_condition(max_iterations: int) -> str:
    """Give standard experiment settings stable labels in saved reports."""
    if max_iterations == 0:
        return "baseline"
    if max_iterations == 3:
        return "bounded_correction"
    return f"exploratory_{max_iterations}_iterations"
