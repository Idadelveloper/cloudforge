"""Validation and deployment tooling wrappers (proposal phases 2 and 4).

App code:   flake8 (syntax/style), pytest (functional), Bandit (security)
Terraform:  terraform validate (syntax/schema), Checkov (security policies)
Deployment: tflocal against LocalStack (semantic/runtime verification)

Each wrapper returns a ValidationResult dict so the pipeline can gate on it,
feed the raw output back to the LLM for self-correction, and preserve it for
the failure-taxonomy analysis.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from .state import ValidationResult

VENV_BIN = Path(sys.executable).parent
REPO_ROOT = Path(__file__).resolve().parent.parent
TF_PLUGIN_CACHE = REPO_ROOT / ".tfcache"

LOCALSTACK_URL = os.environ.get("LOCALSTACK_URL", "http://localhost:4566")

MAX_OUTPUT_CHARS = 6000  # keep correction prompts bounded


def _tool(name: str) -> str:
    """Prefer the venv's copy of a tool, fall back to PATH."""
    candidate = VENV_BIN / name
    return str(candidate) if candidate.exists() else name


def _checkov_bin() -> str:
    """Checkov lives in its own venv: it depends on bc-python-hcl2 while
    terraform-local depends on python-hcl2>=8, and both ship the same `hcl2`
    module — they cannot coexist in one environment."""
    override = os.environ.get("CHECKOV_BIN")
    if override:
        return override
    isolated = REPO_ROOT / ".venv-checkov" / "bin" / "checkov"
    if isolated.exists():
        return str(isolated)
    return _tool("checkov")


def _aws_env() -> dict:
    env = os.environ.copy()
    env.update(
        {
            "AWS_ACCESS_KEY_ID": "test",
            "AWS_SECRET_ACCESS_KEY": "test",
            "AWS_DEFAULT_REGION": "us-east-1",
            "AWS_ENDPOINT_URL": LOCALSTACK_URL,
            "TF_IN_AUTOMATION": "1",
            "TF_PLUGIN_CACHE_DIR": str(TF_PLUGIN_CACHE),
        }
    )
    return env


def _run(
    cmd: list[str], cwd: str, timeout: int = 300, max_chars: int = MAX_OUTPUT_CHARS
) -> tuple[int, str, float]:
    TF_PLUGIN_CACHE.mkdir(exist_ok=True)
    start = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_aws_env(),
        )
        output = (proc.stdout + "\n" + proc.stderr).strip()
        return proc.returncode, output[:max_chars], time.time() - start
    except subprocess.TimeoutExpired:
        return 124, f"{cmd[0]} timed out after {timeout}s", time.time() - start
    except FileNotFoundError:
        return 127, f"{cmd[0]} is not installed or not on PATH", time.time() - start


def _result(
    iteration: int, target: str, tool: str, rc: int, output: str, duration: float
) -> ValidationResult:
    return {
        "iteration": iteration,
        "target": target,
        "tool": tool,
        "passed": rc == 0,
        "output": output,
        "duration_s": round(duration, 2),
    }


# ---------------------------------------------------------------------------
# Application-code validators
# ---------------------------------------------------------------------------


def run_flake8(app_dir: str, iteration: int) -> ValidationResult:
    rc, out, dt = _run(
        [_tool("flake8"), "--max-line-length", "120", "."], cwd=app_dir, timeout=120
    )
    return _result(iteration, "app", "flake8", rc, out or "clean", dt)


def run_pytest(app_dir: str, iteration: int) -> ValidationResult:
    rc, out, dt = _run(
        [sys.executable, "-m", "pytest", "-q", "--no-header"],
        cwd=app_dir,
        timeout=240,
    )
    return _result(iteration, "app", "pytest", rc, out, dt)


def run_bandit(app_dir: str, iteration: int) -> ValidationResult:
    rc, out, dt = _run(
        [
            _tool("bandit"),
            "-r",
            ".",
            "-x",
            "./tests",
            "--severity-level",
            "medium",
            "-q",
        ],
        cwd=app_dir,
        timeout=120,
    )
    return _result(iteration, "app", "bandit", rc, out or "clean", dt)


