"""Guardrails for running analysis images on a worker with one shared disk.

Analysis images are pulled at run time rather than baked into the worker image,
and an ephemeral worker has no separate volume for ``/var/lib/docker``. Both
choices are deliberate; both introduce a failure the rest of the system cannot
see:

* a long silent pull looks exactly like a dead worker to the server-side reaper;
* a payload that fills the disk wedges the VM rather than failing its own run.

Every test here covers one of those, plus the three traps in the implementation
(a streamed pull does not raise on a *registry* error but does raise on a
*daemon* one; a failed heartbeat must not fail a run).
"""

from unittest import mock

import docker
import pytest

from girder_sivacor.errors import FailureCode, SubmissionError, classify
from girder_sivacor.worker_plugin.lib import (
    _IMAGE_FAMILY_COMPRESSED_GB,
    DISK_FLOOR_BYTES,
    HEARTBEAT_INTERVAL,
    PULL_RETRY_BACKOFF,
    _is_out_of_space,
    disk_floor_bytes,
    disk_shortfall,
    image_on_disk_estimate,
    pull_image,
    pull_space_shortfall,
    workspace_has_own_filesystem,
)

IMAGE = "dataeditors/stata19_5-mp:2026-04-15"


@pytest.fixture(autouse=True)
def slept():
    """Record the retry backoff instead of serving it.

    Without this the failure tests really do sleep the budget: this file went
    from 2.6 s to 3 min 21 s the moment the retry landed, because every
    transient-failure test now waits 50 s for nothing. The recorded calls are
    also what the budget assertions below read, so the fixture is a probe as
    much as it is a speed-up.
    """
    with mock.patch("girder_sivacor.worker_plugin.lib.time.sleep") as patched:
        yield patched


def _pull_sequence(*outcomes):
    """A `cli.api.pull` that behaves differently on successive attempts.

    Each outcome is either a list of events to yield or an exception to raise.
    Like the real one, an exception surfaces when the generator is iterated
    rather than when it is called.
    """
    remaining = iter(outcomes)

    def stream(*_args, **_kwargs):
        outcome = next(remaining)
        if isinstance(outcome, BaseException):
            raise outcome
        yield from outcome

    return stream


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


def _api_error(status, explanation):
    """The exception docker-py raises when the *daemon* answers with an error.

    Built from a real response rather than by hand, because ``APIError.__str__``
    reads ``response.status_code``/``.url``/``.reason`` -- a stub that lacks them
    would make the message under test unlike the one production produces.
    """
    response = mock.Mock()
    response.status_code = status
    response.url = (
        "http+docker://localhost/v1.52/images/create"
        "?tag=4.4.2&fromImage=rocker%2Fgeospatial"
    )
    response.reason = "Internal Server Error" if status >= 500 else "Not Found"
    return docker.errors.APIError(
        response.reason, response=response, explanation=explanation
    )


def _raising_pull(exc, events=()):
    """A pull stream that yields ``events`` and then raises, like the real one.

    With no events this raises on the generator's first ``next()``, which is what
    ``_raise_for_status()`` does for a daemon-level failure -- before any event,
    before any heartbeat.
    """

    def stream(*_args, **_kwargs):
        yield from events
        raise exc

    return stream


