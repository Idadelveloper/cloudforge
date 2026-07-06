# ⚒️ CloudForge

**Automated Cloud Application Generation from Natural Language Prompts Using
Large Language Models** — INM373 final project artifact.

CloudForge takes a single natural-language specification and *symmetrically*
generates both the application backend (Python Flask/FastAPI) and its
Infrastructure as Code (Terraform), linked through a shared, bounded
deployment feedback loop. It empirically tests whether providing functional
application context to the LLM mitigates the **Correctness–Congruence Gap**
(Nekrasov et al., 2025) and prevents **Temporal Blind Spot Failures**
(Burton, 2026).

## Pipeline (LangGraph explicit cyclic state machine)

1. **Prompt parsing & parallel generation** — the spec is parsed into a shared
   structured plan, then routed to two parallel Claude generation nodes: one
   produces the Python backend, the other Terraform.
2. **Automated validation** — app code: flake8, pytest, Bandit; Terraform:
   `terraform validate`, Checkov.
3. **Conditional self-correction** — failing tool logs are fed back to the
   generation nodes alongside the original spec, bounded to **3 iterations**.
4. **Deployment verification** — validated Terraform is deployed to
   [LocalStack](https://localstack.cloud) via `tflocal`, catching semantic and
   runtime failures static linters miss. Deploy errors also feed the loop.

Every run writes `runs/<run_id>/` with the generated `app/` and `infra/`
directories plus `report.json` (validation results per iteration, token
usage/cost, timings, and first-pass failure-taxonomy tags) — the raw data for
the 15–25-spec benchmark and error-taxonomy analysis.

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env        # add your ANTHROPIC_API_KEY

# prerequisites for phase 4
brew install terraform      # or opentofu

# LocalStack: run the pinned community image directly via Docker.
# (The 2026 LocalStack CLI requires an account; the 4.x community image
# does not, which also keeps the research setup reproducible.)
open -a Docker              # make sure the Docker daemon is running
docker run -d --name cloudforge-localstack -p 4566:4566 \
  -v /var/run/docker.sock:/var/run/docker.sock localstack/localstack:4.6

# later: docker start cloudforge-localstack / docker stop cloudforge-localstack
```

## Run

```bash
venv/bin/streamlit run app.py
```

Enter a specification (three tiered examples are built in), watch each stage
stream live, then inspect the plan, generated code, validation table,
deployment log, and cost metrics.

## Project layout

```
app.py                  Streamlit interface
cloudforge/
  state.py              Typed LangGraph state (reducers for parallel fan-in)
  prompts.py            Few-shot / CoT prompt templates + output schemas
  llm.py                Claude API wrapper (streaming, structured outputs,
                        adaptive thinking, prompt caching, cost accounting)
  validators.py         flake8 / pytest / Bandit / terraform / Checkov / tflocal
  nodes.py              Pipeline nodes (4 phases + bounded correction)
  graph.py              StateGraph wiring
  report.py             report.json writer + failure-taxonomy auto-tagging
runs/                   Per-run artifacts (gitignored)
```

> ⚠️ **Experimental research artifact.** Generated outputs regularly pass
> automated checks while diverging from user intent. All deployments target
> the sandboxed LocalStack emulator only; never apply generated Terraform to
> a real AWS account without review.
