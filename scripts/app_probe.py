"""Joint functional probe: run the GENERATED application against the DEPLOYED
LocalStack resources and test the application's own endpoints.

This is the step that guarantees "the deployed app is what you test": the
generated backend is started locally (with AWS_ENDPOINT_URL pointing at
LocalStack) so every request it serves is handled by the generated code and
executed against the infrastructure that `tflocal apply` actually created.
If resource names, schemas, or permissions diverge between the two generated
artifacts, requests fail here even though each artifact passed its own gates
— which makes this the strongest automated congruence evidence in the study.

Interpretation contract (fixed before the campaign):
- launch failure (the app process dies before serving) = runtime failure of
  the generated application;
- per endpoint, 2xx/3xx/4xx = the application answered (dummy IDs and empty
  bodies make 404/422 expected); 5xx or timeout = a runtime integration
  failure (often app-vs-infra mismatch).

Usage:
    .venv/bin/python scripts/app_probe.py            # newest completed run
    .venv/bin/python scripts/app_probe.py RUN_ID
    .venv/bin/python scripts/app_probe.py RUN_ID --serve   # keep it running
                                                           # for manual curls

Writes runs/<run_id>/app_probe.json and prints a table.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

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


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def launch_command(framework: str, port: int) -> list[str]:
    if framework == "flask":
        return [sys.executable, "-m", "flask", "--app", "app", "run",
                "--port", str(port), "--host", "127.0.0.1"]
    return [sys.executable, "-m", "uvicorn", "app:app",
            "--port", str(port), "--host", "127.0.0.1", "--log-level", "warning"]


def probe(method: str, url: str) -> tuple[str, bool]:
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
    except Exception as exc:
        return type(exc).__name__, False


def wait_ready(proc: subprocess.Popen, base: str, timeout: int = 25) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        try:
            urllib.request.urlopen(base + "/", timeout=2)
            return True
        except urllib.error.HTTPError:
            return True  # any HTTP status means the app answered
        except Exception:
            time.sleep(0.5)
    return proc.poll() is None


def main() -> None:
    args = [a for a in sys.argv[1:] if a != "--serve"]
    serve = "--serve" in sys.argv[1:]
    report_path = pick_run(args[0] if args else None)
    run_dir = report_path.parent
    report = json.loads(report_path.read_text())
    plan = report.get("plan") or {}
    endpoints = plan.get("endpoints", [])
    if not endpoints:
        sys.exit(f"Run {run_dir.name} has no planned endpoints to probe.")
    app_dir = run_dir / "app"
    if not app_dir.exists():
        sys.exit(f"Run {run_dir.name} has no app/ directory.")

    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env.update(
        {
            "AWS_ENDPOINT_URL": LOCALSTACK_URL,
            "AWS_ACCESS_KEY_ID": "test",
            "AWS_SECRET_ACCESS_KEY": "test",
            "AWS_DEFAULT_REGION": "us-east-1",
        }
    )
    framework = plan.get("framework", "fastapi")
    proc = subprocess.Popen(
        launch_command(framework, port),
        cwd=app_dir,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    output = {
        "run_id": run_dir.name,
        "probed_at": datetime.now(timezone.utc).isoformat(),
        "framework": framework,
        "localstack_url": LOCALSTACK_URL,
        "base_url": base,
        "launched": False,
        "results": [],
    }

    if not wait_ready(proc, base):
        proc.terminate()
        tail = (proc.stdout.read() if proc.stdout else "")[-2000:]
        output["launch_error"] = tail
        (run_dir / "app_probe.json").write_text(json.dumps(output, indent=2))
        print("The generated application FAILED TO START — runtime failure evidence.")
        print(tail)
        sys.exit(2)

    output["launched"] = True
    print(f"Generated {framework} app serving at {base} (against {LOCALSTACK_URL})")
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
    (run_dir / "app_probe.json").write_text(json.dumps(output, indent=2))
    print(
        f"\nApp endpoints serving against deployed infra: {serving_count}/{len(endpoints)} "
        f"(saved to runs/{run_dir.name}/app_probe.json)"
    )

    if serve:
        print(f"\n--serve: the generated app stays up at {base} for manual testing.")
        print("Press Ctrl+C to stop it.")
        try:
            proc.wait()
        except KeyboardInterrupt:
            pass
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    sys.exit(0 if serving_count == len(endpoints) else 1)


if __name__ == "__main__":
    main()
