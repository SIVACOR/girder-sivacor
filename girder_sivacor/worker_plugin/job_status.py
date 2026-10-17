"""Keeping a chain task's status write from being lost.

A chain task's status is written in exactly one place --
``gw_task_success`` -> ``_update_status`` -> ``JobManager.updateStatus`` -- and
upstream that place makes **two unprotected network calls**:

.. code-block:: python

    def updateStatus(self, status):          # girder_worker/utils.py:291
        ...
        self._flush()                        # :302 -- raise_for_status() at :266
        self.status = status                 # :303 -- not reached if the flush throws
        req = self._session.request(..., data={'status': status}, ...)
        req.raise_for_status()               # :307

The flush runs **first**, so a throw there means the status ``PUT`` is never
even attempted. And ``gw_task_success`` catches only ``AttributeError`` and
``StateTransitionException``, so a ``requests`` exception escapes it into a
celery signal handler -- which fires *after* the task body returned and after
the chain's successor was dispatched. Celery logs it and carries on. The run is
completely unaffected; the job document stays RUNNING forever.

That is 2026-10-16: a three-stage submission ran 34 h, every stage exited 0, the
TRO was signed and the researcher was emailed, and the stage-1 job has been
RUNNING ever since. Nothing noticed -- not the reaper, not the autoscaler, not
the alert digest. A human found it in the admin list.
``../development_notes/incidents/2026-10-16-lost-job-status.md`` and
``girder-sivacor#139``.

**What this module changes, and the rule behind it.** *Losing a progress line is
acceptable; losing a status is not.* :class:`~girder_sivacor.worker_plugin.lib.LogRelay`'s
contract point 1 already says the first half out loud. So:

1. The flush is attempted and its failure is swallowed. The lines are not lost
   -- ``_flush`` clears its buffer only after ``raise_for_status()`` returns, so
   they ride up with the next flush that works.
2. The status ``PUT`` is then attempted **regardless**, and retried, each retry
   on a fresh connection. Re-sending a status Girder already applied is a no-op
   there (``updateJob`` does nothing when the status is unchanged), so a retry
   is safe even when the first attempt actually landed and only its response was
   lost.
3. Every girder_worker celery signal handler is wrapped, so a network exception
   inside one is logged against the job it belongs to instead of appearing as
   celery's generic ``Signal handler ... raised``.
4. One of them -- ``gw_task_failure`` -- is **replaced** rather than merely
   wrapped, by :func:`sivacor_task_failure`. Upstream appends the traceback to
   the job *log*, which is the same field the researcher's container output is
   teed into; that mixed stream is what made a deleted submission's chain
   impossible to either keep or drop. The traceback now goes to ``meta``
   through ``PUT /sivacor/failure/:id``, with upstream's log write kept as the
   fallback when that cannot be reached. girder-sivacor#129 and
   :mod:`girder_sivacor.chain`.

**None of that can be sufficient**, which is why it is only half the fix. A
retry still fails if the outage outlasts it, and the failure is still only a log
line on a worker that is about to be reaped. The other half is server-side and
does not depend on the worker being alive to report anything: ``/sivacor/reap``
reconciles a job left RUNNING whose successor reached a terminal state
(``rest.py``, ``_reconcile_lost_statuses``).

**The audit this incident asked for** (#139 item 4), because ``LogRelay``'s
docstring presented the poll loop's ``print()`` as *the* channel by which a
job-log write escapes, which is how ``updateStatus``'s own ``_flush`` stayed out
of view. Every call site of ``JobManager._flush``:

``JobManager.write``
    Reached by **every** ``print()`` and every write to stderr in a task
    process, since girder_worker tees both into the JobManager. Rate-limited, so
    most writes do not flush. Covered inside ``recorded_run``'s poll loop by
    ``LogRelay``, which is where a run spends hours; the handful of other prints
    in ``run_submission``/``lib`` are not covered and are deliberately left
    alone -- a throw there fails the task, which is *loud*: the run errors, the
    researcher is told, telemetry records it. Loud is not the class of bug this
    module is about.
``JobManager.updateStatus``
    This incident. Fixed here.
``JobManager.updateProgress``
    Same shape as ``write``. Nothing in SIVACOR calls it.
``gw_task_postrun``
    ``task.job_manager._flush()`` under ``except AttributeError`` only. A throw
    there loses the last buffered log lines and nothing else -- the ``finally``
    still releases stdout/stderr. Wrapped here with the rest of the handlers.
"""

