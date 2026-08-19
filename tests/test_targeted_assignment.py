"""The switch between the shared dispatch queue and controller-assigned work.

Under targeted assignment ``submit_job`` stops publishing: the fleet controller
picks an instance for a submission and publishes the chain to that instance's
private queue instead. See P2 in development_notes/worker_sizing_plan.md.

Two failure shapes drive everything here, and both are silent in production:

* **Both paths running at once** -- Girder publishes to the shared queue *and*
  the controller assigns the same submission -- puts two workers on one
  workspace. So the route is recorded per submission, at submit time, and a
  submission is never eligible for the path it was not created for.
* **Neither path running** looks exactly like a healthy idle system: no error,
  no queue depth, submissions simply sit. Hence one setting rather than two
  environment variables, and hence the waiting submission's own reaper bound.
"""

import datetime

import mock
import pytest
from girder.exceptions import ValidationException
from girder.models.folder import Folder
from girder.models.setting import Setting
from girder.models.token import Token
from girder_jobs.constants import REST_CREATE_JOB_TOKEN_SCOPE, JobStatus
from girder_jobs.models.job import Job
from girder_sivacor.rest import build_submission_chain, targeted_assignment
from girder_sivacor.settings import PluginSettings
from girder_sivacor.worker_plugin.routing import (
    DISPATCH_QUEUE,
    LOCAL_QUEUE,
    UNPINNED_TASKS,
)
from girder_sivacor.worker_plugin.run_submission import sign_tro
from pytest_girder.assertions import assertStatusOk

from .conftest import submit_sivacor_job, upload_test_file

STAGES = [
    {"image_name": "rocker/r-ver", "image_tag": "4.3.1", "main_file": "main.R"}
]


def submit(server, user, uploads_folder, secrets=None):
    """Submit without letting celery run the chain, and return (resp, sent)."""
    fobj = upload_test_file(uploads_folder, user, "with_space_R.zip")
    with mock.patch("celery.canvas._chain.apply_async") as sent:
        resp = submit_sivacor_job(server, user, fobj, STAGES, secrets=secrets)
    assertStatusOk(resp)
    return resp, sent


# --- the setting -----------------------------------------------------------


@pytest.mark.plugin("sivacor")
def test_targeted_assignment_is_off_by_default(db):
    """The shared dispatch queue stays the route until someone says otherwise.

    An older controller ignores this setting entirely, so a deployment that
    defaulted it on would publish nothing and run nothing.
    """
    assert Setting().get(PluginSettings.TARGETED_ASSIGNMENT) is False
    assert targeted_assignment() is False


@pytest.mark.plugin("sivacor")
@pytest.mark.parametrize("value", ["true", "false", 1, 0, None, "on"])
def test_a_non_boolean_arm_flag_is_refused(db, value):
    """``"false"`` is truthy, and arming this by accident stops every dispatch."""
    with pytest.raises(ValidationException):
        Setting().set(PluginSettings.TARGETED_ASSIGNMENT, value)


@pytest.mark.plugin("sivacor")
def test_the_arm_flag_accepts_booleans(db):
    for value in (True, False):
        Setting().set(PluginSettings.TARGETED_ASSIGNMENT, value)
        assert targeted_assignment() is value


# --- what submit_job does on each side of the flag -------------------------


@pytest.mark.plugin("sivacor")
def test_with_the_flag_off_the_chain_still_goes_to_the_shared_queue(
    server, db, user, fsAssetstore, uploads_folder
):
    """The status quo, asserted so a regression here cannot hide behind the flag."""
    resp, sent = submit(server, user, uploads_folder)

    sent.assert_called_once()
    assert sent.call_args.kwargs["queue"] == DISPATCH_QUEUE

    job = Job().load(resp.json["_id"], force=True)
    assert job["meta"]["awaiting_assignment"] is False
    # Nothing to build a chain from later: it was already built and published.
    assert "sivacorChain" not in job


@pytest.mark.plugin("sivacor")
def test_with_the_flag_on_nothing_is_published(
    server, db, user, fsAssetstore, uploads_folder
):
    """The submission waits, marked, with everything a builder will need."""
    Setting().set(PluginSettings.TARGETED_ASSIGNMENT, True)
    resp, sent = submit(server, user, uploads_folder, secrets=[{"key": "AWS_KEY", "value": "s3cret"}])

    sent.assert_not_called()

    job = Job().load(resp.json["_id"], force=True)
    assert job["meta"]["awaiting_assignment"] is True
    # RUNNING from the moment it is accepted, exactly as before: the researcher
    # is waiting for a machine, not for the server to accept the submission.
    assert job["status"] == JobStatus.RUNNING
    assert "worker_queue" not in job.get("meta", {})

    stashed = job["sivacorChain"]
    assert stashed["stages"] == STAGES
    assert set(stashed["secrets"]) == {"encrypted_secrets", "wrapped_job_key"}
    # Encrypted at submit time, as always -- the plaintext never lands in Mongo.
    assert "s3cret" not in str(stashed["secrets"])


