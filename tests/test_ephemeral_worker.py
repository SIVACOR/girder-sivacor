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
