from pathlib import Path

from app import (
    build_initial_state,
    benchmark_tier_counts,
    read_benchmark_specs,
    summarize_selected_spec,
)


def test_read_benchmark_specs_indexes_entries_by_id(tmp_path: Path) -> None:
    fixture = tmp_path / "specs.yaml"
    fixture.write_text(
        "\n".join(
            [
                "specs:",
                "  - id: S1-notes",
                "    tier: simple",
                "    spec: Build a notes API",
                "    congruence_checklist:",
                "      - DynamoDB table",
                "  - id: M1-fileshare",
                "    tier: moderate",
                "    spec: Build a file-sharing API",
                "    congruence_checklist:",
                "      - S3 bucket",
            ]
        )
    )

    specs = read_benchmark_specs(fixture)

    assert list(specs) == ["S1-notes", "M1-fileshare"]
    assert specs["M1-fileshare"]["tier"] == "moderate"


def test_benchmark_tier_counts_only_returns_benchmark_tiers() -> None:
    counts = benchmark_tier_counts(
        {
            "S1": {"tier": "simple"},
            "S2": {"tier": "simple"},
            "M1": {"tier": "moderate"},
            "C1": {"tier": "complex"},
        }
    )

    assert counts == {"simple": 2, "moderate": 1, "complex": 1}


def test_summarize_selected_spec_reports_custom_defaults() -> None:
    assert summarize_selected_spec(None) == {
        "benchmark_id": "Custom",
        "tier": "custom",
        "checklist_items": 0,
        "word_count": 0,
        "complexity_score": 0,
        "cloud_components": 0,
        "api_operations": 0,
    }


def test_build_initial_state_carries_benchmark_metadata(tmp_path: Path) -> None:
    selected = {
        "id": "S3-shortener",
        "tier": "simple",
        "complexity": {"score": 6},
        "congruence_checklist": ["URL mapping table"],
    }

    state = build_initial_state(
        spec="Build a URL shortener",
        run_id="20260830-123000-abcdef",
        run_dir=tmp_path / "run",
        max_iterations=3,
        deploy_enabled=True,
        checkov_blocking=False,
        selected=selected,
    )

    assert state["spec"] == "Build a URL shortener"
    assert state["benchmark_id"] == "S3-shortener"
    assert state["tier"] == "simple"
    assert state["evaluation_condition"] == "bounded_correction"
    assert state["benchmark_complexity"] == {"score": 6}
    assert state["benchmark_checklist"] == ["URL mapping table"]
    assert len(state["benchmark_fingerprint"]) == 64
    assert state["checkov_blocking"] is False
