"""CloudForge — Streamlit web interface.

Enter a natural-language specification, watch each pipeline stage execute in
real time, and inspect generated code alongside validation, deployment, and
cost results (proposal section 3.2).
"""

import csv
import io
import json
import os
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components
from dotenv import load_dotenv

from cloudforge.benchmark import (
    benchmark_fingerprint,
    evaluation_condition,
    read_benchmark_specs,
)
from cloudforge.graph import build_graph
from cloudforge.llm import MODEL, GenerationError, estimate_cost_usd
from cloudforge.validators import LOCALSTACK_URL, localstack_running

load_dotenv()

ROOT_DIR = Path(__file__).resolve().parent
RUNS_DIR = ROOT_DIR / "runs"

CUSTOM_OPTION = "— write your own specification —"
SPEC_CHOICE_KEY = "spec_choice"
SPEC_INPUT_KEY = "spec_input"
CUSTOM_DRAFT_KEY = "custom_spec_draft"

# Client-side ticking clock: keeps counting every second even while the
# server thread is busy inside a long LLM generation call.
LIVE_TIMER_HTML = """
<div id="cf-timer"
     style="font-family: -apple-system, 'Segoe UI', sans-serif;
            font-size: 1.05rem; font-weight: 700; color: #1c7c84;
            padding: 0.3rem 0;">
  &#9201; 0:00 elapsed
</div>
<script>
  const cfStart = Date.now();
  const cfEl = document.getElementById("cf-timer");
  setInterval(function () {
    const s = Math.floor((Date.now() - cfStart) / 1000);
    const m = Math.floor(s / 60);
    cfEl.textContent =
      "\\u23F1 " + m + ":" + String(s % 60).padStart(2, "0") + " elapsed";
  }, 1000);
</script>
"""


@st.cache_data
def load_benchmark_specs() -> dict:
    return read_benchmark_specs()


@st.cache_resource
def get_graph():
    return build_graph()


def benchmark_tier_counts(benchmark: dict) -> dict[str, int]:
    counts = Counter(entry.get("tier", "unknown") for entry in benchmark.values())
    return {tier: counts.get(tier, 0) for tier in ("simple", "moderate", "complex")}


def summarize_selected_spec(entry: dict | None) -> dict[str, str | int]:
    if not entry:
        return {
            "benchmark_id": "Custom",
            "tier": "custom",
            "checklist_items": 0,
            "word_count": 0,
            "complexity_score": 0,
            "cloud_components": 0,
            "api_operations": 0,
        }
    profile = entry.get("complexity", {})
    return {
        "benchmark_id": entry["id"],
        "tier": entry["tier"],
        "checklist_items": len(entry.get("congruence_checklist", [])),
        "word_count": len(entry.get("spec", "").split()),
        "complexity_score": profile.get("score", 0),
        "cloud_components": profile.get("cloud_components", 0),
        "api_operations": profile.get("api_operations", 0),
    }


def build_initial_state(
    spec: str,
    run_id: str,
    run_dir: Path,
    max_iterations: int,
    deploy_enabled: bool,
    checkov_blocking: bool,
    selected: dict | None,
) -> dict:
    """Build the initial LangGraph state for a run."""
    return {
        "spec": spec.strip(),
        "run_id": run_id,
        "run_dir": str(run_dir),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "benchmark_id": selected["id"] if selected else "",
        "tier": selected["tier"] if selected else "",
        "evaluation_condition": evaluation_condition(max_iterations),
        "benchmark_complexity": dict(selected.get("complexity", {})) if selected else {},
        "benchmark_checklist": list(selected.get("congruence_checklist", [])) if selected else [],
        "benchmark_fingerprint": benchmark_fingerprint(selected),
        "max_iterations": max_iterations,
        "deploy_enabled": deploy_enabled,
        "checkov_blocking": checkov_blocking,
        "status": "running",
    }


def sync_spec_from_choice(benchmark: dict) -> None:
    """Keep benchmark selections and custom drafts stable across reruns."""
    choice = st.session_state[SPEC_CHOICE_KEY]
    if choice == CUSTOM_OPTION:
        st.session_state[SPEC_INPUT_KEY] = st.session_state.get(CUSTOM_DRAFT_KEY, "")
        return
    selected = benchmark.get(choice)
    if selected:
        st.session_state[SPEC_INPUT_KEY] = selected["spec"].strip()


