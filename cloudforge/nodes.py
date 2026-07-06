"""LangGraph nodes for the four-phase CloudForge pipeline.

Phase 1: parse_spec -> (generate_app || generate_iac)   parallel superstep
Phase 2: (validate_app || validate_iac) -> assess       parallel superstep
Phase 3: assess -> correct -> regenerate failing side(s), bounded iterations
Phase 4: deploy (tflocal -> LocalStack) -> finalize
"""

import shutil
import time
from pathlib import Path

from . import prompts, validators
from .llm import generate_json
from .report import write_report
from .state import CloudForgeState, GeneratedFile


def _write_files(base_dir: str, files: list[GeneratedFile]) -> None:
    """Materialise generated files, refusing paths that escape the run dir."""
    base = Path(base_dir).resolve()
    if base.exists():
        shutil.rmtree(base)
    base.mkdir(parents=True)
    for f in files:
        target = (base / f["path"]).resolve()
        if not target.is_relative_to(base):
            raise ValueError(f"Generated file escapes its sandbox: {f['path']}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f["content"])


def _truncate(text: str, limit: int = 6000) -> str:
    return text if len(text) <= limit else text[:limit] + "\n... [truncated]"


# ---------------------------------------------------------------------------
# Phase 1a — prompt parsing
# ---------------------------------------------------------------------------


def parse_spec(state: CloudForgeState) -> dict:
    start = time.time()
    plan, usage = generate_json(
        prompts.PARSE_SYSTEM,
        prompts.parse_user(state["spec"]),
        prompts.PLAN_SCHEMA,
        purpose="specification parsing",
    )
    resources = ", ".join(
        f"{r['service']}:{r['name']}" for r in plan.get("aws_resources", [])
    )
    return {
        "plan": plan,
        "iteration": 0,
        "status": "running",
        "usage": [usage],
        "timings": [{"node": "parse_spec", "seconds": round(time.time() - start, 1)}],
        "events": [
            f"🧭 Parsed specification → {plan['app_name']} "
            f"({plan['framework']}); AWS resources: {resources or 'none'}"
        ],
    }


# ---------------------------------------------------------------------------
# Phase 1b — parallel generation
# ---------------------------------------------------------------------------


def generate_app(state: CloudForgeState) -> dict:
    start = time.time()
    iteration = state["iteration"]
    if iteration == 0:
        user = prompts.app_user(state["spec"], state["plan"])
        label = "initial"
    else:
        user = prompts.app_fix_user(
            state["spec"], state["plan"], state["app_files"], state["app_errors"]
        )
        label = f"correction {iteration}"
    data, usage = generate_json(
        prompts.APP_SYSTEM, user, prompts.FILES_SCHEMA, purpose="application code"
    )
    _write_files(str(Path(state["run_dir"]) / "app"), data["files"])
    return {
        "app_files": data["files"],
        "usage": [usage],
        "timings": [{"node": "generate_app", "seconds": round(time.time() - start, 1)}],
        "events": [
            f"🐍 Generated application code ({label}): "
            f"{len(data['files'])} files — {data['summary']}"
        ],
    }


def generate_iac(state: CloudForgeState) -> dict:
    start = time.time()
    iteration = state["iteration"]
    if iteration == 0:
        user = prompts.iac_user(state["spec"], state["plan"])
        label = "initial"
    else:
        user = prompts.iac_fix_user(
            state["spec"], state["plan"], state["iac_files"], state["iac_errors"]
        )
        label = f"correction {iteration}"
    data, usage = generate_json(
        prompts.IAC_SYSTEM, user, prompts.FILES_SCHEMA, purpose="Terraform"
    )
    _write_files(str(Path(state["run_dir"]) / "infra"), data["files"])
    return {
        "iac_files": data["files"],
        "usage": [usage],
        "timings": [{"node": "generate_iac", "seconds": round(time.time() - start, 1)}],
        "events": [
            f"🏗️ Generated Terraform ({label}): "
            f"{len(data['files'])} files — {data['summary']}"
        ],
    }


# ---------------------------------------------------------------------------
# Phase 2 — parallel validation
# ---------------------------------------------------------------------------


def validate_app(state: CloudForgeState) -> dict:
    start = time.time()
    app_dir = str(Path(state["run_dir"]) / "app")
    iteration = state["iteration"]
    results = [
        validators.run_flake8(app_dir, iteration),
        validators.run_pytest(app_dir, iteration),
        validators.run_bandit(app_dir, iteration),
    ]
    passed = all(r["passed"] for r in results)
    errors = "\n\n".join(
        f"--- {r['tool']} ---\n{_truncate(r['output'])}"
        for r in results
        if not r["passed"]
    )
    verdicts = ", ".join(
        f"{r['tool']} {'✅' if r['passed'] else '❌'}" for r in results
    )
    return {
        "validations": results,
        "app_passed": passed,
        "app_errors": errors,
        "timings": [{"node": "validate_app", "seconds": round(time.time() - start, 1)}],
        "events": [f"🔎 App validation (iteration {iteration}): {verdicts}"],
    }


def validate_iac(state: CloudForgeState) -> dict:
    start = time.time()
    infra_dir = str(Path(state["run_dir"]) / "infra")
    iteration = state["iteration"]
    results = [
        validators.run_terraform_validate(infra_dir, iteration),
        validators.run_checkov(
            infra_dir, iteration, blocking=state.get("checkov_blocking", True)
        ),
    ]
    passed = all(r["passed"] for r in results)
    errors = "\n\n".join(
        f"--- {r['tool']} ---\n{_truncate(r['output'])}"
        for r in results
        if not r["passed"]
    )
    verdicts = ", ".join(
        f"{r['tool']} {'✅' if r['passed'] else '❌'}" for r in results
    )
    return {
        "validations": results,
        "iac_passed": passed,
        "iac_errors": errors,
        "timings": [{"node": "validate_iac", "seconds": round(time.time() - start, 1)}],
        "events": [f"🔎 IaC validation (iteration {iteration}): {verdicts}"],
    }


# ---------------------------------------------------------------------------
# Phase 3 — bounded self-correction
# ---------------------------------------------------------------------------


def assess(state: CloudForgeState) -> dict:
    """Fan-in point after parallel validation; routing happens on its edges."""
    if state["app_passed"] and state["iac_passed"]:
        message = "✅ All validations passed."
    elif state["iteration"] >= state["max_iterations"]:
        message = (
            f"🛑 Validation still failing after {state['iteration']} correction "
            f"iteration(s); the {state['max_iterations']}-iteration bound is "
            "reached (Temporal Blind Spot safeguard)."
        )
    else:
        failing = [
            side
            for side, ok in (("app", state["app_passed"]), ("iac", state["iac_passed"]))
            if not ok
        ]
        message = f"♻️ Validation failed for: {', '.join(failing)} — starting correction."
    return {"events": [message]}


def route_after_assess(state: CloudForgeState) -> str:
    if state["app_passed"] and state["iac_passed"]:
        return "deploy" if state.get("deploy_enabled") else "finalize"
    if state["iteration"] < state["max_iterations"]:
        return "correct"
    return "finalize"


def correct(state: CloudForgeState) -> dict:
    """Bump the bounded iteration counter; regeneration targets are routed
    from here so only the failing side(s) are regenerated."""
    iteration = state["iteration"] + 1
    return {
        "iteration": iteration,
        "events": [
            f"🔧 Correction iteration {iteration}/{state['max_iterations']}: "
            "feeding error logs back with the original specification."
        ],
    }


def route_correction_targets(state: CloudForgeState) -> list[str]:
    targets = []
    if not state["app_passed"]:
        targets.append("generate_app")
    if not state["iac_passed"]:
        targets.append("generate_iac")
    return targets or ["generate_app", "generate_iac"]


# ---------------------------------------------------------------------------
# Phase 4 — deployment verification on LocalStack
# ---------------------------------------------------------------------------


def deploy(state: CloudForgeState) -> dict:
    start = time.time()
    infra_dir = str(Path(state["run_dir"]) / "infra")
    if not validators.localstack_running():
        return {
            "deploy_passed": False,
            "deploy_skipped": True,
            "deploy_output": (
                f"LocalStack is not reachable at {validators.LOCALSTACK_URL}. "
                "Start it with `localstack start -d` (or Docker) and rerun."
            ),
            "events": ["⚠️ Deployment skipped — LocalStack is not running."],
        }
    result = validators.run_tflocal_deploy(infra_dir, state["iteration"])
    update = {
        "validations": [result],
        "deploy_passed": result["passed"],
        "deploy_skipped": False,
        "deploy_output": result["output"],
        "timings": [{"node": "deploy", "seconds": round(time.time() - start, 1)}],
        "events": [
            "🚀 Deployed to LocalStack successfully."
            if result["passed"]
            else "💥 LocalStack deployment failed — runtime errors captured."
        ],
    }
    if not result["passed"]:
        # Deployment errors ground the next IaC correction (IaCGen-style).
        update["iac_passed"] = False
        update["iac_errors"] = f"--- tflocal apply ---\n{_truncate(result['output'])}"
    return update


def route_after_deploy(state: CloudForgeState) -> str:
    if state["deploy_passed"] or state.get("deploy_skipped"):
        return "finalize"
    if state["iteration"] < state["max_iterations"]:
        return "correct"
    return "finalize"


# ---------------------------------------------------------------------------
# Finalisation — persist the research artifact
# ---------------------------------------------------------------------------


def finalize(state: CloudForgeState) -> dict:
    success = bool(state.get("app_passed") and state.get("iac_passed"))
    if state.get("deploy_enabled") and not state.get("deploy_skipped"):
        success = success and bool(state.get("deploy_passed"))
    status = "success" if success else "failed"
    report_path = write_report(state, status)
    return {
        "status": status,
        "events": [
            f"🏁 Run finished: {status}. Report and artifacts saved to {report_path}"
        ],
    }
