"""Behavioural verification against redeployed run infrastructure.

Post-campaign batch-pass tool (problems-log P19): redeploys one completed
run's Terraform onto a fresh LocalStack, launches the generated app against
it, exercises the spec's behavioural rule, and records evidence to
runs/<run_id>/behavioral_check.json. Orchestration (reset, deploy) is done
by the caller; this script assumes the run's infra is already applied.

Usage: behavioral_checks.py <scenario>
Scenarios: s5-stock | m4-capacity | m6-alert | c5-threshold | c6-idempotency
"""

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

import boto3

ROOT = Path(__file__).resolve().parent.parent
LOCALSTACK_URL = os.environ.get("LOCALSTACK_URL", "http://localhost:4566")
AWS_KW = dict(
    endpoint_url=LOCALSTACK_URL,
    region_name="us-east-1",
    aws_access_key_id="test",
    aws_secret_access_key="test",
)

SCENARIO_RUNS = {
    "s5-stock": "20260831-221834-51ccc0",
    "m4-capacity": "20260901-000802-a4dad5",
    "m6-alert": "20260901-063810-8c9f18",
    "c5-threshold": "20260901-112551-bbef06",
    "c6-idempotency": "20260901-114234-d441be",
}


def request(method, base, path, payload=None, timeout=15):
    url = base + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
            return resp.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()
        try:
            return exc.code, json.loads(body)
        except Exception:
            return exc.code, {"raw": body[:300]}


def launch_app(run_dir):
    port = _free_port()
    env = os.environ.copy()
    env.update({
        "AWS_ENDPOINT_URL": LOCALSTACK_URL,
        "AWS_ACCESS_KEY_ID": "test",
        "AWS_SECRET_ACCESS_KEY": "test",
        "AWS_DEFAULT_REGION": "us-east-1",
    })
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--port", str(port),
         "--host", "127.0.0.1", "--log-level", "warning"],
        cwd=run_dir / "app", env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 25
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("app exited: " + (proc.stdout.read() or "")[-1500:])
        try:
            urllib.request.urlopen(base + "/health", timeout=2)
            return proc, base
        except urllib.error.HTTPError:
            return proc, base
        except Exception:
            time.sleep(0.5)
    proc.terminate()
    raise RuntimeError("app failed to become ready")


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def make_alert_inbox(topic_match):
    """Create a fresh SQS queue subscribed to the first topic whose ARN
    contains topic_match; returns (sqs_client, queue_url)."""
    sns = boto3.client("sns", **AWS_KW)
    sqs = boto3.client("sqs", **AWS_KW)
    topics = [t["TopicArn"] for t in sns.list_topics()["Topics"]
              if topic_match in t["TopicArn"]]
    if not topics:
        raise RuntimeError(f"no SNS topic matching {topic_match!r}")
    queue_url = sqs.create_queue(QueueName="behavioral-probe-inbox")["QueueUrl"]
    queue_arn = sqs.get_queue_attributes(
        QueueUrl=queue_url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    sns.subscribe(TopicArn=topics[0], Protocol="sqs", Endpoint=queue_arn)
    return sqs, queue_url, topics[0]


def drain_inbox(sqs, queue_url, wait=4):
    msgs = []
    deadline = time.time() + wait
    while time.time() < deadline:
        got = sqs.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=10,
                                  WaitTimeSeconds=1).get("Messages", [])
        for m in got:
            msgs.append(m["Body"][:400])
            sqs.delete_message(QueueUrl=queue_url, ReceiptHandle=m["ReceiptHandle"])
        if msgs and not got:
            break
    return msgs


def check(steps, name, passed, detail):
    steps.append({"check": name, "passed": bool(passed), "detail": str(detail)[:300]})
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}: {str(detail)[:120]}")


def s5_stock(base, steps):
    code, _ = request("POST", base, "/products",
                      {"sku": "W-1", "name": "Widget", "price": 9.5, "quantity": 10})
    check(steps, "create product", code in (200, 201), f"HTTP {code}")
    code, body = request("POST", base, "/products/W-1/adjust-stock", {"delta": -3})
    check(steps, "adjust stock -3 accepted", code == 200, f"HTTP {code} {body}")
    code, body = request("GET", base, "/products/W-1")
    check(steps, "quantity now 7", body.get("quantity") == 7, body.get("quantity"))
    code, body = request("POST", base, "/products/W-1/adjust-stock", {"delta": -100})
    check(steps, "over-draw handled (4xx or floor)", code < 500,
          f"HTTP {code} {body}")


def m4_capacity(base, steps):
    code, ev = request("POST", base, "/events",
                       {"title": "Demo", "date": "2026-09-15", "capacity": 2})
    event_id = ev.get("event_id") or ev.get("id")
    check(steps, "create event cap=2", code in (200, 201) and event_id, ev)
    codes = []
    for i in range(3):
        code, body = request(
            "POST", base, f"/events/{event_id}/registrations",
            {"attendee_name": f"Person {i}", "attendee_email": f"p{i}@example.com"})
        codes.append(code)
    check(steps, "first two registrations accepted",
          all(c in (200, 201) for c in codes[:2]), codes)
    check(steps, "third rejected (capacity)", codes[2] >= 400, codes)
    code, listing = request("GET", base, f"/events/{event_id}/registrations")
    if isinstance(listing, list):
        count = len(listing)
    else:
        count = len(listing.get("items") or listing.get("registrations") or [])
    check(steps, "exactly 2 stored", count == 2, count)