from __future__ import annotations

import functools
import json
import logging
import time
import traceback as tb

from celery.signals import (
    task_failure,
    task_postrun,
    task_prerun,
    task_revoked,
    task_success,
)
from girder_worker.utils import (
    JobManager,
    JobStatus,
    StateTransitionException,
    _update_status,
    is_builtin_celery_task,
)
from requests import HTTPError

from ..errors import failure_record

logger = logging.getLogger(__name__)

#: Captured at import, before anything replaces it.
_ORIGINAL_UPDATE_STATUS = JobManager.updateStatus

#: Attempts at the status ``PUT``, including the first.
#:
#: **Not redundant with the retries the session already has**, which is worth
#: stating because the adapter looks like it covers this: ``JobManager``
#: mounts ``HTTPAdapter(max_retries=10)``, and ``Retry.from_int(10)`` means ten
#: *connect* retries with ``read=False`` and an empty ``status_forcelist``. So
#: urllib3 already redials a refused connection -- and retries neither a 5xx
#: response nor a read error on an established one. Those two are exactly the
#: shapes 2026-10-16 could not be narrowed past: a 502 Traefik generated for a
#: request the backend never saw (which is literally 2026-09-18), or a session
#: held open 34 hours whose far end had stopped honouring it.
#:
#: Deliberately small. This runs in a signal handler after the task body
#: returned, so the cost of being wrong is a worker blocked for a few seconds.
STATUS_ATTEMPTS = 3

#: Seconds before the second attempt; doubled for each one after it.
STATUS_BACKOFF_SECONDS = 1.0


def _quiet_log(emit, *args):
    """Report a failure without being able to cause one.

    The same reasoning as :meth:`LogRelay._log`, and for the same reason it is
    not shared with it: girder_worker tees *stderr* into the JobManager too, so
    a log handler writing there goes out over the very connection that just
    failed. Nothing escapes, and no ``exc_info`` -- the exception is
    interpolated as text instead.
    """
    try:
        emit(*args)
    except Exception:
        pass


def _fresh_connection(manager):
    """Drop the session's pooled sockets so the next request dials again.

    ``Session.close()`` clears the adapters' connection pools; the session
    itself stays usable and opens a new connection on the next request. This is
    the one thing a retry can do about the failure mode that fits 2026-10-16
    best -- a keep-alive connection the far end stopped honouring hours ago,
    where the request that notices is the one that dies.
    """
    try:
        manager._session.close()
    except Exception:
        pass


def _put_status(manager, status):
    """One attempt at the status ``PUT``, with upstream's 400 handling intact.

    ``gw_task_prerun`` and ``gw_task_success`` both branch on
    ``StateTransitionException`` to keep a cancelled job cancelled, so that
    translation has to survive exactly as upstream wrote it. The only departure
    is upstream's ``print`` of the rejection message: printing from in here is
    itself a job-log write, which is the kind of call this module exists to stop
    trusting.
    """
    req = manager._session.request(
        manager.method.upper(),
        manager.url,
        headers=manager.headers,
        data={"status": status},
        allow_redirects=True,
    )
    try:
        req.raise_for_status()
    except HTTPError as exc:
        response = exc.response
        if response is not None and response.status_code == 400:
            try:
                body = response.json()
            except Exception:
                body = {}
            if body.get("field") == "status":
                raise StateTransitionException(body.get("message"), exc)
        raise


