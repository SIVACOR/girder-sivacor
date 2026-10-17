"""A chain task's status write, and why it must outlive a failed log flush.

girder_worker writes a chain task's status in exactly one place, and that place
flushes the job log *first*:

.. code-block:: python

    self._flush()            # ends in raise_for_status()
    self.status = status     # never reached if the flush throws
    ...                      # so the status PUT is never even attempted

On 2026-10-16 one of those two calls threw. ``gw_task_success`` catches only
``AttributeError`` and ``StateTransitionException``, so it escaped into a celery
signal handler that fires after the chain's successor was already dispatched --
celery logged it and carried on. The run finished perfectly, signed its TRO and
emailed the researcher, while its stage-1 job stayed RUNNING forever
(``2026-10-16-lost-job-status.md``, ``girder-sivacor#139``).

The rule these tests pin is one sentence: **losing a progress line is
acceptable, losing a status is not.** ``LogRelay``'s contract point 1 already
says the first half.

``test_the_status_is_written_even_when_the_flush_fails`` is the regression
itself. ``test_a_rejected_transition_still_raises_StateTransitionException`` is
the thing a fix here is most likely to break, since the CANCELING path in two
of girder_worker's signal handlers is built on that exception.
"""

from unittest import mock

# ``girder_worker.app`` is imported for its side effect, and the side effect is
# the thing under test: importing it runs discover_tasks(), which instantiates
# SIVACORWorkerPlugin, whose __init__ calls job_status.install(). Imported
# explicitly so this module tests the real wiring even when it is collected
# alone.
import girder_worker.app  # noqa: F401
import pytest
import requests
from girder_worker.utils import JobManager, StateTransitionException

from girder_sivacor.worker_plugin import job_status

JOB_URL = "http://girder.invalid/api/v1/job/6acfd0ac81e3e6792c37096f"


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error", response=self)


@pytest.fixture
def manager():
    """A JobManager that talks to nothing.

    ``logPrint=False`` matters: with it on, the constructor tees stdout and
    stderr into this object, and pytest's own output would start making HTTP
    requests to ``girder.invalid``.
    """
    return JobManager(logPrint=False, url=JOB_URL)


@pytest.fixture(autouse=True)
def no_sleep():
    """The backoff is real seconds; no test here needs to spend them."""
    with mock.patch.object(job_status.time, "sleep") as sleep:
        yield sleep


def statuses_put(session):
    """The status values this fake session was asked to write."""
    return [
        call.kwargs["data"]["status"]
        for call in session.request.call_args_list
        if "status" in (call.kwargs.get("data") or {})
    ]


def fake_session(manager, *responses):
    """Replace the manager's session; ``responses`` are returned in order.

    An exception instance in the list is raised instead of returned, which is
    how a connection-level failure arrives.
    """
    session = mock.MagicMock()

    def respond(*_args, **_kwargs):
        reply = responses[min(session.request.call_count - 1, len(responses) - 1)]
        if isinstance(reply, BaseException):
            raise reply
        return reply

    session.request.side_effect = respond
    manager._session = session
    return session


# -- the regression -------------------------------------------------------


def test_the_status_is_written_even_when_the_flush_fails(manager):
    """The 2026-10-16 ordering, inverted.

    Upstream flushes, then sets the status, then PUTs it -- so a throw in the
    flush takes the status with it. Here the flush failure is swallowed and the
    PUT happens anyway.
    """
    session = fake_session(manager, FakeResponse())
    with mock.patch.object(
        JobManager, "_flush", side_effect=requests.HTTPError("502 Bad Gateway")
    ):
        manager.updateStatus(3)

    assert statuses_put(session) == [3]
    assert manager.status == 3


def test_a_failed_flush_keeps_its_lines_buffered(manager):
    """Nothing is lost by swallowing it, which is why swallowing is allowed.

    ``_flush`` clears its buffer only after ``raise_for_status()`` returns, so
    the lines that did not go up are still queued for the next flush that
    works. Same property ``LogRelay`` relies on, one call site along.
    """
    manager._buf = b"the line that did not make it\n"
    fake_session(manager, FakeResponse())
    with mock.patch.object(
        JobManager, "_flush", side_effect=requests.HTTPError("502 Bad Gateway")
    ):
        manager.updateStatus(3)

    assert manager._buf == b"the line that did not make it\n"


def test_the_status_put_is_retried_on_a_fresh_connection(manager):
    """The other candidate from the incident: a 34-hour-old keep-alive socket.

    urllib3's adapter redials a *refused* connection already; what it does not
    retry is a 5xx or a read error on an established one. Hence a retry here,
    and hence closing the session first so the attempt is not made down the
    same dead socket.
    """
    session = fake_session(manager, FakeResponse(502), FakeResponse())

    manager.updateStatus(3)

    assert statuses_put(session) == [3, 3]
    assert manager.status == 3
    session.close.assert_called_once()