def m6_alert(base, steps):
    sqs, inbox, topic = make_alert_inbox("low-rating")
    request("POST", base, "/feedback",
            {"product_id": "p1", "rating": 5, "comment": "great"})
    quiet = drain_inbox(sqs, inbox, wait=3)
    check(steps, "rating 5 -> no alert", len(quiet) == 0, f"{len(quiet)} messages")
    request("POST", base, "/feedback",
            {"product_id": "p1", "rating": 1, "comment": "awful"})
    alerts = drain_inbox(sqs, inbox, wait=6)
    check(steps, "rating 1 -> SNS alert published", len(alerts) >= 1,
          alerts[0] if alerts else "no message")


def c5_threshold(base, steps):
    sqs, inbox, topic = make_alert_inbox("alert")
    code, _ = request("POST", base, "/devices",
                      {"device_id": "dev1", "name": "Sensor",
                       "threshold_celsius": 30.0})
    check(steps, "register device thr=30", code in (200, 201), f"HTTP {code}")
    code, body = request("POST", base, "/readings",
                         {"device_id": "dev1", "temperature_celsius": 25.0})
    below = drain_inbox(sqs, inbox, wait=3)
    check(steps, "25C -> no alert",
          len(below) == 0 and not body.get("alert_triggered"),
          f"alert_triggered={body.get('alert_triggered')} msgs={len(below)}")
    code, body = request("POST", base, "/readings",
                         {"device_id": "dev1", "temperature_celsius": 35.0})
    above = drain_inbox(sqs, inbox, wait=6)
    check(steps, "35C -> alert fires",
          bool(body.get("alert_triggered")) and len(above) >= 1,
          f"alert_triggered={body.get('alert_triggered')} msgs={len(above)}")
    code, stats = request("GET", base, "/devices/dev1/stats/daily")
    text = json.dumps(stats)
    check(steps, "daily stats reflect 25/35",
          code == 200 and "25" in text and "35" in text, text[:200])


def c6_idempotency(base, steps):
    sqs, inbox, topic = make_alert_inbox("upgrade")
    code, cust = request("POST", base, "/customers",
                         {"email": "ada@example.com", "name": "Ada"})
    cid = cust.get("customer_id") or cust.get("id")
    check(steps, "create customer", code in (200, 201) and cid, cust)
    code1, _ = request("POST", base, "/purchases",
                       {"idempotency_key": "K1", "customer_id": cid,
                        "amount_cents": 15000})
    code2, dup = request("POST", base, "/purchases",
                         {"idempotency_key": "K1", "customer_id": cid,
                          "amount_cents": 15000})
    check(steps, "duplicate K1 not re-queued as new",
          code1 in (200, 201, 202) and code2 < 500, f"{code1}/{code2} {dup}")
    request("POST", base, "/admin/process-queue")
    time.sleep(1)
    request("POST", base, "/admin/process-queue")
    code, bal = request("GET", base, f"/customers/{cid}/balance")
    points = bal.get("points") or bal.get("balance") or bal.get("points_balance")
    check(steps, "balance 150 once, not 300 (dedup)", points == 150, bal)
    request("POST", base, "/purchases",
            {"idempotency_key": "K2", "customer_id": cid, "amount_cents": 90000})
    request("POST", base, "/admin/process-queue")
    time.sleep(1)
    code, bal = request("GET", base, f"/customers/{cid}/balance")
    points = bal.get("points") or bal.get("balance") or bal.get("points_balance")
    check(steps, "balance 1050 after K2", points == 1050, bal)
    upgrades = drain_inbox(sqs, inbox, wait=6)
    code, cust = request("GET", base, f"/customers/{cid}")
    check(steps, "gold tier + SNS on crossing 1000",
          cust.get("tier") == "gold" and len(upgrades) >= 1,
          f"tier={cust.get('tier')} msgs={len(upgrades)}")


SCENARIOS = {
    "s5-stock": s5_stock,
    "m4-capacity": m4_capacity,
    "m6-alert": m6_alert,
    "c5-threshold": c5_threshold,
    "c6-idempotency": c6_idempotency,
}


def main():
    scenario = sys.argv[1]
    run_id = SCENARIO_RUNS[scenario]
    run_dir = ROOT / "runs" / run_id
    print(f"=== {scenario} against {run_id}")
    proc, base = launch_app(run_dir)
    steps = []
    try:
        SCENARIOS[scenario](base, steps)
    finally:
        proc.terminate()
    result = {
        "run_id": run_id,
        "scenario": scenario,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "localstack_url": LOCALSTACK_URL,
        "procedure": "operator-directed batch pass per problems-log P19",
        "checks": steps,
        "all_passed": all(s["passed"] for s in steps),
    }
    (run_dir / "behavioral_check.json").write_text(json.dumps(result, indent=2))
    print(f"=> {sum(s['passed'] for s in steps)}/{len(steps)} passed; saved "
          f"runs/{run_id}/behavioral_check.json")


if __name__ == "__main__":
    main()
