"""Heartbeat and stranded-submission reaping.

These build jobs and submission folders directly instead of running a
submission. The thing under test is what the server does when a worker stops
reporting, and the cheapest way to produce that state is to write the
timestamps a lost worker would have left behind.
"""

import datetime

import mock
import pytest
from girder.exceptions import ValidationException
from girder.models.folder import Folder
from girder.models.setting import Setting
from girder_jobs.constants import JobStatus
from girder_jobs.models.job import Job
from girder_sivacor.settings import PluginSettings
from pytest_girder.assertions import assertStatus, assertStatusOk


def make_submission(user, submission_collection, status=JobStatus.RUNNING):
    """A running submission: a job plus the folder that mirrors its status."""
    job = Job().createJob(
        title="Stranded submission",
        type="sivacor_submission",
        public=False,
        user=user,
    )
    job = Job().updateJob(job, "Preparing\n", status=status)
    folder = Folder().createFolder(
        submission_collection,
        f"submission-{job['_id']}",
        parentType="collection",
        public=False,
    )
    Folder().setMetadata(
        folder,
        {"job_id": str(job["_id"]), "creator_id": str(user["_id"]), "status": "processing"},
    )
    return job, folder


def backdate(job, minutes=None, hours=None, heartbeat=None):
    """Rewrite a job's liveness timestamps as a lost worker would leave them.

    ``created``/``updated`` are what Girder itself maintains and
    ``meta.heartbeat`` is what the worker posts; the reaper takes the most
    recent of the three, so a test has to move all of them to look stale.
    """
    delta = datetime.timedelta(minutes=minutes or 0, hours=hours or 0)
    then = datetime.datetime.now(datetime.timezone.utc) - delta
    updates = {"created": then, "updated": then}
    if heartbeat is not None:
        updates["meta.heartbeat"] = heartbeat
    Job().collection.update_one({"_id": job["_id"]}, {"$set": updates})
    return Job().load(job["_id"], force=True)


def reap(server, admin):
    return server.request(path="/sivacor/reap", method="POST", user=admin)


@pytest.mark.plugin("sivacor")
def test_heartbeat_records_liveness(server, db, admin, user, submission_collection):
    """The endpoint stamps meta.heartbeat without disturbing the job."""
    job, _ = make_submission(user, submission_collection)
    assert "heartbeat" not in Job().load(job["_id"], force=True).get("meta", {})

    resp = server.request(
        path=f"/sivacor/heartbeat/{job['_id']}", method="POST", user=admin
    )
    assertStatusOk(resp)

    reloaded = Job().load(job["_id"], force=True)
    assert isinstance(reloaded["meta"]["heartbeat"], datetime.datetime)
    # A heartbeat is not progress: it must not move the job out of RUNNING or
    # append to the log, or a submission would look busy purely from pinging.
    assert reloaded["status"] == JobStatus.RUNNING


@pytest.mark.plugin("sivacor")
def test_heartbeat_requires_admin(server, db, user, submission_collection):
    """It carries an admin-scoped worker token, so it is admin-only."""
    job, _ = make_submission(user, submission_collection)
    resp = server.request(
        path=f"/sivacor/heartbeat/{job['_id']}", method="POST", user=user
    )
    assertStatus(resp, 403)


@pytest.mark.plugin("sivacor")
def test_reap_leaves_live_submissions_alone(
    server, db, admin, user, submission_collection
):
    """A submission that has just reported must survive the sweep."""
    job, folder = make_submission(user, submission_collection)

    resp = reap(server, admin)
    assertStatusOk(resp)
    assert resp.json["reaped"] == []
    assert Job().load(job["_id"], force=True)["status"] == JobStatus.RUNNING