def render_theme() -> None:
    st.markdown(
        """
        <style>
        :root {
            --cf-ink: #16313d;
            --cf-muted: #4f6572;
            --cf-border: rgba(22, 49, 61, 0.14);
            --cf-cool: #1c7c84;
        }

        .stApp {
            background:
                radial-gradient(circle at top left, rgba(207, 91, 45, 0.14), transparent 34%),
                radial-gradient(circle at top right, rgba(28, 124, 132, 0.16), transparent 28%),
                linear-gradient(180deg, #fbf7ef 0%, #f3ecdf 100%);
            color: var(--cf-ink);
        }

        [data-testid="stSidebar"] {
            background:
                linear-gradient(180deg, rgba(16, 44, 54, 0.97) 0%, rgba(22, 49, 61, 0.93) 100%);
            color: #f8f4ec;
        }

        /* The light base theme paints labels dark; the sidebar is dark navy,
           so force its text and widget labels to stay light. */
        [data-testid="stSidebar"] label,
        [data-testid="stSidebar"] p,
        [data-testid="stSidebar"] .stMarkdown,
        [data-testid="stSidebar"] [data-testid="stWidgetLabel"] {
            color: #f2ece0 !important;
        }

        /* Alerts (LocalStack status, API key status) sit on the dark sidebar:
           give them a light card so their own dark text stays readable. */
        [data-testid="stSidebar"] [data-testid="stAlert"] {
            background: rgba(251, 247, 239, 0.95);
            border-radius: 12px;
        }
        [data-testid="stSidebar"] [data-testid="stAlert"] p {
            color: #16313d !important;
        }

        .cf-hero {
            background: linear-gradient(135deg, rgba(255, 251, 244, 0.96), rgba(240, 231, 213, 0.92));
            border: 1px solid var(--cf-border);
            border-radius: 20px;
            padding: 1.35rem 1.4rem;
            box-shadow: 0 18px 45px rgba(22, 49, 61, 0.08);
            margin-bottom: 0.9rem;
        }

        .cf-kicker {
            display: inline-block;
            color: var(--cf-cool);
            letter-spacing: 0.08em;
            font-size: 0.72rem;
            font-weight: 700;
            text-transform: uppercase;
            margin-bottom: 0.45rem;
        }

        .cf-title {
            font-family: "Iowan Old Style", "Palatino Linotype", Georgia, serif;
            color: var(--cf-ink);
            font-size: 2.2rem;
            line-height: 1.1;
            margin: 0 0 0.45rem 0;
        }

        .cf-copy {
            color: var(--cf-muted);
            font-size: 1rem;
            margin: 0;
            max-width: 64rem;
        }

        .cf-note {
            background: rgba(255, 251, 244, 0.75);
            border: 1px solid rgba(207, 91, 45, 0.18);
            border-radius: 16px;
            padding: 0.95rem 1rem;
            margin: 0.75rem 0 0.25rem 0;
        }

        .cf-note strong {
            color: var(--cf-ink);
        }

        [data-testid="stMetric"] {
            background: linear-gradient(180deg, rgba(22, 49, 61, 0.92), rgba(16, 44, 54, 0.95));
            border: 1px solid rgba(28, 124, 132, 0.22);
            border-radius: 18px;
            padding: 0.95rem 1rem;
            box-shadow: 0 14px 34px rgba(22, 49, 61, 0.14);
        }

        [data-testid="stMetricLabel"] {
            color: rgba(232, 240, 242, 0.82);
            font-size: 0.8rem;
            font-weight: 600;
            letter-spacing: 0.03em;
        }

        [data-testid="stMetricValue"] {
            color: #f7fbfc;
            font-weight: 700;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_overview(benchmark: dict, max_iterations: int, deploy_enabled: bool) -> None:
    counts = benchmark_tier_counts(benchmark)
    localstack_up = localstack_running()
    st.markdown(
        """
        <div class="cf-hero">
          <div class="cf-kicker">Open Research Artifact</div>
          <h1 class="cf-title">CloudForge</h1>
          <p class="cf-copy">
            Symmetric backend-and-Terraform generation from one natural-language prompt,
            instrumented for dissertation-grade evaluation rather than black-box demos.
          </p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Benchmark specs", len(benchmark))
    col2.metric("Simple / Mod / Complex", f"{counts['simple']} / {counts['moderate']} / {counts['complex']}")
    col3.metric("Retry budget", max_iterations)
    col4.metric(
        "Deployment target",
        "LocalStack" if deploy_enabled else ("offline" if localstack_up else "disabled"),
    )


def render_benchmark_picker(benchmark: dict) -> tuple[dict | None, str]:
    if SPEC_CHOICE_KEY not in st.session_state:
        st.session_state[SPEC_CHOICE_KEY] = CUSTOM_OPTION
    if SPEC_INPUT_KEY not in st.session_state:
        st.session_state[SPEC_INPUT_KEY] = ""
    if CUSTOM_DRAFT_KEY not in st.session_state:
        st.session_state[CUSTOM_DRAFT_KEY] = ""

    def label_for(key: str) -> str:
        entry = benchmark.get(key)
        if entry is None:
            return key
        preview = entry["spec"].strip().replace("\n", " ")
        return f"{entry['id']} · {entry['tier']} — {preview[:72]}…"

    choice = st.selectbox(
        "Benchmark specification",
        [CUSTOM_OPTION] + list(benchmark.keys()),
        key=SPEC_CHOICE_KEY,
        format_func=label_for,
        on_change=sync_spec_from_choice,
        args=(benchmark,),
        help=(
            "Choose one of the 18 pre-registered evaluation prompts, or switch to a "
            "custom specification for exploratory runs."
        ),
    )
    selected = benchmark.get(choice)
    spec = st.text_area(
        "Application specification",
        key=SPEC_INPUT_KEY,
        height=170,
        placeholder="Describe the cloud application you want, in plain English…",
    )
    if choice == CUSTOM_OPTION:
        st.session_state[CUSTOM_DRAFT_KEY] = spec
    return selected, spec


def render_selected_spec_panel(selected: dict | None) -> None:
    summary = summarize_selected_spec(selected)
    left, right = st.columns([1, 1.3])
    with left:
        st.subheader("Evaluation framing")
        st.markdown(
            f"""
            <div class="cf-note">
              <strong>{summary['benchmark_id']}</strong><br />
              Tier: <strong>{summary['tier']}</strong><br />
              SCP score: <strong>{summary['complexity_score']}</strong><br />
              Cloud components / API operations:
              <strong>{summary['cloud_components']} / {summary['api_operations']}</strong><br />
              Expected congruence items: <strong>{summary['checklist_items']}</strong><br />
              Spec length: <strong>{summary['word_count']} words</strong>
            </div>
            """,
            unsafe_allow_html=True,
        )
        if selected:
            st.caption(
                "The pre-run Specification Complexity Profile is saved in `report.json`. "
                "Use the evaluation run plan to decide whether this pair starts with the baseline "
                "or bounded-correction condition."
            )
        else:
            st.caption(
                "Custom prompts are useful for exploratory demos, but only the benchmark suite "
                "feeds the dissertation's comparable evaluation dataset."
            )

    with right:
        if selected:
            with st.expander(
                f"Congruence checklist for {selected['id']} "
                f"({len(selected['congruence_checklist'])} items)",
                expanded=True,
            ):
                for item in selected["congruence_checklist"]:
                    st.markdown(f"- {item}")
        else:
            st.info(
                "For a custom specification, score congruence manually using the same idea as the "
                "benchmark: pre-register the required endpoints, resources, and behaviours before you run it."
            )


def render_plan(plan: dict) -> None:
    """Readable rendering of the shared plan, the pipeline's core mechanism."""
    if not plan:
        st.info("No plan recorded.")
        return
    st.markdown(
        f"**{plan.get('app_name', 'unnamed')}** · framework: `{plan.get('framework', '?')}`"
    )
    if plan.get("summary"):
        st.caption(plan["summary"])
    endpoints = plan.get("endpoints", [])
    if endpoints:
        st.markdown("**Endpoints**")
        st.table(
            [
                {
                    "method": e.get("method", ""),
                    "path": e.get("path", ""),
                    "purpose": e.get("purpose", ""),
                }
                for e in endpoints
            ]
        )
    models = plan.get("data_models", [])
    if models:
        st.markdown("**Data models**")
        st.table(
            [
                {"name": m.get("name", ""), "fields": ", ".join(m.get("fields", []))}
                for m in models
            ]
        )
    resources = plan.get("aws_resources", [])
    if resources:
        st.markdown("**AWS resources** (every resource must trace to a feature)")
        st.table(
            [
                {
                    "service": r.get("service", ""),
                    "name": r.get("name", ""),
                    "justification": r.get("justification", ""),
                }
                for r in resources
            ]
        )
    assumptions = plan.get("assumptions", [])
    if assumptions:
        st.markdown("**Recorded assumptions**")
        for assumption in assumptions:
            st.markdown(f"- {assumption}")
    with st.expander("Raw plan JSON"):
        st.json(plan)


def congruence_rows(checklist: list[str], run_id: str) -> list[dict]:
    """Collect the three manual judgments per pre-registered checklist item."""
    rows = []
    for idx, item in enumerate(checklist):
        text_col, app_col, iac_col, joint_col = st.columns([6, 1, 1, 1.2])
        text_col.markdown(f"{idx + 1}. {item}")
        rows.append(
            {
                "item_index": idx + 1,
                "item": item,
                "app_present": app_col.checkbox(
                    "App", key=f"cg-{run_id}-{idx}-app",
                    help="The generated application implements this capability.",
                ),
                "iac_present": iac_col.checkbox(
                    "IaC", key=f"cg-{run_id}-{idx}-iac",
                    help="The generated Terraform provisions or supports it.",
                ),
                "joint_congruent": joint_col.checkbox(
                    "Joint", key=f"cg-{run_id}-{idx}-joint",
                    help="Both sides are present with architecturally compatible roles.",
                ),
            }
        )
    return rows


def render_congruence_scoring(final_state: dict, run_id: str) -> None:
    """Manual scoring aid for the evaluation guide's congruence procedure."""
    checklist = final_state.get("benchmark_checklist", [])
    if not checklist:
        st.info(
            "Custom specification — no pre-registered checklist to score. "
            "Benchmark runs show the scoring panel here."
        )
        return
    st.caption(
        "Score every pre-registered item against the generated artifacts, then save. "
        "Joint congruence requires both sides present **and** architecturally compatible; "
        "static or deployment success alone does not prove it."
    )
    rows = congruence_rows(checklist, run_id)
    n = len(rows)
    app_n = sum(r["app_present"] for r in rows)
    iac_n = sum(r["iac_present"] for r in rows)
    joint_n = sum(r["joint_congruent"] for r in rows)
    inconsistent = [
        r["item_index"]
        for r in rows
        if r["joint_congruent"] and not (r["app_present"] and r["iac_present"])
    ]
    col1, col2, col3 = st.columns(3)
    col1.metric("App coverage", f"{app_n} / {n}")
    col2.metric("IaC coverage", f"{iac_n} / {n}")
    col3.metric("Joint congruence", f"{joint_n} / {n}")
    if inconsistent:
        st.warning(
            f"Item(s) {inconsistent} are marked Joint without both App and IaC — "
            "joint congruence requires both sides."
        )
    note = st.text_area(
        "Evidence / divergence note (file names, resource names, mismatches)",
        key=f"cg-{run_id}-note",
        placeholder="e.g. app is a standalone FastAPI server but Terraform provisions API Gateway + Lambda",
    )
    if st.button("💾 Save scores to run directory", key=f"cg-{run_id}-save"):
        buffer = io.StringIO()
        writer = csv.DictWriter(
            buffer,
            fieldnames=["item_index", "item", "app_present", "iac_present", "joint_congruent"],
        )
        writer.writeheader()
        writer.writerows(rows)
        run_dir = RUNS_DIR / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "congruence.csv").write_text(buffer.getvalue())
        summary = (
            f"run_id: {run_id}\n"
            f"app_coverage: {app_n}/{n}\niac_coverage: {iac_n}/{n}\n"
            f"joint_congruence: {joint_n}/{n}\nnote: {note.strip()}\n"
        )
        (run_dir / "congruence_summary.txt").write_text(summary)
        st.success(
            f"Saved to `runs/{run_id}/congruence.csv`. Copy the three fractions and the "
            "note into the master evaluation log."
        )


