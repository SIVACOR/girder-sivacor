"""What the permanent record does with a ``phase`` it no longer writes.

Julia submissions briefly ran as two containers per stage -- a dependency
resolve and the run -- and wrote a telemetry row for each. Environment setup is
an ordinary stage the researcher writes now, so nothing emits ``phase`` any
more, but the rows written in between are kept indefinitely and ``n_stages``
has to keep meaning the same thing across all three eras: before the phase,
during it, and after. A sanitizer that dropped the field would not delete those
records, only make them incomparable with the ones on either side.
"""

import pytest

from girder_sivacor.telemetry import sanitize_record
from girder_sivacor.worker_plugin.lib import performance_data_name

ANALYSIS = "analysis"
RESOLVE = "resolve"


def stage(phase=None, **extra):
    row = {"image_name": "ghcr.io/sivacor/julia1.11", "image_tag": "1.11.9-20260916"}
    if phase is not None:
        row["phase"] = phase
    row.update(extra)
    return row


def test_n_stages_counts_analysis_phases_only():
    record = sanitize_record(
        {"status": "completed", "stages": [stage(RESOLVE), stage(ANALYSIS)]},
        "2026-09-16",
    )
    # Two rows, one stage. Counting rows would report this submission as twice
    # the size of the identical thing run in R.
    assert len(record["stages"]) == 2
    assert record["n_stages"] == 1


def test_a_row_without_a_phase_is_an_analysis():
    """Which is every row the worker writes today, and every row it wrote before Julia."""
    record = sanitize_record({"status": "completed", "stages": [stage()]}, "2026-09-16")
    assert record["stages"][0]["phase"] == ANALYSIS
    assert record["n_stages"] == 1


def test_a_setup_stage_counts_as_a_stage():
    """The reversal, in one assertion.

    A researcher who writes their own ``Pkg.instantiate()`` stage has submitted
    two stages, and the record says two -- where the injected resolve phase it
    replaced deliberately said one. Both are right for what they describe, which
    is why the phase has to stay readable rather than be reinterpreted.
    """
    record = sanitize_record(
        {"status": "completed", "stages": [stage(), stage()]}, "2026-09-16"
    )
    assert record["n_stages"] == 2


@pytest.mark.parametrize("value", ["", "ANALYSIS", "exfiltrate", 7, None, {"a": 1}])
def test_an_unknown_phase_falls_back_rather_than_being_stored(value):
    """The allow-list direction: a phase we do not know is not a phase we keep."""
    record = sanitize_record(
        {"status": "completed", "stages": [stage(value)]}, "2026-09-16"
    )
    assert record["stages"][0]["phase"] == ANALYSIS


def test_mixed_stacks_count_only_their_runs():
    """A worker mid-rollout is still running the old code and still posting ``resolve``."""
    record = sanitize_record(
        {
            "status": "completed",
            "stages": [
                stage(RESOLVE),
                stage(ANALYSIS),
                stage(ANALYSIS, image_name="rocker/r-ver"),
            ],
        },
        "2026-09-16",
    )
    assert record["n_stages"] == 2


def test_performance_data_is_named_by_stage_alone():
    """One stage, one container, one metrics file -- and no phase in the name."""
    assert performance_data_name(1) == "performance_data_stage_1.json"
    assert performance_data_name(2) != performance_data_name(1)
