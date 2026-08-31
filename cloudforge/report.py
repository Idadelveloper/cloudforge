"""Run reporting and failure-taxonomy tagging (proposal deliverables 2 and 3).

Every run writes runs/<run_id>/report.json containing the spec, the shared
plan, all generated files, every validation result per iteration, token
usage, timings, and an automatic first-pass taxonomy tag per failure using
the Nekrasov et al. (2025) categories. Intent misalignment and Temporal
Blind Spot failures require qualitative coding and are flagged for manual
review rather than auto-tagged.
"""

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .llm import MODEL, estimate_cost_usd
from .state import CloudForgeState, ValidationResult

HALLUCINATION_MARKERS = (
    "ModuleNotFoundError",
    "No module named",
    "Could not find a version that satisfies",
    "Failed to query available provider packages",
)
REPO_ROOT = Path(__file__).resolve().parent.parent


def repository_metadata() -> dict[str, str | bool]:
    """Capture the source revision that produced a run without blocking finalisation."""
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        porcelain = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        # runs/ is version-controlled evidence, but a run's own files are
        # necessarily untracked while it executes — they must not mark the
        # SOURCE as dirty (P17).
        meaningful = [
            line
            for line in porcelain
            if line.strip()
            and not line.startswith("?? runs/")
            and not line.startswith('?? "runs/')
        ]
        return {"commit": commit, "dirty": bool(meaningful)}
    except (OSError, subprocess.CalledProcessError):
        return {"commit": "unavailable", "dirty": False}


SCHEMA_MARKERS = (
    "Unsupported argument",
    "Unsupported block type",
    "Invalid resource type",
    "unexpected attribute",
    "An argument named",
)


def classify_failure(result: ValidationResult) -> str:
    """First-pass automatic tag; manual qualitative coding refines these."""
    output = result["output"]
    if any(marker in output for marker in HALLUCINATION_MARKERS):
        return "hallucinated_dependency"
    tool = result["tool"]
    if tool == "flake8":
        return "syntax_violation"
    if tool.startswith("terraform"):
        if any(marker in output for marker in SCHEMA_MARKERS):
            return "schema_mismatch"
        return "syntax_violation"
    if tool.startswith("checkov") or tool == "bandit":
        return "security_noncompliance"
    if tool == "pytest" or result["target"] == "deploy":
        return "runtime_failure"
    return "manual_review"


def write_report(state: CloudForgeState, status: str) -> str:
    run_dir = Path(state["run_dir"])
    run_dir.mkdir(parents=True, exist_ok=True)

    finished_at = datetime.now(timezone.utc)
    started_at = state.get("started_at", "")
    wall_seconds = None
    if started_at:
        try:
            wall_seconds = round(
                (finished_at - datetime.fromisoformat(started_at)).total_seconds(), 1
            )
        except ValueError:
            wall_seconds = None

    validations = state.get("validations", [])
    failures = [v for v in validations if not v["passed"]]
    per_iteration: dict[int, dict] = {}
    for v in validations:
        entry = per_iteration.setdefault(
            v["iteration"], {"iteration": v["iteration"], "passed": 0, "failed": 0}
        )
        entry["passed" if v["passed"] else "failed"] += 1

    timings = state.get("timings", [])
    report = {
        "run_id": state["run_id"],
        "timestamp": finished_at.isoformat(),
        "started_at": started_at,
        "finished_at": finished_at.isoformat(),
        # True elapsed time per specification (RQ4). The per-node sum
        # double-counts the parallel generate/validate supersteps, so it is
        # recorded separately and must not be reported as duration.
        "wall_seconds": wall_seconds,
        "node_seconds_sum": round(
            sum(entry.get("seconds", 0) for entry in timings), 1
        ),
        "model": MODEL,
        "status": status,
        "spec": state["spec"],
        "benchmark_id": state.get("benchmark_id", ""),
        "tier": state.get("tier", ""),
        "evaluation_condition": state.get("evaluation_condition", "custom"),
        "benchmark": {
            "fingerprint": state.get("benchmark_fingerprint", ""),
            "complexity": state.get("benchmark_complexity", {}),
            "congruence_checklist": state.get("benchmark_checklist", []),
        },
        "repository": repository_metadata(),
        "plan": state.get("plan"),
        "max_iterations": state.get("max_iterations"),
        "iterations_used": state.get("iteration", 0),
        "deploy": {
            "enabled": state.get("deploy_enabled", False),
            "skipped": state.get("deploy_skipped", False),
            "passed": state.get("deploy_passed", False),
            "output": state.get("deploy_output", ""),
        },
        "validation_pass_rate_per_iteration": sorted(
            per_iteration.values(), key=lambda e: e["iteration"]
        ),
        "validations": validations,
        "failure_taxonomy": [
            {
                "iteration": v["iteration"],
                "target": v["target"],
                "tool": v["tool"],
                "category": classify_failure(v),
            }
            for v in failures
        ],
        "usage": state.get("usage", []),
        "estimated_cost_usd": estimate_cost_usd(state.get("usage", [])),
        "timings": state.get("timings", []),
        "events": state.get("events", []),
        "app_files": [f["path"] for f in state.get("app_files", [])],
        "iac_files": [f["path"] for f in state.get("iac_files", [])],
    }
    report_path = run_dir / "report.json"
    report_path.write_text(json.dumps(report, indent=2))
    return str(run_dir)