@pytest.mark.plugin("sivacor")
def test_reap_leaves_long_run_with_fresh_heartbeat_alone(
    server, db, admin, user, submission_collection
):
    """The heartbeat is the whole point: a silent-but-live run is not stranded.

    ``updated`` is hours old because nothing logged, which is exactly the state
    a naive staleness check would misread as a dead worker.
    """
    job, _ = make_submission(user, submission_collection)
    backdate(
        job,
        hours=3,
        heartbeat=datetime.datetime.now(datetime.timezone.utc),
    )

    resp = reap(server, admin)
    assertStatusOk(resp)
    assert resp.json["reaped"] == []
    assert Job().load(job["_id"], force=True)["status"] == JobStatus.RUNNING


@pytest.mark.plugin("sivacor")
def test_reap_fails_stranded_submission(
    server, db, admin, user, submission_collection
):
    """No sign of life past the threshold: fail the job and tell the user."""
    job, folder = make_submission(user, submission_collection)
    stale = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=2)
    backdate(job, hours=2, heartbeat=stale)

    with mock.patch("smtplib.SMTP") as smtp_class:
        smtp_class.return_value = mock.MagicMock()
        resp = reap(server, admin)
        assertStatusOk(resp)

    assert resp.json["reaped"] == [str(job["_id"])]

    reaped = Job().load(job["_id"], includeLog=True, force=True)
    assert reaped["status"] == JobStatus.ERROR
    assert any("abandoned" in line for line in reaped["log"])

    # Going through updateJob() is what drives jobs.job.update.after, so the
    # folder the user sees has to have followed the job.
    assert Folder().load(folder["_id"], force=True)["meta"]["status"] == "failed"


@pytest.mark.plugin("sivacor")
def test_reap_enforces_absolute_max_runtime(
    server, db, admin, user, submission_collection
):
    """A heartbeat cannot keep a submission alive forever."""
    job, _ = make_submission(user, submission_collection)
    backdate(
        job,
        hours=48,
        heartbeat=datetime.datetime.now(datetime.timezone.utc),
    )

    with mock.patch("smtplib.SMTP") as smtp_class:
        smtp_class.return_value = mock.MagicMock()
        resp = reap(server, admin)
        assertStatusOk(resp)

    assert resp.json["reaped"] == [str(job["_id"])]
    reaped = Job().load(job["_id"], includeLog=True, force=True)
    assert reaped["status"] == JobStatus.ERROR
    assert any("maximum runtime" in line for line in reaped["log"])


@pytest.mark.plugin("sivacor")
def test_reap_settles_child_jobs(server, db, admin, user, submission_collection):
    """The per-step celery jobs are stranded too; nothing else will touch them."""
    job, _ = make_submission(user, submission_collection)
    child = Job().createJob(
        title="execute_workflow",
        type="celery",
        args=[{"job_id": str(job["_id"])}],
        user=user,
    )
    child = Job().updateJob(child, status=JobStatus.RUNNING)
    backdate(job, hours=2)

    with mock.patch("smtplib.SMTP") as smtp_class:
        smtp_class.return_value = mock.MagicMock()
        assertStatusOk(reap(server, admin))

    assert Job().load(child["_id"], force=True)["status"] == JobStatus.ERROR


@pytest.mark.plugin("sivacor")
def test_reap_ignores_terminal_and_foreign_jobs(
    server, db, admin, user, submission_collection
):
    """Only RUNNING sivacor_submission jobs are candidates."""
    done, _ = make_submission(user, submission_collection, status=JobStatus.RUNNING)
    Job().updateJob(Job().load(done["_id"], force=True), status=JobStatus.SUCCESS)
    backdate(done, hours=5)

    other = Job().createJob(title="Unrelated", type="celery", user=user)
    other = Job().updateJob(other, status=JobStatus.RUNNING)
    backdate(other, hours=5)

    resp = reap(server, admin)
    assertStatusOk(resp)
    assert resp.json["reaped"] == []
    assert Job().load(other["_id"], force=True)["status"] == JobStatus.RUNNING


@pytest.mark.plugin("sivacor")
def test_reap_requires_admin(server, db, user):
    resp = server.request(path="/sivacor/reap", method="POST", user=user)
    assertStatus(resp, 403)


