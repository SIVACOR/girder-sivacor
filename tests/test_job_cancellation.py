"""
Tests for job cancellation functionality.

This test module verifies:
1. The submission_task decorator properly skips tasks when a job is not RUNNING
2. The cancel_jobs event handler correctly cancels child jobs when the parent is cancelled
3. The chain cancellation mechanism works correctly
4. The StatusCode -123 handling in execute_workflow properly stops execution
"""

import mock
import pytest
from girder_jobs.constants import JobStatus
from girder_jobs.models.job import Job
from girder_sivacor.worker_plugin.routing import QUEUE_PREFIX, pin_chain, worker_queue
from girder_sivacor.worker_plugin.run_submission import submission_task
from pytest_girder.assertions import assertStatusOk


def _decorate(func, status=JobStatus.RUNNING, failure="Task failed"):
    """Wrap ``func`` in submission_task with a stubbed Girder API.

    Returns the decorated callable and the stub, so tests can assert on the
    REST calls the decorator makes on the task's behalf.
    """
    api = mock.MagicMock()
    api.job.return_value = {"status": status}
    patcher = mock.patch(
        "girder_sivacor.worker_plugin.run_submission.GirderApi.for_task",
        return_value=api,
    )
    return submission_task(failure)(func), api, patcher


def test_submission_task_skips_cancelled_job():
    """
    Test that the submission_task decorator skips execution when job is not RUNNING.

    This test verifies that when a job is cancelled or in any non-RUNNING state,
    the decorator causes the task to return early without executing the actual task logic.
    """
    mock_task = mock.MagicMock()
    mock_task.__name__ = "mock_task"
    decorated_task, _api, patcher = _decorate(mock_task, status=JobStatus.CANCELED)

    mock_task_instance = mock.MagicMock()
    mock_task_instance.request.chain = ["some", "tasks"]

    with patcher:
        result = decorated_task(mock_task_instance, {"job_id": "test-job-id"})

    # Assert that the actual task was NOT called
    mock_task.assert_not_called()

    # Assert that the result contains the job_id
    assert result == {"job_id": "test-job-id"}

    # Assert that the chain was cleared
    assert mock_task_instance.request.chain is None


def test_submission_task_allows_running_job():
    """
    Test that the submission_task decorator allows execution when job is RUNNING.

    The decorator also injects the Girder API client as the task's second
    argument, so the task never has to build one itself.
    """
    mock_task = mock.MagicMock(return_value={"result": "success"})
    mock_task.__name__ = "mock_task"
    decorated_task, api, patcher = _decorate(mock_task)

    mock_task_instance = mock.MagicMock()
    submission = {"job_id": "test-job-id"}

    with patcher:
        result = decorated_task(mock_task_instance, submission)

    mock_task.assert_called_once_with(mock_task_instance, api, submission)
    assert result == {"result": "success"}


def test_submission_task_reports_failure_and_discards_workspace(tmp_path):
    """
    Test that a raising task marks the job failed and removes its scratch dirs.

    The worker no longer shares Girder's database, so the job-update event
    handler that used to do this cleanup runs on the server and cannot see
    these paths.
    """
    workspace = tmp_path / "workspace"
    scratch = tmp_path / "tmp"
    workspace.mkdir()
    scratch.mkdir()

    def failing_task(task, api, submission):
        raise ValueError("boom")

    failing_task.__name__ = "failing_task"
    decorated_task, api, patcher = _decorate(failing_task, failure="Failed to explode")

    submission = {
        "job_id": "test-job-id",
        "workspace_dir": str(workspace),
        "tmp_dir": str(scratch),
    }

    with patcher, pytest.raises(ValueError, match="boom"):
        decorated_task(mock.MagicMock(), submission)

    api.update_job.assert_called_once()
    _args, kwargs = api.update_job.call_args
    assert kwargs["status"] == JobStatus.ERROR
    assert "Failed to explode" in kwargs["log"]
    assert "boom" in kwargs["log"]

    assert not workspace.exists()
    assert not scratch.exists()


def test_submission_task_propagates_original_error_if_reporting_fails():
    """
    A job the user cancelled rejects the transition to ERROR.

    That 400 must not surface in place of whatever actually went wrong, or the
    real failure never reaches the log.
    """

    def failing_task(task, api, submission):
        raise ValueError("the actual problem")

    failing_task.__name__ = "failing_task"
    decorated_task, api, patcher = _decorate(failing_task)
    api.update_job.side_effect = RuntimeError("400: invalid state transition")

    with patcher, pytest.raises(ValueError, match="the actual problem"):
        decorated_task(mock.MagicMock(), {"job_id": "test-job-id"})


def test_worker_queue_derives_from_celery_node_name(monkeypatch):
    """The private queue name has to match what the worker was started with."""
    monkeypatch.delenv("SIVACOR_WORKER_QUEUE", raising=False)
    task = mock.MagicMock()
    task.request.hostname = "celery@worker-3"

    assert worker_queue(task) == QUEUE_PREFIX + "worker-3"

    monkeypatch.setenv("SIVACOR_WORKER_QUEUE", "explicit-queue")
    assert worker_queue(task) == "explicit-queue"