def test_a_connection_error_is_retried_too(manager):
    """Not every failure arrives as a response."""
    session = fake_session(
        manager, requests.ConnectionError("connection reset by peer"), FakeResponse()
    )

    manager.updateStatus(3)

    assert statuses_put(session) == [3, 3]
    assert manager.status == 3


def test_a_persistent_failure_gives_up_and_says_so(manager):
    """What is left for the backstop.

    The exception reaches the caller rather than being hidden, and the manager
    does **not** record a status the server never took -- so a later call with
    the same status tries again instead of returning on line one.
    """
    session = fake_session(manager, FakeResponse(502))

    with pytest.raises(requests.HTTPError):
        manager.updateStatus(3)

    assert len(statuses_put(session)) == job_status.STATUS_ATTEMPTS
    assert manager.status is None


# -- what must not change -------------------------------------------------


def test_a_rejected_transition_still_raises_StateTransitionException(manager):
    """girder_worker's cancel handling is built on this exception.

    ``gw_task_prerun`` and ``gw_task_success`` both catch it to keep a
    cancelled job cancelled. A fix that turned Girder's 400 into a generic
    error -- or retried it -- would break cancellation while looking like it
    only touched error handling.
    """
    session = fake_session(
        manager,
        FakeResponse(400, {"field": "status", "message": "Invalid state transition"}),
    )

    with pytest.raises(StateTransitionException):
        manager.updateStatus(3)

    # Not retried: Girder answered, and the answer is the same every time.
    assert len(statuses_put(session)) == 1


def test_an_unrelated_400_is_not_mistaken_for_a_transition(manager):
    """Only ``field == 'status'`` means a transition was refused."""
    session = fake_session(manager, FakeResponse(400, {"field": "log"}))

    with pytest.raises(requests.HTTPError):
        manager.updateStatus(3)

    # And not retried either: a 4xx is Girder declining a request it read, so
    # asking twice more spends the backoff to be told the same thing.
    assert len(statuses_put(session)) == 1


def test_a_404_is_not_retried(manager):
    """The job was deleted. There is no answer a retry could get instead."""
    session = fake_session(manager, FakeResponse(404))

    with pytest.raises(requests.HTTPError):
        manager.updateStatus(3)

    assert len(statuses_put(session)) == 1


def test_the_short_circuits_upstream_relies_on_are_intact(manager):
    """No url, no status, or the status it already has: all no-ops."""
    session = fake_session(manager, FakeResponse())
    manager.status = 3
    manager.updateStatus(3)
    manager.updateStatus(None)
    manager.url = None
    manager.updateStatus(4)

    session.request.assert_not_called()


# -- the never-raise discipline, which is the whole point -----------------


def test_reporting_a_failure_cannot_cause_one():
    """Logging is not a safe operation in here.

    girder_worker tees *stderr* into the JobManager as well as stdout, so a
    handler writing there goes out over the very connection that just failed.
    An exception from inside the ``except`` that is supposed to be containing
    the failure would defeat the containment -- which is exactly how 2026-09-18
    reached the poll loop. Same reasoning as ``LogRelay._log``.
    """

    def emit_that_fails(*_args):
        raise requests.HTTPError("502 Bad Gateway")

    job_status._quiet_log(emit_that_fails, "anything")  # must not raise


def test_closing_a_broken_session_cannot_break_the_retry():
    """The connection is already in an unknown state; that is why it is closed.

    ``close()`` walking a pool of sockets that are mid-failure is the least
    surprising place for a second exception, and it must not take the retry it
    was about to enable.
    """
    manager = JobManager(logPrint=False, url=JOB_URL)
    session = fake_session(manager, FakeResponse(502), FakeResponse())
    session.close.side_effect = OSError("broken pipe")

    manager.updateStatus(3)  # must not raise

    assert statuses_put(session) == [3, 3]
    assert manager.status == 3


def test_a_400_that_is_not_json_stays_the_error_it_was(manager):
    """Traefik's 400 is HTML, and Girder's is JSON. Only one can be read.

    A ``json()`` that throws must leave the original ``HTTPError`` on its way
    out rather than replacing it with a ``JSONDecodeError`` about the body --
    which would name the parser in the log and not the request.
    """
    response = FakeResponse(400)
    response.json = mock.Mock(side_effect=ValueError("not json"))
    session = fake_session(manager, response)

    with pytest.raises(requests.HTTPError):
        manager.updateStatus(3)

    assert len(statuses_put(session)) == 1


# -- what the log line says about --------------------------------------------