def test_a_daemon_level_failure_is_a_pull_failure_not_a_platform_defect():
    """The trap's other half, and what it cost.

    `cli.api.pull(stream=True)` ends in `_raise_for_status()`, so a 4xx/5xx from
    the daemon is raised by the generator's first iteration -- it never arrives
    as an event, so the branch written for exactly this situation could not see
    it. The exception escaped to `submission_task`'s generic handler and
    `classify()` recorded it as `unexpected`/`APIError`: the code reserved for
    our own bugs, keeping only the class name.

    On 2026-10-12 that was a transient DNS failure, 7.9 s into stage 5 of a
    50 h 39 m run. The permanent record said nothing true about why it ended,
    and the one earlier `APIError` could not be compared against it.
    """
    cli = mock.MagicMock()
    cli.api.pull.side_effect = _raising_pull(
        _api_error(
            500,
            'failed to resolve reference "docker.io/rocker/geospatial:4.4.2": '
            'failed to do request: Head "https://registry-1.docker.io/v2/'
            'rocker/geospatial/manifests/4.4.2": dial tcp: lookup '
            "registry-1.docker.io on 127.0.0.53:53: server misbehaving",
        )
    )

    with pytest.raises(SubmissionError) as exc:
        pull_image(cli, mock.MagicMock(), {"job_id": "job-1"}, IMAGE)

    assert exc.value.code is FailureCode.IMAGE_PULL_FAILED
    assert exc.value.detail == IMAGE
    # And the whole point: the permanent record no longer calls this a defect.
    assert classify(exc.value) == (FailureCode.IMAGE_PULL_FAILED, IMAGE)


def test_a_daemon_level_failure_does_not_read_as_a_broken_package():
    """The researcher's half. They cannot fix our DNS, and a wall of registry
    jargon under the heading of their own submission invites them to try."""
    cli = mock.MagicMock()
    cli.api.pull.side_effect = _raising_pull(
        _api_error(500, "server misbehaving")
    )

    with pytest.raises(SubmissionError) as exc:
        pull_image(cli, mock.MagicMock(), {"job_id": "job-1"}, IMAGE)

    message = str(exc.value)
    assert IMAGE in message
    assert "not with your replication package" in message
    # The daemon's own words still reach the job log, where they are useful.
    assert "server misbehaving" in message


def test_the_daemons_traceback_survives_the_reclassification():
    """`raise ... from exc` rather than a bare raise: the frames naming
    `_raise_for_status` are the only record of *which* remote call failed, and
    a post-mortem reads them. #129 will carry this same traceback through the
    chain redaction."""
    original = _api_error(500, "server misbehaving")
    cli = mock.MagicMock()
    cli.api.pull.side_effect = _raising_pull(original)

    with pytest.raises(SubmissionError) as exc:
        pull_image(cli, mock.MagicMock(), {"job_id": "job-1"}, IMAGE)

    assert exc.value.__cause__ is original


def test_a_daemon_error_that_is_really_enospc_is_still_a_disk_failure():
    """Disk is tested before registry on the exception path too.

    Routing an ENOSPC daemon error to IMAGE_PULL_FAILED would reintroduce
    through a new door exactly the misattribution C0.1 closed.
    """
    cli = mock.MagicMock()
    cli.api.pull.side_effect = _raising_pull(
        _api_error(
            500,
            "failed to register layer: write /var/lib/docker/overlay2/abc/diff: "
            "no space left on device",
        )
    )

    with pytest.raises(SubmissionError) as exc:
        pull_image(cli, mock.MagicMock(), {"job_id": "job-1"}, IMAGE)

    assert exc.value.code is FailureCode.OUT_OF_DISK
    assert exc.value.detail is None


def test_a_tag_that_does_not_exist_is_a_pull_failure():
    """ImageNotFound is an APIError subclass, so the 404 lands in the same
    branch -- which is right: a tag nobody can fetch is a failed pull, whether
    the daemon says so by raising or by emitting an event."""
    response = mock.Mock(status_code=404, url="http+docker://localhost/images/create")
    response.reason = "Not Found"
    cli = mock.MagicMock()
    cli.api.pull.side_effect = _raising_pull(
        docker.errors.ImageNotFound(
            "Not Found", response=response, explanation="manifest unknown"
        )
    )

    with pytest.raises(SubmissionError) as exc:
        pull_image(cli, mock.MagicMock(), {"job_id": "job-1"}, IMAGE)

    assert exc.value.code is FailureCode.IMAGE_PULL_FAILED
    assert exc.value.detail == IMAGE


