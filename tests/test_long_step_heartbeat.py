"""A long, quiet pipeline step must not look like a dead worker.

The server's liveness signal is ``max(meta.heartbeat, job.updated,
job.created)`` against ``sivacor.heartbeat_timeout`` (30 min on production).
Only ``recorded_run`` ticks ``meta.heartbeat``, so every step outside a
container run is silent for its whole duration -- and on 2026-09-24
``upload_workspace`` was silent for **1 h 20 m** while it packaged a 28.7 GiB
workspace. The reaper failed the submission 39 % of the way through its own
upload, after all seven stages had already succeeded. See
``development_notes/incidents/2026-09-25-reaped-during-upload.md``.

The guard is :class:`ProgressBeat`, and the property that matters is not just
"it beats" but **where the beats come from**: they are driven by work done -- a
file archived, a chunk moved -- never by a clock of their own. A background
thread would cover the same steps in one line and would also keep a *wedged*
worker looking alive until ``sivacor.max_runtime`` (168 h). The last test here
is what pins that choice down.
"""

import io
import itertools
import pathlib
import shutil
import uuid
import zipfile

import mock
from girder_jobs.constants import JobStatus
from girder_sivacor.worker_plugin.run_submission import (
    create_workspace,
    upload_workspace,
)
from girder_sivacor.worker_plugin.lib import (
    HEARTBEAT_INTERVAL,
    PROGRESS_LOG_INTERVAL,
    ProgressBeat,
)


# --- ProgressBeat itself ------------------------------------------------------


def test_a_tick_before_the_interval_does_not_beat():
    """Call sites tick per file or per chunk, which can be thousands a second.

    Turning each of those into a REST call would cost more than the silence it
    is fixing.
    """
    api = mock.MagicMock()
    with mock.patch(
        "girder_sivacor.worker_plugin.lib.time.monotonic",
        side_effect=[0.0] + [1.0] * 50,
    ):
        beat = ProgressBeat(api, "job-1")
        for _ in range(50):
            beat.tick()

    assert api.heartbeat.call_count == 0


def test_a_tick_after_the_interval_beats():
    """The whole point: meta.heartbeat advances while a long step runs."""
    api = mock.MagicMock()
    with mock.patch(
        "girder_sivacor.worker_plugin.lib.time.monotonic",
        side_effect=[0.0, HEARTBEAT_INTERVAL + 1],
    ):
        ProgressBeat(api, "job-1").tick()

    api.heartbeat.assert_called_once_with("job-1")


def test_beats_are_rate_limited_to_one_per_interval():
    """A long step ticks continuously; the server must see one beat a minute."""
    api = mock.MagicMock()
    # Four ticks, each one interval apart: four beats, not four per tick.
    clock = [0.0] + [HEARTBEAT_INTERVAL * (i + 1) + 1 for i in range(4)]
    with mock.patch(
        "girder_sivacor.worker_plugin.lib.time.monotonic", side_effect=clock
    ):
        beat = ProgressBeat(api, "job-1")
        for _ in range(4):
            beat.tick()

    assert api.heartbeat.call_count == 4


def test_a_failed_heartbeat_never_fails_the_step():
    """Best effort, for the same reason the pull's heartbeat is.

    A submission that is running fine must not die because one ping lost a race
    with a proxy restart; missing several in a row is what the reaper acts on.
    """
    api = mock.MagicMock()
    api.heartbeat.side_effect = RuntimeError("503 Service Unavailable")
    with mock.patch(
        "girder_sivacor.worker_plugin.lib.time.monotonic",
        side_effect=[0.0, HEARTBEAT_INTERVAL + 1],
    ):
        ProgressBeat(api, "job-1").tick()  # must not raise


def test_a_failed_progress_log_never_fails_the_step():
    api = mock.MagicMock()
    api.update_job.side_effect = RuntimeError("502 Bad Gateway")
    with mock.patch(
        "girder_sivacor.worker_plugin.lib.time.monotonic",
        side_effect=[0.0, PROGRESS_LOG_INTERVAL + 1],
    ):
        ProgressBeat(api, "job-1", log_message=lambda: "still going").tick()


