import os
import tarfile
import tempfile

import pytest
from girder.models.file import File
from girder_jobs.constants import JobStatus
from pytest_girder.assertions import assertStatusOk

from .conftest import (
    assert_submission_metadata,
    get_submission_folder,
    submit_sivacor_job,
    upload_test_file,
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
    stages = [
        {"image_name": "rocker/r-ver", "image_tag": "4.3.1", "main_file": "main.R"}
    ]
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


@pytest.mark.plugin("sivacor")
def test_secrets(
    server,
    db,
    user,
    eagerWorkerTasks,
    fsAssetstore,
    patched_gpg,
    uploads_folder,
    submission_collection,
):
    """Test if secrets are properly passed to the R job."""
    main_file = "main.R"
    stages = [
        {"image_name": "rocker/r-ver", "image_tag": "4.3.1", "main_file": main_file}
    ]
    secrets = [{"key": "SECRET", "value": "my_secret_value"}]
    with (
        tempfile.NamedTemporaryFile(suffix=".tar.gz") as temp_archive,
        tempfile.TemporaryDirectory() as temp_dir,
    ):
        with open(os.path.join(temp_dir, main_file), "w") as f:
            f.write("cat(Sys.getenv('SECRET'))\n")

        with tarfile.open(temp_archive.name, "w:gz") as tar:
            tar.add(temp_dir, arcname=".")

        # Upload the modified archive
        fobj = upload_test_file(uploads_folder, user, temp_archive.name)

    resp = submit_sivacor_job(server, user, fobj, stages, secrets=secrets)
    assertStatusOk(resp)
    job = resp.json

    # Verify job failed due to command failure (since the secret value will cause an unrecognized command)
    # but the secret value should not appear in the stdout, nor job logs
    resp = server.request(
        path=f"/job/{job['_id']}",
        method="GET",
        user=user,
    )
    assertStatusOk(resp)
    job = resp.json
    assert job["status"] == JobStatus.SUCCESS

    log_content = "".join(job["log"])
    assert "my_secret_value" not in log_content

    # Get submission folder and verify metadata
    resp = get_submission_folder(server, user, job["_id"], submission_collection)
    assertStatusOk(resp)
    assert len(resp.json) == 1

    submission_folder = resp.json[0]
    metadata = submission_folder["meta"]

    # Verify stdout does not contain the secret value but contains the expected output
    stdout = File().load(metadata["stdout_file_id"], force=True)
    with File().open(stdout) as f:
        stdout_content = f.read()
    assert "my_secret_value" not in stdout_content.decode("utf-8")
    assert "SECRET_REDACTED" in stdout_content.decode("utf-8")