def test_the_guard_names_the_job_when_it_can_and_the_task_when_it_cannot():
    """The handlers disagree about where the task is, and one fires too early.

    ``gw_task_prerun`` takes it as ``task``; the rest as ``sender``. And prerun
    is the one signal that can fire before a JobManager exists, which is the
    case with no job URL to name.
    """
    sender = mock.Mock()
    sender.job_manager.url = JOB_URL
    assert job_status._subject({"sender": sender}) == JOB_URL

    task = mock.Mock()
    task.job_manager.url = JOB_URL
    assert job_status._subject({"task": task}) == JOB_URL

    unspecced = mock.Mock(spec=["name"])
    unspecced.name = "girder_sivacor.worker_plugin.run_submission.execute_workflow"
    assert job_status._subject({"sender": unspecced}) == unspecced.name

    assert job_status._subject({}) == "an unidentified task"


# -- the wiring -----------------------------------------------------------


def test_girder_worker_is_patched_on_import():
    """Installed from ``SIVACORWorkerPlugin.__init__``, which discover_tasks runs.

    Asserted rather than performed: importing the worker plugin is what
    installs it, and that has already happened by the time any test runs.
    """
    assert job_status._installed
    assert JobManager.updateStatus is job_status.resilient_update_status


@pytest.mark.parametrize(
    "name", [name for _, name in job_status._GUARDED_SIGNALS]
)
def test_every_girder_worker_signal_handler_is_guarded(name):
    """Including the ones that did not hurt this time.

    ``gw_task_failure`` writes ERROR through the same unprotected call, and
    ``gw_task_postrun`` flushes under ``except AttributeError`` alone. Neither
    was the 2026-10-16 path; both are the same mistake, and #139 item 4 is
    explicitly about not fixing only the call site that last hurt.
    """
    import girder_worker.app as gw_app

    signal = next(sig for sig, n in job_status._GUARDED_SIGNALS if n == name)
    receivers = [receiver for _, receiver in signal.receivers]
    assert job_status._wrappers[name] in receivers
    assert getattr(gw_app, name) not in receivers


def test_a_handler_that_raises_does_not_escape_the_guard():
    """Celery would swallow it anyway; what the guard adds is who it was about.

    The point is the log line: celery's own is ``Signal handler <function
    gw_task_success> raised``, which a post-mortem cannot connect to the job
    document that is now wrong.
    """
    def exploding(**_kwargs):
        raise requests.HTTPError("502 Bad Gateway")

    exploding.__name__ = "gw_task_success"
    guard = job_status._guarded(exploding)
    sender = mock.Mock()
    sender.job_manager.url = JOB_URL

    with mock.patch.object(job_status.logger, "error") as logged:
        guard(sender=sender)  # must not raise

    assert JOB_URL in logged.call_args.args


def test_install_is_idempotent():
    """It has already run; a second call must not wrap the wrapper."""
    before = {name: job_status._wrappers[name] for _, name in job_status._GUARDED_SIGNALS}
    job_status.install()
    assert {
        name: job_status._wrappers[name] for _, name in job_status._GUARDED_SIGNALS
    } == before


@pytest.fixture
def reinstalled():
    """Run a test against a fresh ``install()``, and put the process back.

    These two tests are the only ones that watch the patch being applied rather
    than its result, and the patch is process-global -- so the restore is in a
    ``finally``: a failure here must not leave every later test in this worker
    running against an unpatched girder_worker.
    """
    job_status.uninstall()
    try:
        yield
    finally:
        job_status.uninstall()
        job_status.install()


def test_uninstall_puts_girder_workers_own_handlers_back(reinstalled):
    """Because the tests below need it, and a half-applied patch is worse than none."""
    import girder_worker.app as gw_app

    assert not job_status._installed
    assert JobManager.updateStatus is job_status._ORIGINAL_UPDATE_STATUS
    for signal, name in job_status._GUARDED_SIGNALS:
        receivers = [receiver for _, receiver in signal.receivers]
        assert getattr(gw_app, name) in receivers


def test_a_handler_girder_worker_does_not_have_is_skipped_loudly(reinstalled):
    """An upstream that has been reshaped under us must not fail silently.

    Guarding is the only thing standing between a network blip and a lost
    status, so a handler that has been renamed or removed upstream has to be
    *said*, not skipped. Silence here is the exact failure mode this module
    exists to fix, which is why there is a log line rather than a bare
    ``continue``.
    """
    import girder_worker.app as gw_app

    with (
        mock.patch.object(gw_app, "gw_task_success", None),
        mock.patch.object(job_status.logger, "warning") as warned,
    ):
        job_status.install()

    assert "gw_task_success" in warned.call_args.args
    assert "gw_task_success" not in job_status._wrappers
    # And the other four are still guarded: one missing handler must not cost
    # the rest of them.
    assert len(job_status._wrappers) == len(job_status._GUARDED_SIGNALS) - 1
