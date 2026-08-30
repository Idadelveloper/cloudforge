"""Compute specification-complexity and outcome metrics for every run.

Produces the per-specification measurement table used in the dissertation
(Chapters 3 and 4): intrinsic complexity dimensions taken from the parsed
plan, effort/outcome measures taken from the pipeline report, and generated
artifact sizes measured from disk.

Usage:
    venv/bin/python scripts/complexity_metrics.py            # markdown table
    venv/bin/python scripts/complexity_metrics.py --csv out.csv
"""

import argparse
import csv
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
RUNS = REPO / "runs"


def loc(path: Path, suffixes: tuple[str, ...]) -> int:
    total = 0
    if not path.exists():
        return 0
    for f in path.rglob("*"):
        if f.suffix in suffixes and f.is_file() and "__pycache__" not in f.parts:
            total += sum(1 for line in f.read_text(errors="ignore").splitlines() if line.strip())
    return total


def analyse_run(report_path: Path) -> dict:
    r = json.loads(report_path.read_text())
    run_dir = report_path.parent
    plan = r.get("plan") or {}
    services = sorted({res["service"].lower() for res in plan.get("aws_resources", [])})
    timings = r.get("timings", [])
    usage = r.get("usage", [])
    validations = r.get("validations", [])
    iter0 = [v for v in validations if v["iteration"] == 0]
    return {
        "run_id": r["run_id"],
        "benchmark_id": r.get("benchmark_id", ""),
        "tier": r.get("tier", ""),
        "spec": r["spec"][:70].replace("\n", " "),
        # ---- intrinsic complexity dimensions (from the shared plan) ----
        "endpoints": len(plan.get("endpoints", [])),
        "data_models": len(plan.get("data_models", [])),
        "aws_resources": len(plan.get("aws_resources", [])),
        "aws_services": len(services),
        "services": ",".join(services),
        # ---- generated artifact size ----
        "app_files": len(r.get("app_files", [])),
        "iac_files": len(r.get("iac_files", [])),
        "app_loc": loc(run_dir / "app", (".py",)),
        "iac_loc": loc(run_dir / "infra", (".tf",)),
        # ---- effort / outcome ----
        "status": r["status"],
        "deploy_passed": r.get("deploy", {}).get("passed", False),
        "iterations_used": r.get("iterations_used", 0),
        "first_attempt_clean": all(v["passed"] for v in iter0) if iter0 else False,
        "llm_calls": len(usage),
        "input_tokens": sum(u.get("input_tokens", 0) for u in usage),
        "output_tokens": sum(u.get("output_tokens", 0) for u in usage),
        "cost_usd": r.get("estimated_cost_usd", 0.0),
        "pipeline_seconds": round(sum(t.get("seconds", 0) for t in timings), 1),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", help="also write results to this CSV path")
    args = parser.parse_args()

    rows = []
    for report in sorted(RUNS.glob("*/report.json")):
        try:
            rows.append(analyse_run(report))
        except (json.JSONDecodeError, KeyError) as exc:
            print(f"skipping {report}: {exc}", file=sys.stderr)

    if not rows:
        print("No completed runs found under runs/.", file=sys.stderr)
        sys.exit(1)

    cols = list(rows[0].keys())
    if args.csv:
        with open(args.csv, "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=cols)
            writer.writeheader()
            writer.writerows(rows)
        print(f"wrote {args.csv}", file=sys.stderr)

    # Markdown table (drop the long text columns for readability)
    show = [c for c in cols if c not in ("spec", "services")]
    print("| " + " | ".join(show) + " |")
    print("|" + "|".join("---" for _ in show) + "|")
    for row in rows:
        print("| " + " | ".join(str(row[c]) for c in show) + " |")


if __name__ == "__main__":
    main()