def test_a_failure_part_way_through_a_stream_is_caught_too():
    """The daemon can also give up mid-pull, after some layers have arrived."""
    cli = mock.MagicMock()
    cli.api.pull.side_effect = _raising_pull(
        _api_error(500, "server misbehaving"), events=_pull_events(3)
    )

    with pytest.raises(SubmissionError) as exc:
        pull_image(cli, mock.MagicMock(), {"job_id": "job-1"}, IMAGE)

    assert exc.value.code is FailureCode.IMAGE_PULL_FAILED


def test_a_docker_py_bug_is_still_recorded_as_a_defect():
    """Only APIError is caught, not DockerException. "The daemon answered with
    an error" is a pull failure; a broken socket or a docker-py surprise is a
    platform defect, and `unexpected` is the honest code for those -- turning
    every docker exception into IMAGE_PULL_FAILED would hide our own bugs in a
    bucket labelled "the registry's fault"."""
    cli = mock.MagicMock()
    cli.api.pull.side_effect = _raising_pull(
        docker.errors.DockerException("Error while fetching server API version")
    )

    with pytest.raises(docker.errors.DockerException) as exc:
        pull_image(cli, mock.MagicMock(), {"job_id": "job-1"}, IMAGE)

    assert classify(exc.value) == (FailureCode.UNEXPECTED, "DockerException")


# --- retry (#127) ----------------------------------------------------------
#
# There is no resume: a TRO certifies one supervised in-situ execution, so a
# pull that dies at stage 5 throws away stages 1-4 with it. Prevention is the
# only lever, which is why the default here is to retry and the exceptions are
# named one by one rather than the other way round.


def test_a_transient_failure_is_retried_and_the_run_survives():
    """The 2026-10-12 case, had this existed: one failed DNS lookup 7.9 s into
    stage 5 of a 50 h 39 m run, against an image already on the worker."""
    cli = mock.MagicMock()
    cli.api.pull.side_effect = _pull_sequence(
        _api_error(500, "dial tcp: lookup registry-1.docker.io: server misbehaving"),
        _pull_events(3),
    )
    api = mock.MagicMock()

    pull_image(cli, api, {"job_id": "job-1"}, IMAGE)  # must not raise

    assert cli.api.pull.call_count == 2


def test_a_retried_pull_says_so_in_the_job_log():
    """A pull that silently took three tries is a registry problem nobody will
    ever investigate."""
    cli = mock.MagicMock()
    cli.api.pull.side_effect = _pull_sequence(
        _api_error(500, "server misbehaving"),
        _api_error(500, "server misbehaving"),
        _pull_events(2),
    )
    api = mock.MagicMock()

    pull_image(cli, api, {"job_id": "job-1"}, IMAGE)

    written = " ".join(
        str(call.kwargs.get("log", "")) for call in api.update_job.call_args_list
    )
    assert "retrying" in written
    assert "attempt 3 of" in written  # the success line names which attempt won


def test_the_backoff_grows_and_is_served_in_order(slept):
    cli = mock.MagicMock()
    cli.api.pull.side_effect = _pull_sequence(
        *[_api_error(500, "server misbehaving")] * 3, _pull_events(1)
    )

    pull_image(cli, mock.MagicMock(), {"job_id": "job-1"}, IMAGE)

    assert [call.args[0] for call in slept.call_args_list] == list(
        PULL_RETRY_BACKOFF
    )


def test_a_pull_that_never_recovers_fails_after_a_bounded_number_of_attempts():
    cli = mock.MagicMock()
    cli.api.pull.side_effect = _pull_sequence(
        *[_api_error(500, "server misbehaving")] * (len(PULL_RETRY_BACKOFF) + 1)
    )

    with pytest.raises(SubmissionError) as exc:
        pull_image(cli, mock.MagicMock(), {"job_id": "job-1"}, IMAGE)

    assert exc.value.code is FailureCode.IMAGE_PULL_FAILED
    assert cli.api.pull.call_count == len(PULL_RETRY_BACKOFF) + 1


