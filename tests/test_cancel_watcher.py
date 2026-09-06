"""The cancel signal travels over HTTP, and a broken transport is not an answer.

``recorded_run``'s poll loop used to ask ``girder_worker``'s ``task.canceled``
once a second, which is a synchronous ``inspect().revoked()`` broadcast over the
celery pidbox. That put a *control* signal on the redis broker, and the broker's
own failures then answered the question:

* deaf connection -> ``revoked()`` returns ``None`` -> ``[]`` -> "not
  cancelled", so a cancelled run kept burning a worker for 30 hours
  (``2026-09-03-cancel-during-wedge.md``);
* reset connection -> the read *raises*, nothing caught it, and a healthy
  60-hour run was destroyed by one TCP RST -- the tenth occurrence of the same
  thing (``2026-09-06-broker-blip-kills-run.md``).

:class:`CancelWatcher` reads the same question off Girder instead. Every test
here pins one of the three properties that make that safe: it never raises, an
unreachable server is never read as a cancellation, and the answer latches.
"""

import logging

import mock
import pytest
from girder_jobs.constants import JobStatus
from girder_jobs.models.job import Job
from girder_sivacor.worker_plugin.lib import CHECKIN_INTERVAL, CancelWatcher
from pytest_girder.assertions import assertStatusOk

from .test_reaper import make_submission


def watcher(interval=CHECKIN_INTERVAL, **api_kwargs):
    """A watcher over a stub API, plus the stub, so tests can drive both."""
    api = mock.MagicMock()
    api.heartbeat.return_value = {"status": JobStatus.RUNNING}
    for key, value in api_kwargs.items():
        setattr(api.heartbeat, key, value)
    return CancelWatcher(api, "job-1", interval=interval), api


def at(*times):
    """Patch time.monotonic to walk ``times``, so no test has to sleep.

    One value per ``poll()`` that reaches the clock -- a poll on a watcher that
    has already latched returns without reading it.
    """
    return mock.patch(
        "girder_sivacor.worker_plugin.lib.time.monotonic", side_effect=list(times)
    )


# -- the server's half ----------------------------------------------------


@pytest.mark.plugin("sivacor")
def test_checkin_reports_the_job_status(
    server, db, admin, user, submission_collection
):
    """The heartbeat response is the cancel channel, so it must carry status.

    Nothing else the worker can reach answers "have I been cancelled?" without
    going through the broker, which is the transport whose failure started all
    of this.
    """
    job, _ = make_submission(user, submission_collection)

    resp = server.request(
        path=f"/sivacor/heartbeat/{job['_id']}", method="POST", user=admin
    )
    assertStatusOk(resp)

    assert resp.json["status"] == JobStatus.RUNNING
    # Still a heartbeat: the liveness stamp is the other half of the same call.
    assert "heartbeat" in resp.json


@pytest.mark.plugin("sivacor")
def test_checkin_reports_a_cancel(server, db, admin, user, submission_collection):
    """After a cancel the very next check-in says so."""
    job, _ = make_submission(user, submission_collection)
    Job().cancelJob(job)

    resp = server.request(
        path=f"/sivacor/heartbeat/{job['_id']}", method="POST", user=admin
    )
    assertStatusOk(resp)

    assert resp.json["status"] == JobStatus.CANCELED


# -- the worker's half: the happy path ------------------------------------


def test_running_job_is_not_cancelled():
    w, api = watcher()

    assert w.poll() is False
    assert w.canceled is False
    api.heartbeat.assert_called_once_with("job-1")


def test_non_running_job_is_a_cancel():
    w, _ = watcher()
    w.api.heartbeat.return_value = {"status": JobStatus.CANCELED}

    assert w.poll() is True
    assert w.canceled is True


@pytest.mark.parametrize(
    "status", [JobStatus.CANCELED, JobStatus.ERROR, JobStatus.SUCCESS, 824]
)
def test_any_terminal_status_stops_the_run(status):
    """The predicate is "not RUNNING", the same one submission_task uses.

    A reaper that failed the job, or girder_worker's CANCELING (824), mean the
    server has stopped expecting this run just as much as a user's cancel does.
    Carrying on would leave the orphan the 2026-08-11 incident is about.
    """
    w, _ = watcher()
    w.api.heartbeat.return_value = {"status": status}

    assert w.poll() is True


def test_the_answer_latches():
    """A blip after the cancel must not resurrect a stopped run.

    The exit-code branch and the failure classification downstream both read
    this; if it could flip back, container.wait()'s 137 from our own SIGKILL
    would be reported to the researcher as their code failing.
    """
    w, api = watcher()
    api.heartbeat.return_value = {"status": JobStatus.CANCELED}
    assert w.poll() is True

    api.heartbeat.return_value = {"status": JobStatus.RUNNING}
    assert w.poll() is True
    assert w.canceled is True


