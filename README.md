# CloudForge

CloudForge takes one natural-language specification and generates two linked
artifacts in parallel: a Python backend and explicit Terraform. The pipeline is
instrumented for dissertation evaluation, not just demo output, so every run
records validation, deployment, timing, and usage data under `runs/<run_id>/`.

## What the pipeline does

1. Parse the specification into a shared structured plan.
2. Generate application code and Terraform in parallel.
3. Validate both sides with automated tools.
4. Feed failing tool output back into a bounded correction loop.
5. Optionally deploy the validated Terraform to LocalStack for a runtime check.

The bounded loop is capped at `3` correction iterations, and deployment errors
feed back into the same loop when phase 4 is enabled.

## Verified stack

These pins were refreshed against upstream package indexes and vendor
documentation on **August 30, 2026**:

| Component | Version |
|---|---|
| Anthropic Python SDK | `1.1.0` |
| LangGraph | `1.2.11` |
| Streamlit | `1.62.0` |
| Flask | `3.1.3` |
| FastAPI | `0.141.1` |
| Pydantic | `2.13.5` |
| pytest | `9.1.1` |
| flake8 | `7.3.0` |
| Bandit | `1.9.4` |
| Checkov | `3.3.15` |
| terraform-local (`tflocal`) | `0.26.0` |
| LocalStack image | `localstack/localstack:4.6` |

## Prerequisites

- Python `3.13`
- Docker Desktop
- Terraform CLI or OpenTofu on `PATH`
- An Anthropic API key in `.env`

Python `3.13` is the recommended project interpreter because it was the
cleanest reproducible path for the repository checks and the isolated Checkov
setup verified locally on August 30, 2026.

Local validation for this refresh was performed with Python `3.13.15`,
Terraform `1.15.8`, and Docker `29.5.3` on August 30, 2026.

## Setup

Create the main environment:

```bash
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Create the isolated Checkov environment:

```bash
python3.13 -m venv .venv-checkov
.venv-checkov/bin/python -m pip install --upgrade pip
.venv-checkov/bin/python -m pip install -r requirements-checkov.txt
```

Checkov is isolated on purpose. `terraform-local` depends on `python-hcl2`,
while Checkov depends on `bc-python-hcl2`, and both expose the `hcl2` module.
Keeping them in separate virtual environments avoids import collisions.

Add your API key:

```bash
cp .env.example .env
```

Then set `ANTHROPIC_API_KEY` inside `.env`.

Create the LocalStack container once:

```bash
open -a Docker
docker run -d --name cloudforge-localstack -p 4566:4566 \
  -v /var/run/docker.sock:/var/run/docker.sock localstack/localstack:4.6
```

After that, reuse it with `docker start cloudforge-localstack` and stop it with
`docker stop cloudforge-localstack`.

> Do not use `localstack start -d`. The current LocalStack CLI flow is account
> oriented, while this project is pinned to a direct Docker-based Community
> image setup for reproducible local evaluation.

## Run the app

The easiest path is:

```bash
./start.sh
```

That script checks Docker, starts the pinned LocalStack container if needed,
and launches Streamlit.

Manual launch:

```bash
open -a Docker
docker start cloudforge-localstack
.venv/bin/streamlit run app.py
```

## Test the repository

Run the repository checks from the project root:

```bash
.venv/bin/python -m pytest -q
.venv/bin/flake8 app.py cloudforge scripts tests --max-line-length 120
.venv/bin/python -m compileall app.py cloudforge scripts tests
```

For dissertation evaluation runs:

1. Start with a benchmark spec and set `max_iterations = 0` for the zero-shot baseline.
2. Rerun the same spec with `max_iterations = 3` for the bounded correction condition.
3. Record whether validation passes, whether LocalStack deployment passes, and how the generated app and Terraform match the benchmark checklist.
4. Use `.venv/bin/python scripts/complexity_metrics.py` to summarise completed run folders.

## Project layout

```text
app.py                  Streamlit interface and evaluation controls
benchmark/specs.yaml    Pre-registered benchmark suite
cloudforge/
  graph.py              LangGraph wiring
  llm.py                Claude SDK wrapper and cost accounting
  nodes.py              Pipeline stages and routing
  prompts.py            Prompt templates and JSON schemas
  report.py             report.json writer and failure tagging
  state.py              Shared typed state
  validators.py         Validation and deployment tool wrappers
scripts/
  complexity_metrics.py Run-summary helper
tests/                  Repository-level tests
runs/                   Generated artifacts and reports
```

CloudForge is an experimental research artifact. Generated output can pass
automated checks and still diverge from user intent, so keep all deployments
inside LocalStack unless you have manually reviewed the result.
