"""Export CloudForge's pre-run benchmark and post-run outcome measures.

The ``--catalogue`` view is the Chapter 3/Appendix table: it measures the
specification before an LLM sees it. The default view measures completed runs,
keeping generated-plan and artifact-size measures distinct from that intrinsic
profile.

Usage:
    .venv/bin/python scripts/complexity_metrics.py
    .venv/bin/python scripts/complexity_metrics.py --csv dissertation/evaluation_runs.csv
    .venv/bin/python scripts/complexity_metrics.py --catalogue
    .venv/bin/python scripts/complexity_metrics.py --catalogue --csv dissertation/specification_complexity.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parent.parent
RUNS = REPO / "runs"
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from cloudforge.benchmark import (  # noqa: E402 - repo path is configured above
    benchmark_fingerprint,
    read_benchmark_specs,
    validate_complexity_profile,
)


def checkov_skip_count(infra_dir: Path) -> int:
    """Count inline `checkov:skip` exemptions the model wrote into its own
    Terraform — self-granted policy waivers are RQ3 evidence, not passes."""
    total = 0
    if not infra_dir.exists():
        return 0
    for tf_file in infra_dir.rglob("*.tf"):
        total += tf_file.read_text(errors="ignore").count("checkov:skip")
    return total


def loc(path: Path, suffixes: tuple[str, ...]) -> int:
    """Count non-empty lines in generated files with the requested suffixes."""
    total = 0
    if not path.exists():
        return 0
    for file_path in path.rglob("*"):
        if file_path.suffix in suffixes and file_path.is_file() and "__pycache__" not in file_path.parts:
            total += sum(
                1 for line in file_path.read_text(errors="ignore").splitlines() if line.strip()
            )
    return total


def profile_fields(profile: dict[str, Any]) -> dict[str, int]:
    """Prefix fixed, pre-run profile fields so they cannot be mistaken for outputs."""
    return {
        "profile_cloud_components": int(profile.get("cloud_components", 0)),
        "profile_aws_service_types": int(profile.get("aws_service_types", 0)),
        "profile_api_operations": int(profile.get("api_operations", 0)),
        "profile_domain_entities": int(profile.get("domain_entities", 0)),
        "profile_workflow_rules": int(profile.get("workflow_rules", 0)),
        "profile_score": int(profile.get("score", 0)),
    }


def catalogue_rows() -> list[dict[str, Any]]:
    """Return the pre-registered complexity table for every benchmark item."""
    entries = read_benchmark_specs()
    errors = [error for entry in entries.values() for error in validate_complexity_profile(entry)]
    if errors:
        raise ValueError("\n".join(errors))

    rows = []
    for entry in entries.values():
        profile = entry["complexity"]
        rows.append(
            {
                "benchmark_id": entry["id"],
                "tier": entry["tier"],
                **profile_fields(profile),
                "checklist_items": len(entry.get("congruence_checklist", [])),
                "specification_words": len(entry.get("spec", "").split()),
                "fingerprint": benchmark_fingerprint(entry)[:12],
                "rationale": profile.get("rationale", ""),
            }
        )
    return rows


def _profile_for_run(report: dict[str, Any], entries: dict[str, dict[str, Any]]) -> tuple[dict[str, Any], str]:
    """Use the profile saved with a new report, falling back to the catalogue for pilots."""
    persisted = report.get("benchmark", {})
    profile = persisted.get("complexity", {})
    fingerprint = persisted.get("fingerprint", "")
    if profile:
        return profile, fingerprint
    entry = entries.get(report.get("benchmark_id", ""))
    if not entry:
        return {}, ""
    return entry.get("complexity", {}), benchmark_fingerprint(entry)


def analyse_run(report_path: Path, entries: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Combine a completed run report with its pre-run complexity profile."""
    report = json.loads(report_path.read_text())
    entries = entries or read_benchmark_specs()
    run_dir = report_path.parent
    plan = report.get("plan") or {}
    services = sorted({resource["service"].lower() for resource in plan.get("aws_resources", [])})
    timings = report.get("timings", [])
    usage = report.get("usage", [])
    validations = report.get("validations", [])
    iter0 = [validation for validation in validations if validation["iteration"] == 0]
    profile, fingerprint = _profile_for_run(report, entries)
    repository = report.get("repository", {})
    return {
        "run_id": report["run_id"],
        "benchmark_id": report.get("benchmark_id", ""),
        "tier": report.get("tier", ""),
        "evaluation_condition": report.get("evaluation_condition", "legacy_or_custom"),
        "max_iterations": report.get("max_iterations", ""),
        "specification_fingerprint": fingerprint[:12],
        **profile_fields(profile),
        "spec": report["spec"][:70].replace("\n", " "),
        # Generated-plan measures are outcomes of parsing, not pre-run profile values.
        "plan_endpoints": len(plan.get("endpoints", [])),
        "plan_data_models": len(plan.get("data_models", [])),
        "plan_aws_resources": len(plan.get("aws_resources", [])),
        "plan_aws_service_types": len(services),
        "services": ",".join(services),
        # Generated artifact size is also outcome-side evidence.
        "app_files": len(report.get("app_files", [])),
        "iac_files": len(report.get("iac_files", [])),
        "app_loc": loc(run_dir / "app", (".py",)),
        "iac_loc": loc(run_dir / "infra", (".tf",)),
        "iac_checkov_skips": checkov_skip_count(run_dir / "infra"),
        # Effort and outcome measures.
        "status": report["status"],
        "deploy_passed": report.get("deploy", {}).get("passed", False),
        "iterations_used": report.get("iterations_used", 0),
        "first_attempt_clean": all(validation["passed"] for validation in iter0) if iter0 else False,
        "llm_calls": len(usage),
        "input_tokens": sum(item.get("input_tokens", 0) for item in usage),
        "output_tokens": sum(item.get("output_tokens", 0) for item in usage),
        "cost_usd": report.get("estimated_cost_usd", 0.0),
        # Wall-clock elapsed time is the RQ4 duration measure. Older reports
        # only have per-node timings, whose sum double-counts the parallel
        # supersteps — fall back to it but label the basis.
        "wall_seconds": report.get("wall_seconds")
        if report.get("wall_seconds") is not None
        else round(sum(item.get("seconds", 0) for item in timings), 1),
        "timing_basis": "wall" if report.get("wall_seconds") is not None else "node_sum_legacy",
        "node_seconds_sum": round(sum(item.get("seconds", 0) for item in timings), 1),
        "repository_commit": repository.get("commit", "legacy_or_unavailable"),
        "repository_dirty": repository.get("dirty", ""),
    }


