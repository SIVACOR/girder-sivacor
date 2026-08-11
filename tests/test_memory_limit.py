"""Capping the analysis container's memory, and reporting it when the cap bites.

An analysis container used to be unlimited, which made the researcher's payload a
competitor of the celery worker supervising it -- and on 2026-08-11 the payload
won. A Stata run peaked at 29.1 GB of the worker's 29.4 GB, held it for three
hours with no swap, and celery's MainProcess stopped answering. The analysis
itself finished with ``exit 0``; nothing was left alive to advance the chain, so
the submission was reaped as "no heartbeat" and the completed work was thrown
away. Three submissions were lost that way over two days.

The cap moves that failure inside the cgroup, where it is survivable and can be
explained. Two things have to hold for that to be an improvement rather than a
new way to fail, and both are tested here:

* the cap must never be so small that it fails submissions a host could have run
  (:func:`container_memory_limit` returning ``None`` beats returning nonsense);
* an OOM kill must be reported *as* an OOM kill. Docker exits 137 for any
  SIGKILL, and the container's own logs say nothing, because the process is
  killed without warning -- so this is the one failure whose cause is invisible
  from inside the container and has to be read from the daemon.
"""

import os
import tarfile
import tempfile

import mock
import pytest
from girder_jobs.constants import JobStatus
from girder_sivacor.errors import FailureCode
from girder_sivacor.worker_plugin.lib import (
    MEMORY_HEADROOM_BYTES,
    MIN_CONTAINER_MEMORY_BYTES,
    container_memory_limit,
)
from pytest_girder.assertions import assertStatusOk

from .conftest import (
    get_submission_folder,
    submit_sivacor_job,
    upload_test_file,
)

GIB = 1024**3


# --- how much to allow -------------------------------------------------------


@pytest.mark.parametrize(
    "flavor,total",
    [
        ("m3.large", 61440 * 1024**2),
        ("m3.medium", 31529943040),  # the MemTotal docker reported on 2026-08-11
    ],
)
def test_cap_is_total_minus_headroom(flavor, total):
    """The whole machine, less what has to survive to report a failure."""
    assert container_memory_limit(total) == total - MEMORY_HEADROOM_BYTES


def test_the_headroom_is_actually_reserved():
    """Regression guard for the bug this exists to prevent.

    The 2026-08-11 wedge needed only ~300 MB more than the box had. Whatever the
    headroom is set to, what matters is that the difference is left unallocated.
    """
    total = 32 * GIB
    assert total - container_memory_limit(total) == MEMORY_HEADROOM_BYTES


def test_a_host_too_small_to_spare_the_headroom_is_left_uncapped():
    """``None``, not a tiny cap.

    A cap below what any analysis image needs would convert "this host is too
    small" into an out-of-memory report against the researcher's code -- blaming
    the submission for a property of the worker. An uncapped run on a small dev
    box is merely the old behaviour.
    """
    assert container_memory_limit(3 * GIB) is None


def test_the_floor_is_inclusive():
    """A host with exactly enough is capped, not rejected.

    Guards the boundary in both directions: one byte less must not be capped.
    """
    exactly_enough = MIN_CONTAINER_MEMORY_BYTES + MEMORY_HEADROOM_BYTES
    assert container_memory_limit(exactly_enough) == MIN_CONTAINER_MEMORY_BYTES
    assert container_memory_limit(exactly_enough - 1) is None


@pytest.mark.parametrize("reported", [None, 0, -1, "62914560", 3.5, True])
def test_unusable_memory_readings_leave_the_container_uncapped(reported):
    """``docker info`` is not guaranteed to answer, and must not be guessed at.

    ``True`` is in here on purpose: ``isinstance(True, int)`` is ``True`` in
    Python, so a bool would otherwise sail through as a 1-byte host.
    """
    assert container_memory_limit(reported) is None


# --- what happens when it bites ---------------------------------------------


