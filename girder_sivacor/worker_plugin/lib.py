import base64
import functools
import json
import logging
import math
import os
import queue
import re
import shutil
import socket
import stat
import tempfile
import time
import zipfile
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pathlib import Path
from threading import Event, Thread

import cpuinfo
import docker
import numpy as np
import pandas as pd
import redis
import requests

from ..errors import FailureCode, SubmissionError

MASTER_KEY_HEX = os.environ.get(
    "MASTER_KEY_HEX", "000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
)
_master_key = bytes.fromhex(MASTER_KEY_HEX)
MASTER_AES = AESGCM(_master_key)
MASK = "***SECRET_REDACTED***"

#: Seconds between liveness pings while a user's container runs. Cheap enough
#: to be frequent; the server's staleness threshold is a large multiple of it.
HEARTBEAT_INTERVAL = 60


class NpEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super(NpEncoder, self).default(obj)


def decrypt_job_secrets(encrypted_secrets_b64, wrapped_job_key_b64):
    """
    Unwraps the job key and decrypts the secrets.
    """
    # Decode and extract nonces (first 12 bytes)
    key_payload = base64.b64decode(wrapped_job_key_b64)
    job_key = MASTER_AES.decrypt(key_payload[:12], key_payload[12:], None)

    secrets_payload = base64.b64decode(encrypted_secrets_b64)
    job_aes = AESGCM(job_key)
    decrypted_json = job_aes.decrypt(secrets_payload[:12], secrets_payload[12:], None)

    return json.loads(decrypted_json.decode("utf-8"))


def get_project_dir(submission):
    return os.path.join(submission["workspace_dir"], "project")


#: Labels stamped on every analysis container so a restarted worker can identify
#: its own leftovers. Scoped by queue rather than just by job: in a dev setup two
#: workers can share one docker socket, and a sweep must never touch a container
#: belonging to the other one.
ORPHAN_LABEL_QUEUE = "org.sivacor.worker_queue"
ORPHAN_LABEL_JOB = "org.sivacor.job_id"


def worker_queue_name() -> str:
    """This worker's private queue name, as the deployment sets it.

    Mirrors ``routing.worker_queue`` but without a celery task in hand -- the
    startup sweep runs before any task exists. Falls back to the hostname, which
    is what routing derives from the celery node name.
    """
    return os.environ.get("SIVACOR_WORKER_QUEUE") or f"sivacor.{socket.gethostname()}"


def reap_orphaned_containers() -> list[str]:
    """Kill analysis containers left behind by a previous incarnation.

    Called once at worker startup. Anything still carrying *this* worker's queue
    label at that moment is by definition an orphan: a fresh worker process has
    no task in flight, so it cannot legitimately own a running container. That
    reasoning is what makes this safe without asking Girder anything -- which
    matters, because the worker has no database and no standing credential.

    Best-effort by design: a docker daemon that is unreachable, or a container
    that vanishes mid-sweep, must not stop the worker from starting.
    """
    queue = worker_queue_name()
    reaped: list[str] = []
    try:
        cli = docker.from_env()
        stale = cli.containers.list(
            all=True, filters={"label": f"{ORPHAN_LABEL_QUEUE}={queue}"}
        )
    except Exception:
        logging.warning("Orphan sweep skipped: docker unavailable", exc_info=True)
        return reaped

    for container in stale:
        job_id = (container.labels or {}).get(ORPHAN_LABEL_JOB, "unknown")
        try:
            if container.status == "running":
                logging.warning(
                    "Killing orphaned analysis container %s (job %s) left by a "
                    "previous worker incarnation",
                    container.name,
                    job_id,
                )
                container.kill()
            container.remove(force=True)
            reaped.append(container.name)
        except docker.errors.NotFound:
            pass
        except Exception:
            logging.warning(
                "Could not reap orphaned container %s", container.name, exc_info=True
            )
    if reaped:
        logging.warning("Orphan sweep reaped %d container(s): %s", len(reaped), reaped)
    return reaped


@functools.lru_cache
def _redis_client_sync() -> redis.Redis:
    url = os.environ.get("GIRDER_NOTIFICATION_REDIS_URL", "redis://localhost:6379")
    return redis.Redis.from_url(url)


class LogPublisher(Thread):
    def __init__(self, container_name, channel, known_secrets):
        super().__init__()
        self.container_name = container_name
        self.channel = channel
        self.client = docker.from_env()
        self._stop_event = Event()
        self.known_secrets = known_secrets
        self.daemon = True  # Allows Python to exit even if this thread is running

    def run(self):
        try:
            container = self.client.containers.get(self.container_name)
            log_stream = container.logs(
                stream=True, follow=True, timestamps=True, tail=0
            )
            print(
                f"Starting log publisher for {self.container_name} on Redis channel {self.channel}"
            )

            for log_line_bytes in log_stream:
                if self._stop_event.is_set():
                    break

                log_line = log_line_bytes.decode("utf-8").strip()
                for secret in self.known_secrets:
                    log_line = log_line.replace(secret, MASK)
                # Use the synchronous client for publishing
                _redis_client_sync().publish(self.channel, log_line)
        except Exception as e:
            print(f"Error in Log Publisher: {e}")
            time.sleep(5)

    def stop(self):
        self._stop_event.set()


class DockerStatsCollectorThread(Thread):
    def __init__(self, container, output_path):
        super(DockerStatsCollectorThread, self).__init__()
        self.daemon = True
        self.container = container
        self.output_path = output_path
        #: Readings actually recorded. ``recorded_run`` uses this to say *why* a
        #: metric is missing instead of silently emitting NaN.
        self.samples = 0

    def container_finished(self, ts):
        try:
            if ts == "0001-01-01T00:00:00Z":
                self.container.reload()
            return self.container.attrs["State"]["Status"] not in ("created", "running")
        except docker.errors.NotFound:
            return True

    def run(self):
        with open(self.output_path + ".csv", mode="w") as fp:
            header = (
                "Timestamp,CPU %,Memory Usage,Memory Limit,Network RX,Network TX,"
                "Block IO Read,Block IO Write,PIDs,CPU Seconds\n"
            )
            fp.write(header)
        # stream=True yields its first reading within milliseconds; stream=False
        # blocks 1-2s because it waits for the two CPU readings a delta needs. That
        # delay meant a container finishing inside the window was never sampled at
        # all -- measured: a 1s container produced zero rows, 3s produced one. So
        # any submission whose stage was quick got no metrics and, worse, an empty
        # CSV that made the aggregation emit NaN into a signed TRO artifact.
        #
        # The tradeoff is that the first reading has no precpu baseline, so its
        # CPU% is 0.0 -- exactly what `docker stats` prints on its first line.
        # Memory, network and block IO are all real from the very first sample.
        try:
            stream = self.container.stats(stream=True, decode=True)
        except docker.errors.NotFound:
            return
        for d in stream:
            # Metrics are best-effort: a malformed reading must not raise in a
            # daemon thread, where it would surface only as an unhandled-exception
            # warning and silently stop collection for the rest of the run.
            if not isinstance(d, dict):
                logging.warning("Ignoring unexpected docker stats payload: %r", type(d))
                continue
            ts = d.get("read")

            # Readings after the container exits keep arriving on a ~1s cadence
            # with the zero timestamp and empty payloads, so the stream ending is
            # not a reliable stop signal -- check the container itself.
            if not ts or ts == "0001-01-01T00:00:00Z":
                if self.container_finished(ts):
                    break
                continue

            self.samples += 1
            mem_usage, mem_limit = self.calculate_memory(d)
            bytes_in, bytes_out = self.calculate_network_bytes(d)
            blkio_rd, blkio_wr = self.calculate_blkio_bytes(d)
            cpu_percent = self.calculate_cpu_percent(d)
            line = (
                f"{ts} - {cpu_percent:.2f}%, {mem_usage} / {mem_limit},"
                f" {bytes_in} / {bytes_out}, {blkio_rd} / {blkio_wr},"
                f" {d.get('pids_stats', {}).get('current', 0)}\n"
            )
            with open(self.output_path, mode="a") as fp:
                fp.write(line)
            with open(self.output_path + ".csv", mode="a") as fp:
                mem_usage, mem_limit = self.calculate_memory(d, convert=False)
                bytes_in, bytes_out = self.calculate_network_bytes(d, convert=False)
                blkio_rd, blkio_wr = self.calculate_blkio_bytes(d, convert=False)
                csv_line = (
                    f'"{ts}",{cpu_percent:.2f},{mem_usage},{mem_limit},'
                    f"{bytes_in},{bytes_out},{blkio_rd},{blkio_wr},"
                    f"{d.get('pids_stats', {}).get('current', 0)},"
                    f"{self.calculate_cpu_seconds(d):.3f}\n"
                )
                fp.write(csv_line)

    @staticmethod
    def convert_size(size_bytes, binary=True):
        if size_bytes == 0:
            return "0B"
        if binary:
            suffix = "i"
            base = 1024
        else:
            suffix = ""
            base = 1000
        size_name = (
            "B",
            f"K{suffix}B",
            f"M{suffix}B",
            f"G{suffix}B",
            f"T{suffix}B",
            f"P{suffix}B",
        )
        i = int(math.floor(math.log(size_bytes, base)))
        p = math.pow(base, i)
        s = round(size_bytes / p, 2)
        return "%s %s" % (s, size_name[i])

    @staticmethod
    def calculate_cpu_seconds(d):
        """Cumulative CPU time the container has consumed, in seconds.

        A **counter**, not a rate, and that is the point. ``CPU %`` is sampled over
        ~1 s windows, so the aggregation can only keep its maximum -- which answers
        "did this ever touch N cores" and not "did this use the machine". On
        2026-08-12 a stage recorded `max_cpu_percent 906` (≈9 cores for one second of
        990) while `top` showed one core busy; both were true, and nothing recorded
        could tell the two readings apart. Mean cores is ``cpu_seconds_total /
        duration_seconds``, and it is the number a researcher optimising their code
        actually needs.

        Being cumulative also makes it robust in a way an average of percentages is
        not: no dependence on sample spacing, no first-sample special case, and the
        aggregation is a ``max()`` over the column because a counter only rises.
        """
        return (
            float(
                d.get("cpu_stats", {}).get("cpu_usage", {}).get("total_usage", 0) or 0
            )
            / 1e9
        )

    @staticmethod
    def calculate_cpu_percent(d):
        cpu_count = d.get("cpu_stats", {}).get("online_cpus", 1)
        cpu_percent = 0.0
        cpu_delta = float(d["cpu_stats"]["cpu_usage"]["total_usage"]) - float(
            d["precpu_stats"]["cpu_usage"]["total_usage"]
        )
        try:
            system_delta = float(d["cpu_stats"]["system_cpu_usage"]) - float(
                d["precpu_stats"]["system_cpu_usage"]
            )
        except KeyError:
            system_delta = 0.0
        if system_delta > 0.0:
            cpu_percent = cpu_delta / system_delta * 100.0 * cpu_count
        return cpu_percent

    def calculate_blkio_bytes(self, d, convert=True):
        bytes_stats = d.get("blkio_stats", {}).get("io_service_bytes_recursive")
        if not bytes_stats:
            return 0, 0
        rd = wr = 0
        for s in bytes_stats:
            if s["op"] == "Read":
                rd += s["value"]
            elif s["op"] == "Write":
                wr += s["value"]
        if not convert:
            return rd, wr
        return self.convert_size(rd, binary=False), self.convert_size(wr, binary=False)

    def calculate_network_bytes(self, d, convert=True):
        networks = d.get("networks")
        if not networks:
            return 0, 0
        rx = tx = 0
        for data in networks.values():
            rx += data["rx_bytes"]
            tx += data["tx_bytes"]
        if not convert:
            return rx, tx
        return self.convert_size(rx, binary=False), self.convert_size(tx, binary=False)

    def calculate_memory(self, d, convert=True):
        memory = d.get("memory_stats")
        if not memory:
            return 0, 0
        if not convert:
            return memory.get("usage", 0), memory.get("limit", 0)
        return self.convert_size(
            memory.get("usage", 0), binary=True
        ), self.convert_size(memory.get("limit", 0), binary=True)


