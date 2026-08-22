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

import docker
import mock
import pytest
from girder_sivacor.errors import FailureCode, SubmissionError
from girder_sivacor.worker_plugin.lib import (
    DISK_FLOOR_BYTES,
    HEARTBEAT_INTERVAL,
    _is_out_of_space,
    disk_shortfall,
    image_on_disk_estimate,
    pull_image,
    pull_space_shortfall,
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

    with pytest.raises(SubmissionError, match="rate limit exceeded") as exc:
        pull_image(cli, api, {"job_id": "job-1"}, IMAGE)

    # The message must name the image, so a registry problem is not mistaken for
    # a broken replication package.
    assert IMAGE in str(exc.value)
    # Classified as infrastructure, and the image reference -- unlike the
    # registry's own error text -- is safe to keep in the execution record.
    assert exc.value.code is FailureCode.IMAGE_PULL_FAILED
    assert exc.value.detail == IMAGE


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
    # Names the request route, and NOT `resources.disk_gb`. Extra scratch disk is
    # granted per user and has no UI yet (C4), so naming the field would send a
    # researcher to hand-edit a workflow file for something they may not be approved
    # for -- the mistake worker sizing made in reverse, shipping an OOM message that
    # named a picker three phases before the picker existed.
    assert "support@sivacor.org" in msg
    assert "disk_gb" not in msg


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


# --- C0.1: a pull that fills the disk is a disk failure ---------------------
#
# Four production submissions failed on 2026-08-20/21 as `image_pull_failed`,
# all of them >5GB packages pulling a ~15 GiB dynare image onto a 60 GB root
# disk that already held their extracted workspace. `out_of_disk` had never been
# recorded once, because the only free-space check ran inside recorded_run's
# poll loop -- which does not exist until a container is running, i.e. after the
# pull. The tests below cover both halves of the fix: the estimate that declines
# to guess, and the observation that cannot be wrong.
# See development_notes/cinder_volumes_plan.md C0.1.

DYNARE = "dynare/dynare:6.1-R2024a"


def _cold_client():
    """A docker client that has none of the images it is asked about."""
    cli = mock.MagicMock()
    cli.images.get.side_effect = docker.errors.ImageNotFound(DYNARE)
    return cli


def test_a_pull_that_fills_the_disk_is_recorded_as_disk_not_registry():
    """The reliable half: classified from what docker reported, not an estimate.

    This is the branch that fixes the four production records. Anything that
    routes an ENOSPC pull back to IMAGE_PULL_FAILED reintroduces exactly the
    misattribution C0.1 exists to remove.
    """
    cli = mock.MagicMock()
    cli.api.pull.return_value = iter(
        _pull_events(
            2, error="failed to register layer: write /var/lib/docker/overlay2/"
            "abc/diff/usr/local/MATLAB: no space left on device"
        )
    )

    with pytest.raises(SubmissionError) as exc:
        pull_image(cli, mock.MagicMock(), {"job_id": "job-1"}, DYNARE)

    assert exc.value.code is FailureCode.OUT_OF_DISK
    assert exc.value.code is not FailureCode.IMAGE_PULL_FAILED
    # The researcher is told what is actually wrong, and that it is about their
    # package's size rather than our registry.
    assert "Ran out of disk space" in str(exc.value)
    assert DYNARE in str(exc.value)
    # No detail: OUT_OF_DISK has no telemetry validator, so anything here would
    # be dropped server-side anyway -- and the path docker names is the
    # researcher's.
    assert exc.value.detail is None


def test_a_genuine_registry_failure_is_still_a_registry_failure():
    """The guard on the guard: a rate limit must not become a disk problem.

    A false positive here tells a researcher to shrink a package that was never
    too big, and there is nothing they can do to disprove it.
    """
    cli = mock.MagicMock()
    cli.api.pull.return_value = iter(
        _pull_events(2, error="toomanyrequests: rate limit exceeded")
    )

    with pytest.raises(SubmissionError) as exc:
        pull_image(cli, mock.MagicMock(), {"job_id": "job-1"}, DYNARE)

    assert exc.value.code is FailureCode.IMAGE_PULL_FAILED
    assert exc.value.detail == DYNARE


@pytest.mark.parametrize(
    "text",
    [
        "no space left on device",
        "Error processing tar file(exit status 1): no space left on device",
        "failed to register layer: write /var/lib/docker/tmp/x: not enough space",
        "NO SPACE LEFT ON DEVICE",
    ],
)
def test_the_shapes_docker_reports_enospc_in_are_all_recognised(text):
    assert _is_out_of_space(text) is True


@pytest.mark.parametrize(
    "text",
    [
        "",
        None,
        "toomanyrequests: rate limit exceeded",
        "manifest for dynare/dynare:9.9 not found",
        "unauthorized: authentication required",
        "dial tcp: i/o timeout",
    ],
)
def test_nothing_else_is_mistaken_for_a_full_disk(text):
    assert _is_out_of_space(text) is False


def test_the_pull_is_refused_up_front_when_the_image_cannot_fit():
    """The cheap half: fail in a second rather than 280 of them.

    All four production failures burned ~282 s pulling before dying. The
    arithmetic was knowable before the first byte.
    """
    cli = _cold_client()
    # 8 GiB free against dynare's ~16 GiB on disk plus the working reserve.
    usage = mock.MagicMock(free=8 * 1024**3)
    with mock.patch(
        "girder_sivacor.worker_plugin.lib.shutil.disk_usage", return_value=usage
    ):
        msg = pull_space_shortfall(cli, {"workspace_dir": "/tmp/w"}, DYNARE)

    assert msg is not None
    assert DYNARE in msg
    # It must say that this is an estimate and what it rests on. An estimate
    # presented as a measurement is worse than no estimate.
    assert "compressed" in msg
    assert "about" in msg
    # The remedy, and it must not go stale. The original wording ended "is the only
    # thing that changes this today", which was true when written and false the moment
    # anyone was approved for extra disk.
    assert "support@sivacor.org" in msg
    assert "only thing that changes" not in msg
    assert "disk_gb" not in msg
    # cli.api.pull is never reached.
    cli.api.pull.assert_not_called()


def test_a_pull_that_fits_is_not_pre_empted():
    """The common case: say nothing, cost nothing."""
    cli = _cold_client()
    usage = mock.MagicMock(free=60 * 1024**3)
    with mock.patch(
        "girder_sivacor.worker_plugin.lib.shutil.disk_usage", return_value=usage
    ):
        assert pull_space_shortfall(cli, {"workspace_dir": "/tmp/w"}, DYNARE) is None


def test_an_image_already_on_the_worker_needs_no_room():
    """A later stage of a multi-stage submission must not be refused.

    The worker already has the image; the pull is a no-op. Refusing it for want
    of space it does not need would be a regression this check invented.
    """
    cli = mock.MagicMock()          # images.get succeeds
    usage = mock.MagicMock(free=1 * 1024**3)   # almost nothing free
    with mock.patch(
        "girder_sivacor.worker_plugin.lib.shutil.disk_usage", return_value=usage
    ):
        assert pull_space_shortfall(cli, {"workspace_dir": "/tmp/w"}, DYNARE) is None
    assert image_on_disk_estimate(cli, DYNARE) == (0, "already present locally")


def test_an_unknown_image_family_is_never_pre_judged():
    """Declining to guess is the safe direction, and it is the chosen one.

    Guessing high refuses a pull that would have fitted and tells the researcher
    something untrue about their package. Guessing low, or not guessing, leaves
    the pull to fail -- which the ENOSPC branch above now labels correctly.
    """
    cli = mock.MagicMock()
    cli.images.get.side_effect = docker.errors.ImageNotFound("who/what:1")
    assert image_on_disk_estimate(cli, "who/what:1") is None
    usage = mock.MagicMock(free=1024)   # 1 KiB free, and still no opinion
    with mock.patch(
        "girder_sivacor.worker_plugin.lib.shutil.disk_usage", return_value=usage
    ):
        assert pull_space_shortfall(cli, {"workspace_dir": "/tmp/w"}, "who/what:1") is None


def test_the_dynare_estimate_matches_what_the_codebase_already_documents():
    """~16 GiB, independently: 6.3 GiB compressed at 2.5x.

    pull_image's own docstring has said "a cold dynare pull is ~15 GB" since
    before this check existed, from a different source. The two agreeing is the
    only validation the multiplier has, so pin it.
    """
    cli = _cold_client()
    needed, how = image_on_disk_estimate(cli, DYNARE)
    assert 14 * 1024**3 < needed < 18 * 1024**3
    assert "2.5x" in how

    # And the spread that explains why only dynare has ever caused this: a Stata
    # pull is an order of magnitude smaller and essentially cannot fill a disk.
    stata_needed, _ = image_on_disk_estimate(_cold_client(), IMAGE)
    assert stata_needed * 8 < needed


def test_the_preflight_check_never_fails_a_run_by_itself():
    """Same rule as disk_shortfall: a check that cannot run is not a failure."""
    cli = _cold_client()
    with mock.patch(
        "girder_sivacor.worker_plugin.lib.shutil.disk_usage",
        side_effect=OSError("stale NFS handle"),
    ):
        assert pull_space_shortfall(cli, {"workspace_dir": "/tmp/w"}, DYNARE) is None

    # ...including when the local-image probe itself is what breaks.
    broken = mock.MagicMock()
    broken.images.get.side_effect = RuntimeError("docker socket gone")
    assert image_on_disk_estimate(broken, "who/what:1") is None
