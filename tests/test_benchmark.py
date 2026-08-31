from cloudforge.benchmark import (
    complexity_score,
    evaluation_condition,
    read_benchmark_specs,
    validate_complexity_profile,
)
from scripts.complexity_metrics import catalogue_rows


def test_benchmark_has_the_pre_registered_tier_balance() -> None:
    entries = read_benchmark_specs()

    assert len(entries) == 18
    assert sum(entry["tier"] == "simple" for entry in entries.values()) == 5
    assert sum(entry["tier"] == "moderate" for entry in entries.values()) == 7
    assert sum(entry["tier"] == "complex" for entry in entries.values()) == 6


def test_each_benchmark_profile_uses_the_declared_formula() -> None:
    for entry in read_benchmark_specs().values():
        assert validate_complexity_profile(entry) == []
        assert complexity_score(entry["complexity"]) == entry["complexity"]["score"]


def test_catalogue_exports_all_pre_run_profiles() -> None:
    rows = catalogue_rows()

    assert len(rows) == 18
    assert rows[0]["benchmark_id"] == "S1-notes"
    assert rows[-1]["benchmark_id"] == "C6-loyalty"
    assert all(row["profile_score"] > 0 for row in rows)


def test_standard_evaluation_conditions_have_stable_labels() -> None:
    assert evaluation_condition(0) == "baseline"
    assert evaluation_condition(3) == "bounded_correction"