def _is_final(exc):
    """Whether retrying ``exc`` could possibly produce a different answer.

    A 4xx is Girder having read the request and declined it -- a stale token, a
    job that has been deleted, a transition it will refuse just as firmly in a
    second's time. Retrying one spends the backoff to be told the same thing
    three times. Everything else -- a 5xx, a connection reset, a read timeout
    on a socket that has gone stale -- is the transport, and the transport is
    what 2026-10-16 was.
    """
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    return status_code is not None and 400 <= status_code < 500


def resilient_update_status(self, status):
    """``JobManager.updateStatus``, with the status write made to matter most.

    Replaces girder_worker's, whose ordering lets a lost progress line take the
    status with it. Same signature, same exceptions to the caller; what changes
    is that the ``PUT`` is reached even when the flush fails, and tried more
    than once.
    """
    if not self.url or status is None or status == self.status:
        return

    try:
        self._flush()
    except Exception as exc:
        # Deliberately swallowed. The buffer survives -- `_flush` clears it only
        # after raise_for_status() returns -- so the lines go up with the next
        # flush that works, and the status below is attempted either way.
        _quiet_log(
            logger.warning,
            "Could not flush the job log for %s before setting status %s (%s); "
            "the lines stay buffered and the status write goes ahead",
            self.url,
            status,
            exc,
        )

    delay = STATUS_BACKOFF_SECONDS
    for attempt in range(1, STATUS_ATTEMPTS + 1):
        try:
            _put_status(self, status)
        except StateTransitionException:
            # Girder refused the transition, which is an answer and not a
            # failure: the handlers above read it to keep a cancelled job
            # cancelled. Upstream assigns self.status before the PUT, so it ends
            # up here with the attempted status set; kept identical so the
            # CANCELING path behaves exactly as it did.
            self.status = status
            raise
        except Exception as exc:
            if attempt == STATUS_ATTEMPTS or _is_final(exc):
                # Left for the caller, and for the guard around the handler.
                # self.status is deliberately *not* set: the server never took
                # this status, so a later call with the same one must try again
                # rather than short-circuit on line 1.
                _quiet_log(
                    logger.error,
                    "Giving up on writing status %s to %s after %d attempt(s) "
                    "(%s). The run is unaffected; the job document is now "
                    "wrong and /sivacor/reap has to reconcile it",
                    status,
                    self.url,
                    attempt,
                    exc,
                )
                raise
            _quiet_log(
                logger.warning,
                "Could not write status %s to %s (attempt %d/%d: %s); retrying "
                "on a new connection",
                status,
                self.url,
                attempt,
                STATUS_ATTEMPTS,
                exc,
            )
            _fresh_connection(self)
            time.sleep(delay)
            delay *= 2
        else:
            self.status = status
            return


def _failure_url(manager):
    """``PUT /sivacor/failure/:id`` for the job ``manager`` writes to.

    Derived from ``JobManager.url`` -- ``<apiUrl>/job/<id>`` -- because that is
    the only thing in a signal handler that knows both the API base and the job
    id. ``jobInfoSpec`` builds it that way (``girder_plugin_worker.utils``), and
    a worker that overrides the API base rewrites the same field, so deriving
    from it follows the override for free.
    """
    base, separator, job_id = (manager.url or "").rpartition("/job/")
    if not separator or not job_id:
        return None
    return f"{base}/sivacor/failure/{job_id}"


