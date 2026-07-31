"""Keeping one submission's tasks on one worker.

A submission downloads its replication package into a local directory and every
later stage of the chain assumes that directory is still there. That was free
while there was a single ``local`` queue; once workers are distributed, celery
is free to hand each task to a different machine.

So every worker consumes two queues: :data:`DISPATCH_QUEUE`, shared by all of
them, and a private one named after the worker. The head of the chain is
published to the shared queue and load-balanced normally; once it lands, it
rewrites the queue of every remaining step to its own private queue, and the
rest of the submission follows it.

A worker therefore has to be started with both, e.g.::

    celery -A girder_worker.app worker -Q sivacor,sivacor.$(hostname)

Periodic housekeeping rides Girder core's own :data:`LOCAL_QUEUE`, which the
co-located worker already has to consume.
"""

import logging
import os

logger = logging.getLogger(__name__)

#: Shared queue that new submissions are published to.
DISPATCH_QUEUE = os.environ.get("SIVACOR_DISPATCH_QUEUE", "sivacor")

#: Prefix for the per-worker queues; the worker's node name is appended.
QUEUE_PREFIX = f"{DISPATCH_QUEUE}."

#: Girder core's own queue, which also carries periodic housekeeping.
#:
#: Core defines ``deleteFolderTask``, ``importDataTask`` and friends as
#: ``@app.task(queue='local')`` and ``ensure_local_worker_available()`` returns
#: HTTP 503 if nothing consumes it, so a co-located worker must subscribe to it
#: regardless. Housekeeping rides along.
#:
#: What matters is only that housekeeping stays off :data:`DISPATCH_QUEUE`:
#: depth there is a count of submissions waiting for a worker, which is the
#: signal an autoscaler scales on, and under scale-to-zero a periodic task
#: sitting in it would book a whole VM to issue one HTTP POST.
#:
#: A dedicated ``sivacor.maintenance`` queue was tried first, to keep the
#: reaper from queuing behind a multi-gigabyte folder delete. That does not
#: work: a celery worker's ``--concurrency`` is one pool shared across every
#: queue it consumes, so a separate queue name buys no reserved execution slot.
#: Real isolation would need a separate worker *process*, and the sweeps are
#: not time-critical enough to warrant one -- the reaper threshold is 30
#: minutes and it re-fires every 10.
LOCAL_QUEUE = "local"

#: Steps that must NOT be pinned to the submitting worker's private queue,
#: matched on the last dotted component of the celery task name.
#:
#: ``sign_tro`` is declared on :data:`LOCAL_QUEUE` because the manager is the
#: only host holding the TRS *private* key (see the plan's D2). Left pinned, it
#: would follow the chain onto the worker and either fail there or -- worse,
#: during a transition where a worker still has key material -- succeed, which
#: is a silent regression of the whole point.
#:
#: Discrimination is by task *name* on purpose. Skipping links that "already
#: have a queue" looks equivalent and is not: links arrive carrying
#: :data:`DISPATCH_QUEUE` (from the task default and the chain's
#: ``apply_async(queue=...)``), so a presence check would skip every step, pin
#: nothing, and scatter one submission's chain across workers that do not hold
#: its workspace.
UNPINNED_TASKS = frozenset({"sign_tro"})


def worker_queue(task):
    """Return the private queue name of the worker running ``task``.

    Derived from the celery node name (``celery@somehost`` -> ``sivacor.somehost``)
    so that a worker started with ``-Q $SIVACOR_DISPATCH_QUEUE,$(hostname)``
    agrees with us without extra configuration. ``SIVACOR_WORKER_QUEUE`` wins
    if the deployment names its queues some other way.
    """
    if queue := os.environ.get("SIVACOR_WORKER_QUEUE"):
        return queue
    hostname = getattr(task.request, "hostname", None) or "unknown"
    return QUEUE_PREFIX + hostname.split("@")[-1]


def is_ephemeral_worker() -> bool:
    """Whether this worker serves exactly one submission and then goes away.

    Set by the provisioning of an autoscaled instance. The static worker on the
    manager must NOT set it: it is long-lived and has to keep consuming.
    """
    return os.environ.get("SIVACOR_EPHEMERAL_WORKER", "").lower() in (
        "1",
        "true",
        "yes",
    )


def stop_accepting_submissions(app, task) -> str | None:
    """Drop this worker's consumer on the shared dispatch queue.

    The keystone of the one-VM-per-submission design. Called as soon as a worker
    accepts a submission, so it cannot be handed a second one, which is what lets
    the controller's arithmetic collapse to ``desired instances == queue depth``
    instead of having to model how many submissions each worker might be holding.

    Returns the node name it was sent to, or ``None`` if this is not an ephemeral
    worker (the manager's static worker must keep consuming).

    **This narrows the window, it does not close it.** ``cancel_consumer`` is a
    broadcast over the broker and takes effect asynchronously, and submissions are
    acked on receipt rather than on completion -- so a worker can be handed a second
    submission in the gap. ``--concurrency=1 --prefetch-multiplier=1`` keeps that to
    at most one extra, and the consequence is mild: the second submission's chain is
    pinned to the same private queue and simply runs after the first. The controller
    should therefore treat ``desired == depth`` as accurate rather than guaranteed,
    and P3.3's supervisor must check for *no active tasks* rather than assuming one
    submission per instance.
    """
    if not is_ephemeral_worker():
        return None
    node = getattr(task.request, "hostname", None)
    if not node:
        # Without a destination this would broadcast to every worker in the fleet
        # and stop all of them consuming -- refuse rather than guess.
        logger.warning(
            "Cannot stop consuming %s: task has no node name", DISPATCH_QUEUE
        )
        return None
    app.control.cancel_consumer(DISPATCH_QUEUE, destination=[node])
    logger.info("Ephemeral worker %s stopped consuming %s", node, DISPATCH_QUEUE)
    return node


def pin_chain(task, queue):
    """Route the remainder of ``task``'s chain to ``queue``.

    ``task.request.chain`` holds the not-yet-published steps as serialized
    signatures (in reverse order); celery re-reads their ``options`` when it
    publishes each one, so overriding the queue here is enough to redirect
    everything downstream.

    Steps named in :data:`UNPINNED_TASKS` keep whatever queue they were built
    with. Called once per submission, from the head of the chain.
    """
    for link in getattr(task.request, "chain", None) or []:
        if link.get("task", "").rsplit(".", 1)[-1] in UNPINNED_TASKS:
            continue
        link.setdefault("options", {})["queue"] = queue
