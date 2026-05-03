import json
import os
import tarfile
import tempfile
import zipfile

import pytest
from girder.models.file import File
from girder_jobs.constants import JobStatus
from girder_jobs.models.job import Job
from pytest_girder.assertions import assertStatusOk

from .conftest import (
    assert_submission_metadata,
    get_submission_folder,
    submit_sivacor_job,
    upload_test_file,
)


@pytest.mark.plugin("sivacor")
@pytest.mark.parametrize("testFile", ["test_stata.tar.gz"])
def test_ignore(
    server,
    db,
    user,
    eagerWorkerTasks,
    fsAssetstore,
    patched_gpg,
    uploads_folder,
    submission_collection,
    testFile,
):
    """Test a successful Stata submission workflow with .sivacorignore file"""
    image_name = "dataeditors/stata18_5-mp"
    image_tag = "2025-02-26"
    main_file = "main.do"
    stages = [
        {"image_name": image_name, "image_tag": image_tag, "main_file": main_file}
    ]

    # Upload test file
    assert uploads_folder is not None

    # inject .sivacorignore file into the test archive before uploading
    with (
        tempfile.NamedTemporaryFile(suffix=".tar.gz") as temp_archive,
        tempfile.TemporaryDirectory() as temp_dir,
    ):
        test_dir = os.path.dirname(os.path.abspath(__file__))
        filepath = os.path.join(test_dir, "data", testFile)
        with tarfile.open(filepath, "r:gz") as tar:
            tar.extractall(path=temp_dir, filter="data")

        # Create .sivacorignore file in the extracted directory
        ignore_file_path = os.path.join(
            temp_dir, "sivacor-test-stata", ".sivacorignore"
        )
        with open(ignore_file_path, "w") as f:
            f.write("ado/\n")
            f.write("fail.do\n")

        # Recreate the archive with the .sivacorignore file included
        with tarfile.open(temp_archive.name, "w:gz") as tar:
            tar.add(temp_dir, arcname=".")

        # Upload the modified archive
        fobj = upload_test_file(uploads_folder, user, temp_archive.name)

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
    assert "StataNow 18.5" in stdout_content.decode("utf-8")

    # Verify that files in the ado/ directory were ignored
    replpack_file_id = submission_folder["meta"]["replpack_file_id"]
    replpack_file = File().load(replpack_file_id, force=True)
    with File().open(replpack_file) as f:
        with zipfile.ZipFile(f) as zipf:
            file_names = zipf.namelist()
            print("Files in replpack archive:", file_names)
            assert not any("ado/" in name for name in file_names)
            assert not any(name.endswith("fail.do") for name in file_names)

    # Verify that TRO has two TRPs and 3 arrangements
    tro_file_id = submission_folder["meta"]["tro_file_id"]
    tro_file = File().load(tro_file_id, force=True)
    with File().open(tro_file) as f:
        tro = json.load(f)
    graph = tro["@graph"][0]
    assert len(graph["trov:hasPerformance"]) == 2
    assert "pruning" in graph["trov:hasPerformance"][-1]["rdfs:comment"]
    assert len(graph["trov:hasArrangement"]) == 3