def sivacor_task_failure(sender=None, exception=None, traceback=None, **rest):
    """girder_worker's ``gw_task_failure``, with the traceback given a field.

    Upstream appends ``"%s: %s\n%s" % (class, exception, frames)`` to the job
    *log*. That log is the same field the researcher's container stdout is
    teed into, which makes it a mixed stream -- and a mixed stream can be
    neither dropped nor kept. Dropping it on deletion would have destroyed
    2026-10-12's ``APIError`` frames (girder-sivacor#126) and 2026-09-25's
    ``Invalid state transition to '3'``; keeping it retains a researcher's
    stdout forever. So the traceback goes to ``meta`` instead, through a
    server-side allow-list, and ``log`` becomes droppable in one move.
    See girder-sivacor#129 and :mod:`girder_sivacor.chain`.

    **The frames leave the log; the headline stays.** Moving the whole thing
    out was a regression nobody would have noticed until the next incident:
    Girder's job detail page renders ``title``, ``type``, ``_id``, ``status``,
    a timeline, ``created`` and the *log* -- and no ``meta`` at all. So a
    traceback written only to ``meta`` is invisible exactly where an operator
    looks first, and step 2 of the playbook in
    ``2026-10-12-dns-pull-kills-run.md`` says to read it off the job's log.

    One line -- ``ClassName: message`` -- therefore still goes to the log, and
    only the frames move. That costs nothing the move was buying: the line is
    not the researcher's content, it is dropped with the log on deletion like
    everything else there, and a single line cannot reproduce 2026-09-18's
    red-herring, which was a *whole traceback* interleaved with another
    thread's. ``sivacor job get <id>`` and ``GET /job/:id`` show the frames;
    girder-sivacor#143 is the view that would put them on the page.

    **The full log write is the fallback, not the removal.** If the PUT cannot
    be made -- an old server with no such route, an outage, a job whose URL
    this cannot parse -- upstream's line is written in full, frames and all.
    Losing a traceback entirely is a worse outcome than keeping one in the
    stream it used to live in, and that stream is dropped on deletion anyway.
    """
    if is_builtin_celery_task(getattr(sender, "name", "")):
        return
    manager = getattr(sender, "job_manager", None)
    if manager is None:
        # Upstream reaches the same no-op through AttributeError.
        return

    frames = tb.format_tb(traceback) if traceback is not None else []
    headline = f"{type(exception).__name__}: {exception}"
    if _put_failure(manager, failure_record(exception, frames)):
        _write(manager, f"{headline}\n")
    else:
        _quiet_log(
            logger.warning,
            "Could not record the traceback for %s out of band; "
            "falling back to the job log",
            getattr(manager, "url", "an unidentified job"),
        )
        _write(manager, f"{headline}\n{''.join(frames)}")

    _update_status(sender, JobStatus.ERROR)


def _write(manager, text):
    """Append to the job log. Never raises.

    Same contract as :meth:`LogRelay._emit` and for the same reason: this runs
    inside an ``except`` that is already handling the researcher's real
    problem, and a narration write must never be the thing that replaces it.
    """
    try:
        manager.write(text)
    except Exception as exc:
        _quiet_log(logger.error, "Could not write the failure to the log (%s)", exc)


def _put_failure(manager, record):
    """Send one failure record. Returns whether it landed. Never raises."""
    url = _failure_url(manager)
    if not url:
        return False
    try:
        response = manager._session.request(
            "PUT",
            url,
            headers={**(manager.headers or {}), "Content-Type": "application/json"},
            data=json.dumps(record),
            allow_redirects=True,
        )
        response.raise_for_status()
        return True
    except Exception as exc:
        _quiet_log(
            logger.warning, "Failure record for %s was refused (%s)", url, exc
        )
        return False


def _subject(kwargs):
    """Whatever identifies the job this handler was working on.

    The handlers disagree about where the task is: ``gw_task_prerun`` and
    ``gw_task_postrun`` take it as ``task``, the rest as ``sender``. The job's
    own URL is the useful answer because it carries the job id; the task name
    is the fallback for a signal that fired before the JobManager existed.
    """
    for key in ("sender", "task"):
        candidate = kwargs.get(key)
        url = getattr(getattr(candidate, "job_manager", None), "url", None)
        if url:
            return url
    for key in ("sender", "task"):
        name = getattr(kwargs.get(key), "name", None)
        if name:
            return name
    return "an unidentified task"


