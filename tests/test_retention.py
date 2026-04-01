import datetime

import pytest
from girder.exceptions import ValidationException
from girder.models.folder import Folder
from girder.models.setting import Setting
from girder_jobs.constants import JobStatus
from girder_jobs.models.job import Job
from girder_sivacor.settings import PluginSettings
from pytest_girder.assertions import assertStatusOk

from .conftest import (
    get_submission_folder,
    submit_sivacor_job,
    upload_test_file,
)


@pytest.mark.plugin("sivacor")
def test_retention_cleanup_old_folders(
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
    Test that cleanup_submissions deletes submission folders older than 30 days.

    The periodic cleanup task removes entire submission folders whose ``created``
    timestamp is more than 30 days in the past.  It does not filter by item size.
    """
    stages = [
        {"image_name": "rocker/r-ver", "image_tag": "4.3.1", "main_file": "main.R"}
    ]
    test_file = upload_test_file(uploads_folder, user, "with_space_R.zip")
    resp = submit_sivacor_job(server, user, test_file, stages)
    assertStatusOk(resp)
    job = resp.json

    job_obj = Job().load(job["_id"], force=True)
    assert job_obj["status"] == JobStatus.SUCCESS

    resp = get_submission_folder(server, user, job["_id"], submission_collection)
    assertStatusOk(resp)
    assert len(resp.json) == 1
    folder_id_str = resp.json[0]["_id"]
    submission_folder = Folder().load(folder_id_str, force=True)

    # Simulate aging — backdate the folder by more than 30 days
    old_date = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        days=31
    )
    Folder().update({"_id": submission_folder["_id"]}, {"$set": {"created": old_date}})

    # Verify folder exists before cleanup
    assert Folder().load(folder_id_str, force=True) is not None

    from girder_sivacor.worker_plugin.run_submission import cleanup_submissions

    cleanup_submissions()

    # Old folder must have been deleted
    assert Folder().load(folder_id_str, force=True) is None


@pytest.mark.plugin("sivacor")
def test_retention_preserves_recent_folders(
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
    Test that cleanup_submissions does NOT delete recently created folders.

    A submission folder created less than 30 days ago must survive the periodic
    cleanup run.
    """
    stages = [
        {"image_name": "rocker/r-ver", "image_tag": "4.3.1", "main_file": "main.R"}
    ]
    test_file = upload_test_file(uploads_folder, user, "with_space_R.zip")
    resp = submit_sivacor_job(server, user, test_file, stages)
    assertStatusOk(resp)
    job = resp.json

    job_obj = Job().load(job["_id"], force=True)
    assert job_obj["status"] == JobStatus.SUCCESS

    resp = get_submission_folder(server, user, job["_id"], submission_collection)
    assertStatusOk(resp)
    assert len(resp.json) == 1
    folder_id_str = resp.json[0]["_id"]

    # Folder was just created — well within the 30-day cutoff
    from girder_sivacor.worker_plugin.run_submission import cleanup_submissions

    cleanup_submissions()

    # Recent folder must still exist
    assert Folder().load(folder_id_str, force=True) is not None


@pytest.mark.plugin("sivacor")
def test_retention_settings_validation(server, db, user):
    """Test that retention settings are properly validated."""

    # RETENTION_DAYS accepts a non-negative float
    Setting().set(PluginSettings.RETENTION_DAYS, 7.5)
    assert Setting().get(PluginSettings.RETENTION_DAYS) == 7.5

    # Negative float is invalid for RETENTION_DAYS
    with pytest.raises(ValidationException):
        Setting().set(PluginSettings.RETENTION_DAYS, -1.0)

    # Zero is a valid (immediate) retention period
    Setting().set(PluginSettings.RETENTION_DAYS, 0.0)
    assert Setting().get(PluginSettings.RETENTION_DAYS) == 0.0

    # Fractional-day edge case: 1 second expressed as days
    Setting().set(PluginSettings.RETENTION_DAYS, 1 / 86400)
    assert Setting().get(PluginSettings.RETENTION_DAYS) == 1 / 86400

    # Verify default after unset
    Setting().unset(PluginSettings.RETENTION_DAYS)
    assert Setting().get(PluginSettings.RETENTION_DAYS) == 7  # Default: 7 days