@pytest.mark.plugin("sivacor")
def test_the_stashed_chain_inputs_do_not_leave_the_server(
    server, db, user, fsAssetstore, uploads_folder
):
    """filtermodel drops unexposed top-level fields, and that is why they are one.

    The stash holds the secret envelope. It is the same ciphertext that would
    otherwise sit in the broker message, so this is not a new exposure -- but a
    job document is handed to the client on every poll, and there is no reason
    to widen it.
    """
    Setting().set(PluginSettings.TARGETED_ASSIGNMENT, True)
    resp, _ = submit(server, user, uploads_folder, secrets=[{"key": "AWS_KEY", "value": "s3cret"}])

    assert "sivacorChain" not in resp.json
    # ...and the same on the way back out through the jobs API.
    fetched = server.request(path=f"/job/{resp.json['_id']}", user=user)
    assertStatusOk(fetched)
    assert "sivacorChain" not in fetched.json


# --- the extracted builder -------------------------------------------------


@pytest.mark.plugin("sivacor")
def test_the_builder_produces_the_chain_the_controller_can_publish(
    server, db, admin, user, fsAssetstore, uploads_folder
):
    """One builder, called from two processes, or the policy can drift.

    Asserted structurally rather than by running it: the controller imports this
    function and has no celery broker of its own at build time.
    """
    Setting().set(PluginSettings.TARGETED_ASSIGNMENT, True)
    resp, _ = submit(server, user, uploads_folder)
    job = Job().load(resp.json["_id"], force=True)
    file = {"_id": job["sivacorChain"]["fileId"], "name": "with_space_R.zip"}

    chain = build_submission_chain(job, file, STAGES, job["sivacorChain"]["secrets"])
    tasks = [task["task"].rsplit(".", 1)[-1] for task in chain.tasks]

    assert tasks[0] == "prepare_submission"
    assert tasks[-1] == "finalize_job"
    # One stage, so one execute_workflow; the surrounding TRO steps are what
    # make the chain long, and a builder that dropped one would still "work".
    assert tasks.count("execute_workflow") == len(STAGES)
    assert "sign_tro" in tasks

    head = chain.tasks[0]
    assert head.args[0] == str(job["userId"])
    assert head.args[1] == str(file["_id"])
    assert head.args[2] == STAGES
    assert head.args[3] == str(job["_id"])
    # The size the submission asked for, read off the job rather than passed in:
    # the controller assigns from the job document and nothing else.
    assert head.args[4] == job["meta"]["requested_memory_gb"]

    # Every step has to carry the credential; girder_worker copies headers from
    # a *running* task onto the next one, which does not help a chain built in
    # one go.
    for task in chain.tasks:
        assert task.options["girder_api_url"]
        assert task.options["girder_client_token"]

    # Losing this scope costs the child jobs, silently: girder_worker's POST to
    # /job 403s and it carries on. See test_reaper's copy of this assertion.
    token = Token().load(
        chain.tasks[0].options["girder_client_token"], force=True, objectId=False
    )
    scopes = token["scope"] if isinstance(token["scope"], list) else [token["scope"]]
    assert REST_CREATE_JOB_TOKEN_SCOPE in scopes

    # No step is stamped with a queue here. Routing is the caller's (one
    # apply_async for the whole chain) and then pin_chain's, which pins every
    # link to the worker that holds the workspace except sign_tro -- signing
    # runs on the manager, which is where the TRS key is. A builder that stamped
    # queues would quietly take that decision away from both. See
    # test_signing_routing.py.
    assert sign_tro.queue == LOCAL_QUEUE
    assert sign_tro.name.rsplit(".", 1)[-1] in UNPINNED_TASKS
    assert not any("queue" in task.options for task in chain.tasks)


# --- the reaper, which would otherwise fail a submission for waiting --------


