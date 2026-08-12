"""Permanent execution records, and the filter that keeps them anonymous.

Two halves. The first exercises ``sanitize_record`` directly: it is a security
boundary -- the one thing standing between a worker payload and a collection
that is never deleted -- so it is tested as one, with inputs it should refuse
rather than only inputs it should accept.

The second checks the property that motivates the whole feature: a record
outlives the submission it describes, by both routes a submission can vanish.
"""

import datetime
import json

import pytest
from girder.models.folder import Folder
from girder.models.setting import Setting
from girder_jobs.constants import JobStatus
from girder_jobs.models.job import Job
from girder_sivacor.errors import FailureCode, SubmissionError, classify
from girder_sivacor.models.execution_record import ExecutionRecord
from girder_sivacor.settings import PluginSettings
from girder_sivacor.telemetry import sanitize_record, size_bucket
from girder_sivacor.worker_plugin.lib import stata_error, stata_error_code
from pytest_girder.assertions import assertStatus, assertStatusOk

from .conftest import submit_sivacor_job, upload_test_file

DATE = "2026-08-06"


def sanitize(**payload):
    return sanitize_record(payload, DATE)


# --- the filter -----------------------------------------------------------


def test_identifiers_are_dropped():
    """Anything that could tie a record to a person must not survive.

    Spelled out as an explicit list because this is the property the retention
    story rests on: if a field here ever starts surviving, these records become
    personal data and everything said about them in the privacy policy is
    wrong.
    """
    stored = sanitize(
        status="completed",
        user_id="507f1f77bcf86cd799439011",
        creator_id="507f1f77bcf86cd799439011",
        job_id="507f1f77bcf86cd799439012",
        folder_id="507f1f77bcf86cd799439013",
        email="jane@example.org",
        first_name="Jane",
        orcid="0000-0002-1825-0097",
        main_file="jane_thesis.do",
        worker_queue="sivacor.worker-01",
        worker={"arch": "x86_64", "hostname": "worker-01", "ncpu": 8},
    )
    forbidden = {
        "user_id",
        "creator_id",
        "job_id",
        "folder_id",
        "email",
        "first_name",
        "orcid",
        "main_file",
        "worker_queue",
    }
    assert forbidden.isdisjoint(stored)
    # ...including one nested a level down, where an allow-list applied only to
    # the top level would miss it.
    assert "hostname" not in stored["worker"]
    assert stored["worker"]["arch"] == "x86_64"


def test_date_comes_from_the_server_and_is_only_a_date():
    """A client-supplied wall-clock time is ignored, not merely coarsened."""
    stored = sanitize(status="completed", date="1999-01-01T04:32:11.123456")
    assert stored["date"] == DATE


@pytest.mark.parametrize(
    "code,detail,expected",
    [
        # The case the whole design turns on: stata_error's message carries the
        # line before r(NNN), which is usually the researcher's failing command.
        ("stata_error", 'use "/home/jane/confidential.dta" r(601)', None),
        ("stata_error", "r(601)", "r(601)"),
        ("stata_error", "License has expired", "License has expired"),
        ("stata_error", "License is fine actually", None),
        ("nonzero_exit", 137, 137),
        ("nonzero_exit", "137; rm -rf /", None),
        ("main_file_ambiguous", 3, 3),
        ("image_pull_failed", "rocker/r-ver:4.6.1", "rocker/r-ver:4.6.1"),
        # A path is not an image reference, however much it looks like one.
        ("image_pull_failed", "/home/jane/../etc/passwd", None),
        ("unsafe_archive", "path_traversal", "path_traversal"),
        ("unsafe_archive", "../../etc/shadow", None),
        ("unexpected", "OSError", "OSError"),
        ("unexpected", "No such file: /home/jane/x.dta", None),
        # Codes that take no detail discard whatever they are handed.
        ("reaped_max_runtime", "jane@example.org", None),
        ("out_of_disk", "/tmp/workspace-abc123", None),
    ],
)
def test_detail_is_validated_per_code(code, detail, expected):
    stored = sanitize(status="failed", error={"step": "s", "code": code, "detail": detail})
    assert stored["error"]["detail"] == expected


def test_detail_is_dropped_rather_than_repaired():
    """Stripping bad characters would leave a mangled but still-identifying
    string; the value has to be refused whole."""
    stored = sanitize(
        status="failed",
        error={"step": "x", "code": "unexpected", "detail": "/home/jane/thesis.dta"},
    )
    assert stored["error"]["detail"] is None


