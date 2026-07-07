"""CloudForge — Streamlit web interface.

Enter a natural-language specification, watch each pipeline stage execute in
real time, and inspect generated code alongside validation, deployment, and
cost results (proposal section 3.2).
"""

import os
import time
import uuid
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from cloudforge.graph import build_graph
from cloudforge.llm import MODEL, GenerationError, estimate_cost_usd
from cloudforge.validators import LOCALSTACK_URL, localstack_running

load_dotenv()

RUNS_DIR = Path(__file__).resolve().parent / "runs"

EXAMPLE_SPECS = {
    "— choose an example or write your own —": "",
    "Simple: notes CRUD API": (
        "Build a REST API for personal notes. Users can create, list, fetch, "
        "update and delete notes. Each note has a title, body and timestamps. "
        "Notes are stored in DynamoDB."
    ),
    "Moderate: file-sharing API with metadata": (
        "Build a file-sharing backend. Clients can upload files to S3 via "
        "presigned URLs, list their files, and delete them. File metadata "
        "(owner, filename, size, upload time) is tracked in DynamoDB. Include "
        "an endpoint that returns storage usage per owner."
    ),
    "Complex: order-processing service with queue": (
        "Build an order-processing service. A REST API accepts new orders and "
        "writes them to DynamoDB, then publishes an event to an SQS queue for "
        "asynchronous fulfilment. An SNS topic notifies subscribers when an "
        "order status changes. Include endpoints to create orders, get order "
        "status, and list orders by customer."
    ),
}


@st.cache_resource
def get_graph():
    return build_graph()


st.set_page_config(page_title="CloudForge", page_icon="⚒️", layout="wide")
st.title("CloudForge")
st.caption(
    "Automated cloud application generation from natural-language prompts — "
    "symmetric app-code + Terraform generation with a bounded deployment "
    "feedback loop."
)

st.warning(
    "**Experimental research artifact.** LLM-generated code and infrastructure "
    "frequently pass automated checks while still diverging from user intent "
    "(the *Correctness–Congruence Gap*, Nekrasov et al., 2025). Review every "
    "artifact before any real-world use; deployments here target only the "
    "sandboxed LocalStack emulator.",
    icon="⚠️",
)

# ---------------------------------------------------------------------------
# Sidebar — run configuration
# ---------------------------------------------------------------------------

with st.sidebar:
    st.header("Configuration")
    st.markdown(f"**Model:** `{MODEL}`")

    api_key_ok = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if api_key_ok:
        st.success("ANTHROPIC_API_KEY found", icon="🔑")
    else:
        st.info(
            "ANTHROPIC_API_KEY not set — the SDK will fall back to an "
            "`ant auth login` profile if one exists.",
            icon="🔑",
        )

    ls_up = localstack_running()
    if ls_up:
        st.success(f"LocalStack running at {LOCALSTACK_URL}", icon="🟢")
    else:
        st.error(
            f"LocalStack not reachable at {LOCALSTACK_URL} — run `./start.sh` "
            "from the project folder (or: `open -a Docker`, then "
            "`docker start cloudforge-localstack`).",
            icon="🔴",
        )

    max_iterations = st.slider(
        "Max self-correction iterations",
        min_value=0,
        max_value=3,
        value=3,
        help="Hard bound on the feedback loop (Temporal Blind Spot safeguard).",
    )
    deploy_enabled = st.toggle(
        "Deploy to LocalStack (phase 4)", value=ls_up, disabled=not ls_up
    )
    checkov_blocking = st.toggle(
        "Checkov failures block the pipeline",
        value=True,
        help="Off = security findings are recorded but don't trigger correction.",
    )

# ---------------------------------------------------------------------------
# Main — specification input
# ---------------------------------------------------------------------------

example = st.selectbox("Example specifications", list(EXAMPLE_SPECS.keys()))
spec = st.text_area(
    "Application specification",
    value=EXAMPLE_SPECS[example],
    height=160,
    placeholder="Describe the cloud application you want, in plain English…",
)

run_clicked = st.button("🚀 Generate & validate", type="primary", disabled=not spec.strip())

