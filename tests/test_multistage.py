import json
import pytest
from girder.models.file import File
from girder.models.folder import Folder
from girder.models.item import Item
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

    # Verify performance data files are created for each stage
    folder_obj = Folder().load(submission_folder["_id"], force=True)
    performance_files = {}
    for stage_num in range(1, len(stages) + 1):
        performance_filename = f"performance_data_stage_{stage_num}.json"
        performance_items = list(
            Folder().childItems(
                folder_obj,
                filters={"name": performance_filename},
                limit=1,
            )
        )
        assert (
            len(performance_items) == 1
        ), f"Performance data file {performance_filename} not found"

        # Verify performance data content
        with Item().childFiles(performance_items[0]) as files:
            for fobj in files:
                with File().open(fobj) as f:
                    performance_data = json.load(f)
                    performance_files[stage_num] = performance_data

                    # Verify expected system information fields
                    assert "Architecture" in performance_data
                    assert "KernelVersion" in performance_data
                    assert "OperatingSystem" in performance_data
                    assert "OSType" in performance_data
                    assert "MemTotal" in performance_data
                    assert "NCPU" in performance_data

                    # Verify container information fields
                    assert "ImageRepoTags" in performance_data
                    assert "StartedAt" in performance_data
                    assert "FinishedAt" in performance_data

                    # Verify performance metrics (if CSV was processed)
                    if "MaxCPUPercent" in performance_data:
                        assert isinstance(
                            performance_data["MaxCPUPercent"], (int, float)
                        )
                    if "MaxMemoryUsage" in performance_data:
                        assert isinstance(
                            performance_data["MaxMemoryUsage"], (int, float)
                        )

    # Verify stdout content
    stdout = File().load(submission_folder["meta"]["stdout_file_id"], force=True)
    with File().open(stdout) as f:
        stdout_content = f.read()
    stdout_content = stdout_content.decode("utf-8")
    assert "StataNow 18.5" in stdout_content
    assert "Stage 1 Output" in stdout_content
    assert "Stage 2 Output" in stdout_content
