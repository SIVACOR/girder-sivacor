"""One submission per ephemeral worker (plan P3.2).

An autoscaled worker exists to serve exactly one submission and then power off. It
stops consuming the shared dispatch queue the moment it accepts one, which is what
lets the controller's arithmetic collapse to ``desired instances == queue depth``
rather than having to model how many submissions each worker might be holding.

Two things these tests pin down, both of which would be silent in production:

* the manager's **static** worker must never stop consuming -- it is long-lived, and
  a stray cancel there would strand every later submission;
* the cancel must be **addressed to this node**. Without a destination
  ``cancel_consumer`` broadcasts, which would stop the entire fleet consuming.
"""

import mock
import pytest
from girder_sivacor.worker_plugin.routing import (
    DISPATCH_QUEUE,
    is_ephemeral_worker,
    stop_accepting_submissions,
)


@pytest.fixture
def task():
    t = mock.MagicMock()
    t.request.hostname = "celery@worker-abc"
    return t


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes"])
def test_ephemeral_mode_recognises_truthy_settings(monkeypatch, value):
    monkeypatch.setenv("SIVACOR_EPHEMERAL_WORKER", value)
    assert is_ephemeral_worker() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
def test_ephemeral_mode_defaults_to_off(monkeypatch, value):
    """
    Off unless explicitly asked for.

    The manager's co-located worker shares this image and this code path, so
    anything other than an explicit opt-in must leave it consuming.
    """
    monkeypatch.setenv("SIVACOR_EPHEMERAL_WORKER", value)
    assert is_ephemeral_worker() is False


def test_absent_setting_is_off(monkeypatch):
    monkeypatch.delenv("SIVACOR_EPHEMERAL_WORKER", raising=False)
    assert is_ephemeral_worker() is False


def test_ephemeral_worker_stops_consuming_the_dispatch_queue(monkeypatch, task):
    monkeypatch.setenv("SIVACOR_EPHEMERAL_WORKER", "1")
    app = mock.MagicMock()

    node = stop_accepting_submissions(app, task)

    assert node == "celery@worker-abc"
    app.control.cancel_consumer.assert_called_once_with(
        DISPATCH_QUEUE, destination=["celery@worker-abc"]
    )


def test_static_worker_keeps_consuming(monkeypatch, task):
    """The manager's worker must be untouched, or submissions stop being served."""
    monkeypatch.delenv("SIVACOR_EPHEMERAL_WORKER", raising=False)
    app = mock.MagicMock()

    assert stop_accepting_submissions(app, task) is None
    app.control.cancel_consumer.assert_not_called()


def test_cancel_is_always_addressed_to_one_node(monkeypatch, task):
    """
    Never broadcast.

    ``cancel_consumer(queue)`` with no destination reaches EVERY worker on the
    broker, so a missing node name must abort rather than fall back -- otherwise one
    submission would stop the whole fleet consuming, and the only symptom would be
    submissions queueing forever with healthy-looking workers.
    """
    monkeypatch.setenv("SIVACOR_EPHEMERAL_WORKER", "1")
    app = mock.MagicMock()
    task.request.hostname = None

    assert stop_accepting_submissions(app, task) is None
    app.control.cancel_consumer.assert_not_called()


def test_private_queue_is_never_cancelled(monkeypatch, task):
    """
    Only the shared queue is dropped.

    The chain's remaining steps are pinned to this worker's private queue, so
    cancelling that one would strand the submission it just accepted.
    """
    monkeypatch.setenv("SIVACOR_EPHEMERAL_WORKER", "1")
    app = mock.MagicMock()

    stop_accepting_submissions(app, task)

    (queue,), kwargs = app.control.cancel_consumer.call_args
    assert queue == DISPATCH_QUEUE
    assert not queue.startswith(f"{DISPATCH_QUEUE}.")


# --- surviving a restart (incident 2026-08-12) ------------------------------
# `cancel_consumer` is a runtime control message: its effect lives in the worker
# process and dies with it. On 2026-08-12 a worker 9.5 h into a four-stage submission
# wedged, the RECOVER_TICKS supervisor correctly restarted its celery container, and
# the restarted process re-subscribed to the dispatch queue -- because nothing
# re-issues the cancel for a chain already in flight. Nine hours later it reserved a
# submission it could not start, and the controller saw depth=0 and created nothing.
#
# These pin the startup half of the fix. They run without Redis: the client is mocked,
# which is also the only way to exercise the "Redis is down" branch.

from girder_sivacor.worker_plugin.run_submission import (  # noqa: E402
    SPENT_KEY_PREFIX,
    SPENT_MARKER_TTL_SECONDS,
    _decline_dispatch_if_spent,
    _mark_spent,
)

