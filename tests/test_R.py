import pytest
from pytest_girder.assertions import assertStatusOk
from .conftest import (
    upload_test_file,
    submit_sivacor_job,
    get_submission_folder,
    assert_submission_metadata,
)


@pytest.mark.plugin("sivacor")
def test_simple_run(
    server,
    db,
    user,
    eagerWorkerTasks,
    fsAssetstore,
    patched_gpg,
    uploads_folder,
    submission_collection,
):
    """
    Test a complete R submission workflow using mocked GPG.

    This test verifies:
    - File upload to user's uploads folder
    - Job submission with GPG mocked for TRO operations
    - Job completion and status verification
    - Submission folder creation with correct metadata

    Uses the patched_gpg fixture to mock tro_utils.gnupg.GPG automatically.
    """
    # Upload test file
    assert uploads_folder is not None
    fobj = upload_test_file(uploads_folder, user, "with_space_R.zip")

    # Submit SIVACOR R job
    stages = [{"image_name": "rocker/r-ver", "image_tag": "4.3.1", "main_file": "main.R"}]
    resp = submit_sivacor_job(server, user, fobj, stages)
    assertStatusOk(resp)
    job = resp.json

    assert job["status"] == 2  # completed

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