class DummyTask:
    canceled = False


def is_stata(image_reference: str) -> bool:
    return image_reference.startswith("dataeditors/stata")


def is_matlab(image_reference: str) -> bool:
    return image_reference.startswith("dynare")


def stata_license_mount_source(api, submission, host_tmp_root: str) -> str:
    """Host path to bind at ``/usr/local/stata/stata.lic``, materializing it if needed.

    Two deployments, two answers:

    * ``STATA_LICENSE_HOSTPATH`` set (the manager, dev boxes, the test suite) --
      return it untouched. The host has the file; this is the long-standing
      behaviour and nothing about it changes.
    * unset (an ephemeral worker) -- fetch the license from the
      ``sivacor.stata_license`` setting using the admin-scoped token already on
      this task, write it into the submission's tmp dir, and return the
      corresponding *host* path.

    **Do not "improve" this by checking ``os.path.exists`` on the env var.** That
    path names the *host* filesystem, and this code runs inside the worker
    container, which does not mount it -- on the manager the same directory is
    visible as ``/srv/data``, on a worker not at all. So the check would report
    False on a perfectly licensed manager and break Stata there. Only the docker
    daemon resolves the bind source, which is exactly why a missing file surfaces
    as a container-create 400 rather than anything catchable here.

    The license lands in ``tmp_dir``, never ``workspace_dir``: arrangements are
    built by walking the workspace, so writing it there would hash a vendor
    license into the TRO composition and ship it inside the researcher's
    replication package.
    """
    if configured := os.environ.get("STATA_LICENSE_HOSTPATH"):
        return configured

    from ..settings import PluginSettings

    settings = api.settings([PluginSettings.STATA_LICENSE])
    license_text = (settings.get(PluginSettings.STATA_LICENSE) or "").strip()
    if not license_text:
        raise SubmissionError(
            FailureCode.NO_STATA_LICENSE,
            "A Stata image was requested but this deployment has no Stata "
            f"license: set the '{PluginSettings.STATA_LICENSE}' Girder setting, "
            "or STATA_LICENSE_HOSTPATH on the worker.",
        )

    # tmp_dir is a path inside THIS container; host_tmp_root translates it for
    # the docker daemon. Same two-view trick as the mounts in recorded_run.
    container_path = os.path.join(submission["tmp_dir"], "stata.lic")
    with open(container_path, "w") as fp:
        fp.write(license_text if license_text.endswith("\n") else license_text + "\n")
    os.chmod(container_path, 0o644)  # read-only mount, but the container's uid must read it
    logging.info("Materialized Stata license for submission %s", submission["job_id"])
    return os.path.join(host_tmp_root, container_path.lstrip("/"))


def stata_error(log_content: str) -> str | None:
    # if any of the lines contains r([0-9]+); return True
    regex = r"r\(\d+\);"
    if result := re.search(regex, log_content):
        # find a line that contains r([0-9]+); and return the previous line as error message
        line_start = log_content[: result.start() - 1].rfind("\n")
        error_message = log_content[line_start : result.start()].strip()
        return error_message + " " + result.group(0)
    elif "License is invalid" in log_content:
        return "License is invalid"
    elif log_content.startswith("Cannot find license file"):
        return "Cannot find license file"
    elif "Your license has expired" in log_content:
        return "License has expired"


#: The fixed diagnoses :func:`stata_error` can return that are not an ``r(NNN)``.
#: They are our own strings, so they are safe to keep in a permanent record
#: verbatim -- unlike the numeric case, whose message carries the failing line.
_STATA_FIXED_DIAGNOSES = (
    "License is invalid",
    "Cannot find license file",
    "License has expired",
)


def stata_error_code(stata_err: str | None) -> str | None:
    """Reduce a :func:`stata_error` message to something safe to keep forever.

    The numeric case is the reason this exists. ``stata_error`` prepends the
    *line before* ``r(NNN);``, which is generally the failing command -- often
    a ``use`` of the researcher's dataset, complete with its path. That belongs
    in the job log, where the researcher can act on it, and nowhere else. The
    return code alone is the part that aggregates, and Stata's return codes are
    a documented, finite set.
    """
    if not stata_err:
        return None
    if match := re.search(r"r\(\d+\)", stata_err):
        return match.group(0)
    if stata_err in _STATA_FIXED_DIAGNOSES:
        return stata_err
    return None


def stop_container(container: docker.models.containers.Container):
    try:
        container.stop()
    except requests.exceptions.ReadTimeout:
        tries = 10
        while tries > 0:
            container.reload()
            if container.status == "exited":
                break
        if container.status != "exited":
            logging.error(f"Unable to stop container: {container.id}")
    except docker.errors.NotFound:
        logging.warning(f"Container {container.id} was already gone.")
    except docker.errors.DockerException as dex:
        logging.error(dex)
        raise


