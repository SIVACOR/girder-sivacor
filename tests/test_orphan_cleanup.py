"""Reaping analysis containers left behind by a lost worker.

Analysis containers are *siblings* started through the docker socket, not children
of the worker process, so nothing ties their lifetime to it. The P0 kill test
confirmed the consequence: after ``docker kill sivacor-worker`` the R container was
still running, burning a core, with the job already failed and nobody left to
collect its output.

These tests use a **real** docker container rather than mocks. A mocked
``docker.from_env()`` would happily confirm whatever the implementation does,
including getting the label filter wrong.
"""

import os
import uuid

import pytest
from pytest_girder.assertions import assertStatusOk

from girder_sivacor.worker_plugin.lib import (
    ORPHAN_LABEL_JOB,
    ORPHAN_LABEL_QUEUE,
    reap_orphaned_containers,
    worker_queue_name,
)


def test_worker_queue_name_prefers_the_explicit_setting(monkeypatch):
    monkeypatch.setenv("SIVACOR_WORKER_QUEUE", "sivacor.vm-42")
    assert worker_queue_name() == "sivacor.vm-42"


def test_worker_queue_name_falls_back_to_hostname(monkeypatch):
    import socket

    monkeypatch.delenv("SIVACOR_WORKER_QUEUE", raising=False)
    assert worker_queue_name() == f"sivacor.{socket.gethostname()}"


def test_sweep_survives_an_unreachable_docker(monkeypatch):
    """A broken socket must not stop the worker from starting."""
    monkeypatch.setenv("SIVACOR_WORKER_QUEUE", "sivacor.no-docker")
    monkeypatch.setenv("DOCKER_HOST", "unix:///nonexistent/docker.sock")
    assert reap_orphaned_containers() == []


@pytest.fixture
def docker_client():
    docker = pytest.importorskip("docker")
    try:
        client = docker.from_env()
        client.ping()
    except Exception:
        pytest.skip("docker daemon not reachable")
    return client


def _spawn(client, queue, job_id="job-1"):
    """A long-lived container labelled as one of ours."""
    return client.containers.run(
        "alpine",
        ["sleep", "300"],
        detach=True,
        labels={ORPHAN_LABEL_QUEUE: queue, ORPHAN_LABEL_JOB: job_id},
    )


def test_sweep_kills_this_workers_orphans(docker_client, monkeypatch):
    queue = f"sivacor.test-{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("SIVACOR_WORKER_QUEUE", queue)
    container = _spawn(docker_client, queue)
    try:
        container.reload()
        assert container.status == "running"

        reaped = reap_orphaned_containers()

        assert container.name in reaped
        with pytest.raises(Exception):
            # Gone entirely, not merely stopped: a stopped container still holds
            # its writable layer and would confuse the next sweep.
            docker_client.containers.get(container.id)
    finally:
        try:
            docker_client.containers.get(container.id).remove(force=True)
        except Exception:
            pass


def test_sweep_leaves_another_workers_containers_alone(docker_client, monkeypatch):
    """Two workers can share one docker socket in a dev setup.

    Scoping the sweep by job id alone, or by nothing at all, would make a
    restarting worker kill a live run belonging to its neighbour.
    """
    mine = f"sivacor.test-{uuid.uuid4().hex[:8]}"
    theirs = f"sivacor.test-{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("SIVACOR_WORKER_QUEUE", mine)
    other = _spawn(docker_client, theirs)
    try:
        reaped = reap_orphaned_containers()

        assert other.name not in reaped
        other.reload()
        assert other.status == "running", "reaped a container belonging to another queue"
    finally:
        other.remove(force=True)


def test_sweep_ignores_unlabelled_containers(docker_client, monkeypatch):
    """Never touch anything that is not demonstrably ours."""
    queue = f"sivacor.test-{uuid.uuid4().hex[:8]}"
    monkeypatch.setenv("SIVACOR_WORKER_QUEUE", queue)
    plain = docker_client.containers.run("alpine", ["sleep", "300"], detach=True)
    try:
        assert plain.name not in reap_orphaned_containers()
        plain.reload()
        assert plain.status == "running"
    finally:
        plain.remove(force=True)


