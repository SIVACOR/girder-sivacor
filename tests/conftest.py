import json
from typing import Any, Generator
import os
import mock
import pytest
from girder.models.collection import Collection
from girder.models.folder import Folder
from girder.models.setting import Setting
from girder.models.upload import Upload
from girder_sivacor.settings import PluginSettings


@pytest.fixture
def gpg_mock() -> mock.MagicMock:
    """
    Mock GPG object for testing TRO utils GPG functionality.

    Returns a properly configured MagicMock that simulates GPG operations
    needed for SIVACOR submission workflows.

    Example usage:
        def test_custom_gpg_behavior(gpg_mock):
            # Customize mock for specific test needs
            gpg_mock.sign.return_value = "custom_signature"

            with mock.patch("tro_utils.tro_utils.gnupg.GPG", return_value=gpg_mock):
                # Your test code here
                pass
    """
    gpg_mock = mock.MagicMock()

    # Mock list_keys response with key_map
    keys_mock = mock.MagicMock()
    keys_mock.key_map = {
        "fingerprint": {"keyid": "dummykeyid"},
    }
    gpg_mock.list_keys.return_value = keys_mock

    # Mock export and signing operations
    gpg_mock.export_keys.return_value = "dummypublickey"
    gpg_mock.sign.return_value = "dummysignature"

    return gpg_mock


@pytest.fixture
def patched_gpg(
    gpg_mock: mock.MagicMock,
) -> Generator[mock.MagicMock | mock.AsyncMock, Any, None]:
    """
    Context manager fixture that patches tro_utils GPG with the mock.

    This fixture automatically handles the patching and cleanup,
    making it easy to use in tests that need GPG functionality.

    Example usage:
        def test_with_mocked_gpg(patched_gpg):
            # GPG is automatically mocked, no manual patching needed
            # Your test code here
            pass

    The gpg_mock is available as patched_gpg.return_value if you need
    to access or customize it during the test.
    """
    with mock.patch("tro_utils.tro_utils.gnupg.GPG", return_value=gpg_mock) as patch:
        yield patch


@pytest.fixture
def uploads_folder(user):
    """
    Get the user's uploads folder.

    Returns the uploads folder for the given user, which is automatically
    created when the user is created by the SIVACOR plugin.
    """
    return Folder().findOne(
        {
            "name": Setting().get(PluginSettings.UPLOADS_FOLDER_NAME),
            "parentCollection": "user",
            "parentId": user["_id"],
        }
    )


@pytest.fixture
def submission_collection():
    """
    Get or create the submissions collection.

    Returns the collection where submission folders are stored.
    Creates the collection if it doesn't exist.
    """
    collection_name = Setting().get(PluginSettings.SUBMISSION_COLLECTION_NAME)
    collection = Collection().findOne({"name": collection_name})
    if not collection:
        # Create the collection if it doesn't exist (needed for tests)
        collection = Collection().createCollection(
            name=collection_name,
            description="Test submissions collection",
            public=True,
            creator=None,  # System collection
        )
    return collection


def upload_test_file(uploads_folder, user, filename):
    """
    Helper function to upload a test file to the uploads folder.

    Args:
        uploads_folder: The folder to upload to
        user: The user uploading the file
        filename: Name of the file in the test data directory

    Returns:
        The uploaded file object
    """
    # Get the correct path to test data directory
    test_dir = os.path.dirname(os.path.abspath(__file__))
    filepath = os.path.join(test_dir, "data", filename)
    with open(filepath, "rb") as f:
        return Upload().uploadFromFile(
            f,
            size=os.path.getsize(filepath),
            name=filename,
            parentType="folder",
            parent=uploads_folder,
            user=user,
        )


def submit_sivacor_job(server, user, file_obj, stages):
    """
    Helper function to submit a SIVACOR job.

    Args:
        server: The test server
        user: The user submitting the job
        file_obj: The uploaded file object
        image_tag: Docker image tag to use
        main_file: Main file to execute

    Returns:
        The server response
    """
    return server.request(
        path="/sivacor/submit_job",
        method="POST",
        user=user,
        params={
            "id": str(file_obj["_id"]),
            "stages": json.dumps(stages),
        },
    )


def get_submission_folder(server, user, job_id, submission_collection):
    """
    Helper function to get the submission folder for a job.

    Args:
        server: The test server
        user: The user
        job_id: The job ID to search for
        submission_collection: The submissions collection

    Returns:
        The submission folder response
    """
    return server.request(
        path="/folder",
        method="GET",
        user=user,
        params={
            "jobId": str(job_id),
            "parentType": "collection",
            "parentId": str(submission_collection["_id"]),
        },
    )


def assert_submission_metadata(
    metadata, user, job_id, stages, status, expected_files
):
    """
    Helper function to assert submission folder metadata.

    Args:
        metadata: The folder metadata to check
        user: The expected user
        job_id: The expected job ID
        image_tag: The expected image tag
        main_file: The expected main file
        status: The expected status
        expected_files: List of file keys that should be present in metadata
    """
    assert metadata["creator_id"] == str(user["_id"])
    assert metadata["job_id"] == str(job_id)
    assert metadata["stages"] == stages

    for key in expected_files:
        assert key in metadata