def test_unknown_code_takes_its_detail_down_with_it():
    """A code the server does not know has no validator, so its detail is
    unvalidated -- it cannot be kept."""
    stored = sanitize(
        status="failed",
        error={"step": "s", "code": "invented", "detail": "jane@example.org"},
    )
    assert stored["error"]["code"] == FailureCode.UNEXPECTED.value
    assert stored["error"]["detail"] is None


def test_step_must_be_an_identifier():
    assert sanitize(
        status="failed", error={"step": "execute_workflow", "code": "unexpected"}
    )["error"]["step"] == "execute_workflow"
    assert sanitize(
        status="failed", error={"step": "/home/jane ran it", "code": "unexpected"}
    )["error"]["step"] is None


def test_stage_fan_out_is_bounded():
    """A malformed payload must not be able to write an unbounded document."""
    stages = [{"image_name": "rocker/r-ver", "image_tag": "4.6.1"}] * 500
    stored = sanitize(status="completed", stages=stages)
    assert stored["n_stages"] == 32


def test_successful_records_carry_no_error_key():
    assert "error" not in sanitize(status="completed")
    assert "error" in sanitize(status="failed")


def test_unknown_status_is_not_taken_at_face_value():
    assert sanitize(status="definitely_fine")["status"] == "failed"


def test_malformed_payload_does_not_raise():
    """The worker is best-effort about sending these; the server must be at
    least as tolerant about receiving them."""
    for payload in ({}, {"stages": "not a list"}, {"worker": 7}, {"error": "boom"}):
        assert sanitize_record(payload, DATE)["date"] == DATE
    assert sanitize_record(None, DATE)["status"] == "failed"


def test_package_size_is_bucketed_not_exact():
    assert size_bucket(50 * 1024**2) == "10-100MB"
    assert size_bucket(9 * 1024**3) == ">5GB"
    assert size_bucket(None) is None
    # An exact byte count offered by the worker is not a bucket, so it is dropped.
    assert sanitize(status="completed", package_size_bucket=52428800)[
        "package_size_bucket"
    ] is None


# --- classification at the raise site --------------------------------------


def test_classify_keeps_the_message_for_the_researcher():
    """The split is the point: the full text still reaches the job log."""
    exc = SubmissionError(
        FailureCode.STATA_ERROR,
        'Stata returned an error (use "/home/jane/x.dta" r(601)).',
        detail="r(601)",
    )
    assert "/home/jane/x.dta" in str(exc)
    assert classify(exc) == (FailureCode.STATA_ERROR, "r(601)")


def test_classify_discards_the_text_of_unknown_exceptions():
    code, detail = classify(OSError("No space left on /home/jane/data"))
    assert code is FailureCode.UNEXPECTED
    assert detail == "OSError"


def test_stata_error_code_extracts_only_the_return_code():
    # stata_error keeps the line immediately before the return code. In a real
    # Stata log that is the diagnostic, which routinely quotes the dataset path.
    log = (
        '. use "/workspace/confidential_survey.dta"\n'
        "file /workspace/confidential_survey.dta not found\n"
        "r(601);\n"
    )
    message = stata_error(log)
    # What the researcher sees names the file...
    assert "confidential_survey.dta" in message
    # ...and what is kept forever does not.
    assert stata_error_code(message) == "r(601)"


@pytest.mark.parametrize(
    "message,expected",
    [
        ("License has expired", "License has expired"),
        ("License is invalid", "License is invalid"),
        (None, None),
        ("something we do not recognise", None),
    ],
)
def test_stata_error_code_fixed_diagnoses(message, expected):
    assert stata_error_code(message) == expected


# --- the endpoint and the records' lifetime --------------------------------


def post_record(server, user, payload):
    return server.request(
        path="/sivacor/execution_record",
        method="POST",
        user=user,
        type="application/json",
        body=json.dumps(payload),
    )


def record_count():
    return len(list(ExecutionRecord().find({})))


@pytest.mark.plugin("sivacor")
def test_endpoint_stores_a_filtered_record(server, db, admin):
    resp = post_record(
        server,
        admin,
        {
            "status": "completed",
            "stack_version": "0.1.3",
            "user_id": "507f1f77bcf86cd799439011",
            "stages": [{"image_name": "rocker/r-ver", "image_tag": "4.6.1"}],
        },
    )
    assertStatusOk(resp)
    stored = list(ExecutionRecord().find({}))
    assert len(stored) == 1
    assert stored[0]["status"] == "completed"
    assert stored[0]["stages"][0]["image_name"] == "rocker/r-ver"
    assert "user_id" not in stored[0]


