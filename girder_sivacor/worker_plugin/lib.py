import functools
import io
import json
import logging
import math
import os
import queue
import re
import stat
import tempfile
import time
import zipfile
from pathlib import Path
from threading import Event, Thread

import cpuinfo
import docker
import numpy as np
import pandas as pd
import redis
import requests
from girder.models.file import File
from girder.models.folder import Folder
from girder.models.item import Item
from girder.models.setting import Setting
from girder.models.upload import Upload
from girder.models.user import User
from girder.settings import SettingKey
from girder.utility import RequestBodyStream


class NpEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super(NpEncoder, self).default(obj)


def annotate_item_type(file_obj: dict, item_type: str) -> None:
    item = Item().load(file_obj["itemId"], force=True)
    if "type" not in item["meta"]:
        Item().setMetadata(item, {"type": item_type})


def get_project_dir(submission):
    return os.path.join(submission["temp_dir"], "project")


def _update_file_from_path(file, path, user):
    size = os.path.getsize(path)
    upload = Upload().createUploadToFile(
        file=file, user=user, size=size, reference=None, assetstore=None
    )
    if size == 0:
        return Upload().finalizeUpload(upload)

    chunkSize = Upload()._getChunkSize()
    with open(path, "rb") as f:
        while True:
            data = f.read(chunkSize)
            if not data:
                break
            upload = Upload().handleChunk(
                upload, RequestBodyStream(io.BytesIO(data), len(data))
            )
    return upload


def _dump_from_fileobj(in_f, out_f, is_zip=False, arcname=None):
    chunk_size = Setting().get(SettingKey.FILEHANDLE_MAX_SIZE)
    while True:
        chunk = in_f.read(chunk_size)
        if not chunk:
            break
        if is_zip:
            if arcname:
                out_f.writestr(arcname, chunk)
            else:
                out_f.writestr(in_f._file["name"], chunk)
        else:
            out_f.write(chunk)


@functools.lru_cache
def _redis_client_sync() -> redis.Redis:
    url = os.environ.get("GIRDER_NOTIFICATION_REDIS_URL", "redis://localhost:6379")
    return redis.Redis.from_url(url)


class LogPublisher(Thread):
    def __init__(self, container_name, channel):
        super().__init__()
        self.container_name = container_name
        self.channel = channel
        self.client = docker.from_env()
        self._stop_event = Event()
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
        while True:
            try:
                d = self.container.stats(stream=False)
            except docker.errors.NotFound:
                break
            ts = d["read"]

            if self.container_finished(ts):
                break

            if ts != "0001-01-01T00:00:00Z":
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
                        f'{bytes_in},{bytes_out},{blkio_rd},{blkio_wr},'
                        f'{d.get("pids_stats", {}).get("current", 0)}\n'
                    )
                    fp.write(csv_line)
            time.sleep(5)

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
        cpu_count = d["cpu_stats"]["online_cpus"] if "online_cpus" in d["cpu_stats"] else 1
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


def stata_error(log_content: str) -> str | None:
    # if any of the lines contains r([0-9]+); return True
    regex = r"r\(\d+\);"
    if result := re.search(regex, log_content):
        return result.group(0)
    elif log_content == "License is invalid\n":
        return "License is invalid"
    elif log_content.startswith("Cannot find license file"):
        return "Cannot find license file"


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
    home_dir = submission["temp_dir"]
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

    if " " in command:
        command = f'"{command}"'

    return entrypoint, command, sub_dir, home_dir