def _infer_run_command(submission, stage):
    project_dir = get_project_dir(submission)
    entrypoint = ["/bin/sh", "-c"]

    # check if project_dir contains a single folder
    items = os.listdir(project_dir)
    try:
        items.remove("R")  # We now inject it...
    except ValueError:
        pass

    # Determine entrypoint based on image
    image_name = stage["image_name"]
    home_dir = "/workspace"
    if image_name.startswith("rocker"):
        entrypoint = ["/usr/local/bin/R", "--no-save", "--no-restore", "-f"]
    elif image_name.startswith("dataeditors/stata"):
        entrypoint = ["/usr/local/stata/stata-mp", "-b", "do"]
    elif image_name.startswith("dynare"):
        entrypoint = ["/usr/local/bin/matlab", "-batch"]
        home_dir = "/home/matlab"
    else:
        raise SubmissionError(
            FailureCode.NO_ENTRYPOINT,
            "Cannot infer the entrypoint for submission",
        )

    # Find the main file, by walking into subdirectories if needed
    base_path = Path(project_dir).resolve()
    relative_paths = []
    renv_paths = []
    for current_dir, _, filenames in os.walk(base_path):
        if stage["main_file"] in filenames:
            full_main_file_path = Path(current_dir) / stage["main_file"]
            relative_path = full_main_file_path.relative_to(base_path)
            relative_paths.append(relative_path)

        if "renv.lock" in filenames:
            full_renv_path = Path(current_dir) / "renv.lock"
            relative_renv_path = full_renv_path.relative_to(base_path)
            renv_paths.append(relative_renv_path)

    if len(relative_paths) == 0:
        raise SubmissionError(
            FailureCode.MAIN_FILE_MISSING,
            f"Cannot infer run command for submission. No {stage['main_file']} found.",
        )
    elif len(relative_paths) > 1:
        # The paths are what makes this actionable -- the researcher has to know
        # which copies to remove -- so they stay in the message. Only the count
        # is kept in the permanent record.
        found = ", ".join(str(path) for path in sorted(relative_paths))
        raise SubmissionError(
            FailureCode.MAIN_FILE_AMBIGUOUS,
            f"Cannot infer run command for submission. Multiple "
            f"{stage['main_file']} files found: {found}",
            detail=len(relative_paths),
        )

    sub_dir = ""
    # If renv.lock is found override sub_dir and command to use it
    if len(renv_paths) == 1:
        print(
            "Found renv.lock, adjusting command to use its location as working directory."
        )
        sub_dir = str(renv_paths[0].parent)
        command = (
            relative_paths[0].parent.relative_to(renv_paths[0].parent)
            / relative_paths[0].name
        ).as_posix()
    else:
        if len(relative_paths[0].parts) > 1:
            sub_dir = str(relative_paths[0].parent)
        command = str(relative_paths[0].name)

    if image_name.startswith("dynare"):
        # For MATLAB, the command is just the main file name without extension
        command = os.path.splitext(command)[0]

    if is_stata(image_name):
        command = f"'\"{command}\"'"
    elif " " in command:
        command = f'"{command}"'

    return entrypoint, command, sub_dir, home_dir


#: Abort a run when free space on the workspace filesystem drops below this.
#: Overridable per host with ``SIVACOR_DISK_FLOOR_BYTES``.
#:
#: **This is a floor for a *shared* filesystem, and that is its whole
#: justification.** With no scratch volume, ``/var/lib/docker`` and the workspace
#: are one filesystem: a runaway payload does not merely fail its own run, it
#: wedges the whole VM -- the docker daemon starts failing, celery cannot write,
#: and the submission has to be cleaned up by the server-side reaper instead of
#: failing cleanly. Stopping at a floor converts that into one failed job with an
#: explanatory message.
#:
#: On a workspace that has its own filesystem -- a Cinder scratch volume -- none
#: of that follows: filling it fails one run and wedges nothing, which is the
#: outcome the floor exists to manufacture. See :func:`disk_floor_bytes`, and
#: open item 3 of ``development_notes/cinder_volumes_plan.md``.
DISK_FLOOR_BYTES = int(os.environ.get("SIVACOR_DISK_FLOOR_BYTES", 5 * 1024**3))

#: Container-visible path whose filesystem backs the docker image store.
#:
#: ``cli.info()['DockerRootDir']`` names a *host* path that does not exist inside
#: the worker container, so it cannot be measured directly. ``/`` can: the worker
#: container's own root is an overlay whose ``statvfs`` passes through to the
#: filesystem holding ``/var/lib/docker`` (verified against the host's ``df``).
#:
#: This rests on one standing decision -- **never move ``/var/lib/docker`` onto
#: the scratch volume** (cinder_volumes_plan.md's "Do not" block), since the
#: volume is per-submission and the image store is per-VM. The override exists for
#: a host that has moved it anyway.
IMAGE_STORE_PATH = os.environ.get("SIVACOR_IMAGE_STORE_PATH", "/")

#: On a workspace with its own filesystem, reserve this share of it rather than
#: the full :data:`DISK_FLOOR_BYTES`.
#:
#: A constant 5 GiB is 25% of a 20 GB volume and half of a 10 GB one, taken from
#: the researcher's own allowance to guard a hazard that filesystem does not have.
#: A share keeps the reserve proportional, and it is capped by the constant so
#: this can only ever *lower* a floor, never raise one.
WORKSPACE_FLOOR_SHARE = float(os.environ.get("SIVACOR_WORKSPACE_FLOOR_SHARE", "0.1"))

#: RAM held back from the analysis container, for everything else on the worker.
#: Overridable per host with ``SIVACOR_MEMORY_HEADROOM_BYTES``.
#:
#: The disk story above, in memory. An analysis container was previously
#: unlimited, so a hungry payload competed with the celery worker that supervises
#: it -- and won. Measured on 2026-08-11: a Stata run peaked at 29.1 GB of the
#: worker's 29.4 GB, held it for three hours with no swap, and celery's
#: MainProcess stopped answering. The analysis finished with ``exit 0``, but
#: nothing was left alive to advance the chain, so the submission was reaped as
#: "no heartbeat" and the completed work was discarded.
#:
#: Capping the container moves the failure inside the cgroup: the kernel kills
#: the *analysis*, the container exits, ``recorded_run`` raises
#: :attr:`~girder_sivacor.errors.FailureCode.OUT_OF_MEMORY`, and the worker
#: survives to report it. One clearly-explained failed submission instead of a
#: silently lost one.
#:
#: 2 GiB is for celery, the docker daemon, and the OS. It is not generous; it is
#: the smallest amount that has to survive for a failure to be *reportable*.
MEMORY_HEADROOM_BYTES = int(
    os.environ.get("SIVACOR_MEMORY_HEADROOM_BYTES", 2 * 1024**3)
)

#: Do not cap below this. A limit this small cannot run any analysis image, so
#: setting one would convert "this host is too small" into a confusing
#: out-of-memory report against the researcher's code.
MIN_CONTAINER_MEMORY_BYTES = 2 * 1024**3


def container_memory_limit(mem_total) -> int | None:
    """Bytes to allow the analysis container, or ``None`` to leave it unlimited.

    ``None`` on a host too small to spare the headroom: an uncapped run on a tiny
    dev box is the status quo and merely risky, whereas a 0-byte limit would fail
    every submission instantly.
    """
    if not isinstance(mem_total, int) or mem_total <= 0:
        logging.warning("Docker reported no MemTotal; analysis container uncapped")
        return None
    limit = mem_total - MEMORY_HEADROOM_BYTES
    if limit < MIN_CONTAINER_MEMORY_BYTES:
        logging.warning(
            "Host has %.1f GiB total, too little to reserve %.1f GiB; "
            "analysis container uncapped",
            mem_total / 1024**3,
            MEMORY_HEADROOM_BYTES / 1024**3,
        )
        return None
    return limit


def directory_usage(*paths) -> int:
    """Bytes actually occupied on disk by everything under ``paths``.

    ``st_blocks``, not ``st_size``: the question this answers is "how much room
    did this run need", so a sparse file should count what it costs, and the
    per-file rounding up to a block matters for a package of many small files.
    This is what ``du`` reports, and deliberately not what the uploaded-package
    size bucket reports -- that one is the archive's apparent size.

    Hardlinked files are counted once, again matching ``du``. The inode set is
    only populated for files with more than one link, which in a replication
    package is almost none of them.

    Never raises: this runs inside the poll loop of a job that is otherwise
    fine, and a file vanishing mid-walk (the analysis is writing to this tree
    as we read it) is expected, not exceptional.
    """
    total = 0
    seen_inodes = set()
    stack = []

    # Directories occupy blocks of their own, and ``du`` charges for them. A
    # package of many small directories is measurably bigger than the sum of
    # its files, so counting only files under-reports exactly the shape of
    # payload most likely to fill a disk.
    for path in paths:
        if not path:
            continue
        try:
            stat_result = os.stat(path, follow_symlinks=False)
        except OSError:
            continue
        total += stat_result.st_blocks * 512
        if stat.S_ISDIR(stat_result.st_mode):
            stack.append(path)

    while stack:
        try:
            with os.scandir(stack.pop()) as entries:
                for entry in entries:
                    try:
                        # A symlink is charged for its own inode -- which is
                        # what it costs, and what ``du`` reports -- but never
                        # followed: one pointing at an ancestor would not
                        # terminate, and one pointing into the tree would be
                        # counted twice.
                        is_dir = entry.is_dir(follow_symlinks=False)
                        stat_result = entry.stat(follow_symlinks=False)
                        if stat_result.st_nlink > 1:
                            inode = (stat_result.st_dev, stat_result.st_ino)
                            if inode in seen_inodes:
                                continue
                            seen_inodes.add(inode)
                        total += stat_result.st_blocks * 512
                        if is_dir:
                            stack.append(entry.path)
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def workspace_usage(submission) -> int:
    """Disk occupied by one submission's scratch directories."""
    return directory_usage(submission.get("workspace_dir"), submission.get("tmp_dir"))