def test_the_retry_budget_cannot_approach_the_reaper(slept):
    """The liveness contract, pinned as arithmetic rather than as a comment.

    The server reaps on `sivacor.heartbeat_timeout` (30 min on production) and
    a backoff sleep is deliberately not a heartbeat -- ProgressBeat ticks on
    progress, never on a clock. So the whole budget has to be small enough that
    nobody needs to argue an exception to that rule. If someone raises
    PULL_RETRY_BACKOFF far enough for this to fail, the choice between the two
    has to be made again rather than drifted past.
    """
    heartbeat_timeout_seconds = 30 * 60

    cli = mock.MagicMock()
    cli.api.pull.side_effect = _pull_sequence(
        *[_api_error(500, "server misbehaving")] * (len(PULL_RETRY_BACKOFF) + 1)
    )
    with pytest.raises(SubmissionError):
        pull_image(cli, mock.MagicMock(), {"job_id": "job-1"}, IMAGE)

    assert sum(call.args[0] for call in slept.call_args_list) == sum(
        PULL_RETRY_BACKOFF
    )
    assert sum(PULL_RETRY_BACKOFF) < heartbeat_timeout_seconds * 0.1


@pytest.mark.parametrize("status", [401, 403, 404])
def test_a_configuration_fault_is_not_retried(status, slept):
    """An unknown manifest or a package nobody logged in for cannot be fixed by
    repetition, and three attempts only delay telling someone."""
    cli = mock.MagicMock()
    cli.api.pull.side_effect = _pull_sequence(_api_error(status, "manifest unknown"))

    with pytest.raises(SubmissionError) as exc:
        pull_image(cli, mock.MagicMock(), {"job_id": "job-1"}, IMAGE)

    assert exc.value.code is FailureCode.IMAGE_PULL_FAILED
    assert cli.api.pull.call_count == 1
    slept.assert_not_called()


def test_a_request_timeout_is_retried_even_though_it_is_a_4xx():
    """408 is the server saying *try again*, which is the one 4xx that means
    the opposite of the rest."""
    cli = mock.MagicMock()
    cli.api.pull.side_effect = _pull_sequence(
        _api_error(408, "request timed out"), _pull_events(1)
    )

    pull_image(cli, mock.MagicMock(), {"job_id": "job-1"}, IMAGE)

    assert cli.api.pull.call_count == 2


def test_an_unknown_manifest_is_not_retried_when_it_arrives_as_an_event(slept):
    """The streamed path has no status code to read, so the decision falls to
    the registry's own words."""
    cli = mock.MagicMock()
    cli.api.pull.side_effect = _pull_sequence(
        _pull_events(1, error="manifest for foo:9.9.9 not found: manifest unknown")
    )

    with pytest.raises(SubmissionError):
        pull_image(cli, mock.MagicMock(), {"job_id": "job-1"}, IMAGE)

    assert cli.api.pull.call_count == 1
    slept.assert_not_called()


def test_a_rate_limit_is_not_retried(slept):
    """The judgment call worth revisiting if SIVACOR ever leaves Docker Hub.

    A rate limit is transient in principle, but Hub's anonymous window is six
    hours against a 50-second budget, so retrying cannot clear it -- it only
    delays the report by the whole budget.
    """
    cli = mock.MagicMock()
    cli.api.pull.side_effect = _pull_sequence(
        _pull_events(1, error="toomanyrequests: rate limit exceeded")
    )

    with pytest.raises(SubmissionError):
        pull_image(cli, mock.MagicMock(), {"job_id": "job-1"}, IMAGE)

    assert cli.api.pull.call_count == 1
    slept.assert_not_called()


def test_an_out_of_disk_pull_is_not_retried(slept):
    """Deterministic, and a second attempt burns the partial pull again."""
    cli = mock.MagicMock()
    cli.api.pull.side_effect = _pull_sequence(
        _pull_events(1, error="failed to register layer: no space left on device")
    )

    with pytest.raises(SubmissionError) as exc:
        pull_image(cli, mock.MagicMock(), {"job_id": "job-1"}, IMAGE)

    assert exc.value.code is FailureCode.OUT_OF_DISK
    assert cli.api.pull.call_count == 1
    slept.assert_not_called()