def _guarded(handler):
    """``handler``, with any exception logged against its job and swallowed.

    Celery already swallows what a signal receiver raises
    (``Signal.send`` catches and logs), so this does not change whether the run
    continues -- it changes what is *said*. Celery's line is
    ``Signal handler <function gw_task_success> raised``, with no job id and no
    hint that a status transition was lost; a post-mortem reading it has no way
    to connect it to the document that is now wrong. This names both.
    """

    @functools.wraps(handler)
    def guard(*args, **kwargs):
        try:
            return handler(*args, **kwargs)
        except Exception as exc:
            _quiet_log(
                logger.error,
                "girder_worker's %s failed for %s (%s). The task itself is "
                "unaffected -- celery has already moved on -- so this job's "
                "status may now be wrong and /sivacor/reap has to reconcile it",
                handler.__name__,
                _subject(kwargs),
                exc,
            )

    return guard


def _girder_worker_app():
    """``girder_worker.app``, imported late on purpose.

    :func:`install` runs from ``SIVACORWorkerPlugin.__init__``, which
    ``girder_worker.app`` calls through ``discover_tasks`` **while that module
    is still executing**. Importing it at the top of this one would be a
    circular import resolved only by CPython's ``sys.modules`` fallback; asking
    for it here, at call time, needs no such luck.
    """
    from girder_worker import app

    return app


#: The handlers replaced, so :func:`install` can be checked and undone.
_GUARDED_SIGNALS = (
    (task_prerun, "gw_task_prerun"),
    (task_success, "gw_task_success"),
    (task_failure, "gw_task_failure"),
    (task_revoked, "gw_task_revoked"),
    (task_postrun, "gw_task_postrun"),
)

#: girder_worker handlers replaced outright rather than merely guarded. The
#: key names the upstream handler that is disconnected; the value is what takes
#: its place, and is wrapped by :func:`_guarded` like the rest.
_REPLACEMENTS = {"gw_task_failure": sivacor_task_failure}

_installed = False
#: Kept alive, and kept reachable for :func:`uninstall`. Celery's ``connect``
#: stores receivers weakly by default, so a wrapper nothing references would be
#: collected and the signal would silently lose its handler.
_wrappers: dict[str, object] = {}


def install():
    """Patch girder_worker, once per process. Safe to call again.

    Called from ``SIVACORWorkerPlugin.__init__``, which girder_worker runs via
    ``discover_tasks`` -- so this lands in the celery worker, and also in the
    Girder server, which imports the same app to publish tasks. Harmless there:
    the server owns no JobManager and receives no task signals.
    """
    global _installed
    if _installed:
        return
    gw_app = _girder_worker_app()
    JobManager.updateStatus = resilient_update_status
    for signal, name in _GUARDED_SIGNALS:
        handler = getattr(gw_app, name, None)
        if handler is None:
            # A girder_worker that does not have this handler is one we do not
            # recognise; skipping is the only safe answer, and silence here
            # would be the exact failure mode this module is about.
            logger.warning("girder_worker has no %s to guard", name)
            continue
        wrapper = _guarded(_REPLACEMENTS.get(name, handler))
        signal.disconnect(handler)
        signal.connect(wrapper, weak=False)
        _wrappers[name] = wrapper
    _installed = True


def uninstall():
    """Undo :func:`install`. For tests; nothing in production calls it."""
    global _installed
    if not _installed:
        return
    gw_app = _girder_worker_app()
    JobManager.updateStatus = _ORIGINAL_UPDATE_STATUS
    for signal, name in _GUARDED_SIGNALS:
        wrapper = _wrappers.pop(name, None)
        if wrapper is None:
            continue
        signal.disconnect(wrapper)
        signal.connect(getattr(gw_app, name), weak=False)
    _installed = False