def test_the_job_log_is_written_far_less_often_than_the_heartbeat():
    """Each log line is an updateJob, which fires jobs.job.update.after.

    The heartbeat writes straight to the collection, so it is cheap; a log line
    is not, and a step running for hours must not fill the job log.
    """
    api = mock.MagicMock()
    # Ticks one heartbeat-interval apart, spanning two log intervals.
    span = PROGRESS_LOG_INTERVAL * 2 + 1
    clock = [0.0] + [
        HEARTBEAT_INTERVAL * (i + 1) for i in range(span // HEARTBEAT_INTERVAL + 1)
    ]
    with mock.patch(
        "girder_sivacor.worker_plugin.lib.time.monotonic", side_effect=clock
    ):
        beat = ProgressBeat(api, "job-1", log_message=lambda: "still going")
        for _ in range(len(clock) - 1):
            beat.tick()

    assert api.update_job.call_count < api.heartbeat.call_count


def test_the_log_message_is_not_built_unless_a_line_is_due():
    """It is a callable so it may be expensive -- a count, a formatted size."""
    api = mock.MagicMock()
    built = mock.MagicMock(return_value="progress")
    with mock.patch(
        "girder_sivacor.worker_plugin.lib.time.monotonic",
        side_effect=[0.0] + [1.0] * 20,
    ):
        beat = ProgressBeat(api, "job-1", log_message=built)
        for _ in range(20):
            beat.tick()

    built.assert_not_called()


def test_the_upload_adapter_ticks():
    """girder_client hands progressCallback a dict; it is the tick that matters."""
    api = mock.MagicMock()
    with mock.patch(
        "girder_sivacor.worker_plugin.lib.time.monotonic",
        side_effect=[0.0, HEARTBEAT_INTERVAL + 1],
    ):
        ProgressBeat(api, "job-1").on_upload_progress(
            {"current": 1024, "total": 2048}
        )

    api.heartbeat.assert_called_once_with("job-1")


# --- the property that rules out a background thread --------------------------


def test_a_step_that_makes_no_progress_stops_beating():
    """**The safety property.** Ticks come from work, not from a clock.

    A background heartbeat thread would cover every silent step with one line
    and would also mean a worker wedged mid-step beats forever -- so instead of
    failing at 30 minutes it would burn to sivacor.max_runtime, 168 h on
    production. Both 2026-08-11 and 2026-09-11 were mid-step worker losses.

    Here: hours pass, but the step does no work, so nothing ticks and the
    reaper's clock keeps running.
    """
    api = mock.MagicMock()
    with mock.patch(
        "girder_sivacor.worker_plugin.lib.time.monotonic",
        side_effect=[0.0, HEARTBEAT_INTERVAL * 1000],
    ):
        ProgressBeat(api, "job-1")  # constructed, then the step wedges

    assert api.heartbeat.call_count == 0


# --- the two steps that carry it ----------------------------------------------
#
# These drive the real tasks rather than ProgressBeat, because the bug was never
# in the ticker -- it was that nothing called one.


def _api_for(folder_meta=None):
    api = mock.MagicMock()
    api.job.return_value = {"status": JobStatus.RUNNING}
    api.folder.return_value = {"meta": folder_meta or {}}
    # Keyed rather than a blanket return value, and that matters: with a truthy
    # MagicMock for every id, upload_workspace's TRO/stdout/stderr branches all
    # fire and supply beats of their own -- which made the walk's own tick look
    # covered when it had been removed. Absent ids resolve to None instead, so
    # a count below is attributable to exactly one call site.
    api.file.side_effect = lambda fid: (
        {"_id": fid, "name": "package.zip"} if fid else None
    )
    return api


def _fast_clock():
    """A monotonic clock that jumps a full interval on every read.

    So every tick beats, and the assertion is about *whether the call site
    ticks at all* rather than about the rate limit, which the tests above
    already cover.
    """
    return mock.patch(
        "girder_sivacor.worker_plugin.lib.time.monotonic",
        side_effect=itertools.count(0, HEARTBEAT_INTERVAL + 1),
    )


def test_upload_workspace_heartbeats_while_it_packages(tmp_path):
    """**The 2026-09-24 regression test.**

    Seven stages had succeeded, the TRO was signed, and this step then went
    quiet for 1 h 20 m zipping a 397 GiB workspace into a 28.7 GiB archive. The
    reaper failed the submission at 30 min. Nothing about the run was wrong.
    """
    workspace = tmp_path / "ws"
    project = workspace / "project"
    project.mkdir(parents=True)
    for i in range(12):
        (project / f"result_{i}.dta").write_text("x" * 128)

    api = _api_for()
    submission = {
        "job_id": "job-1",
        "folder_id": "folder-1",
        "file_id": "file-1",
        "workspace_dir": str(workspace),
    }

    with mock.patch(
        "girder_sivacor.worker_plugin.run_submission.GirderApi.for_task",
        return_value=api,
    ), _fast_clock():
        upload_workspace.run(submission)

    # Exactly one per file archived: the clock grants every tick, and with no
    # TRO/stdout/stderr ids on the folder the walk is the only call site that
    # can beat. Before the fix this was zero however long the step ran.
    assert api.heartbeat.call_count == 12
    api.heartbeat.assert_called_with("job-1")


def test_upload_workspace_reports_progress_through_the_upload(tmp_path):
    """The zip walk is only the first half; the upload is the longer one.

    ``uploadFileToFolder`` blocks for the whole transfer, so its
    ``progressCallback`` is the only seam it has. Without it the guard would
    cover the minutes and miss the hour.
    """
    workspace = tmp_path / "ws"
    (workspace / "project").mkdir(parents=True)
    (workspace / "project" / "a.txt").write_text("hello")

    api = _api_for()
    submission = {
        "job_id": "job-1",
        "folder_id": "folder-1",
        "file_id": "file-1",
        "workspace_dir": str(workspace),
    }

    with mock.patch(
        "girder_sivacor.worker_plugin.run_submission.GirderApi.for_task",
        return_value=api,
    ):
        upload_workspace.run(submission)

    _, kwargs = api.upload_file.call_args
    assert callable(kwargs["progress"]), "the upload must report progress"


def test_create_workspace_heartbeats_while_it_downloads_and_unpacks(tmp_path):
    """The other silent step, and the one that scales hardest with the package.

    10 m 52 s for a 28 GiB package on 2026-09-24 -- inside the 30-minute
    threshold, but only by 2.8x, and it grows with the archive. Both halves are
    covered: the download, which had to become a chunk loop because
    ``downloadFile`` takes no callback, and the extract, which had to become a
    per-member loop for the same reason.
    """
    payload = io.BytesIO()
    with zipfile.ZipFile(payload, "w") as zf:
        for i in range(9):
            zf.writestr(f"pkg/data_{i}.csv", "a,b,c\n1,2,3\n")
    raw = payload.getvalue()

    folder_id = f"heartbeat-test-{uuid.uuid4().hex}"
    api = _api_for()
    # Four chunks, so the download has something to tick on independently of
    # the nine members the extract walks.
    step = len(raw) // 4 + 1
    api.file_chunks.return_value = iter(
        [raw[i : i + step] for i in range(0, len(raw), step)]
    )
    submission = {"job_id": "job-1", "folder_id": folder_id, "file_id": "file-1"}

    try:
        with mock.patch(
            "girder_sivacor.worker_plugin.run_submission.GirderApi.for_task",
            return_value=api,
        ), _fast_clock():
            create_workspace.run(submission)

        project = pathlib.Path(submission["workspace_dir"]) / "project"
        assert sorted(p.name for p in (project / "pkg").iterdir()) == [
            f"data_{i}.csv" for i in range(9)
        ], "per-member extract must land the same tree extractall did"
        # 4 download chunks + 9 extracted members. Before the fix, both halves
        # were single opaque calls and this was zero.
        assert api.heartbeat.call_count == 13
    finally:
        shutil.rmtree(submission.get("workspace_dir", ""), ignore_errors=True)
        shutil.rmtree(submission.get("tmp_dir", ""), ignore_errors=True)