@pytest.mark.plugin("sivacor")
def test_endpoint_is_admin_only(server, db, user):
    """It carries the worker's admin-scoped token, like heartbeat and claim."""
    assertStatus(post_record(server, user, {"status": "completed"}), 403)


def make_submission(user, submission_collection, status="completed"):
    job = Job().createJob(
        title="Run", type="sivacor_submission", public=False, user=user
    )
    # Straight to the document: updateJob enforces the state machine, and the
    # intermediate transitions are not what this test is about.
    job["status"] = JobStatus.SUCCESS
    Job().save(job)
    folder = Folder().createFolder(
        submission_collection, f"sub-{job['_id']}", parentType="collection", creator=user
    )
    Folder().setMetadata(
        folder,
        {"job_id": str(job["_id"]), "creator_id": str(user["_id"]), "status": status},
    )
    return job, folder


@pytest.mark.plugin("sivacor")
def test_record_outlives_a_user_deleted_submission(
    server, db, user, admin, submission_collection, eagerWorkerTasks
):
    """The reason the collection exists: deletion must not take it."""
    job, folder = make_submission(user, submission_collection)
    assertStatusOk(post_record(server, admin, {"status": "completed"}))

    resp = server.request(
        path=f"/sivacor/submission/{folder['_id']}", method="DELETE", user=user
    )
    assertStatusOk(resp)

    assert Job().load(job["_id"], force=True) is None
    assert record_count() == 1


@pytest.mark.plugin("sivacor")
def test_record_outlives_the_retention_sweep(
    server, db, user, admin, submission_collection
):
    job, folder = make_submission(user, submission_collection)
    assertStatusOk(post_record(server, admin, {"status": "completed"}))

    Setting().set(PluginSettings.RETENTION_DAYS, 0.0)
    long_ago = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=30)
    Folder().collection.update_one(
        {"_id": folder["_id"]}, {"$set": {"created": long_ago}}
    )

    resp = server.request(path="/sivacor/cleanup", method="POST", user=admin)
    assertStatusOk(resp)
    assert resp.json["removed"] == 1

    assert Folder().load(folder["_id"], force=True) is None
    assert record_count() == 1


@pytest.mark.plugin("sivacor")
def test_a_real_run_records_itself_end_to_end(
    server,
    db,
    user,
    eagerWorkerTasks,
    fsAssetstore,
    patched_gpg,
    uploads_folder,
    submission_collection,
):
    """Run an actual submission and check a record comes out the other end.

    The rest of this file drives the endpoint directly, which says nothing
    about whether the pipeline ever calls it -- the worker's own posting is
    best-effort and swallows its exceptions, so a broken round trip would
    otherwise show up as silence rather than a failure.
    """
    fobj = upload_test_file(uploads_folder, user, "with_space_R.zip")
    stages = [
        {"image_name": "rocker/r-ver", "image_tag": "4.3.1", "main_file": "main.R"}
    ]
    resp = submit_sivacor_job(server, user, fobj, stages)
    assertStatusOk(resp)
    # The response is the job as it was when submit_job returned; the eager
    # chain has finished by now, so read the settled status back.
    assert Job().load(resp.json["_id"], force=True)["status"] == JobStatus.SUCCESS

    records = list(ExecutionRecord().find({}))
    assert len(records) == 1
    record = records[0]
    assert record["status"] == "completed"
    assert "error" not in record
    assert record["stages"][0]["image_name"] == "rocker/r-ver"
    assert record["stages"][0]["image_tag"] == "4.3.1"
    assert record["stages"][0]["exit_code"] == 0
    # The numbers the reporting is actually for.
    assert record["stages"][0]["duration_seconds"] > 0
    assert record["total_duration_seconds"] > 0
    assert record["worker"]["ncpu"] > 0
    # Peak workspace disk: this run extracts a package and writes output, so it
    # cannot be zero. A run shorter than one heartbeat never samples inside the
    # poll loop, which is exactly the case the post-exit sample exists for --
    # and this test is that case.
    assert record["stages"][0]["max_disk_bytes"] > 0
    # The image had to be on the machine too, and rocker is not small.
    assert record["stages"][0]["image_size_bytes"] > 100 * 1024**2
    assert record["package_size_bucket"] == "<10MB"
    assert record["date"] == datetime.datetime.now(datetime.timezone.utc).date().isoformat()


