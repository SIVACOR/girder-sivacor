"""A stage that dies must still say what it was and what it had measured.

``telemetry_stages`` used to be built and appended in one go at the *end* of
``recorded_run``. Any exception in between skipped it, and — because that append
sits inside the ``tempfile.TemporaryDirectory`` holding the stats CSV — the
measurements were deleted on the way out too. So a failed run recorded
``stages: []``.

That is what `ferocious-fader` produced on 2026-09-18: 20 h 14 m of compute on a
16 vCPU worker, killed by a single 502 on a progress line, and the one store that
outlives the submission got an empty list. The run that most needed post-mortem
telemetry was exactly the run that recorded none
(``2026-09-18-log-flush-502-kills-run.md``).

The row is now appended before the container starts and completed afterwards, so
the static half always survives and the measured half is salvaged on the way out.
"""

import mock
import pytest
from girder_jobs.constants import JobStatus
from girder_jobs.models.job import Job
from girder_sivacor.models.execution_record import ExecutionRecord
from girder_sivacor.worker_plugin.lib import (
    LogRelay,
    aggregate_container_stats,
    measured_stage_telemetry,
)
from pytest_girder.assertions import assertStatusOk

CSV_HEADER = "Timestamp,CPU %,Memory Usage,Memory Limit,Net In,Net Out,Block Read,Block Write,PIDs,CPU Seconds\n"


def write_csv(tmp_path, rows):
    path = tmp_path / "dockerstats"
    path.with_suffix(".csv").write_text(CSV_HEADER + rows)
    return str(path)


# -- aggregate_container_stats: the salvage primitive ----------------------


def test_stats_are_aggregated_from_the_csv(tmp_path):
    path = write_csv(
        tmp_path,
        '"2026-09-18T00:00:01Z",12.5,1000,4000,0,0,0,0,3,1.5\n'
        '"2026-09-18T00:00:02Z",90.0,3000,4000,0,0,0,0,3,9.0\n',
    )
    data = {}
    aggregate_container_stats(path, data)
    assert data["MaxCPUPercent"] == 90.0
    assert data["MaxMemoryUsage"] == 3000
    # max() of a cumulative counter is its final value.
    assert data["CPUSecondsTotal"] == 9.0


def test_a_header_only_csv_reports_metrics_unavailable(tmp_path):
    """The collector writes the header on start, so the file exists even when no
    reading was ever taken. Aggregating that empty frame yields NaN, which
    json.dumps writes as a bare `NaN` literal — invalid JSON, inside something
    that gets hashed into a signed TRO."""
    data = {}
    aggregate_container_stats(write_csv(tmp_path, ""), data)
    assert "MetricsUnavailable" in data
    assert "MaxCPUPercent" not in data


def test_a_missing_csv_is_not_an_error(tmp_path):
    data = {}
    aggregate_container_stats(str(tmp_path / "never-written"), data)
    assert data == {}


def test_a_corrupt_csv_cannot_raise(tmp_path):
    """On the failure path an exception here would replace the real cause with a
    pandas error, so the tolerant version is what both callers get."""
    path = tmp_path / "dockerstats"
    path.with_suffix(".csv").write_text("not,a,valid\nstats\x00file\n")
    aggregate_container_stats(str(path), {})  # must not raise


def test_a_truncated_csv_still_yields_what_it_has(tmp_path):
    """A run killed mid-write leaves a partial last line; the rows before it are
    still the whole point of salvaging."""
    path = write_csv(
        tmp_path,
        '"2026-09-18T00:00:01Z",12.5,1000,4000,0,0,0,0,3,1.5\n'
        '"2026-09-18T00:00:02Z",44.0,2000,4000,0,0,0,0,3,4.0\n',
    )
    data = {}
    aggregate_container_stats(path, data)
    assert data["MaxCPUPercent"] == 44.0


# -- measured_stage_telemetry ---------------------------------------------


def test_measured_fields_are_none_when_nothing_was_measured():
    """The shape a torn-down stage publishes: every key present, values honest."""
    row = measured_stage_telemetry({})
    assert row == {
        "exit_code": None,
        "max_cpu_percent": None,
        "cpu_seconds_total": None,
        "max_memory_bytes": None,
        "max_disk_bytes": None,
        "image_size_bytes": None,
    }


def test_measured_fields_carry_the_exit_code_when_there_is_one():
    row = measured_stage_telemetry({"MaxCPUPercent": 7.0}, 0)
    assert row["exit_code"] == 0
    assert row["max_cpu_percent"] == 7.0


# -- the end-to-end property ----------------------------------------------


@pytest.mark.plugin("sivacor")
def test_a_run_torn_down_mid_loop_still_records_its_stage(
    server,
    db,
    user,
    eagerWorkerTasks,
    fsAssetstore,
    patched_gpg,
    uploads_folder,
    submission_collection,
):
    """The regression, end to end: a real container, killed by an exception in
    the poll loop, must leave a stage row behind.

    ``LogRelay.drain`` is patched to raise because it stands where the 502 landed
    — though the real one is now swallowed there, which is the other half of this
    fix. What is being pinned is the general case: *any* unexpected teardown of
    the loop still records the stage.
    """
    from .conftest import submit_sivacor_job, upload_test_file

    fobj = upload_test_file(uploads_folder, user, "with_space_R.zip")
    stages = [
        {"image_name": "rocker/r-ver", "image_tag": "4.3.1", "main_file": "main.R"}
    ]

    # Raises on the *first* call, which lands on the first pass of the poll
    # loop. Waiting for a later call is not an option: this R package finishes
    # in about two seconds, so the loop may only turn once or twice, and a
    # counter tuned to a longer run simply never fires.
    def exploding_drain(self, log_queue):
        raise OSError("502 Server Error: Bad Gateway")

    with mock.patch.object(LogRelay, "drain", exploding_drain):
        resp = submit_sivacor_job(server, user, fobj, stages, exception=True)
    assertStatusOk(resp)

    job = Job().load(resp.json["_id"], force=True)
    assert job["status"] == JobStatus.ERROR

    records = list(ExecutionRecord().find({}))
    assert len(records) == 1
    record = records[0]

    # The regression: this was [] and n_stages was 0, for a run that really did
    # start a container and really did consume the allocation.
    assert record["stages"], "a failed stage must still be recorded"
    assert record["n_stages"] == 1

    row = record["stages"][0]
    assert row["image_name"] == "rocker/r-ver"
    assert row["image_tag"] == "4.3.1"
    assert row["phase"] == "analysis"
    # Honest about what it does not know: the container never reported one.
    assert row["exit_code"] is None
