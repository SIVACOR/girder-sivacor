import pytest
from girder_jobs.models.job import Job
from pytest_girder.assertions import assertStatusOk


@pytest.mark.plugin("jobs")
@pytest.mark.plugin("sivacor")
def test_simple_run(
    server,
    db,
    user,
):
    """Test a successful Stata submission workflow."""
    overarching_job = Job().createJob(
        "SIVACOR Run for test-R.zip by Jonh Doe",
        "sivacor_submission",
        user=user,
    )

    workflow_jobs = [
        (
            "Moving test-R.zip to submission collection",
            "celery",
            {
                "args": (str(user["_id"]), "file_id", [], str(overarching_job["_id"])),
                "kwargs": {},
            },
        ),
        (
            "Create Workspace",
            "celery",
            {
                "args": (
                    {"file_id": "file_id", "job_id": str(overarching_job["_id"])},
                ),
                "kwargs": {},
            },
        ),
        (
            "Record initial arrangement",
            "celery",
            {
                "args": ({"job_id": str(overarching_job["_id"])}, "add_arrangement"),
                "kwargs": {},
            },
        ),
    ]
    parentJob = overarching_job
    for job_title, job_type, job_params in workflow_jobs:
        child_job = Job().createJob(
            job_title,
            job_type,
            user=user,
            parentJob=parentJob,
            **job_params,
        )
        parentJob = child_job

    resp = server.request(
        path=f"/job/{overarching_job['_id']}/children",
        method="GET",
        user=user,
    )
    assertStatusOk(resp)
    children = resp.json
    assert len(children) == 3
