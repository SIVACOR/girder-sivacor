"""The backstop for a chain job whose own status write was lost.

A chain task's status is written in exactly one place -- ``gw_task_success`` ->
``JobManager.updateStatus`` -- over an unprotected network call, in a celery
signal handler that fires *after* the chain's successor has been dispatched. On
2026-10-16 that write was lost: a three-stage submission ran 34 h, every stage
exited 0, the TRO was signed and the researcher emailed, and the stage-1 job
has been RUNNING ever since. Nothing noticed, because nothing about the run was
wrong (``2026-10-16-lost-job-status.md``, ``girder-sivacor#139``).

``/sivacor/reap`` now reconciles it: *a job in RUNNING whose child job reached a
terminal state cannot still be running.*

Two tests here are worth more than the rest if you are skimming.
``test_a_live_submission_job_is_never_reconciled`` pins the filter that keeps
this from being a far worse bug than the one it fixes -- a ``sivacor_submission``
job is legitimately RUNNING while its chain jobs terminate beneath it. And
``test_a_job_whose_successor_is_still_running_is_left_alone`` pins that the rule
needs the *successor* to be finished, not merely to exist.
"""

import datetime
from unittest import mock

import pytest
from girder_jobs.constants import JobStatus
from girder_jobs.models.job import Job
from pytest_girder.assertions import assertStatusOk

from girder_sivacor import rest
from girder_sivacor.models.alerts import AlertCounter
from girder_sivacor.rest import SIVACOR

pytestmark = pytest.mark.plugin("sivacor")


def chain_job(user, status=JobStatus.RUNNING, parent=None, title="Execute SIVACOR Workflow"):
    """One step of a submission chain, as girder_worker would leave it."""
    job = Job().createJob(
        title=title,
        type="celery",
        public=False,
        user=user,
        parentJob=parent,
    )
    if status != JobStatus.INACTIVE:
        job = Job().updateJob(job, status=JobStatus.QUEUED)
        job = Job().updateJob(job, status=JobStatus.RUNNING)
    if status not in (JobStatus.INACTIVE, JobStatus.QUEUED, JobStatus.RUNNING):
        job = Job().updateJob(job, status=status)
    return job


def quiesce(job, minutes=30):
    """Backdate ``updated`` past the sweep's grace period.

    The grace exists because a chain publishes its successor from the task
    *body* while the predecessor's status is written afterwards, so a quick
    successor can finish first. Every test that expects a reconciliation has to
    step over it.
    """
    then = datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=minutes)
    Job().collection.update_one({"_id": job["_id"]}, {"$set": {"updated": then}})
    return Job().load(job["_id"], force=True)


def reap(server, admin):
    resp = server.request(path="/sivacor/reap", method="POST", user=admin)
    assertStatusOk(resp)
    return resp.json


def status_of(job):
    return Job().load(job["_id"], force=True)["status"]


# -- the incident ---------------------------------------------------------


def test_a_running_job_whose_successor_finished_is_settled(server, db, admin, user):
    """The 2026-10-16 state, repaired.

    A stage job stuck in RUNNING with a SUCCESS successor: the run went on and
    finished, so the only thing wrong is this document.
    """
    stuck = quiesce(chain_job(user))
    chain_job(user, status=JobStatus.SUCCESS, parent=stuck)

    result = reap(server, admin)

    assert result["reconciled"] == [str(stuck["_id"])]
    assert status_of(stuck) == JobStatus.SUCCESS


def test_the_reconciled_job_says_it_was_inferred(server, db, admin, user):
    """A reader must be able to tell a restated status from a reported one.

    The chain is what post-mortems are read from, so a status this sweep
    invented has to announce itself in the log rather than look like one the
    worker sent.
    """
    stuck = quiesce(chain_job(user))
    child = chain_job(user, status=JobStatus.SUCCESS, parent=stuck)

    reap(server, admin)

    reloaded = Job().load(stuck["_id"], force=True, includeLog=True)
    log = "".join(reloaded.get("log") or [])
    assert "reconciled" in log.lower()
    assert str(child["_id"]) in log
    assert "139" in log
    # And the transition is a real one, so the timestamps show when it landed.
    assert [t for t in reloaded["timestamps"] if t["status"] == JobStatus.SUCCESS]


def test_a_terminal_successor_of_any_kind_settles_it(server, db, admin, user):
    """SUCCESS is right even when the successor failed.

    What proves the parent returned is that its successor was *dispatched at
    all* -- a chain publishes the next task from the previous task's body. The
    successor's own outcome says nothing about the parent.
    """
    for child_status in (JobStatus.ERROR, JobStatus.CANCELED):
        stuck = quiesce(chain_job(user))
        chain_job(user, status=child_status, parent=stuck)

        assert str(stuck["_id"]) in reap(server, admin)["reconciled"]
        assert status_of(stuck) == JobStatus.SUCCESS


# -- what it must not touch ------------------------------------------------


