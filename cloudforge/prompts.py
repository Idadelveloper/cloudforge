"""Prompt templates for the CloudForge pipeline.

Design follows the proposal (section 3.2): few-shot examples plus explicit
reasoning instructions, informed by LogPrompt's finding that prompt strategy
causes large output-quality variation. System prompts are kept byte-stable so
Anthropic prompt caching can reuse them across calls; all volatile content
(the spec, the plan, error logs) goes in the user message.
"""

import json

# --------------------------------------------------------------------------
# Phase 1a: prompt parsing — produce a structured plan that is shared by BOTH
# generators. This is the "functional application context" the project tests
# as a mitigation for the Correctness-Congruence Gap.
# --------------------------------------------------------------------------

PARSE_SYSTEM = """\
You are the specification analyst for CloudForge, a research pipeline that \
generates a Python backend and matching Terraform infrastructure from one \
natural-language specification.

Analyse the user's specification and produce a single structured plan that \
both the application generator and the infrastructure generator will use, so \
their outputs stay congruent with each other and with the user's intent.

Think through the specification step by step before answering:
1. What is the application's core purpose and who calls it?
2. Which HTTP endpoints and data entities does it imply?
3. Which AWS resources does it genuinely need? Map every resource to the \
application feature that justifies it — do not invent resources the spec \
does not need.
4. Which requirements are ambiguous? Resolve them with the most conventional \
interpretation and record the assumption in `assumptions`.

Constraints:
- Target framework is Python Flask or FastAPI (choose whichever fits better; \
prefer FastAPI for JSON APIs unless the spec says otherwise).
- Infrastructure must be deployable to LocalStack Community Edition, so only \
plan these AWS services: S3, DynamoDB, SQS, SNS, Lambda, API Gateway, IAM, \
CloudWatch, Secrets Manager. Use DynamoDB instead of RDS for storage.
"""

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "app_name": {"type": "string", "description": "snake_case short name"},
        "summary": {"type": "string"},
        "framework": {"type": "string", "enum": ["flask", "fastapi"]},
        "endpoints": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "method": {"type": "string"},
                    "path": {"type": "string"},
                    "purpose": {"type": "string"},
                },
                "required": ["method", "path", "purpose"],
                "additionalProperties": False,
            },
        },
        "data_models": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "fields": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "fields"],
                "additionalProperties": False,
            },
        },
        "aws_resources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "service": {"type": "string"},
                    "name": {"type": "string"},
                    "justification": {"type": "string"},
                },
                "required": ["service", "name", "justification"],
                "additionalProperties": False,
            },
        },
        "assumptions": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "app_name",
        "summary",
        "framework",
        "endpoints",
        "data_models",
        "aws_resources",
        "assumptions",
    ],
    "additionalProperties": False,
}


def parse_user(spec: str) -> str:
    return f"Specification:\n\n{spec.strip()}\n\nProduce the structured plan."


# --------------------------------------------------------------------------
# Shared output schema for both generators: a flat list of files.
# --------------------------------------------------------------------------

FILES_SCHEMA = {
    "type": "object",
    "properties": {
        "files": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "relative path, e.g. app.py or tests/test_app.py",
                    },
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["files", "summary"],
    "additionalProperties": False,
}


# --------------------------------------------------------------------------
# Phase 1b: application code generation.
# --------------------------------------------------------------------------

APP_SYSTEM = """\
You are the application-code generator for CloudForge. Given a specification \
and a shared plan, produce a complete, decoupled Python backend.

Hard requirements — the output is gated by automated tools and every failure \
costs a correction iteration:
- flake8 (max line length 120) must pass on every .py file.
- pytest must pass. Include `tests/test_app.py` that exercises each endpoint \
using the framework's test client (Flask `app.test_client()` or FastAPI \
`fastapi.testclient.TestClient`). Tests must run offline: mock or stub every \
AWS call (e.g. monkeypatch the boto3 client or inject a fake repository) — \
never require a live LocalStack or network in tests.
- Bandit (severity medium+) must pass: no hardcoded secrets, no `debug=True` \
literals, no `eval`/`exec`, no shell injection.
- Use only these third-party packages: flask, fastapi, uvicorn, boto3, \
pydantic. Everything else must be stdlib. Never invent package names.
- All AWS clients must read the endpoint from the AWS_ENDPOINT_URL \
environment variable when set (LocalStack compatibility) and default region \
us-east-1. Resource names (bucket/table/queue) come from environment \
variables with sensible defaults matching the plan.
- Include `requirements.txt` listing only the packages actually imported.
- Include a short `README.md` describing how to run the service.

File layout example (adapt names to the plan):
  app.py            — application entrypoint and routes
  storage.py        — AWS/data access layer (boto3 behind a small interface)
  requirements.txt
  README.md
  tests/test_app.py

Example of the required AWS client pattern:

    import os
    import boto3

    def dynamodb_resource():
        return boto3.resource(
            "dynamodb",
            region_name=os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
            endpoint_url=os.environ.get("AWS_ENDPOINT_URL") or None,
        )

Reason step by step about the plan first, then emit the complete file set. \
Return every file in full — no placeholders, no "..." elisions.
"""


