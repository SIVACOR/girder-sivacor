import pytest
from girder.models.folder import Folder
from girder.models.setting import Setting
from girder_sivacor.settings import PluginSettings
from pytest_girder.assertions import assertStatus, assertStatusOk


@pytest.mark.plugin("sivacor")
def test_submit_job_missing_params(server, user):
    """Test submit_job endpoint with missing parameters."""
    resp = server.request(
        path="/sivacor/submit_job",
        method="POST",
        user=user,
        params={},  # Missing required parameters
    )
    # Should return a client error for missing parameters
    assertStatus(resp, 400)


@pytest.mark.plugin("sivacor")
def test_submit_job_invalid_file_id(server, user):
    """Test submit_job endpoint with invalid file ID."""
    resp = server.request(
        path="/sivacor/submit_job",
        method="POST",
        user=user,
        params={
            "id": "invalid_file_id",
            "image_tag": "rocker/tidyverse:4.3.1",
            "main_file": "test.R",
        },
    )
    # Should return a client error for invalid file ID
    assertStatus(resp, 400)


@pytest.mark.plugin("sivacor")
def test_uploads_folder_created(server, user):
    """Test that uploads folder is created for new users."""
    # Check if uploads folder was created
    uploads_folder = Folder().findOne(
        {
            "name": Setting().get(PluginSettings.UPLOADS_FOLDER_NAME),
            "parentCollection": "user",
            "parentId": user["_id"],
        }
    )
    assert uploads_folder is not None
    assert uploads_folder["name"] == "Uploads"


@pytest.mark.plugin("sivacor")
def test_folder_search_missing_parent_params(server, user):
    """Test folder search with jobId but missing parent parameters."""
    resp = server.request(
        path="/folder",
        method="GET",
        user=user,
        params={
            "jobId": "test_job_123"
            # Missing parentType and parentId
        },
    )
    assertStatus(resp, 400)
