from cloudforge.report import classify_failure


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