@pytest.mark.plugin("sivacor")
def test_an_oom_killed_analysis_is_reported_as_one(
    server,
    db,
    user,
    eagerWorkerTasks,
    fsAssetstore,
    patched_gpg,
    uploads_folder,
    submission_collection,
):
    """A killed container must not surface as "check stdout/stderr".

    It exits 137 like any SIGKILL and writes nothing on the way out, so without
    reading ``State.OOMKilled`` off the daemon the researcher is told to consult
    a log that cannot explain it. The cap is forced small here rather than
    allocating ~60 GB to reach the real one.
    """
    main_file = "main.R"
    stages = [
        {"image_name": "rocker/r-ver", "image_tag": "4.3.1", "main_file": main_file}
    ]
    with (
        tempfile.NamedTemporaryFile(suffix=".tar.gz") as temp_archive,
        tempfile.TemporaryDirectory() as temp_dir,
    ):
        with open(os.path.join(temp_dir, main_file), "w") as f:
            # Comfortably past the 256 MiB cap below, in one allocation, so the
            # kernel kills it rather than the process degrading slowly.
            f.write("x <- numeric(150e6)\ncat(length(x))\n")
        with tarfile.open(temp_archive.name, "w:gz") as tar:
            tar.add(temp_dir, arcname=".")
        fobj = upload_test_file(uploads_folder, user, temp_archive.name)

    with mock.patch(
        "girder_sivacor.worker_plugin.lib.container_memory_limit",
        return_value=256 * 1024**2,
    ):
        resp = submit_sivacor_job(server, user, fobj, stages)
    assertStatusOk(resp)

    # Re-fetch: the submit response is the job as created, so its status is
    # always RUNNING no matter how the chain ends.
    resp = server.request(path=f"/job/{resp.json['_id']}", method="GET", user=user)
    assertStatusOk(resp)
    job = resp.json

    assert job["status"] == JobStatus.ERROR

    log = "".join(job["log"])
    # The message has to name memory and the actual limit. "Error executing
    # recorded run. Check stdout/stderr for details." -- the generic non-zero
    # branch -- would be a false lead here.
    assert "memory" in log.lower()
    assert "GiB" in log
    assert "Check stdout/stderr" not in log

    resp = get_submission_folder(server, user, job["_id"], submission_collection)
    assertStatusOk(resp)
    assert resp.json[0]["meta"]["status"] == "failed"


@pytest.mark.plugin("sivacor")
def test_a_run_within_its_cap_is_untouched(
    server,
    db,
    user,
    eagerWorkerTasks,
    fsAssetstore,
    patched_gpg,
    uploads_folder,
    submission_collection,
):
    """The cap must be invisible to a submission that fits inside it.

    Worth its own test because the cheapest way to make the test above pass is a
    limit low enough to kill everything.
    """
    main_file = "main.R"
    stages = [
        {"image_name": "rocker/r-ver", "image_tag": "4.3.1", "main_file": main_file}
    ]
    with (
        tempfile.NamedTemporaryFile(suffix=".tar.gz") as temp_archive,
        tempfile.TemporaryDirectory() as temp_dir,
    ):
        with open(os.path.join(temp_dir, main_file), "w") as f:
            f.write("x <- numeric(1e6)\ncat(length(x))\n")
        with tarfile.open(temp_archive.name, "w:gz") as tar:
            tar.add(temp_dir, arcname=".")
        fobj = upload_test_file(uploads_folder, user, temp_archive.name)

    with mock.patch(
        "girder_sivacor.worker_plugin.lib.container_memory_limit",
        return_value=512 * 1024**2,
    ):
        resp = submit_sivacor_job(server, user, fobj, stages)
    assertStatusOk(resp)

    resp = server.request(path=f"/job/{resp.json['_id']}", method="GET", user=user)
    assertStatusOk(resp)
    assert resp.json["status"] == JobStatus.SUCCESS


# --- what is kept about it ---------------------------------------------------


def test_the_limit_survives_the_telemetry_allow_list():
    """``mem_limit_bytes`` is what makes ``max_memory_bytes`` interpretable.

    Without it a flavor change silently re-baselines every comparison against
    older records: "peaked at 29 GB" means something different on a 30 GB worker
    than on a 61 GB one.
    """
    from girder_sivacor.telemetry import sanitize_record

    record = sanitize_record(
        {
            "status": "failed",
            "stages": [
                {
                    "image_name": "rocker/r-ver",
                    "image_tag": "4.3.1",
                    "max_memory_bytes": 58 * GIB,
                    "mem_limit_bytes": 59 * GIB,
                }
            ],
            "error": {
                "step": "execute_workflow",
                "code": FailureCode.OUT_OF_MEMORY.value,
                "detail": 59 * GIB,
            },
        },
        "2026-08-11",
    )

    assert record["stages"][0]["mem_limit_bytes"] == 59 * GIB
    assert record["error"]["code"] == "out_of_memory"
    assert record["error"]["detail"] == 59 * GIB


@pytest.mark.parametrize(
    "detail", ["/home/jane/confidential.dta", -1, 3.7, None, "59 GiB"]
)
def test_only_a_byte_count_is_kept_as_the_failure_detail(detail):
    """The detail is a machine fact or it is nothing.

    Same reasoning as every other validator in telemetry.py: a field that
    accepts free text is a field that will eventually carry a path.
    """
    from girder_sivacor.telemetry import sanitize_record

    record = sanitize_record(
        {
            "status": "failed",
            "stages": [],
            "error": {
                "step": "execute_workflow",
                "code": FailureCode.OUT_OF_MEMORY.value,
                "detail": detail,
            },
        },
        "2026-08-11",
    )
    assert record["error"]["detail"] is None
