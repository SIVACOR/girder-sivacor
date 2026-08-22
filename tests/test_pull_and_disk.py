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
    disk_floor_bytes,
    disk_shortfall,
    image_on_disk_estimate,
    pull_image,
    pull_space_shortfall,
    workspace_has_own_filesystem,
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


# --- open item 3: what these checks mean once the workspace is on a volume ---
#
# A Cinder scratch volume splits one filesystem into two, and both checks above
# were written when there was only one. The floor's justification (a full disk
# wedges the VM) does not survive the split, and the pre-pull check was measuring
# the wrong side of it: the image is unpacked into the per-VM store, never onto
# the per-submission volume. Answered 2026-08-22; see
# development_notes/cinder_volumes_plan.md open item 3.

VOLUME_GB = 20


def _split_filesystems(volume_free, root_free, volume_total=VOLUME_GB * 1000**3):
    """Patch in a workspace on its own filesystem, with per-path free space."""
    def usage(path):
        if path == "/":
            return mock.MagicMock(free=root_free, total=500 * 1000**3)
        return mock.MagicMock(free=volume_free, total=volume_total)

    return (
        mock.patch(
            "girder_sivacor.worker_plugin.lib.workspace_has_own_filesystem",
            return_value=True,
        ),
        mock.patch(
            "girder_sivacor.worker_plugin.lib.shutil.disk_usage", side_effect=usage
        ),
    )


def test_the_floor_is_unchanged_where_it_still_protects_the_vm(tmp_path):
    """No volume, no change. The 5 GiB floor is load-bearing here."""
    assert disk_floor_bytes({"workspace_dir": str(tmp_path)}) == DISK_FLOOR_BYTES


def test_the_floor_on_a_volume_is_a_share_of_the_volume():
    """5 GiB is a quarter of a 20 GB volume, taken from the user's own allowance.

    The volume holds only the workspace, so filling it fails one run cleanly --
    which is the outcome the floor exists to manufacture in the first place.
    """
    own_fs, usage = _split_filesystems(volume_free=10 * 1024**3, root_free=40 * 1024**3)
    with own_fs, usage:
        floor = disk_floor_bytes({"workspace_dir": "/home/ubuntu/volumes/tmp"})
    assert floor == int(VOLUME_GB * 1000**3 * 0.1)
    assert floor < DISK_FLOOR_BYTES, "this may only ever lower a floor"


def test_a_run_on_a_volume_is_not_aborted_at_the_shared_disk_floor():
    """The concrete cost of the old constant: 4 GiB free on a 20 GB volume.

    Under the shared-disk floor this aborted a run that had 4 of its 20 GB left.
    """
    own_fs, usage = _split_filesystems(volume_free=4 * 1024**3, root_free=40 * 1024**3)
    with own_fs, usage:
        assert disk_shortfall({"workspace_dir": "/home/ubuntu/volumes/tmp"}) is None


def test_a_volume_that_is_genuinely_nearly_full_still_stops_the_run():
    """Lowered, not removed: the proportional floor still has to bite."""
    own_fs, usage = _split_filesystems(volume_free=1 * 1024**3, root_free=40 * 1024**3)
    with own_fs, usage:
        msg = disk_shortfall({"workspace_dir": "/home/ubuntu/volumes/tmp"})
    assert msg is not None and "Ran out of disk space" in msg
    # The floor it names is the one it used, not the constant. 10% of 20 decimal
    # GB is 1.9 GiB, which is also a reminder that the two units differ.
    expected = f"{int(VOLUME_GB * 1000**3 * 0.1) / 1024**3:.1f} GiB floor"
    assert expected in msg, msg
    assert f"{DISK_FLOOR_BYTES / 1024**3:.1f} GiB floor" not in msg


def test_the_preflight_check_measures_where_the_image_actually_lands():
    """The regression this exists to prevent, and it is invisible without a volume.

    The workspace volume has 80 GB free and the worker's own disk has 4 -- so a
    check that reads the workspace clears a ~16 GiB pull that cannot fit. It
    would first bite on a multi-stage submission, where each stage unpacks
    another image onto the same per-VM store.
    """
    cli = _cold_client()
    own_fs, usage = _split_filesystems(
        volume_free=80 * 1024**3, root_free=4 * 1024**3, volume_total=100 * 1000**3
    )
    with own_fs, usage:
        msg = pull_space_shortfall(cli, {"workspace_dir": "/home/ubuntu/volumes/tmp"}, DYNARE)

    assert msg is not None, "the pull cannot fit the disk it is actually going to"
    assert "worker's own disk" in msg
    # It must not blame the package: the package is on the volume, which is not
    # what is short, and nothing the researcher does to it would help.
    assert "Reduce the size of the package" not in msg
    assert "support@sivacor.org" in msg
    cli.api.pull.assert_not_called()


def test_a_pull_is_not_charged_the_workspace_floor_on_another_filesystem():
    """The mirror image: room on the root disk, a nearly-full volume.

    The image fits where it is going, so the pull proceeds. Whether the *run*
    then has room is disk_shortfall's question, on the other filesystem.
    """
    cli = _cold_client()
    own_fs, usage = _split_filesystems(volume_free=1 * 1024**3, root_free=20 * 1024**3)
    with own_fs, usage:
        assert (
            pull_space_shortfall(cli, {"workspace_dir": "/home/ubuntu/volumes/tmp"}, DYNARE)
            is None
        )


def test_a_shared_filesystem_is_the_conservative_answer_when_it_cannot_be_told():
    """A stat that fails must not silently lower a floor."""
    with mock.patch(
        "girder_sivacor.worker_plugin.lib.os.stat", side_effect=OSError("gone")
    ):
        assert workspace_has_own_filesystem("/home/ubuntu/volumes/tmp") is False


def test_the_two_filesystems_are_told_apart_by_device():
    """Asked of the filesystem, not of the submission's requested_disk_gb.

    What matters is where the bytes land; a worker whose layout differs for any
    other reason should get the same answer.
    """
    devices = {"/": 1, "/home/ubuntu/volumes/tmp": 2}

    def stat(path):
        return mock.MagicMock(st_dev=devices[path])

    with mock.patch("girder_sivacor.worker_plugin.lib.os.stat", side_effect=stat):
        assert workspace_has_own_filesystem("/home/ubuntu/volumes/tmp") is True
        devices["/home/ubuntu/volumes/tmp"] = 1
        assert workspace_has_own_filesystem("/home/ubuntu/volumes/tmp") is False


def test_a_full_volume_is_not_told_to_ask_for_a_volume():
    """The advice has to fit the submission that reads it.

    A run that already holds 20 GB of extra scratch disk and filled it must be
    told to ask for a *larger* allowance -- "contact us about extra scratch disk"
    is the same stale-copy failure as naming a control that does not exist, only
    pointing the other way.
    """
    own_fs, usage = _split_filesystems(volume_free=1 * 1024**3, root_free=40 * 1024**3)
    with own_fs, usage:
        msg = disk_shortfall(
            {
                "workspace_dir": "/home/ubuntu/volumes/tmp",
                "telemetry_requested_disk_gb": VOLUME_GB,
            }
        )
    assert msg is not None
    assert f"{VOLUME_GB} GB of extra scratch disk" in msg
    assert "larger allowance" in msg
    assert "about extra scratch disk" not in msg
    # Still not naming the field: an allowance is raised by asking, not by editing.
    assert "disk_gb" not in msg