def test_the_daemons_traceback_still_survives_a_retried_failure():
    """#126's chaining must not be lost to the loop: the frames are the only
    record of which remote call failed, and #129 carries them through the chain
    redaction."""
    last = _api_error(500, "server misbehaving")
    cli = mock.MagicMock()
    cli.api.pull.side_effect = _pull_sequence(
        *[_api_error(500, "server misbehaving")] * len(PULL_RETRY_BACKOFF), last
    )

    with pytest.raises(SubmissionError) as exc:
        pull_image(cli, mock.MagicMock(), {"job_id": "job-1"}, IMAGE)

    assert exc.value.__cause__ is last


def test_the_progress_line_still_counts_layers_across_a_retry():
    """The layer set is shared with the attempt rather than rebuilt by it. A
    fresh set per attempt would leave the job log reporting "0 layers seen" for
    the whole pull -- invisible except to someone reading it during an incident,
    which is exactly when it is read."""
    cli = mock.MagicMock()
    cli.api.pull.side_effect = _pull_sequence(
        _api_error(500, "server misbehaving"), _pull_events(4)
    )
    api = mock.MagicMock()

    pull_image(cli, api, {"job_id": "job-1"}, IMAGE)

    written = " ".join(
        str(call.kwargs.get("log", "")) for call in api.update_job.call_args_list
    )
    assert "4 layers" in written


def test_a_failed_job_log_write_does_not_kill_a_recovering_pull():
    """`api.update_job` is a Girder PUT ending in raise_for_status(), so an
    unguarded call is a remote call on the critical path -- the shape of the
    2026-09-18 incident, where a 502 on a log flush killed a healthy run. It
    would be a poor joke for the retry loop to die reporting that it had
    recovered."""
    cli = mock.MagicMock()
    cli.api.pull.side_effect = _pull_sequence(
        _api_error(500, "server misbehaving"), _pull_events(2)
    )
    api = mock.MagicMock()
    api.update_job.side_effect = RuntimeError("502 Bad Gateway")

    pull_image(cli, api, {"job_id": "job-1"}, IMAGE)  # must not raise

    assert cli.api.pull.call_count == 2


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
# See development_notes/05_cinder_volumes_plan.md C0.1.

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


def test_the_dynare_estimate_matches_the_measured_footprint():
    """~21.6 GiB: 6.3 GiB compressed at 3.5x.

    This used to pin 14-18 GiB and cross-check pull_image's docstring claim that a
    cold dynare pull is "~15 GB" -- the only validation the multiplier had, and the
    guard that caught a per-family ratio being introduced on a misread measurement.

    It is now pinned against a *direct* measurement instead: on a VM built exactly
    like a worker, dynare's footprint is **21.00 GiB** for 6.16 GiB compressed
    (3.41x), corroborated by `docker system df -v` at 22.5 GB. So ~15 GB was itself
    an under-estimate, and 2.5x was too optimistic rather than too pessimistic.
    """
    cli = _cold_client()
    needed, how = image_on_disk_estimate(cli, DYNARE)
    # The real footprint is 21.00 GiB; the estimate must not fall below it.
    assert needed >= 21 * 1024**3, needed / 1024**3
    assert needed < 23 * 1024**3, "and not wildly above it either"
    assert "3.5x" in how

    # And the spread that explains why only dynare has ever caused this: a Stata
    # pull is several times smaller and essentially cannot fill a disk. Measured
    # footprints put it at 21.0 vs 2.68 GiB, i.e. ~7.8x; the estimates give ~7x.
    #
    # This assertion read `* 8` until 2026-08-22 and now reads `* 6`, because the
    # *Stata* entry was corrected upward from 0.5 to 0.9 GiB. The spread narrowed
    # because the small side was wrong, not because dynare moved.
    stata_needed, _ = image_on_disk_estimate(_cold_client(), IMAGE)
    assert stata_needed * 6 < needed, (stata_needed, needed)


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
# development_notes/05_cinder_volumes_plan.md open item 3.

VOLUME_GB = 20