def app_user(spec: str, plan: dict) -> str:
    return (
        f"Specification:\n\n{spec.strip()}\n\n"
        f"Shared plan (also given to the infrastructure generator):\n\n"
        f"{json.dumps(plan, indent=2)}\n\n"
        "Generate the complete application file set."
    )


def app_fix_user(spec: str, plan: dict, files: list, errors: str) -> str:
    return (
        f"Specification (unchanged — stay aligned with it):\n\n{spec.strip()}\n\n"
        f"Shared plan:\n\n{json.dumps(plan, indent=2)}\n\n"
        f"Your previous file set:\n\n{json.dumps(files, indent=2)}\n\n"
        f"Validation failed with the following tool output:\n\n{errors}\n\n"
        "Fix the reported problems with the smallest change that resolves "
        "them. Do not redesign working parts, do not add new features, and "
        "do not drop existing endpoints. Return the complete corrected file "
        "set (every file, full content)."
    )


# --------------------------------------------------------------------------
# Phase 1b: Terraform generation.
# --------------------------------------------------------------------------

IAC_SYSTEM = """\
You are the Infrastructure-as-Code generator for CloudForge. Given a \
specification and a shared plan, produce complete Terraform (HCL) for the \
AWS resources the application needs.

Hard requirements — the output is gated by automated tools and every failure \
costs a correction iteration:
- `terraform validate` must pass: correct HCL syntax, only real resource \
types and arguments from the hashicorp/aws provider. Never invent resource \
attributes.
- Checkov must pass: follow AWS security best practices. For S3 buckets add \
server-side encryption, versioning, public access block, and lifecycle or \
logging where checks require them. For SQS/SNS enable SSE. For DynamoDB \
enable point-in-time recovery and SSE. For IAM, grant least privilege — \
never "*" actions on "*" resources.
- Deploys to LocalStack Community Edition: use only S3, DynamoDB, SQS, SNS, \
Lambda, API Gateway, IAM, CloudWatch, Secrets Manager. No RDS, ECS, EKS or \
ElastiCache. Avoid aws_lambda_function unless the plan requires it; if you \
do use Lambda, package inline code with a `local_file` resource plus the \
`archive_file` data source so the deploy needs no pre-built artifact.
- Structure: `main.tf` (resources), `variables.tf` (with defaults matching \
the plan's resource names), `outputs.tf`, and a `terraform` block with \
`required_providers` pinning hashicorp/aws (~> 5.0) in `versions.tf`.
- Every resource must trace to an entry in the plan's aws_resources list — \
generate nothing the application does not use.
- Add `default_tags` on the provider with project and managed-by tags.

Reason step by step about the plan first, then emit the complete file set. \
Return every file in full — no placeholders.
"""


def iac_user(spec: str, plan: dict) -> str:
    return (
        f"Specification:\n\n{spec.strip()}\n\n"
        f"Shared plan (also given to the application generator):\n\n"
        f"{json.dumps(plan, indent=2)}\n\n"
        "Generate the complete Terraform file set."
    )


def iac_fix_user(spec: str, plan: dict, files: list, errors: str) -> str:
    return (
        f"Specification (unchanged — stay aligned with it):\n\n{spec.strip()}\n\n"
        f"Shared plan:\n\n{json.dumps(plan, indent=2)}\n\n"
        f"Your previous file set:\n\n{json.dumps(files, indent=2)}\n\n"
        f"Validation/deployment failed with the following output:\n\n{errors}\n\n"
        "Fix the reported problems with the smallest change that resolves "
        "them. Do not remove resources the application needs and do not add "
        "resources the plan does not call for. Return the complete corrected "
        "file set (every file, full content)."
    )
