import json

from cloudforge.report import classify_failure, write_report


def test_classify_failure_marks_missing_modules_as_hallucinations() -> None:
    result = {
        "iteration": 0,
        "target": "app",
        "tool": "pytest",
        "passed": False,
        "output": "ModuleNotFoundError: No module named 'imaginary_sdk'",
        "duration_s": 0.12,
    }

    assert classify_failure(result) == "hallucinated_dependency"


def test_classify_failure_marks_schema_errors_for_terraform_validation() -> None:
    result = {
        "iteration": 0,
        "target": "iac",
        "tool": "terraform validate",
        "passed": False,
        "output": "Error: Unsupported argument",
        "duration_s": 0.3,
    }

    assert classify_failure(result) == "schema_mismatch"


def test_write_report_preserves_benchmark_provenance(tmp_path) -> None:
    state = {
        "run_dir": str(tmp_path),
        "run_id": "test-run",
        "started_at": "2026-08-30T17:00:00+00:00",
        "spec": "Build a notes API",
        "benchmark_id": "S1-notes",
        "tier": "simple",
        "evaluation_condition": "baseline",
        "benchmark_fingerprint": "abc123",
        "benchmark_complexity": {"score": 7},
        "benchmark_checklist": ["DynamoDB table for notes"],
        "max_iterations": 0,
        "iteration": 0,
        "deploy_enabled": True,
        "deploy_skipped": False,
        "deploy_passed": True,
        "deploy_output": "applied",
        "validations": [],
        "usage": [],
        "timings": [],
        "events": [],
        "app_files": [],
        "iac_files": [],
    }

    write_report(state, "success")

    report = json.loads((tmp_path / "report.json").read_text())
    assert report["evaluation_condition"] == "baseline"
    assert report["benchmark"]["complexity"] == {"score": 7}
    assert report["benchmark"]["fingerprint"] == "abc123"
    assert report["repository"]["commit"]
    assert report["started_at"] == "2026-08-30T17:00:00+00:00"
    assert isinstance(report["wall_seconds"], float)
    assert report["wall_seconds"] > 0
    assert report["node_seconds_sum"] == 0