def render_results(final_state: dict, max_iterations: int, run_id: str) -> None:
    tab_plan, tab_app, tab_iac, tab_val, tab_deploy, tab_metrics, tab_score = st.tabs(
        [
            "📋 Plan",
            "🐍 App code",
            "🏗️ Terraform",
            "🔎 Validation",
            "🚀 Deployment",
            "📊 Metrics",
            "✅ Congruence scoring",
        ]
    )

    with tab_plan:
        render_plan(final_state.get("plan", {}))

    with tab_app:
        for generated_file in final_state.get("app_files", []):
            with st.expander(
                generated_file["path"],
                expanded=generated_file["path"].endswith("app.py"),
            ):
                st.code(generated_file["content"], language="python")

    with tab_iac:
        for generated_file in final_state.get("iac_files", []):
            with st.expander(
                generated_file["path"],
                expanded=generated_file["path"] == "main.tf",
            ):
                st.code(generated_file["content"], language="hcl")

    with tab_val:
        validations = final_state.get("validations", [])
        if validations:
            st.dataframe(
                [
                    {
                        "iteration": entry["iteration"],
                        "target": entry["target"],
                        "tool": entry["tool"],
                        "passed": "✅" if entry["passed"] else "❌",
                        "duration (s)": entry["duration_s"],
                    }
                    for entry in validations
                ],
                use_container_width=True,
            )
            for entry in validations:
                if not entry["passed"]:
                    with st.expander(
                        f"❌ iteration {entry['iteration']} · {entry['target']} · {entry['tool']}"
                    ):
                        st.code(entry["output"])
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
        total_in = sum(item.get("input_tokens", 0) for item in usage)
        total_out = sum(item.get("output_tokens", 0) for item in usage)
        cache_read = sum(item.get("cache_read_input_tokens", 0) for item in usage)
        col1, col2, col3, col4, col5 = st.columns(5)
        col1.metric("LLM calls", len(usage))
        col2.metric("Input tokens", f"{total_in:,}")
        col3.metric("Output tokens", f"{total_out:,}")
        col4.metric("Cache-read tokens", f"{cache_read:,}")
        col5.metric("Est. cost (USD)", f"${estimate_cost_usd(usage):.3f}")
        report_path = RUNS_DIR / run_id / "report.json"
        report_data = {}
        if report_path.exists():
            try:
                report_data = json.loads(report_path.read_text())
            except json.JSONDecodeError:
                report_data = {}
        iter_col, wall_col = st.columns(2)
        iter_col.metric(
            "Correction iterations used",
            f"{final_state.get('iteration', 0)} / {max_iterations}",
        )
        if report_data.get("wall_seconds") is not None:
            wall_col.metric(
                "Wall-clock time (whole run)",
                f"{report_data['wall_seconds']:.0f} s",
                help=(
                    "True elapsed time. The per-node stage timings below sum to more "
                    "because generation and validation branches run in parallel."
                ),
            )
        if usage:
            st.dataframe(usage, use_container_width=True)
        timings = final_state.get("timings", [])
        if timings:
            st.subheader("Stage timings")
            st.dataframe(timings, use_container_width=True)
        if report_path.exists():
            st.download_button(
                "⬇️ Download report.json",
                report_path.read_bytes(),
                file_name=f"{run_id}-report.json",
                mime="application/json",
                key=f"dl-{run_id}",
            )

    with tab_score:
        render_congruence_scoring(final_state, run_id)

    st.info(
        f"📁 All artifacts and `report.json` were saved to `runs/{run_id}/` for the "
        "benchmark dataset and failure-taxonomy analysis."
    )