def test_latched_answer_stops_asking():
    """Once cancelled there is nothing left to learn, so no more requests."""
    w, api = watcher()
    api.heartbeat.return_value = {"status": JobStatus.CANCELED}

    w.poll()
    calls = api.heartbeat.call_count
    for _ in range(5):
        assert w.poll() is True
    assert api.heartbeat.call_count == calls


# -- the worker's half: the interval --------------------------------------


def test_polling_is_rate_limited_not_per_call():
    """The container loop ticks at 1Hz and calls poll() every time.

    The interval lives in the watcher precisely so the caller does not have to
    keep its own timer -- the old loop's hand-rolled one is what made the
    pidbox RPC a per-second event in the first place.
    """
    w, api = watcher(interval=10)

    # t=0 polls; t=1..9 are inside the interval; t=10 polls again.
    with at(0, 1, 2, 5, 9, 10):
        for _ in range(6):
            w.poll()

    assert api.heartbeat.call_count == 2


def test_first_poll_happens_immediately():
    """A cancel landing between the pre-flight and container start is caught."""
    w, api = watcher(interval=600)

    with at(1_000_000.0):
        w.poll()

    assert api.heartbeat.call_count == 1


def test_disk_sampling_is_not_on_the_checkin_interval():
    """A ~0.4s workspace walk must not inherit the check-in's cadence.

    Shortening the check-in for the researcher's sake would otherwise tax every
    run with a 100k-file tree six times over.
    """
    from girder_sivacor.worker_plugin.lib import DISK_SAMPLE_INTERVAL

    assert CHECKIN_INTERVAL < DISK_SAMPLE_INTERVAL


# -- the worker's half: failure is not an answer --------------------------


def test_unreachable_server_is_not_a_cancellation():
    """"I could not ask" and "no" share a return value on purpose.

    The only safe default is to keep running: a run stopped in error cannot be
    resumed, and if Girder is really gone the reaper settles it from the other
    side.
    """
    w, api = watcher()
    api.heartbeat.return_value = None

    assert w.poll() is False
    assert w.canceled is False


def test_a_raising_api_does_not_reach_the_caller():
    """The regression from 2026-09-06, in one assertion.

    ``GirderApi.heartbeat`` swallows its own exceptions, so this can only
    happen if something changes underneath -- which is exactly what happened
    last time. The guard is deliberately duplicated.
    """
    w, api = watcher()
    api.heartbeat.side_effect = ConnectionError(
        "Error while reading from 10.3.37.97:6379 : (104, 'Connection reset by peer')"
    )

    assert w.poll() is False  # must not raise
    assert w.canceled is False


def test_an_outage_is_retried_and_the_cancel_still_lands():
    """Intermittent failure delays the answer; it does not lose it."""
    w, api = watcher(interval=10)
    api.heartbeat.side_effect = [
        None,
        None,
        RuntimeError("503 Service Unavailable"),
        {"status": JobStatus.CANCELED},
    ]

    with at(0, 100, 200, 300):
        assert w.poll() is False
        assert w.poll() is False
        assert w.poll() is False
        assert w.poll() is True


def test_failure_retries_sooner_than_the_normal_cadence():
    """A missed check-in is also a missed heartbeat, and the reaper counts."""
    w, api = watcher(interval=10)
    api.heartbeat.return_value = None

    # t=0 fails, so the next attempt is due at RETRY_BACKOFF_BASE, not +10.
    with at(0, w.RETRY_BACKOFF_BASE):
        w.poll()
        w.poll()

    assert api.heartbeat.call_count == 2


def test_backoff_never_exceeds_the_normal_interval():
    """A long outage must not cost more traffic than a healthy run.

    And the exponent is capped, not just the result: ``_failures`` counts the
    whole outage, so ``2 ** 21599`` would otherwise be evaluated once a second.
    """
    w, _ = watcher(interval=10)

    delays = []
    for failures in (1, 2, 3, 4, 10, 100, 100_000):
        w._failures = failures
        delays.append(w._delay())

    assert delays == [2, 4, 8, 10, 10, 10, 10]


def test_recovery_resets_the_backoff():
    w, api = watcher(interval=10)
    api.heartbeat.side_effect = [None, None, {"status": JobStatus.RUNNING}]

    with at(0, 100, 200):
        w.poll()
        w.poll()
        w.poll()

    assert w._failures == 0
    assert w._delay() == 10


