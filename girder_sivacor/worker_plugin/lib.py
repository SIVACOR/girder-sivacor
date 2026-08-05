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
                "Block IO Read,Block IO Write,PIDs\n"
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
                    f"{d.get('pids_stats', {}).get('current', 0)}\n"
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
        raise ValueError(
            "A Stata image was requested but this deployment has no Stata "
            f"license: set the '{PluginSettings.STATA_LICENSE}' Girder setting, "
            "or STATA_LICENSE_HOSTPATH on the worker."
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
        raise ValueError("Cannot infer the entrypoint for submission")

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
        raise ValueError(
            f"Cannot infer run command for submission. No {stage['main_file']} found."
        )
    elif len(relative_paths) > 1:
        raise ValueError(
            f"Cannot infer run command for submission. Multiple {stage['main_file']} "
            "files found: {relative_paths}"
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
#: An ephemeral worker has no separate volume, so ``/var/lib/docker`` and the
#: workspace share one filesystem: a runaway payload does not merely fail its own
#: run, it wedges the whole VM -- the docker daemon starts failing, celery cannot
#: write, and the submission has to be cleaned up by the server-side reaper
#: instead of failing cleanly. Stopping at a floor converts that into one failed
#: job with an explanatory message.
DISK_FLOOR_BYTES = int(os.environ.get("SIVACOR_DISK_FLOOR_BYTES", 5 * 1024**3))


def disk_shortfall(submission) -> str | None:
    """Return an explanatory message if the workspace filesystem is nearly full.

    Checked from ``recorded_run``'s existing poll loop rather than by a separate
    watchdog, so it costs nothing extra. Returns ``None`` while there is room.

    The path checked is inside the worker container, but the numbers are the
    host's: the workspace is a bind mount, so ``statvfs`` reports the backing
    filesystem -- the same one holding ``/var/lib/docker``.
    """
    path = submission.get("workspace_dir") or "/tmp"
    try:
        free = shutil.disk_usage(path).free
    except OSError:
        # Never fail a run because the check itself could not run.
        logging.warning("Could not determine free space for %s", path, exc_info=True)
        return None
    if free >= DISK_FLOOR_BYTES:
        return None
    return (
        f"Ran out of disk space: {free / 1024**3:.1f} GiB free on the workspace "
        f"filesystem, below the {DISK_FLOOR_BYTES / 1024**3:.1f} GiB floor. The "
        "replication package plus its outputs and the analysis image must fit in "
        "the worker's disk; this submission needs more room than this worker has."
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
        # Infrastructure, not user error: a registry timeout or a rate limit must
        # not read as "your replication package is broken".
        raise RuntimeError(f"Failed to pull image {image_reference}: {error}")
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
                    if shortfall := disk_shortfall(submission):
                        stop_container(container)
                        raise RuntimeError(shortfall)
                time.sleep(1)
                container = cli.containers.get(container.id)
        except docker.errors.NotFound:
            pass

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
        if ret["StatusCode"] != 0:
            raise ValueError(
                "Error executing recorded run. Check stdout/stderr for details."
            )
        elif is_stata(image_reference) and stata_err:
            raise ValueError(
                f"Stata returned an error ({stata_err}). Check stdout/stderr for details."
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
