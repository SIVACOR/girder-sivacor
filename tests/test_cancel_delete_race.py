"""The cancel/delete race of 2026-09-03, from both ends.

A cancel is a two-party operation: Girder revokes the task, and the *worker*
then spends around twelve seconds stopping the container and uploading the
run's performance data, stdout, stderr and dockerstats. Marking the submission
folder terminal on the first of those made it deletable during the second, and
a researcher who cancelled and then deleted eight seconds later had their
worker's first upload answered ``No such folder``.

Two fixes, tested here together because neither is much use alone:

* the folder says ``canceling`` until the worker says otherwise, which closes
  the window; and
* a write that loses the race anyway is recorded as ``submission_deleted``
  rather than as ``unexpected``/``HttpError``, the code reserved for defects.

Plus the two ways the transitional status could otherwise strand a submission
its owner can no longer delete: a worker that dies mid-cancel (settled by
``/sivacor/reap``) and a revoke that lands during ``upload_workspace``, the one
step ``submission_task`` does not wrap (settled by ``finalize_job``).

See ``development_notes/incidents/2026-09-03-cancel-delete-race.md``.
"""

import datetime

import mock
import pytest
from girder.models.folder import Folder
from girder_client import HttpError
from girder_jobs.constants import JobStatus
from girder_jobs.models.job import Job
from girder_sivacor.errors import FailureCode, SubmissionError
from girder_sivacor.statuses import CANCELING, COMPLETED, DELETABLE, FAILED
from girder_sivacor.worker_plugin.girder_api import GirderApi
from girder_sivacor.worker_plugin.run_submission import (
    _classify_vanished_submission,
    abandon,
    finalize_job,
    settle_canceling_folder,
    submission_task,
)
from pytest_girder.assertions import assertStatus, assertStatusOk

from .test_reaper import backdate, make_submission, reap


# --- the vocabulary itself ------------------------------------------------


def test_canceling_is_not_deletable():
    """The guard in delete_submission is this tuple; state it as a fact.

    ``delete_submission`` needed no change for this incident -- the whole fix
    on that side is that CANCELING is absent here. A future edit that "tidies"
    it in would silently restore the race, with no other test failing.
    """
    assert CANCELING not in DELETABLE
    assert DELETABLE == (COMPLETED, FAILED)


# --- the server half: the status a cancel now writes ----------------------


@pytest.mark.plugin("sivacor")
def test_cancel_marks_folder_transitional_not_failed(
    server, db, admin, user, submission_collection
):
    """A cancelled job's folder says `canceling`, because the worker is not done."""
    job, folder = make_submission(user, submission_collection)

    with mock.patch("smtplib.SMTP") as smtp_class:
        smtp_class.return_value = mock.MagicMock()
        Job().updateJob(job, "Cancelled\n", status=JobStatus.CANCELED)

    assert Folder().load(folder["_id"], force=True)["meta"]["status"] == CANCELING


@pytest.mark.plugin("sivacor")
def test_error_marks_folder_failed_immediately(
    server, db, admin, user, submission_collection
):
    """ERROR gets no transitional state, and must not acquire one.

    A step that raises has already finished its write-back -- the transition to
    ERROR is the last thing the worker does, not the first -- so a failed
    submission is deletable at once, as it always was.
    """
    job, folder = make_submission(user, submission_collection)

    with mock.patch("smtplib.SMTP") as smtp_class:
        smtp_class.return_value = mock.MagicMock()
        Job().updateJob(job, "Boom\n", status=JobStatus.ERROR)

    assert Folder().load(folder["_id"], force=True)["meta"]["status"] == FAILED