@pytest.mark.plugin("sivacor")
def test_reaper_thresholds_are_configurable(
    server, db, admin, user, submission_collection
):
    """Shrinking the threshold brings a borderline submission into scope."""
    job, _ = make_submission(user, submission_collection)
    backdate(job, minutes=5)

    # Default is 30 minutes, so five minutes of silence is fine.
    assertStatusOk(reap(server, admin))
    assert Job().load(job["_id"], force=True)["status"] == JobStatus.RUNNING

    Setting().set(PluginSettings.HEARTBEAT_TIMEOUT, 1.0)
    with mock.patch("smtplib.SMTP") as smtp_class:
        smtp_class.return_value = mock.MagicMock()
        assertStatusOk(reap(server, admin))
    assert Job().load(job["_id"], force=True)["status"] == JobStatus.ERROR


@pytest.mark.plugin("sivacor")
def test_reaper_threshold_validation(server, db):
    """Zero would fail every running submission on the next sweep."""
    for key in (PluginSettings.HEARTBEAT_TIMEOUT, PluginSettings.MAX_RUNTIME):
        Setting().set(key, 15.0)
        assert Setting().get(key) == 15.0
        for bad in (0.0, -1.0):
            with pytest.raises(ValidationException):
                Setting().set(key, bad)

    Setting().unset(PluginSettings.HEARTBEAT_TIMEOUT)
    assert Setting().get(PluginSettings.HEARTBEAT_TIMEOUT) == 30.0
    Setting().unset(PluginSettings.MAX_RUNTIME)
    assert Setting().get(PluginSettings.MAX_RUNTIME) == 24.0


@pytest.mark.plugin("sivacor")
def test_maintenance_tasks_are_off_the_dispatch_queue(server, db):
    """Housekeeping must not inflate the queue an autoscaler measures.

    Depth on the dispatch queue is meant to be a count of submissions waiting
    for a worker; a periodic task sitting in it would, under scale-to-zero,
    book a whole VM to issue one HTTP POST.
    """
    from girder_sivacor.worker_plugin.routing import DISPATCH_QUEUE, LOCAL_QUEUE
    from girder_sivacor.worker_plugin.run_submission import (
        cleanup_submissions,
        prepare_submission,
        reap_stranded_submissions,
    )

    # Housekeeping rides Girder core's queue, which the co-located worker has to
    # consume anyway. A separate queue name would not have given it its own
    # execution slot -- concurrency is one pool per worker, not per queue.
    assert LOCAL_QUEUE != DISPATCH_QUEUE
    for task in (cleanup_submissions, reap_stranded_submissions):
        assert task.queue == LOCAL_QUEUE
    assert prepare_submission.queue == DISPATCH_QUEUE


@pytest.mark.plugin("sivacor")
def test_worker_heartbeats_a_real_run(
    server,
    db,
    user,
    eagerWorkerTasks,
    fsAssetstore,
    patched_gpg,
    uploads_folder,
    submission_collection,
):
    """The worker really reaches the endpoint while a container runs.

    ``recorded_run`` swallows heartbeat errors on purpose, so a test that only
    checks the call was made would pass even if every POST 403'd. Run an actual
    submission and look for the timestamp on the far side.
    """
    from .conftest import submit_sivacor_job, upload_test_file

    fobj = upload_test_file(uploads_folder, user, "with_space_R.zip")
    stages = [
        {"image_name": "rocker/r-ver", "image_tag": "4.3.1", "main_file": "main.R"}
    ]
    resp = submit_sivacor_job(server, user, fobj, stages)
    assertStatusOk(resp)

    job = Job().load(resp.json["_id"], force=True)
    assert job["status"] == JobStatus.SUCCESS
    assert isinstance(job.get("meta", {}).get("heartbeat"), datetime.datetime)


@pytest.mark.plugin("sivacor")
def test_reap_task_drives_the_endpoint(server, db, admin, user, submission_collection):
    """The worker-side task is a thin trigger for the server-side sweep."""
    job, _ = make_submission(user, submission_collection)
    backdate(job, hours=2)

    from girder_sivacor.worker_plugin.run_submission import reap_stranded_submissions

    with mock.patch("smtplib.SMTP") as smtp_class:
        smtp_class.return_value = mock.MagicMock()
        reap_stranded_submissions()

    assert Job().load(job["_id"], force=True)["status"] == JobStatus.ERROR


