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
| Anthropic Python SDK | `1.2.0` |
| Generation model | `claude-opus-5` (override with `CLOUDFORGE_MODEL`) |
| LangGraph | `1.2.11` |
| Streamlit | `1.62.0` |
| Flask | `3.1.3` |
| FastAPI | `0.141.1` |
| Pydantic | `2.13.5` |
| pytest | `9.1.1` |
| flake8 | `7.3.0` |
| Bandit | `1.9.4` |
| Checkov | `3.3.16` (isolated in `.venv-checkov`) |
| terraform-local (`tflocal`) | `0.26.0` |
| LocalStack image | `localstack/localstack:4.6` |

The July 2026 pilot runs used `claude-opus-4-8`; that model became a legacy
release during the project window, so the evaluation campaign is frozen on
`claude-opus-5` (same pricing, documented in the problems log).

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

Create the isolated Checkov environment (from the main venv's interpreter, so
the two environments can never disagree about which Python they use):

```bash
.venv/bin/python -m venv .venv-checkov
.venv-checkov/bin/python -m pip install --upgrade pip
.venv-checkov/bin/python -m pip install -r requirements-checkov.txt
```

Checkov is isolated on purpose. `terraform-local` depends on `python-hcl2`,
while Checkov depends on `bc-python-hcl2`, and both expose the `hcl2` module.
Keeping them in separate virtual environments avoids import collisions.

> Troubleshooting: if `.venv-checkov/bin/checkov --version` crashes with a
> stdlib import error (for example `No module named '_posixsubprocess'`), the
> venv's interpreter symlinks have drifted after a system Python upgrade.
> Delete `.venv-checkov` and recreate it with the two commands above — always
> from the same interpreter as the main `.venv`.

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

### Troubleshooting: Docker commands hang / "LocalStack not reachable"

Docker Desktop's backend can enter a wedged state where its processes are
running but the engine socket answers nothing — then **every** `docker`
command (`docker info`, `docker ps`, `docker start …`) blocks forever
(problems log P5, P13). `start.sh` and the reset script now detect this with
a timeout-guarded probe and print the fix instead of hanging. To check and
repair manually:

```bash
curl -s --max-time 3 --unix-socket ~/.docker/run/docker.sock http://localhost/_ping
```

`OK` means the engine is fine. No output means it is wedged; restart it:

```bash
pkill -f com.docker.backend && open -a Docker
```

Wait ~20 seconds, then rerun `./start.sh`. Three more notes:

- A nearly full startup disk can wedge the engine or stop it from booting at
  all. Keep several GB free during evaluation; `docker image prune -a -f`
  reclaims images no container uses (LocalStack's Lambda runtime images,
  `public.ecr.aws/lambda/python:*`, are kept while the container exists —
  leave them, Lambda specs need them).
- The very first `./start.sh` or reset on a machine pulls the ~1.9 GB
  LocalStack image; the scripts show pull progress instead of silence.
- If the app's sidebar still reports LocalStack unreachable, check
  `curl -s http://localhost:4566/_localstack/health` and
  `docker logs --tail 20 cloudforge-localstack`.

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

1. Follow `dissertation/EVALUATION_AND_TESTING_GUIDE.md` and its 36-trial plan rather than choosing an ad-hoc order.
2. Reset the disposable LocalStack container before each primary trial with `./scripts/reset_localstack.sh --confirm`.
3. Run the planned baseline (`max_iterations = 0`) or bounded-correction (`max_iterations = 3`) condition without editing the benchmark prompt.
4. Record the automated report fields, then score app coverage, IaC coverage, and joint congruence in the UI's "Congruence scoring" tab — it saves `congruence.csv` beside the run's `report.json`.
5. Use `.venv/bin/python scripts/complexity_metrics.py --catalogue` for the pre-run complexity table and the default command for completed-run outcomes.

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
  complexity_metrics.py Pre-run catalogue and run-summary helper
  reset_localstack.sh   Guarded clean-emulator reset for evaluation trials
tests/                  Repository-level tests
runs/                   Generated artifacts and reports
dissertation/           Evaluation protocol, logs, and chapter materials
```

CloudForge is an experimental research artifact. Generated output can pass
automated checks and still diverge from user intent, so keep all deployments
inside LocalStack unless you have manually reviewed the result.