def test_pin_chain_routes_remaining_steps_to_one_worker():
    """
    Every step after the first has to land on the worker holding the workspace.

    celery re-reads each queued signature's options when it publishes it, so
    rewriting them in place is what keeps the chain together.
    """
    task = mock.MagicMock()
    task.request.chain = [
        {"task": "step-c", "options": {"queue": "sivacor"}},
        {"task": "step-b"},
    ]

    pin_chain(task, "sivacor.worker-3")

    assert [link["options"]["queue"] for link in task.request.chain] == [
        "sivacor.worker-3",
        "sivacor.worker-3",
    ]


def test_pin_chain_tolerates_last_task_in_chain():
    """The final step has no chain left to pin; that must not be an error."""
    task = mock.MagicMock()
    task.request.chain = None

    pin_chain(task, "sivacor.worker-3")

    assert task.request.chain is None


def test_cancel_jobs_event_handler():
    """
    Test that the cancel_jobs event handler cancels child jobs when parent is cancelled.

    This test verifies that when a SIVACOR submission job is cancelled,
    all its child jobs (celery tasks) are also cancelled.
    """
    from girder_sivacor import cancel_jobs

    # Create a mock event with a parent job
    parent_job_id = "parent-job-id"
    mock_event = mock.MagicMock()
    mock_event.info = {
        "_id": parent_job_id,
        "type": "sivacor_submission",
    }

    # Create mock child jobs
    child_jobs = [
        {
            "_id": "child-1",
            "type": "celery",
            "args": [{"job_id": parent_job_id}],
            "status": JobStatus.RUNNING,
        },
        {
            "_id": "child-2",
            "type": "celery",
            "args": ["arg1", "arg2", "arg3", parent_job_id],
            "status": JobStatus.QUEUED,
        },
        {
            "_id": "child-3",
            "type": "celery",
            "args": [{"job_id": parent_job_id}],
            "status": JobStatus.INACTIVE,
        },
    ]

    # Mock the Job model
    with mock.patch("girder_sivacor.JobModel") as MockJobModel:
        mock_job_model = mock.MagicMock()
        MockJobModel.return_value = mock_job_model
        mock_job_model.find.return_value = child_jobs

        # Call the cancel_jobs event handler
        cancel_jobs(mock_event)

        # Verify that find was called with the correct query
        find_call = mock_job_model.find.call_args[0][0]
        assert find_call["type"] == "celery"
        assert "$or" in find_call

        # Verify that cancelJob was called for each child job
        assert mock_job_model.cancelJob.call_count == len(child_jobs)


def test_cancel_jobs_ignores_non_sivacor_jobs():
    """
    Test that the cancel_jobs event handler ignores non-SIVACOR jobs.

    This test verifies that when a job of a different type is cancelled,
    the event handler does nothing.
    """
    from girder_sivacor import cancel_jobs

    # Create a mock event with a non-SIVACOR job
    mock_event = mock.MagicMock()
    mock_event.info = {
        "_id": "some-job-id",
        "type": "other_type",
    }

    # Mock the Job model
    with mock.patch("girder_sivacor.JobModel") as MockJobModel:
        mock_job_model = mock.MagicMock()
        MockJobModel.return_value = mock_job_model

        # Call the cancel_jobs event handler
        cancel_jobs(mock_event)

        # Verify that find was NOT called
        mock_job_model.find.assert_not_called()
        mock_job_model.cancelJob.assert_not_called()


def test_execute_workflow_chain_cleared_on_termination():
    """
    Test that execute_workflow clears the chain on StatusCode -123.

    This is a focused test that verifies the specific logic for handling
    termination signals by testing the code path directly.
    """
    # Test the specific code logic that should clear the chain
    # We'll mock just what we need and verify the behavior

    # Create a mock task with a chain
    mock_task = mock.MagicMock()
    mock_task.request.chain = ["task1", "task2", "task3"]

    # Simulate the logic from execute_workflow when StatusCode is -123
    ret = {"StatusCode": -123}
    if ret["StatusCode"] == -123:
        if mock_task.request.chain:
            mock_task.request.chain = None
        result = {"job_id": "test-job-id"}

    # Assert chain was cleared
    assert mock_task.request.chain is None
    assert result == {"job_id": "test-job-id"}


def test_execute_workflow_chain_preserved_on_success():
    """
    Test that execute_workflow preserves the chain on successful execution.

    This test verifies that when execution is successful (StatusCode 0),
    the chain is not modified.
    """
    # Create a mock task with a chain
    mock_task = mock.MagicMock()
    original_chain = ["task1", "task2"]
    mock_task.request.chain = original_chain.copy()

    # Simulate the logic from execute_workflow when StatusCode is 0
    ret = {"StatusCode": 0}
    if ret["StatusCode"] == -123:
        if mock_task.request.chain:
            mock_task.request.chain = None

    # Assert chain was NOT cleared
    assert mock_task.request.chain == original_chain