def test_a_long_outage_does_not_flood_the_log(caplog):
    """An outage should be obvious in the log, not the only thing in it."""
    w, api = watcher(interval=10)
    api.heartbeat.return_value = None

    with caplog.at_level(logging.WARNING), at(*range(0, 2000, 10)):
        for _ in range(100):
            w.poll()

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    # NOISY_FAILURES in full, then one per QUIET_EVERY -- not 100.
    assert len(warnings) == w.NOISY_FAILURES + 100 // w.QUIET_EVERY


# -- the worker's half: an older server -----------------------------------


def test_falls_back_to_the_job_document_on_an_old_server():
    """A rolling upgrade must not silently disable cancellation.

    A worker booted against a server that predates the status in the check-in
    response would otherwise never see a cancel at all.
    """
    w, api = watcher()
    api.heartbeat.return_value = {"heartbeat": "2026-09-06T12:00:00Z"}
    api.job.return_value = {"status": JobStatus.CANCELED}

    assert w.poll() is True
    api.job.assert_called_once_with("job-1")


def test_the_fallback_is_not_used_when_the_status_is_reported():
    """The extra round trip is only for old servers."""
    w, api = watcher()

    w.poll()

    api.job.assert_not_called()


# -- end to end: a cancel really stops a real container --------------------


@pytest.mark.plugin("sivacor")
def test_a_cancel_stops_a_running_container(
    server,
    db,
    user,
    eagerWorkerTasks,
    fsAssetstore,
    patched_gpg,
    uploads_folder,
    submission_collection,
):
    """The whole point, end to end: a long run stops when the job is cancelled.

    **This test could not be written against the old implementation.**
    ``task.canceled`` -> ``is_revoked`` short-circuits on
    ``task.request.is_eager``, so under ``eagerWorkerTasks`` the answer was
    always False no matter what the server said -- the cancel path was
    unreachable from the suite, which is a large part of why two incidents
    found it before a test did. Reading the status off Girder is testable
    because it is the same HTTP the run already speaks.

    The job is cancelled from inside the first check-in so the test does not
    have to race a real container. The status is written straight to the
    collection: that ``Job().cancelJob`` produces it is
    ``test_checkin_reports_a_cancel``'s job, and dragging the revoke machinery
    in here would test the half that already has coverage.
    """
    import os
    import tarfile
    import tempfile
    import time

    from girder_sivacor.worker_plugin.girder_api import GirderApi

    from .conftest import submit_sivacor_job, upload_test_file

    # Long enough that finishing on its own is unmistakably a failure of the
    # thing under test, short enough that such a failure does not hang CI.
    sleep_seconds = 90
    main_file = "main.R"
    stages = [
        {"image_name": "rocker/r-ver", "image_tag": "4.3.1", "main_file": main_file}
    ]
    with (
        tempfile.NamedTemporaryFile(suffix=".tar.gz") as temp_archive,
        tempfile.TemporaryDirectory() as temp_dir,
    ):
        with open(os.path.join(temp_dir, main_file), "w") as f:
            f.write(f"Sys.sleep({sleep_seconds})\n")
        with tarfile.open(temp_archive.name, "w:gz") as tar:
            tar.add(temp_dir, arcname=".")
        fobj = upload_test_file(uploads_folder, user, temp_archive.name)

    real_heartbeat = GirderApi.heartbeat
    cancelled_at = []

    def cancel_then_check_in(self, job_id):
        if not cancelled_at:
            cancelled_at.append(time.monotonic())
            Job().collection.update_one(
                {"_id": Job().load(job_id, force=True)["_id"]},
                {"$set": {"status": JobStatus.CANCELED}},
            )
        return real_heartbeat(self, job_id)

    started = time.monotonic()
    with mock.patch.object(GirderApi, "heartbeat", cancel_then_check_in):
        resp = submit_sivacor_job(server, user, fobj, stages)
    assertStatusOk(resp)
    elapsed = time.monotonic() - started

    assert cancelled_at, "the run never checked in, so nothing was under test"
    # The container was stopped rather than left to finish. Generous, because
    # the elapsed time includes pulling the image and unpacking the workspace;
    # what it must exclude is the sleep.
    assert elapsed - (cancelled_at[0] - started) < sleep_seconds

    job = Job().load(resp.json["_id"], force=True)
    # Still CANCELED: a cancelled run is not the researcher's failure, so
    # nothing may rewrite it to ERROR on the way out.
    assert job["status"] == JobStatus.CANCELED
    log = "".join(job.get("log") or [])
    assert "Check stdout/stderr" not in log
