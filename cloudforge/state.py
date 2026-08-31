"""Typed shared state for the CloudForge LangGraph pipeline.

LangGraph models the pipeline as a cyclic state machine over this schema.
Keys written concurrently by parallel branches (events, validations, usage,
timings) use `operator.add` reducers so fan-in merges instead of overwriting.
"""

import operator
from typing import Annotated, Any, TypedDict


class GeneratedFile(TypedDict):
    path: str
    content: str


class ValidationResult(TypedDict):
    iteration: int
    target: str  # "app" | "iac" | "deploy"
    tool: str
    passed: bool
    output: str
    duration_s: float


class CloudForgeState(TypedDict, total=False):
    # ---- inputs ----
    spec: str
    run_id: str
    run_dir: str
    started_at: str  # ISO-8601 UTC; lets the report record true wall-clock time
    benchmark_id: str  # id from benchmark/specs.yaml ("" for ad-hoc specs)
    tier: str  # simple | moderate | complex | ""
    evaluation_condition: str  # baseline | bounded_correction | exploratory_*
    benchmark_complexity: dict[str, Any]  # pre-run Specification Complexity Profile
    benchmark_checklist: list[str]  # pre-registered manual congruence items
    benchmark_fingerprint: str  # SHA-256 of the evaluation-relevant benchmark fields
    max_iterations: int  # bounded self-correction (proposal O2: max 3)
    deploy_enabled: bool
    checkov_blocking: bool  # treat Checkov failures as gating vs advisory

    # ---- phase 1: prompt parsing (shared functional context) ----
    plan: dict[str, Any]

    # ---- phase 1: parallel generation outputs ----
    app_files: list[GeneratedFile]
    iac_files: list[GeneratedFile]

    # ---- phase 2/3: validation + correction feedback ----
    validations: Annotated[list[ValidationResult], operator.add]
    app_passed: bool
    iac_passed: bool
    app_errors: str  # error log fed back to the app generation node
    iac_errors: str  # error log fed back to the IaC generation node

    # ---- phase 4: deployment verification ----
    deploy_passed: bool
    deploy_skipped: bool
    deploy_output: str

    # ---- loop control ----
    iteration: int  # correction rounds used so far (0 = initial attempt)

    # ---- observability / research metrics ----
    events: Annotated[list[str], operator.add]
    usage: Annotated[list[dict], operator.add]  # per-LLM-call token usage
    timings: Annotated[list[dict], operator.add]  # per-node wall time
    status: str  # "running" | "success" | "failed"
