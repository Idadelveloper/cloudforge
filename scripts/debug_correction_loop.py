"""Drive the bounded-correction routing functions with controlled state.

Purpose: step through the correction loop in a debugger deterministically,
without spending an API call or waiting for a live run. The functions called
here are the production ones from cloudforge.nodes — nothing is reimplemented;
only the state they receive is constructed by hand.

Usage:
    python scripts/debug_correction_loop.py

Suggested breakpoints for a walkthrough:
    cloudforge/nodes.py  ->  route_after_assess()
    cloudforge/nodes.py  ->  correct()
    cloudforge/nodes.py  ->  route_correction_targets()
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cloudforge import nodes  # noqa: E402


def make_state(app_passed, iac_passed, iteration, max_iterations=3):
    """A minimal CloudForgeState slice: only the keys routing depends on."""
    return {
        "app_passed": app_passed,
        "iac_passed": iac_passed,
        "iteration": iteration,
        "max_iterations": max_iterations,
        "deploy_enabled": True,
    }


def show(title, state):
    print(f"\n--- {title}")
    print(f"    state: app_passed={state['app_passed']} "
          f"iac_passed={state['iac_passed']} "
          f"iteration={state['iteration']}/{state['max_iterations']}")
    print(f"    assess():               {nodes.assess(state)['events'][0]}")
    route = nodes.route_after_assess(state)
    print(f"    route_after_assess():   -> '{route}'")
    if route == "correct":
        bumped = nodes.correct(state)
        print(f"    correct():              iteration -> {bumped['iteration']}")
        targets = nodes.route_correction_targets(state)
        print(f"    route_correction_targets(): -> {targets}")
    return route


if __name__ == "__main__":
    # 1. Infrastructure failed its gates on the first attempt, budget remains.
    show("Case 1: IaC gate failed, iteration 0 of 3", make_state(True, False, 0))

    # 2. Both artifacts pass: the loop is skipped entirely.
    show("Case 2: all gates passed", make_state(True, True, 0))

    # 3. The bound is reached: correction stops even though gates still fail.
    show("Case 3: still failing at the 3-iteration bound", make_state(False, False, 3))

    print("\nThe bound is structural: route_after_assess() cannot return "
          "'correct' once iteration == max_iterations.\n")
