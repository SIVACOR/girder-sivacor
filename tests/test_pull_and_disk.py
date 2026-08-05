"""Guardrails for running analysis images on a worker with one shared disk.

Analysis images are pulled at run time rather than baked into the worker image,
and an ephemeral worker has no separate volume for ``/var/lib/docker``. Both
choices are deliberate; both introduce a failure the rest of the system cannot
see:

* a long silent pull looks exactly like a dead worker to the server-side reaper;
* a payload that fills the disk wedges the VM rather than failing its own run.

Every test here covers one of those, plus the two traps in the implementation
(a streamed pull does not raise on error; a failed heartbeat must not fail a run).
"""

import mock
import pytest
from girder_sivacor.worker_plugin.lib import (
    DISK_FLOOR_BYTES,
    HEARTBEAT_INTERVAL,
    disk_shortfall,
    pull_image,
)

IMAGE = "dataeditors/stata19_5-mp:2026-04-15"


def _pull_events(n, error=None):
    """A plausible docker pull stream: layer progress, then optionally an error."""
    events = [
        {"status": "Pulling fs layer", "id": f"layer{i}"} for i in range(n)
    ]
    if error:
        events.append({"error": error})
    return events


def _run_pull(events, elapsed=()):
    """Drive pull_image with a stubbed docker client and controlled clock.

    ``elapsed`` supplies successive time.monotonic() values so a test can make
    the heartbeat interval elapse without sleeping.
    """
    cli = mock.MagicMock()
    cli.api.pull.return_value = iter(events)
    api = mock.MagicMock()
    submission = {"job_id": "job-1"}
    ticks = list(elapsed) or [0.0] * (len(events) + 2)
    with mock.patch(
        "girder_sivacor.worker_plugin.lib.time.monotonic", side_effect=ticks
    ):
        pull_image(cli, api, submission, IMAGE)
    return cli, api


def test_pull_streams_rather_than_blocking():
    """The low-level streaming API is what makes progress observable at all.

    images.pull() blocks and emits nothing, so there is no seam to heartbeat
    from -- which is the entire reason this helper exists.
    """
    cli, _ = _run_pull(_pull_events(3))

    cli.api.pull.assert_called_once_with(IMAGE, stream=True, decode=True)
    cli.images.pull.assert_not_called()


def test_pull_heartbeats_so_the_reaper_does_not_kill_a_healthy_run():
    """A cold multi-gigabyte pull must keep meta.heartbeat advancing.

    Without this the only liveness signals are job.updated and job.created, both
    of which stand still through a pull -- so a long enough pull is reaped at
    sivacor.heartbeat_timeout and the researcher is told their submission failed.
    """
    # Clock jumps past the interval on the second event, then stays put.
    ticks = [0.0, 0.0, HEARTBEAT_INTERVAL + 1, HEARTBEAT_INTERVAL + 1]
    _, api = _run_pull(_pull_events(3), elapsed=ticks)

    api.heartbeat.assert_called_with("job-1")
    assert api.heartbeat.call_count >= 1


def test_pull_does_not_heartbeat_more_often_than_the_interval():
    """Chatty progress streams must not turn into a heartbeat per event."""
    _, api = _run_pull(_pull_events(25), elapsed=[0.0] * 30)

    assert api.heartbeat.call_count == 0


def test_failed_pull_raises_rather_than_looking_like_success():
    """
    The trap: a streamed pull does NOT raise the way images.pull() does.

    docker yields an event carrying 'error' and the iterator ends normally. Left
    unchecked the pull looks successful and the run fails later at
    containers.create with an ImageNotFound that points nowhere near the cause.
    """
    cli = mock.MagicMock()
    cli.api.pull.return_value = iter(
        _pull_events(2, error="toomanyrequests: rate limit exceeded")
    )
    api = mock.MagicMock()

    with pytest.raises(RuntimeError, match="rate limit exceeded") as exc:
        pull_image(cli, api, {"job_id": "job-1"}, IMAGE)

    # The message must name the image, so a registry problem is not mistaken for
    # a broken replication package.
    assert IMAGE in str(exc.value)


def test_heartbeat_failure_does_not_abort_a_healthy_pull():
    """A transient Girder blip must not kill a pull that is going fine."""
    cli = mock.MagicMock()
    cli.api.pull.return_value = iter(_pull_events(3))
    api = mock.MagicMock()
    api.heartbeat.side_effect = RuntimeError("503 Service Unavailable")

    ticks = [0.0, 0.0, HEARTBEAT_INTERVAL + 1, HEARTBEAT_INTERVAL + 1]
    with mock.patch(
        "girder_sivacor.worker_plugin.lib.time.monotonic", side_effect=ticks
    ):
        pull_image(cli, api, {"job_id": "job-1"}, IMAGE)  # must not raise


def test_disk_shortfall_is_silent_while_there_is_room(tmp_path):
    """The common case must cost nothing and say nothing."""
    usage = mock.MagicMock(free=DISK_FLOOR_BYTES * 4)
    with mock.patch(
        "girder_sivacor.worker_plugin.lib.shutil.disk_usage", return_value=usage
    ):
        assert disk_shortfall({"workspace_dir": str(tmp_path)}) is None


def test_disk_shortfall_explains_itself_when_the_disk_is_nearly_full(tmp_path):
    """
    The message is the point.

    A full shared disk otherwise surfaces as an unrelated-looking failure inside
    Stata or R -- or wedges the worker entirely, leaving the server-side reaper to
    clean up 30 minutes later. Naming the actual cause is what turns that into a
    submission the researcher can act on.
    """
    usage = mock.MagicMock(free=DISK_FLOOR_BYTES // 2)
    with mock.patch(
        "girder_sivacor.worker_plugin.lib.shutil.disk_usage", return_value=usage
    ):
        msg = disk_shortfall({"workspace_dir": str(tmp_path)})

    assert msg is not None
    assert "Ran out of disk space" in msg
    assert "GiB" in msg


def test_disk_check_never_fails_a_run_by_itself(tmp_path):
    """If the check cannot run, that is not grounds for failing the submission."""
    with mock.patch(
        "girder_sivacor.worker_plugin.lib.shutil.disk_usage",
        side_effect=OSError("stale NFS handle"),
    ):
        assert disk_shortfall({"workspace_dir": str(tmp_path)}) is None


def test_disk_check_tolerates_a_submission_without_a_workspace():
    """Called before create_workspace, it must fall back rather than KeyError."""
    usage = mock.MagicMock(free=DISK_FLOOR_BYTES * 4)
    with mock.patch(
        "girder_sivacor.worker_plugin.lib.shutil.disk_usage", return_value=usage
    ) as du:
        assert disk_shortfall({}) is None
    du.assert_called_once_with("/tmp")
