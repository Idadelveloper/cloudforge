"""CloudForge pipeline graph (LangGraph 1.x explicit cyclic state machine).

        START
          │
      parse_spec
        ╱    ╲            (parallel superstep)
 generate_app  generate_iac
      │            │
 validate_app  validate_iac   (parallel superstep)
        ╲    ╱
        assess ──────────────► deploy ──► finalize ──► END
          │                      │            ▲
          ▼                      ▼            │
        correct ◄───(deploy errors feed back)─┘
        ╱    ╲
  (regenerate only the failing side, bounded to max_iterations)
"""

from langgraph.graph import END, START, StateGraph

from . import nodes
from .state import CloudForgeState


def build_graph():
    builder = StateGraph(CloudForgeState)

    builder.add_node("parse_spec", nodes.parse_spec)
    builder.add_node("generate_app", nodes.generate_app)
    builder.add_node("generate_iac", nodes.generate_iac)
    builder.add_node("validate_app", nodes.validate_app)
    builder.add_node("validate_iac", nodes.validate_iac)
    builder.add_node("assess", nodes.assess)
    builder.add_node("correct", nodes.correct)
    builder.add_node("deploy", nodes.deploy)
    builder.add_node("finalize", nodes.finalize)

    builder.add_edge(START, "parse_spec")

    # Phase 1: symmetric parallel generation from the shared plan.
    builder.add_edge("parse_spec", "generate_app")
    builder.add_edge("parse_spec", "generate_iac")

    # Phase 2: each artifact is validated by its own toolchain.
    builder.add_edge("generate_app", "validate_app")
    builder.add_edge("generate_iac", "validate_iac")

    # Fan-in, then route: deploy / bounded correction / give up.
    builder.add_edge("validate_app", "assess")
    builder.add_edge("validate_iac", "assess")
    builder.add_conditional_edges(
        "assess",
        nodes.route_after_assess,
        {"deploy": "deploy", "correct": "correct", "finalize": "finalize"},
    )

    # Phase 3: regenerate only the failing side(s).
    builder.add_conditional_edges(
        "correct",
        nodes.route_correction_targets,
        ["generate_app", "generate_iac"],
    )

    # Phase 4: deployment errors also feed the bounded correction loop.
    builder.add_conditional_edges(
        "deploy",
        nodes.route_after_deploy,
        {"correct": "correct", "finalize": "finalize"},
    )

    builder.add_edge("finalize", END)
    return builder.compile()
