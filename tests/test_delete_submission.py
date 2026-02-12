import pytest
from girder.models.folder import Folder
from girder_jobs.constants import JobStatus
from girder_jobs.models.job import Job
from pytest_girder.assertions import assertStatus, assertStatusOk


@pytest.mark.plugin("sivacor")
def test_delete_submission_success(
    server,
    db,
    user,
    admin,
    submission_collection,
    eagerWorkerTasks,
):
    """Test successful deletion of a completed submission by the creator."""
    # Create a job
    job = Job().createJob(
        title="Test Job",
        type="sivacor_submission",
        user=user,
    )
    job["status"] = JobStatus.SUCCESS
    Job().save(job)

    # Create a completed submission folder
    submission_folder = Folder().createFolder(
        parent=submission_collection,
        name="test-submission",
        parentType="collection",
        creator=user,
    )

    # Set metadata with completed status
    submission_folder["meta"] = {
        "creator_id": str(user["_id"]),
        "job_id": str(job["_id"]),
        "status": "completed",
    }
    Folder().save(submission_folder)

    # Delete the submission
    resp = server.request(
        path=f"/sivacor/submission/{submission_folder['_id']}",
        method="DELETE",
        user=user,
    )
    assertStatusOk(resp)
    assert "message" in resp.json
    assert "Marked submission" in resp.json["message"]
    assert submission_folder["name"] in resp.json["message"]

    # Verify job was removed
    deleted_job = Job().load(job["_id"], force=True)
    assert deleted_job is None


@pytest.mark.plugin("sivacor")
def test_delete_submission_non_creator(
    server,
    db,
    user,
    admin,
    submission_collection,
):
    """Test that non-creator cannot delete a submission."""
    # Create a job for user
    job = Job().createJob(
        title="Test Job",
        type="sivacor_submission",
        user=user,
    )
    job["status"] = JobStatus.SUCCESS
    Job().save(job)

    # Create a completed submission folder by user
    submission_folder = Folder().createFolder(
        parent=submission_collection,
        name="user-submission",
        parentType="collection",
        creator=user,
    )

    # Set metadata showing user is the creator
    submission_folder["meta"] = {
        "creator_id": str(user["_id"]),
        "job_id": str(job["_id"]),
        "status": "completed",
    }
    Folder().save(submission_folder)

    # Try to delete as admin (different user)
    resp = server.request(
        path=f"/sivacor/submission/{submission_folder['_id']}",
        method="DELETE",
        user=admin,
    )
    # Should return 403 Forbidden
    assertStatus(resp, 403)
    assert "You do not have permission to delete" in resp.json["message"]


@pytest.mark.plugin("sivacor")
def test_delete_submission_non_completed_status(
    server,
    db,
    user,
    admin,
    uploads_folder,
    submission_collection,
):
    """Test that submissions with non-completed/failed status cannot be deleted."""
    # Create a submission folder with 'running' status
    job = Job().createJob(
        title="Test Job",
        type="sivacor_submission",
        user=user,
    )
    job["status"] = JobStatus.RUNNING
    Job().save(job)

    submission_folder = Folder().createFolder(
        parent=submission_collection,
        name="test-submission",
        parentType="collection",
        creator=user,
    )

    # Set metadata with 'running' status
    submission_folder["meta"] = {
        "creator_id": str(user["_id"]),
        "job_id": str(job["_id"]),
        "status": "running",
    }
    Folder().save(submission_folder)

    # Try to delete the running submission
    resp = server.request(
        path=f"/sivacor/submission/{submission_folder['_id']}",
        method="DELETE",
        user=user,
    )
    # Should return 400 Bad Request
    assertStatus(resp, 400)
    assert "Only completed or failed submissions can be deleted" in resp.json["message"]