def make_owned_submission(user, submission_collection, status):
    """A submission the delete endpoint will accept as the user's own.

    ``make_submission`` from test_reaper builds a folder with no creator, which
    is all the reaper needs; ``delete_submission`` additionally requires READ
    access on the folder *and* a matching ``meta.creator_id``.
    """
    job = Job().createJob(
        title="Cancelled submission",
        type="sivacor_submission",
        public=False,
        user=user,
    )
    job = Job().updateJob(job, "Preparing\n", status=JobStatus.RUNNING)
    folder = Folder().createFolder(
        submission_collection,
        f"submission-{job['_id']}",
        parentType="collection",
        public=False,
        creator=user,
    )
    Folder().setMetadata(
        folder,
        {
            "job_id": str(job["_id"]),
            "creator_id": str(user["_id"]),
            "status": status,
        },
    )
    return job, folder


@pytest.mark.plugin("sivacor")
def test_delete_is_refused_while_canceling(
    server, db, user, submission_collection
):
    """The click that lost the race in production now gets an answer instead.

    Refusing is the fix; the message is the other half of it. "Only completed
    or failed submissions can be deleted" is baffling in front of a panel the
    UI has just labelled *Job Canceled*.
    """
    job, folder = make_owned_submission(user, submission_collection, CANCELING)

    resp = server.request(
        path=f"/sivacor/submission/{folder['_id']}", method="DELETE", user=user
    )
    assertStatus(resp, 400)
    assert "still saving" in resp.json["message"]

    # And nothing was taken down on the way out: the folder is what the
    # worker is still writing to.
    assert Folder().load(folder["_id"], force=True) is not None
    assert Job().load(job["_id"], force=True) is not None


@pytest.mark.plugin("sivacor")
def test_delete_is_allowed_once_settled(
    server, db, user, submission_collection, eagerWorkerTasks
):
    """...and permitted the moment the worker writes the terminal status.

    ``eagerWorkerTasks`` because a permitted delete dispatches
    ``deleteFolderTask``, which otherwise wants a live broker.
    """
    _job, folder = make_owned_submission(user, submission_collection, FAILED)

    resp = server.request(
        path=f"/sivacor/submission/{folder['_id']}", method="DELETE", user=user
    )
    assertStatusOk(resp)


# --- the worker half: settling the transitional status --------------------


def _api(status=CANCELING):
    api = mock.MagicMock()
    api.folder.return_value = {"_id": "folder-id", "meta": {"status": status}}
    return api


def test_settle_writes_terminal_status_over_canceling():
    api = _api()
    settle_canceling_folder(api, {"folder_id": "folder-id"})
    api.set_folder_metadata.assert_called_once_with("folder-id", {"status": FAILED})


@pytest.mark.parametrize("status", [COMPLETED, FAILED, "processing", None])
def test_settle_touches_nothing_else(status):
    """Only the transitional value is ours to overwrite.

    ``abandon()`` is also reached from ``submission_task``'s pre-flight check,
    which fires for *any* job that has left RUNNING -- including one that
    errored, whose folder already says something true.
    """
    api = _api(status)
    settle_canceling_folder(api, {"folder_id": "folder-id"})
    api.set_folder_metadata.assert_not_called()


def test_settle_without_a_folder_is_a_no_op():
    """Cancelled before prepare_submission ran: there is no folder to settle."""
    api = _api()
    settle_canceling_folder(api, {"job_id": "job-id"})
    api.folder.assert_not_called()
    api.set_folder_metadata.assert_not_called()


def test_settle_survives_the_folder_having_been_deleted():
    """The likeliest failure, and not an error.

    A submission that reached a genuinely terminal state and was deleted by its
    owner has nothing left to settle. Raising here would turn that into a
    failed task on the way out of a chain that is already finished.
    """
    api = _api()
    api.folder.side_effect = HttpError(400, "No such folder", "url", "GET")
    settle_canceling_folder(api, {"folder_id": "folder-id"})
    api.set_folder_metadata.assert_not_called()


def test_abandon_settles_and_clears_the_chain():
    api = _api()
    task = mock.MagicMock()
    task.request.chain = ["some", "tasks"]

    result = abandon(task, api, {"job_id": "job-id", "folder_id": "folder-id"})

    assert result == {"job_id": "job-id"}
    assert task.request.chain is None
    api.set_folder_metadata.assert_called_once_with("folder-id", {"status": FAILED})


