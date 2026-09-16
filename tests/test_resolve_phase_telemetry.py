"""What the permanent record does with a stage that ran in two phases.

A Julia submission writes two telemetry rows per stage -- the dependency
resolve and the run -- where every other stack writes one. ``n_stages`` has to
keep meaning the same thing across that change, or every historical record
silently becomes incomparable with every new one.
"""

import pytest

from girder_sivacor.telemetry import sanitize_record
from girder_sivacor.worker_plugin.lib import (
    PHASE_ANALYSIS,
    PHASE_RESOLVE,
    performance_data_name,
)


def stage(phase=None, **extra):
    row = {"image_name": "ghcr.io/sivacor/julia1.11", "image_tag": "1.11.9-20260916"}
    if phase is not None:
        row["phase"] = phase
    row.update(extra)
    return row


def test_n_stages_counts_analysis_phases_only():
    record = sanitize_record(
        {"status": "completed", "stages": [stage(PHASE_RESOLVE), stage(PHASE_ANALYSIS)]},
        "2026-09-16",
    )
    # Two rows, one stage. Counting rows would report this submission as twice
    # the size of the identical thing run in R.
    assert len(record["stages"]) == 2
    assert record["n_stages"] == 1


def test_a_row_without_a_phase_is_an_analysis():
    """Every record written before the resolve phase existed has no phase field."""
    record = sanitize_record({"status": "completed", "stages": [stage()]}, "2026-09-16")
    assert record["stages"][0]["phase"] == PHASE_ANALYSIS
    assert record["n_stages"] == 1


@pytest.mark.parametrize("value", ["", "ANALYSIS", "exfiltrate", 7, None, {"a": 1}])
def test_an_unknown_phase_falls_back_rather_than_being_stored(value):
    """The allow-list direction: a phase we do not know is not a phase we keep."""
    record = sanitize_record(
        {"status": "completed", "stages": [stage(value)]}, "2026-09-16"
    )
    assert record["stages"][0]["phase"] == PHASE_ANALYSIS


def test_mixed_stacks_count_only_their_runs():
    record = sanitize_record(
        {
            "status": "completed",
            "stages": [
                stage(PHASE_RESOLVE),
                stage(PHASE_ANALYSIS),
                stage(PHASE_ANALYSIS, image_name="rocker/r-ver"),
            ],
        },
        "2026-09-16",
    )
    assert record["n_stages"] == 2


def test_performance_data_name_separates_the_phases():
    """Both phases upload by name into one folder; a shared name loses one of them."""
    assert performance_data_name(1) == "performance_data_stage_1.json"
    assert performance_data_name(1, PHASE_ANALYSIS) == "performance_data_stage_1.json"
    assert (
        performance_data_name(1, PHASE_RESOLVE)
        == "performance_data_stage_1_resolve.json"
    )
    assert performance_data_name(1, PHASE_RESOLVE) != performance_data_name(1)
