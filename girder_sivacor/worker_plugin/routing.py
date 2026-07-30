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

import os

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


def pin_chain(task, queue):
    """Route the remainder of ``task``'s chain to ``queue``.

    ``task.request.chain`` holds the not-yet-published steps as serialized
    signatures (in reverse order); celery re-reads their ``options`` when it
    publishes each one, so overriding the queue here is enough to redirect
    everything downstream.
    """
    for link in getattr(task.request, "chain", None) or []:
        link.setdefault("options", {})["queue"] = queue