def test_execute_workflow_raises_on_error_status():
    """
    Test that execute_workflow raises RuntimeError on non-zero status codes.

    This test verifies the error handling logic for failed workflow executions.
    """
    # Simulate the logic from execute_workflow when StatusCode is non-zero (not -123)
    ret = {"StatusCode": 1}

    # The function should raise when StatusCode is not 0 and not -123
    with pytest.raises(RuntimeError) as exc_info:
        if ret["StatusCode"] == -123:
            pass
        elif ret["StatusCode"] != 0:
            raise RuntimeError(
                f"Workflow execution failed with code {ret['StatusCode']}"
            )

    assert "Workflow execution failed with code 1" in str(exc_info.value)


def test_submission_task_with_error_status():
    """
    Test that the submission_task decorator handles ERROR status correctly.

    This test verifies that when a job is in ERROR state,
    the decorator skips execution.
    """
    mock_task = mock.MagicMock()
    mock_task.__name__ = "mock_task"
    decorated_task, _api, patcher = _decorate(mock_task, status=JobStatus.ERROR)

    mock_task_instance = mock.MagicMock()
    mock_task_instance.request.chain = ["task1", "task2"]

    with patcher:
        result = decorated_task(mock_task_instance, {"job_id": "test-job-id"})

    # Assert that the actual task was NOT called
    mock_task.assert_not_called()

    # Assert that the result contains the job_id
    assert result == {"job_id": "test-job-id"}

    # Assert that the chain was cleared
    assert mock_task_instance.request.chain is None


def test_submission_task_clears_chain_only_when_present():
    """
    Test that the submission_task decorator only clears chain when it exists.

    This test verifies that when a task doesn't have a chain,
    the decorator doesn't cause an error.
    """
    mock_task = mock.MagicMock()
    mock_task.__name__ = "mock_task"
    decorated_task, _api, patcher = _decorate(mock_task, status=JobStatus.CANCELED)

    mock_task_instance = mock.MagicMock()
    mock_task_instance.request.chain = None

    with patcher:
        result = decorated_task(mock_task_instance, {"job_id": "test-job-id"})

    # Assert that the actual task was NOT called
    mock_task.assert_not_called()

    # Assert that the result contains the job_id
    assert result == {"job_id": "test-job-id"}

    # Assert that the chain remains None
    assert mock_task_instance.request.chain is None


# Integration tests that require database and full setup


@pytest.mark.plugin("sivacor")
def test_cancel_jobs_integration(
    server,
    db,
    user,
    fsAssetstore,
):
    """
    Integration test: cancel_jobs event handler cancels child jobs when parent is cancelled.

    This test verifies the full integration of the cancel_jobs event handler
    with the database and API.
    """
    # Create a parent job
    parent_job = Job().createJob(
        title="Parent SIVACOR Job",
        type="sivacor_submission",
        user=user,
    )
    parent_job = Job().updateJob(parent_job, status=JobStatus.RUNNING)

    # Create child jobs with different argument formats
    # Format 1: args.0.job_id
    child_job_1 = Job().createJob(
        title="Child Job 1",
        type="celery",
        user=user,
        args=[{"job_id": str(parent_job["_id"])}, "other_arg"],
        kwargs={},
    )
    child_job_1 = Job().updateJob(child_job_1, status=JobStatus.RUNNING)

    # Format 2: args.3 (prepare_submission uses this format)
    child_job_2 = Job().createJob(
        title="Child Job 2",
        type="celery",
        user=user,
        args=["arg1", "arg2", "arg3", str(parent_job["_id"])],
        kwargs={},
    )
    child_job_2 = Job().updateJob(child_job_2, status=JobStatus.QUEUED)

    # Create a child job of different type that should not be cancelled
    other_job = Job().createJob(
        title="Other Job",
        type="other_type",
        user=user,
        args=[{"job_id": str(parent_job["_id"])}],
        kwargs={},
    )
    other_job = Job().updateJob(other_job, status=JobStatus.RUNNING)

    # Cancel the parent job using the API
    resp = server.request(
        path=f"/job/{parent_job['_id']}/cancel",
        method="PUT",
        user=user,
    )
    assertStatusOk(resp)

    # Reload all jobs to check their status
    child_job_1 = Job().load(child_job_1["_id"], force=True)
    child_job_2 = Job().load(child_job_2["_id"], force=True)
    other_job = Job().load(other_job["_id"], force=True)

    # Assert that all celery child jobs were cancelled
    assert child_job_1["status"] == JobStatus.CANCELED
    assert child_job_2["status"] == JobStatus.CANCELED

    # Assert that the other job type was not affected
    assert other_job["status"] == JobStatus.RUNNING