@pytest.mark.plugin("sivacor")
def test_a_failing_run_records_why(
    server,
    db,
    user,
    eagerWorkerTasks,
    fsAssetstore,
    patched_gpg,
    uploads_folder,
    submission_collection,
):
    """A main file that is not in the package: the record names the step and
    the reason, and keeps neither the filename nor the path."""
    fobj = upload_test_file(uploads_folder, user, "with_space_R.zip")
    stages = [
        {
            "image_name": "rocker/r-ver",
            "image_tag": "4.3.1",
            "main_file": "definitely_not_here.R",
        }
    ]
    resp = submit_sivacor_job(server, user, fobj, stages, exception=True)
    assertStatusOk(resp)

    records = list(ExecutionRecord().find({}))
    assert len(records) == 1
    assert records[0]["status"] == "failed"
    assert records[0]["error"]["code"] == FailureCode.MAIN_FILE_MISSING.value
    assert records[0]["error"]["step"] == "execute_workflow"
    assert "definitely_not_here" not in json.dumps(records[0], default=str)


@pytest.mark.plugin("sivacor")
def test_reaper_records_the_run_the_worker_could_not(
    server, db, user, admin, submission_collection
):
    """A lost worker never reports, so the server records on its behalf --
    otherwise the failures most worth reporting on are the ones missing."""
    job = Job().createJob(
        title="Stranded", type="sivacor_submission", public=False, user=user
    )
    Job().updateJob(job, "running\n", status=JobStatus.RUNNING)
    folder = Folder().createFolder(
        submission_collection, f"sub-{job['_id']}", parentType="collection", creator=user
    )
    Folder().setMetadata(
        folder,
        {
            "job_id": str(job["_id"]),
            "creator_id": str(user["_id"]),
            "status": "processing",
            "stages": [{"image_name": "dataeditors/stata18_5-mp", "image_tag": "2024"}],
        },
    )
    stale = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=48)
    Job().collection.update_one(
        {"_id": job["_id"]}, {"$set": {"created": stale, "updated": stale}}
    )

    resp = server.request(path="/sivacor/reap", method="POST", user=admin)
    assertStatusOk(resp)
    assert resp.json["reaped"] == [str(job["_id"])]

    records = list(ExecutionRecord().find({}))
    assert len(records) == 1
    assert records[0]["status"] == "reaped"
    assert records[0]["error"]["code"] in {
        FailureCode.REAPED_MAX_RUNTIME.value,
        FailureCode.REAPED_NO_HEARTBEAT.value,
    }
    assert records[0]["stages"][0]["image_name"] == "dataeditors/stata18_5-mp"


# --- cumulative CPU (added 2026-08-12) --------------------------------------


def test_cpu_seconds_total_survives_and_makes_max_cpu_readable():
    """The number that turns `max_cpu_percent` from a curiosity into a measurement.

    `max_cpu_percent` is the peak of a rate sampled over ~1 s windows, so it answers
    "did this ever touch N cores" and not "did this use the machine". On 2026-08-12 a
    stage recorded 906 % while `top` showed a single core busy -- both true, and nothing
    stored could tell them apart. Mean cores is cpu_seconds_total / duration_seconds.
    """
    stored = sanitize(
        status="completed",
        stages=[
            {
                "image_name": "rocker/geospatial",
                "image_tag": "4.3.2",
                "duration_seconds": 990.0,
                "max_cpu_percent": 906.57,
                "cpu_seconds_total": 1009.8,
            }
        ],
    )
    (stage,) = stored["stages"]
    assert stage["cpu_seconds_total"] == 1009.8
    # ~1.02 cores of 16 over the run, which is the honest reading of that 906 % peak.
    assert stage["cpu_seconds_total"] / stage["duration_seconds"] < 1.1


@pytest.mark.parametrize(
    "value", [-1, "12.5", True, float("nan"), float("inf"), None, {"a": 1}]
)
def test_cpu_seconds_total_rejects_anything_that_is_not_a_duration(value):
    """Same allow-list discipline as every other numeric field: drop, never repair."""
    stored = sanitize(
        status="completed",
        stages=[{"image_name": "rocker/r-ver", "cpu_seconds_total": value}],
    )
    (stage,) = stored["stages"]
    assert stage["cpu_seconds_total"] is None