INSTANCE = "16ccbfc2-ad81-4604-9294-9bd940f8d2fb"
QUEUE = f"sivacor.{INSTANCE}"


@pytest.fixture
def worker():
    """A celery worker instance as ``celeryd_after_setup`` supplies it."""
    return mock.MagicMock()


@pytest.fixture
def ephemeral(monkeypatch):
    monkeypatch.setenv("SIVACOR_EPHEMERAL_WORKER", "1")
    monkeypatch.setenv("SIVACOR_WORKER_QUEUE", QUEUE)


def _redis(monkeypatch, *, spent=True, raises=False):
    client = mock.MagicMock()
    if raises:
        client.exists.side_effect = RuntimeError("redis down")
    else:
        client.exists.return_value = 1 if spent else 0
    monkeypatch.setattr(
        "girder_sivacor.worker_plugin.run_submission._redis_client_sync",
        lambda: client,
    )
    return client


def test_spent_worker_declines_the_dispatch_queue_after_a_restart(
    monkeypatch, ephemeral, worker
):
    client = _redis(monkeypatch, spent=True)
    _decline_dispatch_if_spent(sender="celery@abc", instance=worker)
    client.exists.assert_called_once_with(f"{SPENT_KEY_PREFIX}{INSTANCE}")
    worker.app.amqp.queues.deselect.assert_called_once_with(DISPATCH_QUEUE)


def test_the_private_queue_survives_the_decline(monkeypatch, ephemeral, worker):
    """The point of restarting mid-chain is to consume the queued next step."""
    _redis(monkeypatch, spent=True)
    _decline_dispatch_if_spent(sender="celery@abc", instance=worker)
    deselected = [c.args[0] for c in worker.app.amqp.queues.deselect.call_args_list]
    assert QUEUE not in deselected


def test_an_unspent_worker_keeps_consuming(monkeypatch, ephemeral, worker):
    """A fresh instance must take work; this is the normal boot."""
    _redis(monkeypatch, spent=False)
    _decline_dispatch_if_spent(sender="celery@abc", instance=worker)
    worker.app.amqp.queues.deselect.assert_not_called()


def test_the_static_worker_never_declines(monkeypatch, worker):
    """The manager's worker is long-lived and serves many submissions."""
    monkeypatch.delenv("SIVACOR_EPHEMERAL_WORKER", raising=False)
    monkeypatch.setenv("SIVACOR_WORKER_QUEUE", "sivacor.static-01")
    client = _redis(monkeypatch, spent=True)
    _decline_dispatch_if_spent(sender="celery@abc", instance=worker)
    client.exists.assert_not_called()
    worker.app.amqp.queues.deselect.assert_not_called()


def test_unreachable_redis_keeps_consuming(monkeypatch, ephemeral, worker):
    """Refusing work on a *failed probe* would strand the instance permanently.

    Over-counted headroom is the milder error, and the controller's unclaimed-demand
    signal catches its consequence; an instance that consumes nothing can never serve
    anything and cannot be told apart from the D9 phantom.
    """
    _redis(monkeypatch, raises=True)
    _decline_dispatch_if_spent(sender="celery@abc", instance=worker)
    worker.app.amqp.queues.deselect.assert_not_called()


def test_no_configured_queue_is_a_no_op(monkeypatch, worker):
    monkeypatch.setenv("SIVACOR_EPHEMERAL_WORKER", "1")
    monkeypatch.delenv("SIVACOR_WORKER_QUEUE", raising=False)
    _decline_dispatch_if_spent(sender="celery@abc", instance=worker)
    worker.app.amqp.queues.deselect.assert_not_called()


def test_mark_spent_writes_a_key_that_outlives_the_submission(monkeypatch):
    client = _redis(monkeypatch)
    _mark_spent(INSTANCE)
    (key, _), kwargs = client.set.call_args
    assert key == f"{SPENT_KEY_PREFIX}{INSTANCE}"
    # Must exceed the controller's max_lifetime -- production runs 180 h -- or the
    # marker expires under a running submission and re-arms the bug.
    assert kwargs["ex"] == SPENT_MARKER_TTL_SECONDS
    assert SPENT_MARKER_TTL_SECONDS > 180 * 60 * 60


def test_mark_spent_never_fails_the_submission(monkeypatch):
    """Losing the marker costs headroom accounting, not a researcher's run."""
    client = _redis(monkeypatch)
    client.set.side_effect = RuntimeError("redis down")
    _mark_spent(INSTANCE)  # must not raise
