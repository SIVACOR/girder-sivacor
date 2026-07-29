"""
Tests for the one-submission-at-a-time restriction.

A user may only have a single ``sivacor_submission`` job in a non-terminal
state (INACTIVE / QUEUED / RUNNING). Submitting while such a job exists is
rejected with a 409; finished jobs (SUCCESS / ERROR / CANCELED) don't block.
"""

import pytest
from girder_jobs.constants import JobStatus
from girder_jobs.models.job import Job
from girder_sivacor.rest import SIVACOR
from pytest_girder.assertions import assertStatus

from .conftest import submit_sivacor_job, upload_test_file

STAGES = [
    {
        "image_name": "dataeditors/stata18_5-mp",
        "image_tag": "2025-02-26",
        "main_file": "main.do",
    }
]


def make_submission_job(user, status, title="Existing SIVACOR Run"):
    """Create a sivacor_submission job for ``user`` in the given status."""
    job = Job().createJob(
        title=title,
        type="sivacor_submission",
        public=False,
        user=user,
    )
    if status != JobStatus.INACTIVE:
        # SUCCESS/ERROR aren't reachable from INACTIVE, so walk via RUNNING.
        if status in (JobStatus.SUCCESS, JobStatus.ERROR):
            job = Job().updateJob(job, status=JobStatus.RUNNING)
        job = Job().updateJob(job, status=status)
    return job


@pytest.mark.plugin("sivacor")
@pytest.mark.parametrize(
    "status", [JobStatus.INACTIVE, JobStatus.QUEUED, JobStatus.RUNNING]
)
def test_submit_job_rejected_while_submission_active(
    server, db, user, fsAssetstore, uploads_folder, status
):
    """An unfinished submission blocks a new one, whatever its status."""
    fobj = upload_test_file(uploads_folder, user, "test_stata.tar.gz")
    existing = make_submission_job(user, status)

    resp = submit_sivacor_job(server, user, fobj, STAGES)

    assertStatus(resp, 409)
    assert "already have a submission in progress" in resp.json["message"]
    assert resp.json["extra"] == str(existing["_id"])
    # No second job was created.
    assert Job().collection.count_documents({"type": "sivacor_submission"}) == 1


@pytest.mark.plugin("sivacor")
def test_submit_job_allowed_after_previous_finished(
    server,
    db,
    user,
    eagerWorkerTasks,
    fsAssetstore,
    patched_gpg,
    uploads_folder,
    submission_collection,
    email_stdout,
):
    """A user can submit again once their previous submission has finished."""
    fobj = upload_test_file(uploads_folder, user, "test_stata.tar.gz")

    resp = submit_sivacor_job(server, user, fobj, STAGES)
    assertStatus(resp, 200)
    first = Job().load(resp.json["_id"], force=True)
    assert first["status"] == JobStatus.SUCCESS

    # The finished job must not stand in the way of a second submission.
    # A fresh upload is needed: the first file was moved into the submission
    # collection, so the user no longer has ADMIN access to it.
    fobj = upload_test_file(uploads_folder, user, "test_stata.tar.gz")
    resp = submit_sivacor_job(server, user, fobj, STAGES)
    assertStatus(resp, 200)
    assert resp.json["_id"] != str(first["_id"])


@pytest.mark.plugin("sivacor")
@pytest.mark.parametrize(
    "status", [JobStatus.SUCCESS, JobStatus.ERROR, JobStatus.CANCELED]
)
def test_finished_submissions_do_not_block(db, user, status):
    """Terminal statuses are not considered active."""
    make_submission_job(user, status)
    assert SIVACOR._active_submission(user) is None


@pytest.mark.plugin("sivacor")
def test_other_users_submissions_do_not_block(db, user, admin):
    """The limit is per user, not global."""
    make_submission_job(admin, JobStatus.RUNNING)
    assert SIVACOR._active_submission(user) is None
    assert SIVACOR._active_submission(admin) is not None


@pytest.mark.plugin("sivacor")
def test_other_job_types_do_not_block(db, user):
    """Only sivacor_submission jobs count towards the limit."""
    Job().createJob(title="unrelated", type="celery", public=False, user=user)
    assert SIVACOR._active_submission(user) is None


@pytest.mark.plugin("sivacor")
def test_active_submission_reports_oldest(db, user):
    """The error points at the oldest in-flight submission."""
    first = make_submission_job(user, JobStatus.RUNNING, title="first")
    make_submission_job(user, JobStatus.RUNNING, title="second")
    assert SIVACOR._active_submission(user)["_id"] == first["_id"]


@pytest.mark.plugin("sivacor")
def test_older_than_excludes_newer_jobs(db, user):
    """The race-reconciliation query only looks at jobs older than its own."""
    first = make_submission_job(user, JobStatus.RUNNING, title="first")
    second = make_submission_job(user, JobStatus.RUNNING, title="second")
    # The newer job sees the older one and loses; the older one sees nothing.
    assert SIVACOR._active_submission(user, older_than=second["_id"])["_id"] == (
        first["_id"]
    )
    assert SIVACOR._active_submission(user, older_than=first["_id"]) is None