# Both disk messages below point at support@sivacor.org rather than naming
# `resources.disk_gb`, and that is deliberate.
#
# Extra scratch disk is granted per user, and for almost every account the allowance is
# zero -- so naming the field would tell the reader to set something the server will
# refuse. That is the mistake worker sizing made in the other direction: its OOM message
# told researchers to "choose a larger worker size" and shipped an image ahead of the
# picker, naming a control that did not yet exist.
#
# C4 shipped on 2026-08-22, so "there is no UI" is no longer the reason -- but the
# wording does not change, and the reason it does not is worth keeping: the control
# renders *disabled* for an unapproved account and names this same address itself. The
# route is the answer for the reader who has no allowance, and the reader who has one
# already has a control. Both are served by naming the address rather than the field.
#
# The earlier wording here ended "is the only thing that changes this today". True when
# written, false the moment anyone is approved -- and a hedge like "today" still reads as
# a factual claim and does not expire on its own. Naming the request route instead is
# true before C4 and stays true after it.


def workspace_has_own_filesystem(path) -> bool:
    """Whether ``path`` is on a different filesystem from the image store.

    True exactly when a scratch volume is mounted under the workspace. Asked of
    the filesystem rather than of the submission's ``requested_disk_gb`` on
    purpose: what matters is where the bytes actually land, and a worker whose
    layout differs for any other reason should get the same answer.

    Falls back to ``False`` -- i.e. treat the disk as shared, which is the
    conservative direction -- if either path cannot be stat'ed.
    """
    try:
        return os.stat(path).st_dev != os.stat(IMAGE_STORE_PATH).st_dev
    except OSError:
        logging.warning("Could not compare filesystems for %s", path, exc_info=True)
        return False


def disk_floor_bytes(submission) -> int:
    """Free space to keep on this submission's workspace filesystem.

    :data:`DISK_FLOOR_BYTES` when the workspace shares the image store's
    filesystem, because there the floor protects the *VM*. On a scratch volume it
    protects nothing structural -- filling it fails one run cleanly -- so the
    reserve becomes a share of the volume, capped by the constant.

    **What a floor on a volume does and does not buy.** It does leave room for
    ``upload_workspace``, which writes a zip of the project *inside* the
    project's own filesystem (``workspace_disk_waste.md``) -- and that peak is
    proportional to the package, which is why a proportional reserve fits it
    better than a constant. It does not *guarantee* that upload: a nearly-full
    20 GB volume needs more headroom than any floor here reserves. The floor is
    not what protects that step, and this docstring is the place that says so.
    """
    if not workspace_has_own_filesystem(submission.get("workspace_dir") or "/tmp"):
        return DISK_FLOOR_BYTES
    total = _workspace_disk_total(submission)
    if not total:
        return DISK_FLOOR_BYTES
    return min(DISK_FLOOR_BYTES, int(total * WORKSPACE_FLOOR_SHARE))


def _workspace_disk_total(submission) -> int | None:
    """Total bytes of the filesystem holding the workspace, or ``None``.

    The disk counterpart of ``MemTotal``, written into ``performance_data`` so a
    peak can be read as a fraction of what was available. On a volume-backed run
    this is the volume; otherwise it is the worker's root disk.

    Same defensive shape as :func:`disk_shortfall`: a run must never fail because
    a *reporting* call could not be made.
    """
    path = submission.get("workspace_dir") or "/tmp"
    try:
        return shutil.disk_usage(path).total
    except OSError:
        logging.warning("Could not determine size of %s", path, exc_info=True)
        return None


def disk_shortfall(submission) -> str | None:
    """Return an explanatory message if the workspace filesystem is nearly full.

    Checked from ``recorded_run``'s existing poll loop rather than by a separate
    watchdog, so it costs nothing extra. Returns ``None`` while there is room.

    The path checked is inside the worker container, but the numbers are the
    host's: the workspace is a bind mount, so ``statvfs`` reports the backing
    filesystem -- which is the scratch volume when there is one, and otherwise
    the one holding ``/var/lib/docker``.
    """
    path = submission.get("workspace_dir") or "/tmp"
    try:
        free = shutil.disk_usage(path).free
    except OSError:
        # Never fail a run because the check itself could not run.
        logging.warning("Could not determine free space for %s", path, exc_info=True)
        return None
    floor = disk_floor_bytes(submission)
    if free >= floor:
        return None
    head = (
        f"Ran out of disk space: {free / 1024**3:.1f} GiB free on the workspace "
        f"filesystem, below the {floor / 1024**3:.1f} GiB floor. "
    )
    granted = submission.get("telemetry_requested_disk_gb")
    if granted:
        # This submission *has* extra scratch disk, so telling it to ask for
        # extra scratch disk would be nonsense -- the same stale-copy failure as
        # naming a control that does not exist, in the other direction. Name the
        # size it was given, because that is the number to raise.
        return head + (
            f"This ran with {granted} GB of extra scratch disk and filled it. The "
            "replication package plus everything your code writes must fit there. "
            "Reduce the size of the package, or contact support@sivacor.org about a "
            "larger allowance."
        )
    return head + (
        "The replication package plus its outputs and the analysis image must fit in "
        "the worker's disk. Reduce the size of the package, or contact "
        "support@sivacor.org about extra scratch disk."
    )


#: Multiplier from a registry manifest's *compressed* layer total to bytes on
#: disk. Overridable per host with ``SIVACOR_IMAGE_ON_DISK_MULTIPLIER``.
#:
#: A manifest reports the size of each compressed layer blob; what lands in
#: ``/var/lib/docker`` is those layers extracted, and every layer that rewrites a
#: file from a lower one is stored in full again.
#:
#: **3.5, raised from 2.5 on 2026-08-22 because 2.5 was measured to be an
#: UNDER-estimate.** Measured on a purpose-built `m3.medium` running
#: `Featured-Ubuntu24` and `apt-get install docker.io` exactly as
#: ``worker-cloud-init.sh`` does -- so, a worker in every respect that matters:
#:
#: ===================================  ==========  =========  =====
#: image                                compressed  footprint  ratio
#: ===================================  ==========  =========  =====
#: ``dynare:6.1-R2024a``                6.16 GiB    21.00 GiB  3.41x
#: ``rocker/r-ver:4.6.1``               0.34 GiB     1.26 GiB  3.69x
#: ``stata19-mp:2026-06-03``            0.52 GiB     1.73 GiB  3.33x
#: ``stata19_5-mp-i-python:2026-06-03`` 0.89 GiB     2.68 GiB  3.01x
#: ===================================  ==========  =========  =====
#:
#: "Footprint" is the **free-space delta on the image store filesystem**, which is
#: the quantity this check actually compares against, corroborated by
#: ``docker system df -v`` (22.5 GB and 1.35 GB). Transient peak equalled the
#: resting footprint -- sampled every 2 s across a 141 s pull -- so there is no
#: extra headroom to reserve for the pull itself.
#:
#: **Why it is so much more than "extracted layers".** Workers run docker.io 29,
#: whose default image store is containerd's (`io.containerd.snapshotter.v1`,
#: confirmed on the same VM). That keeps the compressed blobs in the content store
#: *and* materialises a snapshot per layer, so a rewritten file is stored in the
#: blob, in its own layer's snapshot, and again in the layer above.
#:
#: **The asymmetry that justified 2.5 has flipped.** It said over-estimating
#: refuses a pull that would have fitted while under-estimating "merely lets the
#: pull fail the way it does today" -- true when a failed pull was mislabelled
#: ``image_pull_failed``. Since C0.1, ENOSPC is labelled ``out_of_disk`` correctly,
#: so the cost of under-estimating is now a *correct* failure 141 seconds and a
#: whole image download later, and the cost of over-estimating is a fast refusal.
#: Catching it early is worth more than it used to be.
#:
#: **All three families are now measured** (dataeditors added 2026-08-22, same
#: method, same gate). 3.5 covers dynare's 3.41 -- the family responsible for every
#: ENOSPC failure here and the one with by far the largest absolute error -- and
#: dataeditors' 3.01-3.33, with a small margin over each. It sits 0.19 below rocker's
#: 3.69, where that gap is 0.06 GiB of a 1.26 GiB image and cannot decide anything.
#:
#: Note the ratio *falls* as an image grows (3.69 -> 3.33 -> 3.01 -> 3.41), because
#: the per-layer duplication that drives it depends on how much each layer rewrites
#: rather than on total size. So a single multiplier is the right shape here; what it
#: cannot absorb is a family whose *compressed* entry is wrong, which is a separate
#: table and a separate failure -- see :data:`_IMAGE_FAMILY_COMPRESSED_GB`.
IMAGE_ON_DISK_MULTIPLIER = float(
    os.environ.get("SIVACOR_IMAGE_ON_DISK_MULTIPLIER", "3.5")
)

#: Fragments docker uses when a write fails for want of space. Matched
#: case-insensitively against the pull's free-form error string.
#:
#: Matching text is unpleasant and it is the only option: the streaming pull API
#: yields an ``error`` string, not an errno, and the alternative is to keep
#: reporting these as registry failures. Kept narrow on purpose -- a false
#: positive here relabels a genuine registry problem as a disk problem and sends
#: the researcher to shrink a package that was never too big.
_OUT_OF_SPACE_FRAGMENTS = (
    "no space left on device",
    "not enough space",
    "insufficient space",
    "write /var/lib/docker",  # the ENOSPC path docker reports on layer registration
)


