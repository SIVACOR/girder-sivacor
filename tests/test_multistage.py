import pytest
from girder.models.file import File
from girder_jobs.constants import JobStatus
from girder_jobs.models.job import Job
from pytest_girder.assertions import assertStatusOk
from .conftest import (
    upload_test_file,
    submit_sivacor_job,
    get_submission_folder,
    assert_submission_metadata,
)


@pytest.mark.plugin("sivacor")
def test_multistage_run(
    server,
    db,
    user,
    eagerWorkerTasks,
    fsAssetstore,
    patched_gpg,
    uploads_folder,
    submission_collection,
):
    """Test a successful Stata submission workflow."""
    stages = [
        {
            "image_name": "dataeditors/stata18_5-mp",
            "image_tag": "2025-02-26",
            "main_file": "main.do",
        },
        {"image_name": "rocker/r-ver", "image_tag": "4.3.1", "main_file": "main.R"},
    ]

    # Upload test file
    assert uploads_folder is not None
    fobj = upload_test_file(uploads_folder, user, "test_stata.tar.gz")

    # Submit SIVACOR Stata job
    resp = submit_sivacor_job(server, user, fobj, stages)
    assertStatusOk(resp)
    job = resp.json

    # Verify job completion
    job = Job().load(job["_id"], force=True)
    assert job["status"] == JobStatus.SUCCESS

    # Get submission folder and verify metadata
    resp = get_submission_folder(server, user, job["_id"], submission_collection)
    assertStatusOk(resp)
    assert len(resp.json) == 1

    submission_folder = resp.json[0]
    expected_files = [
        "tro_file_id",
        "stdout_file_id",
        "stderr_file_id",
        "tsr_file_id",
        "replpack_file_id",
    ]
    assert_submission_metadata(
        submission_folder["meta"],
        user,
        job["_id"],
        stages,
        "completed",
        expected_files,
    )

    # Verify stdout content
    stdout = File().load(submission_folder["meta"]["stdout_file_id"], force=True)
    with File().open(stdout) as f:
        stdout_content = f.read()
    stdout_content = stdout_content.decode("utf-8")
    assert "StataNow 18.5" in stdout_content
    assert "Stage 1 Output" in stdout_content
    assert "Stage 2 Output" in stdout_content