def write_csv(rows: list[dict[str, Any]], destination: str) -> None:
    """Write a reproducible CSV without requiring spreadsheet software."""
    with open(destination, "w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def print_markdown_table(rows: list[dict[str, Any]], catalogue: bool) -> None:
    """Print a compact, copy-ready Markdown table for the dissertation."""
    if catalogue:
        shown = [
            "benchmark_id",
            "tier",
            "profile_cloud_components",
            "profile_aws_service_types",
            "profile_api_operations",
            "profile_domain_entities",
            "profile_workflow_rules",
            "profile_score",
            "checklist_items",
        ]
    else:
        shown = [
            "run_id",
            "benchmark_id",
            "tier",
            "evaluation_condition",
            "max_iterations",
            "profile_score",
            "status",
            "deploy_passed",
            "iterations_used",
            "iac_checkov_skips",
            "cost_usd",
            "wall_seconds",
            "timing_basis",
        ]
    print("| " + " | ".join(shown) + " |")
    print("|" + "|".join("---" for _ in shown) + "|")
    for row in rows:
        print("| " + " | ".join(str(row[field]) for field in shown) + " |")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalogue", action="store_true", help="export the pre-run specification table")
    parser.add_argument("--csv", help="also write rows to this CSV path")
    args = parser.parse_args()

    if args.catalogue:
        rows = catalogue_rows()
    else:
        entries = read_benchmark_specs()
        rows = []
        for report_path in sorted(RUNS.glob("*/report.json")):
            try:
                rows.append(analyse_run(report_path, entries))
            except (json.JSONDecodeError, KeyError) as exc:
                print(f"skipping {report_path}: {exc}", file=sys.stderr)
        if not rows:
            print("No completed runs found under runs/.", file=sys.stderr)
            sys.exit(1)

    if args.csv:
        write_csv(rows, args.csv)
        print(f"wrote {args.csv}", file=sys.stderr)
    print_markdown_table(rows, catalogue=args.catalogue)


if __name__ == "__main__":
    main()