def _is_out_of_space(error_text) -> bool:
    """Whether a pull's error string says the disk filled, rather than the registry.

    See :data:`_OUT_OF_SPACE_FRAGMENTS` for why this is textual.
    """
    if not error_text:
        return False
    lowered = str(error_text).lower()
    return any(fragment in lowered for fragment in _OUT_OF_SPACE_FRAGMENTS)


#: Compressed registry size per image family, in GiB, read from Docker Hub
#: 2026-08-21. Multiplied by :data:`IMAGE_ON_DISK_MULTIPLIER` to estimate what a
#: cold pull costs on disk.
#:
#: Measured, not guessed: ``dynare/dynare:6.1-R2024a`` 6.2, ``:5.5-R2023a`` 6.3,
#: ``rocker/geospatial:4.3.2`` 1.5, ``rocker/r-ver:4.6.1`` 0.3,
#: ``dataeditors/stata19-mp:2026-06-03`` 0.5, ``dataeditors/stata16:2023-06-13``
#: 0.4. Each entry below takes the **largest** tag seen in that family, because
#: the check's job is to catch the case that will not fit.
#:
#: **The spread is the point, and it is why only one family has ever caused this
#: failure.** dynare is ~5x a Stata image on disk: 6.2 GiB against ~1.2 GiB. All
#: four of the production pull failures this check was written for were dynare, and
#: a Stata pull essentially cannot cause one.
#:
#: **Refined 2026-08-22 (C5.3), and the multiplier survives the scrutiny.** Measured
#: unpack ratios, same tag on both sides: dynare 6.16 GiB compressed -> 6.21 GiB
#: unpacked (**1.01x** -- an already-compressed MATLAB runtime), rocker/r-ver 2.58x,
#: dataeditors 2.26-2.82x. Taken alone that made 2.5x look 2.5x too pessimistic for
#: dynare, and a per-family ratio looked like the fix.
#:
#: **It is not, because the fleet keeps both copies.** Workers install docker.io 29,
#: whose default image store is containerd's: compressed blobs stay in the content
#: store *and* the unpacked snapshot is created, so a resting footprint is roughly
#: compressed + unpacked -- ~12.4 GiB for dynare, not 6.2. That is why
#: ``ImageSize`` (which reports only the content-store half; see recorded_run) reads
#: so much smaller than the disk it costs, and it is independent corroboration of
#: pull_image's long-standing "~15 GB" figure rather than a refutation of it.
#:
#: **The measurement was then taken, and it says 3.41x for dynare** -- 6.16 GiB
#: compressed becomes a **21 GiB** footprint, because the containerd store keeps the
#: blobs *and* a snapshot per layer. So neither reading above was right: 2.5x was not
#: too pessimistic, it was too *optimistic*. :data:`IMAGE_ON_DISK_MULTIPLIER` carries
#: the numbers and how they were obtained.
#:
#: A family with no entry is **not** pre-checked -- see :func:`image_on_disk_estimate`
#: for why guessing high is the worse error. Per-tag figures are available live from
#: ``https://hub.docker.com/v2/repositories/<repo>/tags/<tag>`` (``full_size``) if
#: this table ever proves too coarse; it is deliberately not called at run time,
#: because a registry lookup in the run path is a new way for a run to fail.
_IMAGE_FAMILY_COMPRESSED_GB = {
    "dynare": 6.3,
    "rocker": 1.5,
    # 0.9, not the 0.5 this held until 2026-08-22. The table's own rule is "the
    # largest tag seen in that family", and 0.5 had stopped satisfying it:
    # dataeditors/stata19_5-mp-i-python:2026-06-03 is 0.89 GiB compressed, so the
    # family's biggest image was being estimated at 1.75 GiB against a measured
    # 2.68 GiB footprint. Small in absolute terms against a 5 GiB floor, and the
    # same class of error as the multiplier it multiplies.
    #
    # The cost of the correction is over-estimating the *smaller* Stata images by
    # ~1.3 GiB, which is affordable precisely because Stata footprints are small:
    # the check has ~6 GiB of slack in the cases where a Stata pull is decided.
    "dataeditors": 0.9,
}



def image_on_disk_estimate(cli, image_reference) -> tuple[int, str] | None:
    """Estimate what pulling ``image_reference`` will add to the disk.

    Returns ``(bytes, how)`` where ``how`` names the basis, so the researcher-facing
    message can say which it was -- an estimate presented as a measurement is worse
    than no estimate. ``None`` means no basis could be established, and the caller
    must then let the pull proceed: a check that cannot run must never fail a run,
    the same rule :func:`disk_shortfall` follows.

    Zero is a meaningful answer, not a missing one: an image already present locally
    costs nothing to "pull". That case matters more than it looks -- a worker part
    way through a multi-stage submission already has the image, and refusing its
    second stage for want of space it does not need would be a regression invented
    by this check.

    **An unknown family returns ``None`` rather than a default.** The two ways to be
    wrong are not symmetric. Guessing high refuses a pull that would have fitted,
    which the researcher experiences as being wrongly told their package is too big
    -- and there is no way for them to disprove it. Guessing low, or declining to
    guess, merely leaves the pull to fail as it does today, which
    :func:`_is_out_of_space` now labels correctly anyway. So this check only ever
    speaks up about families whose size is known.
    """
    try:
        cli.images.get(image_reference)
    except docker.errors.ImageNotFound:
        pass
    except Exception:
        logging.warning(
            "Could not check whether %s is present locally",
            image_reference,
            exc_info=True,
        )
    else:
        return 0, "already present locally"

    family = str(image_reference).split("/", 1)[0].lower()
    compressed_gb = _IMAGE_FAMILY_COMPRESSED_GB.get(family)
    if compressed_gb is None:
        logging.info(
            "No recorded size for image family %r, so the pull is not pre-checked",
            family,
        )
        return None
    return (
        int(compressed_gb * 1024**3 * IMAGE_ON_DISK_MULTIPLIER),
        f"{compressed_gb:g} GiB compressed for {family} images, "
        f"at {IMAGE_ON_DISK_MULTIPLIER:g}x on disk",
    )


def pull_space_shortfall(cli, submission, image_reference) -> str | None:
    """Explain why ``image_reference`` cannot be pulled here, or ``None`` to proceed.

    **Why this exists as a separate check from** :func:`disk_shortfall`. That one
    runs inside ``recorded_run``'s poll loop, which does not exist until a container
    is running -- so a pull that exhausts the disk could never reach it, and
    surfaced instead as ``IMAGE_PULL_FAILED`` naming the image. Four production
    submissions failed that way on 2026-08-20/21, all of them ``>5GB`` packages
    pulling a ~15 GB dynare image onto a 60 GB root disk already holding their
    extracted workspace. The record said the image could not be fetched, which
    reads as *our* registry problem and sent nobody to look at disk.

    See ``development_notes/cinder_volumes_plan.md`` C0.1. The permanent
    consequence of that misattribution: ``out_of_disk`` has never been recorded, so
    any before/after comparison across this change has to read the *old* side as
    ``image_pull_failed`` on large packages.
    """
    estimate = image_on_disk_estimate(cli, image_reference)
    if estimate is None:
        return None
    needed, how = estimate
    if not needed:
        return None

    # **The image lands where the image store is, not where the workspace is**,
    # and on a volume-backed submission those are different filesystems. Checking
    # the workspace there would read the volume's free space -- large and nearly
    # empty -- and clear a pull that has to fit on the root disk. That blindness
    # would only show up for exactly the submissions this feature exists for, and
    # first on a multi-stage one: each stage pulls its own image onto the same
    # per-VM store. See open item 3 of cinder_volumes_plan.md.
    workspace = submission.get("workspace_dir") or "/tmp"
    shared = not workspace_has_own_filesystem(workspace)
    measured = workspace if shared else IMAGE_STORE_PATH
    try:
        free = shutil.disk_usage(measured).free
    except OSError:
        logging.warning("Could not determine free space for %s", measured, exc_info=True)
        return None

    # On a shared filesystem the floor is part of what the pull must leave
    # behind: filling the disk to within a byte of it only moves the same failure
    # to the first thing the analysis writes. On a volume-backed run the floor
    # belongs to the *other* filesystem, where disk_shortfall guards it, so
    # requiring it here would refuse pulls that fit.
    floor = disk_floor_bytes(submission) if shared else 0
    required = needed + floor
    if free >= required:
        return None
    if shared:
        return (
            f"Not enough disk space to pull {image_reference}: "
            f"{free / 1024**3:.1f} GiB free on the workspace filesystem, but the image "
            f"needs about {needed / 1024**3:.1f} GiB ({how}) plus a "
            f"{floor / 1024**3:.1f} GiB working reserve. The replication "
            "package, its outputs and the analysis image all share this worker's disk, "
            "and together they do not fit. Reduce the size of the package -- or, if this "
            "work genuinely needs more room than one worker has, contact "
            "support@sivacor.org about extra scratch disk."
        )
    # The scratch volume is not the constraint here and the package is not the
    # problem, so neither is mentioned: the software image alone does not fit the
    # worker's own disk, which is nothing the researcher can act on.
    return (
        f"Not enough disk space to pull {image_reference}: "
        f"{free / 1024**3:.1f} GiB free on the worker's own disk, but the image needs "
        f"about {needed / 1024**3:.1f} GiB ({how}). Your extra scratch disk holds the "
        "replication package and is not what is short -- the software image is "
        "unpacked onto the worker itself. Please contact support@sivacor.org."
    )