def test_finalize_job_settles_a_cancel_that_arrived_during_upload():
    """finalize_job is the only chain step submission_task does not wrap.

    A revoke during ``upload_workspace`` leaves the folder CANCELING and the
    chain running to its end, so without this the submission would be
    undeletable until /sivacor/reap noticed, up to half an hour later.
    """
    api = _api()
    api.job.return_value = {"status": JobStatus.CANCELED}

    with mock.patch(
        "girder_sivacor.worker_plugin.run_submission.GirderApi.for_task",
        return_value=api,
    ):
        # bind=True, so celery passes the task instance itself as `task`.
        finalize_job({"job_id": "job-id", "folder_id": "folder-id"})

    api.set_folder_metadata.assert_called_once_with("folder-id", {"status": FAILED})


def test_finalize_job_does_not_settle_a_successful_run():
    api = _api(COMPLETED)
    api.job.return_value = {"status": JobStatus.RUNNING}

    with mock.patch(
        "girder_sivacor.worker_plugin.run_submission.GirderApi.for_task",
        return_value=api,
    ):
        finalize_job({"job_id": "job-id", "folder_id": "folder-id"})

    api.set_folder_metadata.assert_not_called()


# --- classifying a write that lost the race anyway ------------------------


def _http_error(status=400):
    return HttpError(status, f"No such folder: deadbeef ({status})", "url", "POST")


def test_vanished_submission_is_classified_not_unexpected():
    api = mock.MagicMock()
    api.folder_exists.return_value = False
    exc = _http_error()

    out = _classify_vanished_submission(api, {"folder_id": "gone"}, exc)

    assert isinstance(out, SubmissionError)
    assert out.code is FailureCode.SUBMISSION_DELETED
    assert out.detail is None


def test_http_error_against_a_live_folder_is_left_alone():
    """A 400 with the folder still there is a real failure; do not relabel it.

    Decided by asking Girder whether the folder exists rather than by reading
    its error text -- the message is upstream's, and this is the question that
    actually matters.
    """
    api = mock.MagicMock()
    api.folder_exists.return_value = True
    exc = _http_error(500)

    assert _classify_vanished_submission(api, {"folder_id": "here"}, exc) is exc


def test_non_http_failures_are_never_reclassified():
    api = mock.MagicMock()
    api.folder_exists.return_value = False
    exc = ValueError("a genuine bug")

    assert _classify_vanished_submission(api, {"folder_id": "gone"}, exc) is exc
    api.folder_exists.assert_not_called()


def test_submission_task_records_the_deletion_rather_than_a_bug():
    """End to end through the decorator: the record is what this is for.

    The production run was filed forever as ``unexpected``/``HttpError`` in
    step ``execute_workflow``, which is indistinguishable from a defect. This
    asserts on the payload that reaches ``record_execution``, because that is
    the artifact that outlives the submission.
    """

    def execute_workflow(task, api, submission):
        raise _http_error()

    execute_workflow.__name__ = "execute_workflow"

    api = mock.MagicMock()
    api.job.return_value = {"status": JobStatus.RUNNING}
    api.folder_exists.return_value = False
    api.record_execution.return_value = None

    decorated = submission_task("Failed to execute workflow")(execute_workflow)

    with mock.patch(
        "girder_sivacor.worker_plugin.run_submission.GirderApi.for_task",
        return_value=api,
    ):
        with pytest.raises(SubmissionError) as raised:
            decorated(mock.MagicMock(), {"job_id": "job-id", "folder_id": "gone"})

    assert raised.value.code is FailureCode.SUBMISSION_DELETED
    # The HttpError is still the cause, so a traceback still shows the 400.
    assert isinstance(raised.value.__cause__, HttpError)

    record = api.record_execution.call_args[0][0]
    assert record["error"] == {
        "step": "execute_workflow",
        "code": FailureCode.SUBMISSION_DELETED.value,
        "detail": None,
    }


# --- folder_exists, the discriminator ------------------------------------