@pytest.mark.plugin("sivacor")
def test_delete_submission_invalid_parent_folder(
    server,
    db,
    user,
    admin,
    uploads_folder,
    submission_collection,
):
    """Test that submissions not in the submission collection cannot be deleted."""
    # Create a job
    job = Job().createJob(
        title="Test Job",
        type="sivacor_submission",
        user=user,
    )
    job["status"] = JobStatus.SUCCESS
    Job().save(job)

    # Create a folder in user's uploads folder (not in submission collection)
    wrong_folder = Folder().createFolder(
        parent=uploads_folder,
        name="wrong-location",
        parentType="folder",
        creator=user,
    )

    # Set metadata with 'completed' status
    wrong_folder["meta"] = {
        "creator_id": str(user["_id"]),
        "job_id": str(job["_id"]),
        "status": "completed",
    }
    Folder().save(wrong_folder)

    # Try to delete the folder from wrong location
    resp = server.request(
        path=f"/sivacor/submission/{wrong_folder['_id']}",
        method="DELETE",
        user=user,
    )
    # Should return 400 Bad Request
    assertStatus(resp, 400)
    assert "Invalid submission folder" in resp.json["message"]


@pytest.mark.plugin("sivacor")
def test_delete_submission_failed_status(
    server,
    db,
    user,
    admin,
    submission_collection,
    eagerWorkerTasks,
):
    """Test that failed submissions can be deleted."""
    # Create a submission folder with 'failed' status
    job = Job().createJob(
        title="Test Job",
        type="sivacor_submission",
        user=user,
    )
    job["status"] = JobStatus.ERROR
    Job().save(job)

    submission_folder = Folder().createFolder(
        parent=submission_collection,
        name="failed-submission",
        parentType="collection",
        creator=user,
    )

    # Set metadata with 'failed' status
    submission_folder["meta"] = {
        "creator_id": str(user["_id"]),
        "job_id": str(job["_id"]),
        "status": "failed",
    }
    Folder().save(submission_folder)

    # Delete the failed submission
    resp = server.request(
        path=f"/sivacor/submission/{submission_folder['_id']}",
        method="DELETE",
        user=user,
    )
    assertStatusOk(resp)
    assert "message" in resp.json
    assert "Marked submission" in resp.json["message"]


@pytest.mark.plugin("sivacor")
def test_delete_submission_missing_creator_id(
    server,
    db,
    user,
    admin,
    submission_collection,
):
    """Test that submissions without creator_id in metadata cannot be deleted."""
    # Create a submission folder without creator_id
    job = Job().createJob(
        title="Test Job",
        type="sivacor_submission",
        user=user,
    )
    job["status"] = JobStatus.SUCCESS
    Job().save(job)

    submission_folder = Folder().createFolder(
        parent=submission_collection,
        name="no-creator-submission",
        parentType="collection",
        creator=user,
    )

    # Set metadata without creator_id
    submission_folder["meta"] = {
        "job_id": str(job["_id"]),
        "status": "completed",
    }
    Folder().save(submission_folder)

    # Try to delete the submission without creator_id
    resp = server.request(
        path=f"/sivacor/submission/{submission_folder['_id']}",
        method="DELETE",
        user=user,
    )
    # Should return 403 Forbidden
    assertStatus(resp, 403)
    assert "You do not have permission to delete" in resp.json["message"]


@pytest.mark.plugin("sivacor")
def test_delete_submission_without_job(
    server,
    db,
    user,
    admin,
    submission_collection,
    eagerWorkerTasks,
):
    """Test that submissions can be deleted even if the associated job doesn't exist."""
    # Create a job then remove it
    job = Job().createJob(
        title="Test Job",
        type="sivacor_submission",
        user=user,
    )
    job_id = str(job["_id"])
    Job().remove(job)  # Remove the job so it doesn't exist

    # Create a submission folder referencing the non-existent job
    submission_folder = Folder().createFolder(
        parent=submission_collection,
        name="no-job-submission",
        parentType="collection",
        creator=user,
    )

    # Set metadata with completed status and reference to non-existent job
    submission_folder["meta"] = {
        "creator_id": str(user["_id"]),
        "job_id": job_id,
        "status": "completed",
    }
    Folder().save(submission_folder)

    # Delete the submission (should work even though job doesn't exist)
    resp = server.request(
        path=f"/sivacor/submission/{submission_folder['_id']}",
        method="DELETE",
        user=user,
    )
    assertStatusOk(resp)
    assert "message" in resp.json
    assert "Marked submission" in resp.json["message"]