#: How often to put a progress line in the job log while an image is pulling.
#: Deliberately much coarser than the heartbeat: each one is an updateJob, which
#: fires ``jobs.job.update.after`` server-side, whereas the heartbeat writes
#: straight to the collection.
PULL_LOG_INTERVAL = 120


def pull_image(cli, api, submission, image_reference):
    """Pull an image while keeping the submission visibly alive.

    ``images.pull()`` blocks and emits nothing. Analysis images are pulled on
    demand rather than baked into the worker image, and a cold dynare pull is
    ~15 GB on disk, so that silence can run for many minutes. Meanwhile the
    server's liveness signal is ``max(meta.heartbeat, job.updated,
    job.created)`` measured against ``sivacor.heartbeat_timeout`` (default 30
    min) -- so a large enough pull races the reaper, and losing that race shows
    up to the researcher as their own submission failing.

    Streaming the low-level API gives progress events to tick the heartbeat on.

    Two traps this deliberately handles:

    * ``cli.api.pull(stream=True)`` does **not** raise on failure the way
      ``images.pull()`` does -- it yields an event carrying ``error``. Left
      unchecked, a failed pull would look like success here and resurface as a
      confusing ImageNotFound from ``containers.create`` further down.
    * a heartbeat is best-effort; a failed ping must not fail the run, or a
      transient Girder blip would kill a pull that is going fine.
    """
    job_id = submission["job_id"]
    last_beat = last_log = time.monotonic()
    error = None
    layers = set()

    for event in cli.api.pull(image_reference, stream=True, decode=True):
        if not isinstance(event, dict):
            continue
        if event.get("error"):
            error = event["error"]
            break
        if event.get("id"):
            layers.add(event["id"])

        now = time.monotonic()
        if now - last_beat >= HEARTBEAT_INTERVAL:
            last_beat = now
            try:
                api.heartbeat(job_id)
            except Exception:
                logging.warning("Heartbeat during image pull failed", exc_info=True)
        if now - last_log >= PULL_LOG_INTERVAL:
            last_log = now
            try:
                api.update_job(
                    job_id,
                    log=f"Still pulling {image_reference} ({len(layers)} layers seen)\n",
                )
            except Exception:
                logging.warning("Progress report during image pull failed", exc_info=True)

    if error:
        if _is_out_of_space(error):
            # A pull that filled the disk is a disk failure, not a registry one,
            # and this branch is the reliable half of C0.1: it rests on what
            # docker actually reported rather than on an estimate, so it catches
            # the cases the pre-flight check declines to guess about.
            #
            # No detail: OUT_OF_DISK has no entry in telemetry's
            # _DETAIL_VALIDATORS, so one would be dropped server-side anyway.
            # The free-space figure would be defensible there -- it is a machine
            # fact of the same class as OUT_OF_MEMORY's cap -- but adding it is a
            # deliberate telemetry change with a privacy argument attached, and
            # not part of C0.
            raise SubmissionError(
                FailureCode.OUT_OF_DISK,
                f"Ran out of disk space while pulling {image_reference}. The "
                "replication package, its outputs and the analysis image all "
                "share this worker's disk, and together they did not fit. "
                f"Docker reported: {error}",
            )
        # Infrastructure, not user error: a registry timeout or a rate limit must
        # not read as "your replication package is broken".
        # The registry's own text is free-form and goes no further than the job
        # log; the record keeps the image reference, which is allow-listed.
        raise SubmissionError(
            FailureCode.IMAGE_PULL_FAILED,
            f"Failed to pull image {image_reference}: {error}",
            detail=image_reference,
        )
    logging.info("Pulled %s (%d layers)", image_reference, len(layers))