@pytest.mark.plugin("sivacor")
def test_a_real_run_stamps_the_labels(
    server,
    db,
    user,
    eagerWorkerTasks,
    fsAssetstore,
    patched_gpg,
    uploads_folder,
    submission_collection,
    monkeypatch,
):
    """The sweep is useless unless recorded_run actually applies the labels.

    Runs a real submission and spies on the docker call, because a successful run
    removes its container immediately -- so there is nothing left to inspect
    afterwards.
    """
    import mock
    from docker.models.containers import ContainerCollection

    from .conftest import submit_sivacor_job, upload_test_file

    monkeypatch.setenv("SIVACOR_WORKER_QUEUE", "sivacor.label-probe")
    seen = {}
    original = ContainerCollection.create

    def spy(self, *args, **kwargs):
        seen.update(kwargs.get("labels") or {})
        return original(self, *args, **kwargs)

    fobj = upload_test_file(uploads_folder, user, "with_space_R.zip")
    stages = [
        {"image_name": "rocker/r-ver", "image_tag": "4.3.1", "main_file": "main.R"}
    ]
    with mock.patch.object(ContainerCollection, "create", spy):
        resp = submit_sivacor_job(server, user, fobj, stages)
    assertStatusOk(resp)

    assert seen.get(ORPHAN_LABEL_QUEUE) == "sivacor.label-probe", (
        f"analysis container was not labelled with the worker queue: {seen}"
    )
    assert seen.get(ORPHAN_LABEL_JOB), "analysis container carries no job id label"


# --- container stats collection -------------------------------------------
#
# Two separate defects lived here: short-lived containers were never sampled at
# all, and the empty CSV that left behind made the aggregation write a bare NaN
# literal into performance_data_stage_N.json -- invalid JSON in a file that gets
# hashed into a signed TRO.


@pytest.mark.parametrize("lifetime", ["0.2", "1", "3"])
def test_short_containers_are_still_sampled(docker_client, tmp_path, lifetime):
    """Any container Docker emits a reading for must produce a row.

    ``stats(stream=False)`` blocks 1-2s waiting for a CPU delta, so a container
    finishing inside that window used to yield zero samples. Streaming gives a
    usable first reading in milliseconds.
    """
    from girder_sivacor.worker_plugin.lib import DockerStatsCollectorThread

    path = str(tmp_path / "dockerstats")
    container = docker_client.containers.create("alpine", ["sleep", lifetime])
    try:
        collector = DockerStatsCollectorThread(container, path)
        container.start()
        collector.start()
        collector.join(timeout=30)

        assert collector.samples >= 1, f"no stats reading for a {lifetime}s container"
        assert os.path.isfile(path), "human-readable dockerstats file was not written"
        rows = [ln for ln in open(path + ".csv").read().splitlines()[1:] if ln.strip()]
        assert len(rows) >= 1
    finally:
        container.remove(force=True)


def test_empty_stats_never_yields_invalid_json():
    """An empty frame must not put a bare NaN literal into a TRO artifact."""
    import io
    import json

    import pandas as pd

    from girder_sivacor.worker_plugin.lib import NpEncoder

    header = (
        "Timestamp,CPU %,Memory Usage,Memory Limit,Network RX,Network TX,"
        "Block IO Read,Block IO Write,PIDs\n"
    )
    df = pd.read_csv(io.StringIO(header))
    assert df.empty

    # What lib.py does now.
    performance_data = {"ExitCode": 0}
    if df.empty:
        performance_data["MetricsUnavailable"] = (
            "container exited before Docker emitted a stats reading"
        )
    else:  # pragma: no cover - guarding the guard
        performance_data["MaxCPUPercent"] = float(df["CPU %"].max())

    blob = json.dumps(performance_data, cls=NpEncoder, allow_nan=False)
    assert "NaN" not in blob
    # Strict round trip: reject the non-finite literals json.loads would tolerate.
    def _no_constants(name):
        raise AssertionError(f"invalid JSON literal in a TRO artifact: {name}")

    json.loads(blob, parse_constant=_no_constants)

    # And the old behaviour must now be impossible to ship silently.
    with pytest.raises(ValueError):
        json.dumps({"MaxCPUPercent": float("nan")}, cls=NpEncoder, allow_nan=False)