def test_a_live_submission_job_is_never_reconciled(server, db, admin, user):
    """The filter that keeps this from being a worse bug than the one it fixes.

    A ``sivacor_submission`` job is *legitimately* RUNNING while the chain jobs
    beneath it terminate one after another -- that is exactly what a submission
    in progress looks like. Applying the rule to one would mark a live
    submission finished the moment its first step completed.
    """
    submission = Job().createJob(
        title="Live submission", type="sivacor_submission", public=False, user=user
    )
    submission = Job().updateJob(submission, status=JobStatus.QUEUED)
    submission = Job().updateJob(submission, status=JobStatus.RUNNING)
    submission = quiesce(submission)
    # Its first step is done; the rest of the chain has not run yet.
    chain_job(user, status=JobStatus.SUCCESS, parent=submission)

    assert reap(server, admin)["reconciled"] == []
    assert status_of(submission) == JobStatus.RUNNING


def test_a_job_whose_successor_is_still_running_is_left_alone(server, db, admin, user):
    """A successor that exists but has not finished is not enough.

    Existence alone would in fact be sound -- the successor is published from
    the predecessor's body -- but the terminal reading is the conservative one
    and costs only one sweep.
    """
    stuck = quiesce(chain_job(user))
    chain_job(user, status=JobStatus.RUNNING, parent=stuck)

    assert reap(server, admin)["reconciled"] == []
    assert status_of(stuck) == JobStatus.RUNNING


def test_a_childless_running_job_is_left_alone(server, db, admin, user):
    """The ordinary case: a stage that really is running. Most of the fleet."""
    running = quiesce(chain_job(user))

    assert reap(server, admin)["reconciled"] == []
    assert status_of(running) == JobStatus.RUNNING


def test_a_recent_job_is_inside_the_grace_period(server, db, admin, user):
    """The race the grace period exists for.

    A chain publishes its successor from the task body; the predecessor's own
    status is written afterwards, from ``task_success``. So a short successor
    can reach SUCCESS while its predecessor's correct status is still on the
    wire, and reconciling then would be a guess racing an answer.
    """
    fresh = chain_job(user)
    chain_job(user, status=JobStatus.SUCCESS, parent=fresh)

    assert reap(server, admin)["reconciled"] == []
    assert status_of(fresh) == JobStatus.RUNNING


def test_the_grace_period_is_the_reapers_own_cadence():
    """Pinned because the healing time a post-mortem quotes depends on it."""
    assert rest.RECONCILE_GRACE == datetime.timedelta(minutes=10)


# -- losing the race, and failing, are both allowed ------------------------


def test_a_status_that_arrives_first_wins_the_race(server, db, admin, user):
    """A reported status beats an inferred one, and the write is what enforces it.

    Reproduced without mocks, because the race is real: the sweep reads a job
    that says RUNNING and writes a moment later, and in between the worker's
    own status can land. ``updateJob`` puts the old status in its *query*, so
    the write simply matches nothing -- which is the wanted outcome, not an
    error to recover from.
    """
    stuck = chain_job(user)
    child = chain_job(user, status=JobStatus.SUCCESS, parent=stuck)
    stale = Job().load(stuck["_id"], force=True)
    # The worker's own write lands between the sweep's read and its write.
    Job().updateJob(Job().load(stuck["_id"], force=True), status=JobStatus.ERROR)

    assert SIVACOR._mark_succeeded(stale, child) is False
    assert status_of(stuck) == JobStatus.ERROR


def test_a_reconciliation_that_fails_does_not_abort_the_sweep(
    server, db, admin, user
):
    """One job it cannot repair must not strand the ones behind it.

    The same rule every other periodic sweep here follows: a sweep that stops
    on the first failure leaves the rest of its work undone, and this one shares
    an endpoint with the reaper, whose work is failing stranded submissions.
    """
    first = quiesce(chain_job(user))
    chain_job(user, status=JobStatus.SUCCESS, parent=first)
    second = quiesce(chain_job(user))
    chain_job(user, status=JobStatus.SUCCESS, parent=second)

    update_job = Job.updateJob

    def flaky(self, job, *args, **kwargs):
        if job["_id"] == first["_id"]:
            raise RuntimeError("mongo went away mid-sweep")
        return update_job(self, job, *args, **kwargs)

    with mock.patch.object(Job, "updateJob", flaky):
        result = reap(server, admin)

    assert result["reconciled"] == [str(second["_id"])]
    assert status_of(first) == JobStatus.RUNNING
    assert status_of(second) == JobStatus.SUCCESS


# -- what an operator is told ---------------------------------------------


def test_a_reconciliation_is_counted_for_the_digest(server, db, admin, user):
    """Found by a human browsing the admin list, which is the other half of #139.

    Counted rather than mailed: by the time anyone reads it the document is
    already repaired, so the thing worth seeing is the rate. One in eleven
    months is the measured one.
    """
    AlertCounter().collection.delete_many({})
    stuck = quiesce(chain_job(user))
    chain_job(user, status=JobStatus.SUCCESS, parent=stuck)

    reap(server, admin)

    rows = list(AlertCounter().collection.find({}))
    assert [(row["code"], row["count"]) for row in rows] == [("lost_job_status", 1)]


def test_a_second_sweep_neither_repeats_nor_recounts(server, db, admin, user):
    """Idempotent, because the repair removes the job from its own query."""
    AlertCounter().collection.delete_many({})
    stuck = quiesce(chain_job(user))
    chain_job(user, status=JobStatus.SUCCESS, parent=stuck)

    reap(server, admin)
    assert reap(server, admin)["reconciled"] == []
    assert AlertCounter().collection.count_documents({}) == 1