def recorded_run(api, submission, stage, env_vars, task=None):
    cli = docker.from_env()
    info = cli.info()
    cpu_info = cpuinfo.get_cpu_info()
    performance_data = {
        "Architecture": info.get("Architecture"),
        "KernelVersion": info.get("KernelVersion"),
        "OperatingSystem": info.get("OperatingSystem"),
        "OSType": info.get("OSType"),
        "OSVersion": info.get("OSVersion"),
        "MemTotal": info.get("MemTotal"),
        "NCPU": info.get("NCPU"),
        "Processor": cpu_info.get("brand_raw"),
        # What the researcher ASKED for, beside what the machine had (MemTotal) and
        # what the container was given (DockerRunArgs.mem_limit, added below). The
        # three together are the whole memory story for a run, and only the middle
        # one was certified before this.
        #
        # Performance data rather than a TROV attribute, decided 2026-08-20 (open
        # item 2): a signed *claim* about the requested size would need a matching
        # TRS capability under the warrant chain, i.e. a trace-specification change
        # before any code -- and tro_utils raises ValueError for a TRP attribute with
        # no backing capability, so it cannot be added incrementally. The consequence
        # accepted knowingly: the TRO certifies what a run got, never what was asked
        # for, so a submission that should have been 60 and ran on 30 leaves no
        # signed trace of that intent.
        #
        # None for a chain published by a server older than P1, the same reason
        # prepare_submission defaults it.
        "RequestedMemoryGB": submission.get("telemetry_requested_memory_gb"),
        # The disk story, to the same three parts as the memory one above (V7 of
        # cinder_volumes_plan.md): what the extra scratch volume was asked for,
        # what the workspace filesystem actually had, and -- added after the run
        # -- what the run peaked at (MaxDiskUsage).
        #
        # None rather than 0 when no volume was requested, because absent is the
        # shape the whole feature uses for "no volume": submit_job omits the key,
        # prepare_submission defaults it to None, and a 0 here would read as a
        # volume of zero size rather than as the ordinary root-disk run.
        "RequestedDiskGB": submission.get("telemetry_requested_disk_gb"),
        # MemTotal's counterpart, and the number that makes the other two
        # legible: 20 GiB peak means one thing on a 58 GB root disk and another
        # on a 20 GB volume. Read from the workspace path, so on a volume-backed
        # run it is the volume -- which is also the only place the volume's real
        # size (after mkfs overhead) is ever observed.
        "WorkspaceDiskTotal": _workspace_disk_total(submission),
    }
    # Machine shape for the execution record. Kept as a capability class, not
    # an identity: no hostname, no queue name, nothing naming this instance.
    submission["telemetry_worker"] = {
        "arch": info.get("Architecture"),
        "ncpu": info.get("NCPU"),
        "mem_total_bytes": info.get("MemTotal"),
    }
    user_env = {
        _["key"]: _["value"]
        for _ in decrypt_job_secrets(
            env_vars["encrypted_secrets"], env_vars["wrapped_job_key"]
        )
    }
    known_secrets = list(user_env.values())

    def logging_worker(log_queue, container):
        for line in container.logs(stream=True):
            line = line.decode("utf-8", errors="ignore").strip()
            for secret in known_secrets:
                line = line.replace(secret, MASK)
            log_queue.put(line, block=False)

    task = task or DummyTask
    log_queue = queue.Queue()
    logging.info("Starting recorded run")

    submission_folder = api.folder(submission["folder_id"])
    folder_id = submission_folder["_id"]
    folder_meta = submission_folder.get("meta", {})
    creator_id = folder_meta["creator_id"]
    stage_num = folder_meta["stages"].index(stage) + 1

    image_reference = stage["image_name"] + ":" + stage["image_tag"]
    host_tmp_root = os.environ.get("DOCKER_HOST_TMP_ROOT", "/")
    source_workspace_dir = os.path.join(
        host_tmp_root, submission["workspace_dir"].lstrip("/")
    )
    target_workspace_dir = "/workspace"
    mounts = [
        docker.types.Mount(
            target=target_workspace_dir,
            source=source_workspace_dir,
            type="bind",
            read_only=False,
        ),
        docker.types.Mount(
            source=os.path.join(host_tmp_root, submission["tmp_dir"].lstrip("/")),
            target="/tmp",
            type="bind",
        ),
    ]
    # Only Stata images get the license. Ungated, this mounted it into *every*
    # container, so a missing license file failed unrelated images (a dynare run
    # died on `bind source path does not exist: .../stata.lic.19`). type="bind"
    # does not auto-create the source the way a legacy -v bind does, so the whole
    # submission fails at container create. Invisible on the manager, where the
    # file exists; guaranteed on a fresh ephemeral worker, where it does not --
    # see stata_license_mount_source for how a worker gets one.
    if is_stata(image_reference):
        stata_license_hostpath = stata_license_mount_source(
            api, submission, host_tmp_root
        )
        mounts.append(
            docker.types.Mount(
                target="/usr/local/stata/stata.lic",
                source=stata_license_hostpath,
                type="bind",
                read_only=True,
            )
        )

    # Before the pull, not during it: a pull that exhausts the disk cannot reach
    # disk_shortfall() below, because that only runs once a container exists. C0.1.
    if shortfall := pull_space_shortfall(cli, submission, image_reference):
        raise SubmissionError(FailureCode.OUT_OF_DISK, shortfall)

    pull_image(cli, api, submission, image_reference)

    entrypoint, command, sub_dir, home_dir = _infer_run_command(submission, stage)
    project_dir = get_project_dir(submission)
    logging.info(
        "Setting working directory to: "
        + os.path.join(target_workspace_dir, "project", sub_dir)
    )
    logging.info("Running Tale with command: " + " ".join(entrypoint + [command]))
    if is_matlab(image_reference):
        user = "1001:100"
        read_only = False  # Matlab needs write access to its home dir for preferences, so we set
        # the whole container to read-write in this case...
    else:
        user = f"{os.getuid()}:{os.getgid()}"
        read_only = True

    environment = {
        "TMPDIR": "/tmp",
        "TEMP": "/tmp",
        "TMP": "/tmp",
        "HOME": home_dir,
        "R_LIBS": os.path.join(home_dir, "R", "library"),
        "R_LIBS_USER": os.path.join(home_dir, "R", "library"),
        "MLM_LICENSE_FILE": "27007@rtlicense1.uits.indiana.edu",
    }
    environment.update(user_env)

    container_kwargs = {
        "image": image_reference,
        "entrypoint": entrypoint,
        "command": command,
        "detach": True,
        "mounts": mounts,
        "network_disabled": stage.get("network_isolation", False),
        "read_only": read_only,
        "working_dir": os.path.join(target_workspace_dir, "project", sub_dir),
        "user": user,
        "environment": environment,
        # Analysis containers are siblings started through the docker socket, not
        # children of this process, so nothing ties their lifetime to the worker.
        # Kill the worker mid-run and the container keeps going -- burning a core
        # with no one left to collect its output. These labels are what lets a
        # restarted worker find and reap its own leftovers; see
        # :func:`reap_orphaned_containers`.
        "labels": {
            ORPHAN_LABEL_QUEUE: worker_queue_name(),
            ORPHAN_LABEL_JOB: str(submission.get("job_id", "")),
        },
    }
    if (mem_limit := container_memory_limit(info.get("MemTotal"))) is not None:
        container_kwargs["mem_limit"] = mem_limit
        # Equal to mem_limit, which is how docker spells "no swap for this
        # container". Left unset, docker allows swap up to 2x the limit, and a
        # payload that swaps instead of dying reproduces the original failure in
        # slow motion: the box thrashes, celery stops being scheduled, and the
        # OOM that would have produced a clean error never arrives. The JS2
        # workers have no swap today, so this is insurance against a host that
        # does, not a change in behaviour on the current fleet.
        container_kwargs["memswap_limit"] = mem_limit
        logging.info(
            "Analysis container capped at %.1f GiB of %.1f GiB total",
            mem_limit / 1024**3,
            info["MemTotal"] / 1024**3,
        )
    container = cli.containers.create(**container_kwargs)
    # redact env in container_kwargs since we dump them later
    container_kwargs["environment"] = {
        k: (MASK if k in user_env else v) for k, v in container_kwargs["environment"].items()
    }

    logging_thread = Thread(target=logging_worker, args=(log_queue, container))
    with tempfile.TemporaryDirectory() as container_temp_path:
        dstats_tmppath = os.path.join(container_temp_path, "dockerstats")
        stats_thread = DockerStatsCollectorThread(container, dstats_tmppath)
        publisher = LogPublisher(
            container.name, f"docker:logs:{creator_id}", known_secrets
        )

        # Job output must come from stdout/stderr
        container.start()
        stats_thread.start()
        logging_thread.start()
        publisher.start()

        peak_disk = 0
        try:
            container = cli.containers.get(container.id)
            last_heartbeat = 0.0
            while container.status == "running":
                while not log_queue.empty():
                    print(log_queue.get_nowait(), flush=True)
                if task.canceled:
                    stop_container(container)
                    break
                # A replication can run for hours without writing a line, and
                # the job log is the only thing that otherwise touches the job.
                # This is the sole reason the server can tell a slow run from a
                # dead worker.
                now = time.monotonic()
                if now - last_heartbeat >= HEARTBEAT_INTERVAL:
                    last_heartbeat = now
                    api.heartbeat(submission["job_id"])
                    # Sampled on the heartbeat tick rather than every second:
                    # the walk is ~0.4s over a 100k-file tree, which is nothing
                    # once a minute and a busy loop at 1Hz.
                    peak_disk = max(peak_disk, workspace_usage(submission))
                    if shortfall := disk_shortfall(submission):
                        stop_container(container)
                        raise SubmissionError(FailureCode.OUT_OF_DISK, shortfall)
                time.sleep(1)
                container = cli.containers.get(container.id)
        except docker.errors.NotFound:
            pass

        # A run shorter than one heartbeat never sampled above, and would report
        # nothing at all. This also catches output written right at the end,
        # which is when a replication package is usually at its largest.
        peak_disk = max(peak_disk, workspace_usage(submission))

        stats_thread.join()
        while not log_queue.empty():
            print(log_queue.get_nowait())
        logging_thread.join()
        publisher.stop()
        publisher.join()

        if task.canceled:
            ret = {"StatusCode": -123}
        else:
            ret = container.wait()

        container.reload()
        # Read before container.remove() further down, which takes the state with
        # it. Docker sets this when the kernel OOM-killed the container's cgroup;
        # the exit code is an indistinguishable 137, the same as any SIGKILL.
        oom_killed = bool((container.attrs.get("State") or {}).get("OOMKilled"))
        logging.info(f"Container exited with status: {ret['StatusCode']}")
        logging.info("Collecting performance data...")
        performance_data.update(
            {
                "ImageRepoTags": container.image.attrs.get("RepoTags", []),
                "ImageRepoDigests": container.image.attrs.get("RepoDigests", []),
                "StartedAt": container.attrs["State"]["StartedAt"],
                "FinishedAt": container.attrs["State"]["FinishedAt"],
            }
        )
        performance_data.update({"DockerRunArgs": json.dumps(container_kwargs)})
        # The two halves of "how much disk did this run need": the workspace it
        # grew, and the image it had to have on the machine first. Kept apart
        # because only the first is the researcher's to control, and an analysis
        # image is a gigabyte or more on its own -- reporting the sum would make
        # every Stata run look like a storage hog.
        performance_data["MaxDiskUsage"] = peak_disk
        try:
            # **ImageSize is the COMPRESSED size on this fleet, not the on-disk
            # footprint**, and the difference is a factor of ~2.5 for every family
            # except dynare. Measured 2026-08-22 (C5.3): a rocker/r-ver:4.6.1 run
            # recorded 0.341 GiB, exactly that image's compressed manifest total,
            # while the same image occupies 0.88 GiB unpacked under a classic
            # overlay2 docker.
            #
            # docker's `Size` normally means unpacked. The likely cause is the
            # containerd image store, which accounts the content store rather than
            # the snapshot -- unconfirmed, and one `docker info | grep -i
            # snapshotter` on a worker settles it. The measurement holds either way.
            #
            # **So do not add this to MaxDiskUsage and call it disk used.** C5.3's
            # first pass did exactly that and understated every image by the
            # compression ratio. Worse, the footprint is not even the unpacked size:
            # docker.io 29 defaults to the containerd image store, which keeps the
            # compressed blobs *and* the snapshot, so the disk cost is roughly
            # compressed + unpacked -- about twice this figure for dynare and ~3.5x
            # for the others.
            #
            # The raw figure is kept as-is rather than converted here, because what a
            # run *reports* should be what it observed, not a derived number a later
            # reader cannot check. The conversion belongs wherever a footprint is
            # actually needed, and _IMAGE_FAMILY_COMPRESSED_GB's note says what is
            # still unmeasured about it.
            performance_data["ImageSize"] = cli.images.get(image_reference).attrs["Size"]
        except Exception:
            logging.warning("Could not determine size of %s", image_reference, exc_info=True)
        if os.path.isfile(dstats_tmppath + ".csv"):
            df = pd.read_csv(dstats_tmppath + ".csv")
            # The header is written when the collector starts, so the file exists
            # even when no reading was ever taken. Aggregating that empty frame
            # yields NaN, and json.dumps happily writes a bare `NaN` literal --
            # invalid JSON, inside a file that gets hashed into a signed TRO and
            # read back by strict parsers years later. Say what is missing instead.
            if df.empty:
                performance_data["MetricsUnavailable"] = (
                    "container exited before Docker emitted a stats reading"
                )
            else:
                performance_data.update(
                    {
                        "MaxCPUPercent": float(df["CPU %"].max()),
                        "MaxMemoryUsage": int(df["Memory Usage"].max()),
                    }
                )
                # max() of a cumulative counter is its final value, which is also
                # correct if the last row is truncated or the rows are unordered.
                # Guarded on the column: a CSV written by an older worker image does
                # not have it, and a KeyError here would lose the whole aggregation.
                if "CPU Seconds" in df.columns:
                    performance_data["CPUSecondsTotal"] = float(df["CPU Seconds"].max())
        # Same numbers, kept where finalize_job can still reach them. The
        # performance_data file itself lives in the submission folder and is
        # deleted with it, which is precisely the gap the record fills.
        submission.setdefault("telemetry_stages", []).append(
            {
                # The size the submission asked for, recorded beside the cap it
                # actually got: mem_limit_bytes alone cannot say whether a run
                # was sized deliberately or simply landed on whatever was free.
                "requested_memory_gb": submission.get(
                    "telemetry_requested_memory_gb"
                ),
                "image_name": stage.get("image_name"),
                "image_tag": stage.get("image_tag"),
                "network_isolation": bool(stage.get("network_isolation", False)),
                "exit_code": ret.get("StatusCode"),
                "max_cpu_percent": performance_data.get("MaxCPUPercent"),
                # The denominator-free primitive: divide by duration_seconds for mean
                # cores. Kept as seconds rather than a precomputed mean so the two can
                # never disagree, and so it stays meaningful when duration is unknown.
                "cpu_seconds_total": performance_data.get("CPUSecondsTotal"),
                "max_memory_bytes": performance_data.get("MaxMemoryUsage"),
                # The cap this run was given, so max_memory_bytes can be read as
                # a fraction of what was allowed rather than an absolute number.
                # Without it, a future flavor change silently re-baselines every
                # comparison against older records.
                "mem_limit_bytes": mem_limit,
                "max_disk_bytes": performance_data.get("MaxDiskUsage"),
                "image_size_bytes": performance_data.get("ImageSize"),
            }
        )
        api.upload_bytes(
            folder_id,
            # allow_nan=False is a tripwire: a non-finite float here would be
            # written as an invalid JSON literal rather than rejected.
            json.dumps(performance_data, cls=NpEncoder, allow_nan=False).encode("utf-8"),
            f"performance_data_stage_{stage_num}.json",
            mime_type="text/plain",
            item_type="performance_data",
        )
        logging.info("Performance data collected and uploaded.")

        # Dump run std{out,err} and entrypoint used.
        main_file = stage["main_file"]
        log_files = {}
        for stdout, stderr, key in [
            (True, False, "stdout"),
            (False, True, "stderr"),
        ]:
            target_file = os.path.join(container_temp_path, key)
            if key != "dockerstats":
                with open(target_file, "wb") as fp:
                    fp.write(container.logs(stdout=stdout, stderr=stderr))
                logging.info(f"Dumped docker {key} to {target_file}")

        logging.info(
            "Checking for extra log files containing stdout content, which may be "
            "generated by R or Stata if the main file is detected in the project."
        )
        main_file_noext = os.path.splitext(main_file)[0]
        extra_logfile = None
        if main_file.endswith(".R"):
            extra_logfile = main_file_noext + ".Rout"
            logging.info(f"Looking for R log file: {extra_logfile}")
        elif main_file.endswith(".do") or is_stata(image_reference):
            extra_logfile = main_file_noext.split(".", 1)[0] + ".log"
            logging.info(f"Looking for Stata log file: {extra_logfile}")
        else:
            logging.info(
                f"Cannot infer log file for main file {main_file}, skipping..."
            )

        stata_err = None
        if extra_logfile:
            target_file = os.path.join(container_temp_path, "stdout")
            # find .Rout files if stdout is empty and R
            msg = f"\n\n===== Additional log content from {extra_logfile} =====\n\n"
            for root, dirs, files in os.walk(project_dir):
                for file in files:
                    if file == extra_logfile:
                        with open(os.path.join(root, file), "rb") as fp:
                            with open(target_file, "ab") as out_f:
                                out_f.write(msg.encode("utf-8"))
                                shutil.copyfileobj(fp, out_f)
                            if is_stata(image_reference):
                                fp.seek(0)
                                log_content = fp.read()
                                log_content = log_content.decode(
                                    "utf-8", errors="ignore"
                                )
                                for secret in known_secrets:
                                    log_content = log_content.replace(secret, MASK)
                                stata_err = stata_error(log_content)
                        break

        # Download existing log file if present, so we can append to it instead of overwriting
        for key in ["stdout", "stderr", "dockerstats"]:
            target_file = os.path.join(container_temp_path, key)
            if not os.path.isfile(target_file):
                print(f"{key} file not found, skipping...")
                continue

            # obfuscate secrets in the log file before uploading,
            # line by line to avoid memory issues with large files
            with (
                open(target_file, "rb") as fp,
                open(target_file + ".tmp", "wb") as out_f,
            ):
                for line in fp:
                    line_decoded = line.decode("utf-8", errors="ignore")
                    for secret in known_secrets:
                        line_decoded = line_decoded.replace(secret, MASK)
                    out_f.write(line_decoded.encode("utf-8"))
            os.replace(target_file + ".tmp", target_file)

            log_file = f"/tmp/{key}-{submission['job_id']}"
            log_obj = api.file(folder_meta.get(f"{key}_file_id"))
            if log_obj:
                api.download_file(log_obj["_id"], log_file)

            stage_stamp = f"\n\n===== Stage {stage_num} Output =====\n\n"
            with open(target_file, "rb") as fp:
                logging.info(
                    f"Reading {key} from {target_file} and appending to {log_file}..."
                )
                with open(log_file, "ab") as out_f:
                    out_f.write(stage_stamp.encode("utf-8"))
                    shutil.copyfileobj(fp, out_f)

            log_files[key] = target_file
            if log_obj:
                api.replace_file(log_obj["_id"], log_file)
                api.annotate_item_type(log_obj, key)
            else:
                # Keep the job-stamped basename; upload_workspace pulls these
                # into the zip under the name Girder knows them by.
                fobj = api.upload_file(
                    folder_id, log_file, mime_type="text/plain", item_type=key
                )
                api.set_folder_metadata(
                    folder_id, {f"{key}_file_id": str(fobj["_id"])}
                )
            os.remove(log_file)

    try:
        container.remove()
    except docker.errors.NotFound:
        pass

    if not task.canceled:
        # Before the generic branch: an OOM kill exits 137, which would otherwise
        # be reported as "check stdout/stderr" -- and stdout will say nothing,
        # because the process was killed without warning. This is the one failure
        # whose cause is invisible from inside the container.
        if oom_killed:
            raise SubmissionError(
                FailureCode.OUT_OF_MEMORY,
                (
                    # The cap is quoted from mem_limit rather than derived from
                    # the rung that was asked for: the two can differ (a fleet
                    # whose default flavour disagrees with the catalogue), and
                    # the figure that killed the run is the true one. S1
                    # property 3.
                    #
                    # "Choose a larger size" replaced "ask support about a
                    # larger worker" when the picker shipped (P4.2): a
                    # researcher can now do this themselves for the self-service
                    # rungs. The request route stays for the gated ones, named
                    # with the same words the picker labels them with, so the
                    # message and the form agree.
                    f"The analysis used more memory than this worker allows "
                    f"({mem_limit / 1024**3:.1f} GiB) and was stopped. Reduce the "
                    "memory your code needs, or choose a larger worker size when "
                    "you resubmit. Sizes marked 'by request' need approval -- "
                    "email support@sivacor.org for one of those."
                    if mem_limit is not None
                    else "The analysis was stopped by the kernel for using too "
                    "much memory."
                ),
                detail=mem_limit,
            )
        if ret["StatusCode"] != 0:
            raise SubmissionError(
                FailureCode.NONZERO_EXIT,
                "Error executing recorded run. Check stdout/stderr for details.",
                detail=ret["StatusCode"],
            )
        elif is_stata(image_reference) and stata_err:
            # Stata can fail while still exiting 0, which is why this is a
            # separate branch. The message keeps the failing line for the
            # researcher; the record keeps only the return code.
            raise SubmissionError(
                FailureCode.STATA_ERROR,
                f"Stata returned an error ({stata_err}). Check stdout/stderr for details.",
                detail=stata_error_code(stata_err),
            )

    return ret