def _running_submission(
    user, submission_collection, awaiting=False, assigned_at=None, age_minutes=0
):
    """A RUNNING submission job, aged, with the routing metadata under test.

    The folder comes along because reaping goes through ``updateJob``, whose
    status handler writes the submission folder and emails the researcher --
    the whole reason the reaper stays in the Girder process.
    """
    job = Job().createJob(
        title="Waiting submission",
        type="sivacor_submission",
        public=False,
        user=user,
        otherFields={"meta": {"awaiting_assignment": awaiting}},
    )
    job = Job().updateJob(job, "Preparing\n", status=JobStatus.RUNNING)
    folder = Folder().createFolder(
        submission_collection,
        f"submission-{job['_id']}",
        parentType="collection",
        public=False,
    )
    Folder().setMetadata(
        folder,
        {
            "job_id": str(job["_id"]),
            "creator_id": str(user["_id"]),
            "status": "processing",
        },
    )
    then = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        minutes=age_minutes
    )
    updates = {"created": then, "updated": then}
    if assigned_at is not None:
        updates["meta.assigned_at"] = assigned_at
    Job().collection.update_one({"_id": job["_id"]}, {"$set": updates})
    return Job().load(job["_id"], force=True)


def reap(server, admin):
    resp = server.request(path="/sivacor/reap", method="POST", user=admin)
    assertStatusOk(resp)
    return resp.json["reaped"]


@pytest.mark.plugin("sivacor")
def test_a_submission_waiting_for_a_worker_is_not_reaped_as_lost(
    server, db, admin, user, submission_collection
):
    """Two hours unassigned is a busy fleet, not a dead worker.

    Without this the heartbeat rule fires at 30 minutes and tells the researcher
    their worker was lost -- naming a machine that was never created.
    """
    job = _running_submission(
        user, submission_collection, awaiting=True, age_minutes=120
    )
    Setting().set(PluginSettings.ASSIGNMENT_TIMEOUT, 24 * 60.0)

    assert reap(server, admin) == []
    assert Job().load(job["_id"], force=True)["status"] == JobStatus.RUNNING


@pytest.mark.plugin("sivacor")
def test_a_submission_nobody_ever_assigns_is_failed_for_that_reason(
    server, db, admin, user, submission_collection
):
    """The wait is bounded, or one broken assigner locks a user out for good.

    A user may hold exactly one submission in flight, so an unbounded wait is
    not merely slow -- it is a submission they cannot replace and cannot cancel
    their way past.
    """
    job = _running_submission(
        user, submission_collection, awaiting=True, age_minutes=90
    )
    Setting().set(PluginSettings.ASSIGNMENT_TIMEOUT, 60.0)

    assert str(job["_id"]) in reap(server, admin)
    reloaded = Job().load(job["_id"], force=True, includeLog=True)
    assert reloaded["status"] == JobStatus.ERROR
    assert "no worker could be assigned" in reloaded["log"][-1]


@pytest.mark.plugin("sivacor")
def test_being_assigned_counts_as_a_sign_of_life(
    server, db, admin, user, submission_collection
):
    """The wait must not be charged against the heartbeat window.

    A submission that waited an hour has an hour-old ``created`` and ``updated``
    the moment it is handed a worker, so the run would be reaped before its
    first step ever logged.
    """
    job = _running_submission(
        user,
        submission_collection,
        awaiting=True,
        age_minutes=90,
        assigned_at=datetime.datetime.now(datetime.timezone.utc),
    )
    Setting().set(PluginSettings.HEARTBEAT_TIMEOUT, 30.0)

    assert reap(server, admin) == []
    assert Job().load(job["_id"], force=True)["status"] == JobStatus.RUNNING


@pytest.mark.plugin("sivacor")
def test_the_runtime_limit_starts_when_the_worker_does(
    server, db, admin, user, submission_collection
):
    """Waiting is not running: a queued hour must not eat the runtime budget."""
    now = datetime.datetime.now(datetime.timezone.utc)
    job = _running_submission(
        user,
        submission_collection,
        awaiting=True,
        age_minutes=110,
        assigned_at=now - datetime.timedelta(minutes=50),
    )
    Setting().set(PluginSettings.MAX_RUNTIME, 1.0)
    # Heartbeat rule out of the way; this test is about the runtime clock alone.
    Setting().set(PluginSettings.HEARTBEAT_TIMEOUT, 24 * 60.0)

    assert reap(server, admin) == [], "the run has been going 50 minutes, not 110"

    Job().collection.update_one(
        {"_id": job["_id"]},
        {"$set": {"meta.assigned_at": now - datetime.timedelta(minutes=70)}},
    )
    assert str(job["_id"]) in reap(server, admin)
