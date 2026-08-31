"""Post-deploy smoke probe: does the DEPLOYED API actually serve requests?

Protocol step, not a pipeline gate: run it manually after a deployed trial,
record the outcome in the master log, and keep the pipeline instrument
unchanged. It discovers the REST API that `tflocal apply` created inside
LocalStack, then sends one request per endpoint in the run's shared plan and
records the HTTP status.

Interpretation contract (fixed before the campaign):
- any 2xx/3xx/4xx means the deployed application layer answered — the
  request traversed API Gateway -> integration -> handler ("serving");
- 5xx, a timeout, a connection error, or no API found means the deployed
  system does not work at runtime ("failing"), which static validation and
  a clean `apply` cannot rule out.
Probes use dummy path parameters and empty JSON bodies, so 404/422 responses
are expected and still prove the service is alive.

Usage:
    .venv/bin/python scripts/smoke_probe.py            # newest completed run
    .venv/bin/python scripts/smoke_probe.py RUN_ID

Writes runs/<run_id>/smoke_test.json and prints a table.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import boto3

REPO = Path(__file__).resolve().parent.parent
RUNS = REPO / "runs"
LOCALSTACK_URL = os.environ.get("LOCALSTACK_URL", "http://localhost:4566")
TIMEOUT_S = 15


def pick_run(run_id_arg: str | None) -> Path:
    if run_id_arg:
        report = RUNS / run_id_arg / "report.json"
        if not report.exists():
            sys.exit(f"No report.json under runs/{run_id_arg}/")
        return report
    reports = sorted(RUNS.glob("*/report.json"))
    if not reports:
        sys.exit("No completed runs found under runs/.")
    return reports[-1]


def discover_apis() -> list[dict]:
    client = boto3.client(
        "apigateway",
        endpoint_url=LOCALSTACK_URL,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    apis = []
    for api in client.get_rest_apis().get("items", []):
        stages = client.get_stages(restApiId=api["id"]).get("item", [])
        apis.append(
            {
                "api_id": api["id"],
                "name": api.get("name", ""),
                "stages": [s["stageName"] for s in stages] or ["prod"],
            }
        )
    return apis


def probe(method: str, url: str) -> tuple[str, bool]:
    """Return (status label, serving?) for one request."""
    body = None
    headers = {"Accept": "application/json"}
    if method in ("POST", "PUT", "PATCH"):
        body = b"{}"
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            return str(response.status), True
    except urllib.error.HTTPError as exc:
        return str(exc.code), exc.code < 500
    except Exception as exc:  # timeout, connection refused, DNS
        return type(exc).__name__, False


def main() -> None:
    report_path = pick_run(sys.argv[1] if len(sys.argv) > 1 else None)
    run_dir = report_path.parent
    report = json.loads(report_path.read_text())
    endpoints = (report.get("plan") or {}).get("endpoints", [])
    if not endpoints:
        sys.exit(f"Run {run_dir.name} has no planned endpoints to probe.")

    apis = discover_apis()
    output = {
        "run_id": run_dir.name,
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "localstack_url": LOCALSTACK_URL,
        "apis_found": len(apis),
        "results": [],
    }

    if not apis:
        print("No deployed REST API found in LocalStack — the deployed system is not serving.")
        output["verdict"] = "no_api_found"
    for api in apis:
        stage = api["stages"][0]
        base = f"{LOCALSTACK_URL}/restapis/{api['api_id']}/{stage}/_user_request_"
        print(f"\nAPI {api['api_id']} ({api['name']}), stage '{stage}'")
        print(f"Base URL: {base}")
        print(f"{'method':7} {'path':28} {'status':10} serving")
        for endpoint in endpoints:
            method = endpoint.get("method", "GET").upper()
            path = endpoint.get("path", "/")
            probe_path = "/".join(
                "smoke-id" if part.startswith("{") else part for part in path.split("/")
            )
            status, serving = probe(method, base + probe_path)
            print(f"{method:7} {path:28} {status:10} {'yes' if serving else 'NO'}")
            output["results"].append(
                {
                    "api_id": api["api_id"],
                    "stage": stage,
                    "method": method,
                    "path": path,
                    "probe_path": probe_path,
                    "status": status,
                    "serving": serving,
                }
            )

    serving_count = sum(r["serving"] for r in output["results"])
    output["serving_endpoints"] = serving_count
    output["total_endpoints"] = len(endpoints)
    (run_dir / "smoke_test.json").write_text(json.dumps(output, indent=2))
    print(
        f"\nServing endpoints: {serving_count}/{len(endpoints)} "
        f"(saved to runs/{run_dir.name}/smoke_test.json)"
    )
    sys.exit(0 if output["results"] and serving_count == len(endpoints) else 1)


if __name__ == "__main__":
    main()