@pytest.mark.parametrize(
    "status,exists",
    [
        # Girder's modelParam raises a *validation* error for an id that
        # resolves to nothing, so a deleted folder is a 400 -- not the 404 you
        # would reach for.
        (400, False),
        (404, False),
        # Anything else is unknown, and unknown must answer "still there": a
        # Traefik restart mid-write must never be reported as the user having
        # deleted their own submission.
        (500, True),
        (502, True),
    ],
)
def test_folder_exists_only_believes_a_resolution_failure(status, exists):
    api = GirderApi("http://girder/api/v1", token="t")
    with mock.patch.object(
        api.client, "getFolder", side_effect=HttpError(status, "x", "url", "GET")
    ):
        assert api.folder_exists("folder-id") is exists


def test_folder_exists_treats_a_transport_failure_as_present():
    api = GirderApi("http://girder/api/v1", token="t")
    with mock.patch.object(
        api.client, "getFolder", side_effect=OSError("connection reset")
    ):
        assert api.folder_exists("folder-id") is True


def test_folder_exists_on_a_live_folder():
    api = GirderApi("http://girder/api/v1", token="t")
    with mock.patch.object(api.client, "getFolder", return_value={"_id": "x"}):
        assert api.folder_exists("folder-id") is True


# --- the last resort: a worker that never comes back ---------------------


@pytest.mark.plugin("sivacor")
def test_reap_settles_a_submission_whose_worker_died_mid_cancel(
    server, db, admin, user, submission_collection
):
    """Otherwise the transitional status is a permanent trap.

    A folder stuck at CANCELING cannot be deleted by its owner, and the main
    sweep cannot help: it selects jobs that are still RUNNING, and this job's
    status is terminal.
    """
    job, folder = make_submission(user, submission_collection, status=JobStatus.CANCELED)
    Folder().collection.update_one(
        {"_id": folder["_id"]}, {"$set": {"meta.status": CANCELING}}
    )
    stale = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
    backdate(job, hours=2, heartbeat=stale)
    Folder().collection.update_one({"_id": folder["_id"]}, {"$set": {"updated": stale}})

    resp = reap(server, admin)
    assertStatusOk(resp)

    assert resp.json["settled"] == [str(folder["_id"])]
    # Nothing to fail: the job is already terminal, only the folder was unfinished.
    assert resp.json["reaped"] == []
    assert Folder().load(folder["_id"], force=True)["meta"]["status"] == FAILED


@pytest.mark.plugin("sivacor")
def test_reap_leaves_a_fresh_cancel_alone(
    server, db, admin, user, submission_collection
):
    """The healthy window is ~12 s. Settling inside it re-opens the race."""
    _job, folder = make_submission(
        user, submission_collection, status=JobStatus.CANCELED
    )
    Folder().collection.update_one(
        {"_id": folder["_id"]}, {"$set": {"meta.status": CANCELING}}
    )

    resp = reap(server, admin)
    assertStatusOk(resp)

    assert resp.json["settled"] == []
    assert Folder().load(folder["_id"], force=True)["meta"]["status"] == CANCELING


@pytest.mark.plugin("sivacor")
def test_reap_leaves_a_still_running_job_to_the_main_sweep(
    server, db, admin, user, submission_collection
):
    """A RUNNING job belongs to the sweep above; two settlers must not race."""
    job, folder = make_submission(user, submission_collection)
    Folder().collection.update_one(
        {"_id": folder["_id"]}, {"$set": {"meta.status": CANCELING}}
    )
    stale = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
    backdate(job, hours=2, heartbeat=stale)
    Folder().collection.update_one({"_id": folder["_id"]}, {"$set": {"updated": stale}})

    with mock.patch("smtplib.SMTP") as smtp_class:
        smtp_class.return_value = mock.MagicMock()
        resp = reap(server, admin)
    assertStatusOk(resp)

    assert resp.json["settled"] == []
    # It was reaped instead, which routes through updateJob() and writes the
    # folder status through the event handler.
    assert resp.json["reaped"] == [str(job["_id"])]
    assert Folder().load(folder["_id"], force=True)["meta"]["status"] == FAILED