if run_clicked:
    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    run_dir = RUNS_DIR / run_id
    initial_state = {
        "spec": spec.strip(),
        "run_id": run_id,
        "run_dir": str(run_dir),
        "max_iterations": max_iterations,
        "deploy_enabled": deploy_enabled,
        "checkov_blocking": checkov_blocking,
        "status": "running",
    }

    graph = get_graph()
    final_state = None
    shown_events = 0
    started = time.time()

    with st.status("Running the CloudForge pipeline…", expanded=True) as status_box:
        try:
            for state_snapshot in graph.stream(
                initial_state, config={"recursion_limit": 60}, stream_mode="values"
            ):
                final_state = state_snapshot
                events = state_snapshot.get("events", [])
                for event in events[shown_events:]:
                    st.write(event)
                shown_events = len(events)
        except GenerationError as exc:
            status_box.update(label="Pipeline error", state="error")
            st.error(str(exc))
            st.stop()

        succeeded = final_state.get("status") == "success"
        status_box.update(
            label=(
                f"Pipeline finished in {time.time() - started:.0f}s — "
                f"{'success ✅' if succeeded else 'failed ❌'}"
            ),
            state="complete" if succeeded else "error",
            expanded=False,
        )

    # -----------------------------------------------------------------------
    # Results
    # -----------------------------------------------------------------------

    tab_plan, tab_app, tab_iac, tab_val, tab_deploy, tab_metrics = st.tabs(
        ["📋 Plan", "🐍 App code", "🏗️ Terraform", "🔎 Validation", "🚀 Deployment", "📊 Metrics"]
    )

    with tab_plan:
        st.json(final_state.get("plan", {}))

    with tab_app:
        for f in final_state.get("app_files", []):
            with st.expander(f["path"], expanded=f["path"].endswith("app.py")):
                st.code(f["content"], language="python")

    with tab_iac:
        for f in final_state.get("iac_files", []):
            with st.expander(f["path"], expanded=f["path"] == "main.tf"):
                st.code(f["content"], language="hcl")

    with tab_val:
        validations = final_state.get("validations", [])
        if validations:
            st.dataframe(
                [
                    {
                        "iteration": v["iteration"],
                        "target": v["target"],
                        "tool": v["tool"],
                        "passed": "✅" if v["passed"] else "❌",
                        "duration (s)": v["duration_s"],
                    }
                    for v in validations
                ],
                width="stretch",
            )
            for v in validations:
                if not v["passed"]:
                    with st.expander(
                        f"❌ iteration {v['iteration']} · {v['target']} · {v['tool']}"
                    ):
                        st.code(v["output"])
        else:
            st.info("No validation results recorded.")

    with tab_deploy:
        if final_state.get("deploy_skipped"):
            st.warning(final_state.get("deploy_output", "Deployment skipped."))
        elif final_state.get("deploy_enabled"):
            if final_state.get("deploy_passed"):
                st.success("Deployed to LocalStack.")
            else:
                st.error("Deployment failed.")
            st.code(final_state.get("deploy_output", ""))
        else:
            st.info("Deployment was disabled for this run.")

    with tab_metrics:
        usage = final_state.get("usage", [])
        total_in = sum(u.get("input_tokens", 0) for u in usage)
        total_out = sum(u.get("output_tokens", 0) for u in usage)
        cache_read = sum(u.get("cache_read_input_tokens", 0) for u in usage)
        col1, col2, col3, col4, col5 = st.columns(5)
        col1.metric("LLM calls", len(usage))
        col2.metric("Input tokens", f"{total_in:,}")
        col3.metric("Output tokens", f"{total_out:,}")
        col4.metric("Cache-read tokens", f"{cache_read:,}")
        col5.metric("Est. cost (USD)", f"${estimate_cost_usd(usage):.3f}")
        st.metric(
            "Correction iterations used",
            f"{final_state.get('iteration', 0)} / {max_iterations}",
        )
        if usage:
            st.dataframe(usage, width="stretch")
        timings = final_state.get("timings", [])
        if timings:
            st.subheader("Stage timings")
            st.dataframe(timings, width="stretch")

    st.info(
        f"📁 All artifacts and `report.json` saved to `runs/{run_id}/` "
        "for the benchmark dataset and failure-taxonomy analysis."
    )