def render_app() -> None:
    st.set_page_config(page_title="CloudForge", page_icon="⚒️", layout="wide")
    render_theme()

    benchmark = load_benchmark_specs()

    with st.sidebar:
        st.header("Configuration")
        st.markdown(f"**Model:** `{MODEL}`")

        api_key_ok = bool(os.environ.get("ANTHROPIC_API_KEY"))
        if api_key_ok:
            st.success("ANTHROPIC_API_KEY found", icon="🔑")
        else:
            st.info(
                "ANTHROPIC_API_KEY not set. Add it to `.env` before running the generation pipeline.",
                icon="🔑",
            )

        ls_up = localstack_running()
        if ls_up:
            st.success(f"LocalStack running at {LOCALSTACK_URL}", icon="🟢")
        else:
            st.error(
                f"LocalStack not reachable at {LOCALSTACK_URL}. Run `./start.sh`, or start "
                "`cloudforge-localstack` manually before enabling deployment.",
                icon="🔴",
            )

        max_iterations = st.slider(
            "Max self-correction iterations",
            min_value=0,
            max_value=3,
            value=3,
            help="Hard bound on the feedback loop for safer, comparable benchmark runs.",
        )
        deploy_enabled = st.toggle(
            "Deploy to LocalStack (phase 4)",
            value=ls_up,
            disabled=not ls_up,
        )
        checkov_blocking = st.toggle(
            "Checkov failures block the pipeline",
            value=True,
            help="Turn this off only when you want to record security findings without consuming retry budget.",
        )

    render_overview(benchmark, max_iterations, deploy_enabled)

    st.warning(
        "**Experimental research artifact.** Generated code and Terraform may pass tools while "
        "still diverging from user intent. Review every artifact before any real-world use; "
        "this interface is designed for LocalStack-only evaluation.",
        icon="⚠️",
    )

    selected, spec = render_benchmark_picker(benchmark)
    render_selected_spec_panel(selected)

    run_clicked = st.button(
        "🚀 Generate & validate",
        type="primary",
        disabled=not spec.strip(),
    )

    if run_clicked:
        run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
        run_dir = RUNS_DIR / run_id
        initial_state = build_initial_state(
            spec,
            run_id,
            run_dir,
            max_iterations,
            deploy_enabled,
            checkov_blocking,
            selected,
        )

        graph = get_graph()
        final_state = None
        shown_events = 0
        started = time.time()

        timer_slot = st.empty()
        with timer_slot:
            components.html(LIVE_TIMER_HTML, height=42)

        with st.status("Running the CloudForge pipeline…", expanded=True) as status_box:
            try:
                for state_snapshot in graph.stream(
                    initial_state,
                    config={"recursion_limit": 60},
                    stream_mode="values",
                ):
                    final_state = state_snapshot
                    events = state_snapshot.get("events", [])
                    for event in events[shown_events:]:
                        st.write(f"`{int(time.time() - started)}s` · {event}")
                    shown_events = len(events)
            except GenerationError as exc:
                timer_slot.markdown(
                    f"⏱ **Run aborted after {time.time() - started:.0f}s**"
                )
                status_box.update(label="Pipeline error", state="error")
                st.error(str(exc))
                st.stop()

            succeeded = final_state.get("status") == "success"
            timer_slot.markdown(
                f"⏱ **Finished in {time.time() - started:.0f}s** (wall clock)"
            )
            status_box.update(
                label=(
                    f"Pipeline finished in {time.time() - started:.0f}s — "
                    f"{'success ✅' if succeeded else 'failed ❌'}"
                ),
                state="complete" if succeeded else "error",
                expanded=False,
            )

        # Persist so results (and the scoring panel) survive widget reruns.
        st.session_state["last_run"] = {
            "final_state": final_state,
            "max_iterations": max_iterations,
            "run_id": run_id,
        }

    last_run = st.session_state.get("last_run")
    if not last_run:
        return

    if not run_clicked:
        st.caption(f"Showing results for run `{last_run['run_id']}`.")
    render_results(
        last_run["final_state"], last_run["max_iterations"], last_run["run_id"]
    )


if __name__ == "__main__":
    render_app()