def recorded_run(submission, stage, task=None):
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

    def logging_worker(log_queue, container):
        for line in container.logs(stream=True):
            log_queue.put(line.decode("utf-8").strip(), block=False)

    task = task or DummyTask
    log_queue = queue.Queue()
    logging.info("Starting recorded run")

    submission_folder = Folder().load(submission["folder_id"], force=True)
    creator_id = submission_folder["meta"]["creator_id"]
    stage_num = submission_folder["meta"]["stages"].index(stage) + 1
    admin = User().findOne({"admin": True})

    image_reference = stage["image_name"] + ":" + stage["image_tag"]
    host_tmp_root = os.environ.get("DOCKER_HOST_TMP_ROOT", "/")
    target_tmp_dir = os.path.join(host_tmp_root, submission["temp_dir"].lstrip("/"))
    volumes = {
        target_tmp_dir: {
            "bind": submission["temp_dir"],
            "mode": "rw",
        }
    }
    if stata_license_hostpath := os.environ.get("STATA_LICENSE_HOSTPATH"):
        volumes[stata_license_hostpath] = {
            "bind": "/usr/local/stata/stata.lic",
            "mode": "ro",
        }

    cli.images.pull(image_reference)

    entrypoint, command, sub_dir, home_dir = _infer_run_command(submission, stage)
    project_dir = get_project_dir(submission)
    logging.info("Setting working directory to: " + os.path.join(project_dir, sub_dir))
    logging.info("Running Tale with command: " + " ".join(entrypoint + [command]))

    container = cli.containers.create(
        image=image_reference,
        entrypoint=entrypoint,
        command=command,
        detach=True,
        volumes=volumes,
        working_dir=os.path.join(project_dir, sub_dir),
        user=f"{os.getuid()}:{os.getgid()}",
        environment={
            "HOME": home_dir,
            "R_LIBS": os.path.join(home_dir, "R", "library"),
            "R_LIBS_USER": os.path.join(home_dir, "R", "library"),
            "MLM_LICENSE_FILE": "27007@rtlicense1.uits.indiana.edu",
        },
    )

    logging_thread = Thread(target=logging_worker, args=(log_queue, container))
    with tempfile.TemporaryDirectory() as container_temp_path:
        dstats_tmppath = os.path.join(container_temp_path, "dockerstats")
        stats_thread = DockerStatsCollectorThread(container, dstats_tmppath)
        publisher = LogPublisher(container.name, f"docker:logs:{creator_id}")

        # Job output must come from stdout/stderr
        container.start()
        stats_thread.start()
        logging_thread.start()
        publisher.start()

        try:
            container = cli.containers.get(container.id)
            while container.status == "running":
                while not log_queue.empty():
                    print(log_queue.get_nowait(), flush=True)
                if task.canceled:
                    stop_container(container)
                    break
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
        if os.path.isfile(dstats_tmppath + ".csv"):
            df = pd.read_csv(dstats_tmppath + ".csv")
            performance_data.update(
                {
                    "MaxCPUPercent": df["CPU %"].max(),
                    "MaxMemoryUsage": df["Memory Usage"].max(),
                }
            )
        pdata = io.BytesIO(json.dumps(performance_data, cls=NpEncoder).encode("utf-8"))
        fobj = Upload().uploadFromFile(
            pdata,
            pdata.getbuffer().nbytes,
            f"performance_data_stage_{stage_num}.json",
            parentType="folder",
            parent=submission_folder,
            user=admin,
            mimeType="text/plain",
        )
        annotate_item_type(fobj, "performance_data")
        logging.info("Performance data collected and uploaded.")

        # Dump run std{out,err} and entrypoint used.
        main_file = stage["main_file"]
        log_files = {}
        for stdout, stderr, key in [
            (True, False, "stdout"),
            (False, True, "stderr"),
            (None, None, "dockerstats"),
        ]:
            log_file = f"/tmp/{key}-{submission['job_id']}"
            log_obj = None
            meta_key = f"{key}_file_id"
            if submission_folder["meta"].get(meta_key) is not None:
                log_obj = File().load(submission_folder["meta"][meta_key], force=True)
                with File().open(log_obj) as f:
                    with open(log_file, "wb") as out_f:
                        _dump_from_fileobj(f, out_f)

            if key != "dockerstats":
                container_log_path = os.path.join(container_temp_path, key)
                with open(container_log_path, "wb") as fp:
                    fp.write(container.logs(stdout=stdout, stderr=stderr))

            target_file = os.path.join(container_temp_path, key)
            if not os.path.isfile(target_file):
                print(f"{key} file not found, skipping...")
                continue
            if key == "stdout" and os.path.getsize(target_file) == 0:
                main_file_noext = os.path.splitext(main_file)[0]
                logfile = None
                if main_file.endswith(".R"):
                    logfile = main_file_noext + ".Rout"
                elif main_file.endswith(".do") or is_stata(image_reference):
                    logfile = main_file_noext + ".log"
                else:
                    logging.info(
                        f"Cannot infer log file for main file {main_file}, skipping..."
                    )

                if logfile:
                    # find .Rout files if stdout is empty and R
                    for root, dirs, files in os.walk(project_dir):
                        for file in files:
                            if file == logfile:
                                target_file = os.path.join(root, file)
                                break

            stage_stamp = f"\n\n===== Stage {stage_num} Output =====\n\n"
            with open(target_file, "rb") as fp:
                with open(log_file, "ab") as out_f:
                    out_f.write(stage_stamp.encode("utf-8"))
                    _dump_from_fileobj(fp, out_f)

            with open(log_file, "rb") as fp:
                log_files[key] = target_file
                if not log_obj:
                    fobj = Upload().uploadFromFile(
                        fp,
                        os.path.getsize(fp.name),
                        os.path.basename(fp.name),
                        parentType="folder",
                        parent=submission_folder,
                        user=admin,
                        mimeType="text/plain",
                    )
                    Folder().setMetadata(
                        submission_folder, {key + "_file_id": str(fobj["_id"])}
                    )
                    annotate_item_type(fobj, key)
                else:
                    _update_file_from_path(log_obj, log_file, admin)
                    annotate_item_type(log_obj, key)
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
        elif is_stata(image_reference):
            with open(log_files["stdout"], "r") as fp:
                log_content = fp.read()
                if stata_err := stata_error(log_content):
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
