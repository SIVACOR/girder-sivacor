"""Writing a progress line is a remote call, and it must never kill the run.

``recorded_run``'s poll loop drains the container's stdout with ``print()``.
Under ``girder_worker`` that stdout is teed into a ``JobManager`` that ``PUT``s
the bytes to Girder and ends its flush in ``raise_for_status()`` -- so each
progress line is an HTTP request. On 2026-09-18 one came back ``502`` (from
Traefik; the backend never saw it), nothing caught it, and a healthy run 20 h in
with 8 of 27 sub-runs done was torn down
(``2026-09-18-log-flush-502-kills-run.md``).

That is ``2026-09-06-broker-blip-kills-run.md`` through a second channel. These
tests pin the three properties of :class:`LogRelay` that close it: it never
raises, a failed write neither loses nor duplicates the line, and a failing
write does not spin.
"""

import queue

import mock
import pytest
from girder_sivacor.worker_plugin.lib import LogRelay


def loaded(*lines):
    """A queue holding ``lines``, plus the relay that drains it."""
    q = queue.Queue()
    for line in lines:
        q.put(line)
    return LogRelay("job-1"), q


def boom(*_args, **_kwargs):
    """What a 502 looks like from inside ``print()``."""
    raise OSError("502 Server Error: Bad Gateway")


# -- the property the incident is about -----------------------------------


def test_drain_never_raises_when_the_write_fails():
    """The regression: a failed job-log write must not reach the caller.

    Before LogRelay this exception unwound out of the poll loop, out of the
    enclosing TemporaryDirectory, and destroyed the run.
    """
    relay, q = loaded("a line")
    with mock.patch("builtins.print", boom):
        relay.drain(q)  # must not raise
    assert relay.failures == 1


def test_a_failed_line_is_not_reprinted():
    """``JobManager`` already buffered it; re-printing would duplicate it.

    ``JobManager.write`` appends to its buffer *before* flushing, and ``_flush``
    clears the buffer only after ``raise_for_status()`` returns. So the failed
    line rides up with the next successful flush, and the relay must not send it
    a second time.
    """
    relay, q = loaded("first", "second")
    with mock.patch("builtins.print", boom):
        relay.drain(q)
    # "first" was consumed, not requeued; "second" was never attempted.
    assert list(q.queue) == ["second"]


def test_draining_stops_at_the_first_failure():
    """One attempt per pass, not one per queued line.

    A server refusing one write will refuse the next, and a long queue against a
    dead server would otherwise cost a full round of blocking HTTP calls inside
    a loop that is supposed to tick once a second.
    """
    calls = []

    def fail(line, **_kwargs):
        calls.append(line)
        raise OSError("502")

    relay, q = loaded("a", "b", "c")
    with mock.patch("builtins.print", fail):
        relay.drain(q)
    assert calls == ["a"]


# -- the ordinary path ----------------------------------------------------


def test_drain_writes_every_queued_line_in_order():
    relay, q = loaded("one", "two", "three")
    with mock.patch("builtins.print") as printed:
        relay.drain(q)
    assert [c.args[0] for c in printed.call_args_list] == ["one", "two", "three"]
    assert q.empty()
    assert relay.failures == 0


def test_drain_on_an_empty_queue_is_a_noop():
    relay, q = loaded()
    with mock.patch("builtins.print") as printed:
        relay.drain(q)
    printed.assert_not_called()


def test_the_failure_count_resets_once_a_write_succeeds():
    """So the log says "recovered after N", and backoff-style noise stops."""
    relay, q = loaded("during the outage")
    with mock.patch("builtins.print", boom):
        relay.drain(q)
    assert relay.failures == 1

    q.put("after it")
    with mock.patch("builtins.print"):
        relay.drain(q)
    assert relay.failures == 0


def test_a_queue_that_empties_mid_drain_is_not_an_error():
    """``drain`` races the logging thread; ``get_nowait`` on an empty queue
    raises ``queue.Empty`` and that is the normal exit, not a failure."""
    relay, q = loaded("only one")
    with mock.patch("builtins.print"):
        relay.drain(q)
    assert relay.failures == 0
    assert q.empty()


# -- the noisy-then-quiet logging contract --------------------------------


@pytest.mark.parametrize("failures,expected", [(1, 1), (3, 3), (4, 3), (30, 4)])
def test_a_long_outage_does_not_fill_the_log(failures, expected, caplog):
    """First NOISY_FAILURES in full, then one line per QUIET_EVERY.

    An outage should be obvious in the log, not the only thing in it.
    """
    relay = LogRelay("job-1")
    with mock.patch("builtins.print", boom), caplog.at_level("WARNING"):
        for _ in range(failures):
            q = queue.Queue()
            q.put("x")
            relay.drain(q)
    assert relay.failures == failures
    assert len(caplog.records) == expected


def test_reporting_a_failure_cannot_itself_raise():
    """The guard has to survive its own error path.

    ``girder_worker`` tees stderr as well as stdout into the JobManager, so a
    logging handler can go out over the same connection that just failed. If
    that were allowed to raise it would escape the ``except`` meant to contain
    the original failure -- the exact shape of the bug this class exists to fix,
    reintroduced one frame deeper.
    """
    relay, q = loaded("a line")
    with mock.patch("builtins.print", boom), mock.patch(
        "girder_sivacor.worker_plugin.lib.logging.warning", side_effect=boom
    ):
        relay.drain(q)  # must not raise
    assert relay.failures == 1