@pytest.mark.plugin("sivacor")
def test_maintenance_mints_its_own_token_without_an_api_key(
    server, db, admin, user, submission_collection, monkeypatch
):
    """A co-located worker needs no standing credential.

    ``local_worker`` has ``GIRDER_MONGO_URI`` and the model layer, so requiring
    an admin API key just to POST a trigger is redundant -- it can create an
    admin token itself. Drop the key and the sweep must still work.
    """
    monkeypatch.delenv("GIRDER_API_KEY", raising=False)
    job, _ = make_submission(user, submission_collection)
    backdate(job, hours=2)

    from girder_sivacor.worker_plugin.run_submission import reap_stranded_submissions

    with mock.patch("smtplib.SMTP") as smtp_class:
        smtp_class.return_value = mock.MagicMock()
        reap_stranded_submissions()

    assert Job().load(job["_id"], force=True)["status"] == JobStatus.ERROR


@pytest.mark.plugin("sivacor")
def test_maintenance_skips_when_the_model_layer_is_unreachable(
    server, db, monkeypatch
):
    """A remote worker degrades to a skip rather than raising.

    The REST conversion exists so a worker need not reach MongoDB. If one
    without Mongo somehow consumes ``sivacor.maintenance``, the local-token
    fallback must fail soft -- an unhandled exception in a periodic task is
    silent until someone reads the worker log.
    """
    from girder_sivacor.worker_plugin import run_submission

    monkeypatch.delenv("GIRDER_API_KEY", raising=False)

    # Fail inside the fallback, the way an unreachable Mongo would, rather than
    # stubbing out the function being tested.
    with mock.patch(
        "girder.models.token.Token.createToken",
        side_effect=RuntimeError("no server selected"),
    ):
        assert run_submission._local_admin_token() is None
        assert run_submission._maintenance_api() is None
        # The task turns that into a logged skip, not an exception.
        run_submission.reap_stranded_submissions()


@pytest.mark.plugin("sivacor")
def test_worker_token_can_create_child_jobs(
    server, db, admin, user, fsAssetstore, uploads_folder, submission_collection
):
    """The worker token must carry REST_CREATE_JOB_TOKEN_SCOPE.

    girder_worker records a child job for every step it publishes by POSTing to
    /job, and that endpoint is @access.token(scope=REST_CREATE_JOB_TOKEN_SCOPE,
    required=True) -- which a plain USER_AUTH token fails even as an admin. The
    failure is a logged 403 that girder_worker swallows, so the submission still
    succeeds and nothing points at the cause; only the child jobs silently go
    missing. Assert on the token itself rather than on a chain run.
    """
    from girder.constants import TokenScope
    from girder.models.token import Token
    from girder_jobs.constants import REST_CREATE_JOB_TOKEN_SCOPE

    from .conftest import submit_sivacor_job, upload_test_file

    fobj = upload_test_file(uploads_folder, user, "with_space_R.zip")
    stages = [
        {"image_name": "rocker/r-ver", "image_tag": "4.3.1", "main_file": "main.R"}
    ]
    # Stop before celery actually runs the chain: the token is minted during
    # submit_job, which is all this test cares about.
    with mock.patch("celery.canvas._chain.apply_async"):
        resp = submit_sivacor_job(server, user, fobj, stages)
    assertStatusOk(resp)

    # The most recently minted admin token is the worker's.
    tok = Token().find({"userId": admin["_id"]}, sort=[("created", -1)], limit=1)[0]
    scopes = tok["scope"] if isinstance(tok["scope"], list) else [tok["scope"]]
    assert REST_CREATE_JOB_TOKEN_SCOPE in scopes, (
        "worker token lacks the job-creation scope; child jobs would 403"
    )
    assert TokenScope.USER_AUTH in scopes, "worker token lost normal user access"