#: **Cinder sizes are GiB, not decimal GB.** Verified 2026-08-22: the 20 GB volume
#: from open item 4's probe reported `20G` total under `df -h`, which counts in
#: powers of 1024 -- a decimal 20 GB device would have printed 19G. So a "20 GB"
#: volume is 20 * 2**30, and the floor on it is 2.0 GiB rather than the 1.9 an
#: earlier version of this fixture computed from decimal gigabytes.
def _split_filesystems(volume_free, root_free, volume_total=VOLUME_GB * 1024**3):
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
    assert floor == int(VOLUME_GB * 1024**3 * 0.1)
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
    # The floor it names is the one it used, not the constant: 10% of 20 GiB.
    expected = f"{int(VOLUME_GB * 1024**3 * 0.1) / 1024**3:.1f} GiB floor"
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
        volume_free=80 * 1024**3, root_free=4 * 1024**3, volume_total=100 * 1024**3
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

    **root_free was 20 GiB until 2026-08-22 and had to move to 30.** Not because
    this behaviour changed, but because dynare's footprint was then measured at
    21 GiB: 20 GiB of free space genuinely is not enough for that pull, so refusing
    it became the right answer and this fixture had been asserting the opposite by
    accident. A measurement that lands in a constant lands in test data too.
    """
    cli = _cold_client()
    own_fs, usage = _split_filesystems(volume_free=1 * 1024**3, root_free=30 * 1024**3)
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


# --- C5.3: why the flat 2.5x multiplier stays -------------------------------
#
# Measured 2026-08-22, same tag on both sides: dynare 6.16 GiB compressed -> 6.21
# unpacked (1.01x), rocker/r-ver 2.58x, dataeditors 2.26-2.82x. On those numbers
# alone 2.5x looks 2.5x too pessimistic for dynare, and a per-family ratio looks
# like the fix -- it was written, and then reverted, because the fleet keeps both
# copies: docker.io 29's containerd image store retains the compressed blobs *and*
# the snapshot, so the real footprint is roughly compressed + unpacked. 2.5x is
# therefore about right for dynare and mildly optimistic for the rest.
#
# The test below exists to stop the same well-reasoned wrong change being made
# twice.


def test_the_unpack_multiplier_is_flat_across_families():
    """Deliberately not per-family. See the note above and section 4b of the plan.

    A measurement of unpacked-vs-compressed is NOT a measurement of footprint on
    a containerd image store, and only the second one licenses changing this.
    """
    cli = _cold_client()
    dynare_needed, dynare_how = image_on_disk_estimate(cli, DYNARE)
    stata_needed, stata_how = image_on_disk_estimate(_cold_client(), IMAGE)
    assert "3.5x" in dynare_how and "3.5x" in stata_how
    assert dynare_needed == int(6.3 * 1024**3 * 3.5)
    assert stata_needed == int(0.9 * 1024**3 * 3.5)


def test_the_family_entry_is_the_largest_image_in_that_family():
    """The table's stated rule, pinned because it stopped being true unnoticed.

    `dataeditors` held 0.5 GiB while its largest image, stata19_5-mp-i-python, is
    0.89 GiB compressed -- so the family's biggest member was under-estimated by
    1.8x. Measured footprints for the family are 1.73 and 2.68 GiB.
    """
    assert _IMAGE_FAMILY_COMPRESSED_GB["dataeditors"] >= 0.89
    assert _IMAGE_FAMILY_COMPRESSED_GB["dynare"] >= 6.16
    assert _IMAGE_FAMILY_COMPRESSED_GB["rocker"] >= 1.4

    # And the estimate it produces must clear the measured footprint of the largest
    # image in each family, which is the whole point of the two constants together.
    cli = _cold_client()
    stata_needed, _ = image_on_disk_estimate(cli, IMAGE)
    assert stata_needed >= 2.68 * 1024**3, stata_needed / 1024**3
    dynare_needed, _ = image_on_disk_estimate(_cold_client(), DYNARE)
    assert dynare_needed >= 21.0 * 1024**3, dynare_needed / 1024**3