# ---------------------------------------------------------------------------
# IaC validators
# ---------------------------------------------------------------------------


def run_terraform_validate(infra_dir: str, iteration: int) -> ValidationResult:
    rc, out, dt = _run(
        ["terraform", "init", "-backend=false", "-input=false", "-no-color"],
        cwd=infra_dir,
        timeout=300,
    )
    if rc != 0:
        return _result(iteration, "iac", "terraform validate", rc, out, dt)
    rc2, out2, dt2 = _run(
        ["terraform", "validate", "-no-color"], cwd=infra_dir, timeout=120
    )
    return _result(iteration, "iac", "terraform validate", rc2, out2, dt + dt2)


def run_checkov(infra_dir: str, iteration: int, blocking: bool) -> ValidationResult:
    # Checkov's JSON must be parsed IN FULL: truncating it first breaks
    # json.loads, which the tool-error path then misreads as a crash and
    # silently un-gates security (problems log P15). The stored summary is
    # still capped below; only the parse input gets the high ceiling.
    rc, out, dt = _run(
        [
            _checkov_bin(),
            "-d",
            ".",
            "--framework",
            "terraform",
            "--quiet",
            "--compact",
            "-o",
            "json",
        ],
        cwd=infra_dir,
        timeout=600,
        max_chars=2_000_000,
    )
    summary_text = out
    failed = None
    try:
        data = json.loads(out)
        reports = data if isinstance(data, list) else [data]
        failed_checks = []
        passed_count = 0
        skipped_count = 0
        for report in reports:
            summary = report.get("summary", {})
            passed_count += summary.get("passed", 0)
            skipped_count += summary.get("skipped", 0)
            for check in report.get("results", {}).get("failed_checks", []):
                failed_checks.append(
                    f"{check.get('check_id')} {check.get('check_name')} "
                    f"on {check.get('resource')} ({check.get('file_path')})"
                )
        failed = len(failed_checks)
        # Skips are usually the model exempting itself via inline
        # `checkov:skip` comments — security-relevant evidence (RQ3), so
        # they must stay visible in the recorded summary.
        lines = [
            f"Checkov: {passed_count} passed, {failed} failed, "
            f"{skipped_count} skipped."
        ]
        lines.extend(failed_checks[:40])
        summary_text = "\n".join(lines)
    except (json.JSONDecodeError, AttributeError, TypeError):
        pass  # keep raw output if the JSON shape is unexpected

    if failed is None and rc != 0:
        # Checkov itself crashed (bad install, import error, timeout). That is
        # an environment fault, not a generation fault — recording it as a
        # failure would burn bounded correction iterations on an error no HCL
        # change can fix. Record it as non-gating with the crash preserved.
        return _result(
            iteration,
            "iac",
            "checkov (tool error — not gated)",
            0,
            summary_text[:MAX_OUTPUT_CHARS],
            dt,
        )

    gate_rc = 0 if (failed == 0 or not blocking) else (rc or 1)
    label = "checkov" if blocking else "checkov (advisory)"
    return _result(iteration, "iac", label, gate_rc, summary_text[:MAX_OUTPUT_CHARS], dt)


# ---------------------------------------------------------------------------
# LocalStack deployment (phase 4)
# ---------------------------------------------------------------------------


def localstack_running() -> bool:
    try:
        with urllib.request.urlopen(f"{LOCALSTACK_URL}/_localstack/health", timeout=3):
            return True
    except Exception:
        return False


def run_tflocal_deploy(infra_dir: str, iteration: int) -> ValidationResult:
    rc, out, dt = _run(
        [_tool("tflocal"), "init", "-input=false", "-no-color"],
        cwd=infra_dir,
        timeout=300,
    )
    if rc != 0:
        return _result(iteration, "deploy", "tflocal init", rc, out, dt)
    rc2, out2, dt2 = _run(
        [_tool("tflocal"), "apply", "-auto-approve", "-input=false", "-no-color"],
        cwd=infra_dir,
        timeout=600,
    )
    return _result(iteration, "deploy", "tflocal apply", rc2, out2, dt + dt2)