def zip_symlink(zip_file, symlink_path, arcname=None):
    """
    Add a symlink to a zip file, preserving the link instead of its target.

    Args:
        zip_file (zipfile.ZipFile): Open zip file object (in write mode).
        symlink_path (str): Path to the symlink on disk.
        arcname (str, optional): Name/path to use for the symlink in the zip.
            Defaults to symlink_path.
    """
    # Validate the path is a symlink
    if not os.path.islink(symlink_path):
        raise ValueError(f"{symlink_path} is not a symlink")

    # Get the symlink target (relative or absolute path)
    target = os.readlink(symlink_path)

    # Define the name/path for the symlink in the zip
    arcname = arcname or symlink_path

    # Create a ZipInfo object for the symlink
    zinfo = zipfile.ZipInfo(arcname)

    # Get original symlink permissions (Unix)
    link_stat = os.lstat(symlink_path)
    link_mode = stat.S_IFLNK | (link_stat.st_mode & 0o777)  # Preserve original perms
    zinfo.external_attr = link_mode << 16

    # Set other metadata (optional but recommended)
    # zinfo.date_time = zipfile.ZipInfo(
    #    date_time=os.path.getmtime(symlink_path)
    # ).date_time
    zinfo.compress_type = (
        zipfile.ZIP_STORED
    )  # Symlinks are small; no compression needed

    # Write the symlink to the zip: content is the target path
    zip_file.writestr(zinfo, target)
