import logging
import math
import os
import queue
import time
from threading import Thread

import docker
import requests
from girder.models.folder import Folder
from girder.models.upload import Upload
from girder.models.user import User


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
                line = (
                    f"{ts} - {self.calculate_cpu_percent(d):.2f}%, {mem_usage} / {mem_limit},"
                    f" {bytes_in} / {bytes_out}, {blkio_rd} / {blkio_wr},"
                    f" {d.get('pids_stats', {}).get('current', 0)}\n"
                )
                with open(self.output_path, mode="a") as fp:
                    fp.write(line)
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
        cpu_count = len(d["cpu_stats"]["cpu_usage"].get("percpu_usage", [1]))
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

    def calculate_blkio_bytes(self, d):
        bytes_stats = d.get("blkio_stats", {}).get("io_service_bytes_recursive")
        if not bytes_stats:
            return 0, 0
        rd = wr = 0
        for s in bytes_stats:
            if s["op"] == "Read":
                rd += s["value"]
            elif s["op"] == "Write":
                wr += s["value"]
        return self.convert_size(rd, binary=False), self.convert_size(wr, binary=False)

    def calculate_network_bytes(self, d):
        networks = d.get("networks")
        if not networks:
            return 0, 0
        rx = tx = 0
        for data in networks.values():
            rx += data["rx_bytes"]
            tx += data["tx_bytes"]
        return self.convert_size(rx, binary=False), self.convert_size(tx, binary=False)

    def calculate_memory(self, d):
        memory = d.get("memory_stats")
        if not memory:
            return 0, 0
        return self.convert_size(
            memory.get("usage", 0), binary=True
        ), self.convert_size(memory.get("limit", 0), binary=True)


class DummyTask:
    canceled = False


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


def _infer_run_command(submission):
    temp_dir = submission["temp_dir"]
    entrypoint = ["/bin/sh", "-c"]

    # check if temp_dir contains a single folder
    items = os.listdir(temp_dir)
    sub_dir = ""
    if len(items) == 1 and os.path.isdir(os.path.join(temp_dir, items[0])):
        sub_dir = items[0]

    command = None
    if os.path.exists(os.path.join(temp_dir, sub_dir, "run.sh")):
        command = os.path.join("/workspace", sub_dir, "run.sh")
    elif os.path.exists(os.path.join(temp_dir, sub_dir, "code", "run.sh")):
        command = os.path.join("/workspace", sub_dir, "code", "run.sh")
    else:
        raise ValueError("Cannot infer run command for submission")
    os.chmod(os.path.join(temp_dir, sub_dir, "run.sh"), 0o755)
    return entrypoint, command


def recorded_run(submission, task=None):
    cli = docker.from_env()

    def logging_worker(log_queue, container):
        for line in container.logs(stream=True):
            log_queue.put(line.decode("utf-8").strip(), block=False)

    task = task or DummyTask
    log_queue = queue.Queue()
    print("Starting recorded run")

    submission_folder = Folder().load(submission["folder_id"], force=True)
    admin = User().findOne({"admin": True})

    image_tag = submission_folder["meta"]["image_tag"]
    volumes = {
        submission["temp_dir"]: {
            "bind": "/workspace",
            "mode": "rw",
        }
    }

    entrypoint, command = _infer_run_command(submission)

    print("Running Tale with command: " + " ".join(entrypoint + [command]))

    cli.images.pull(image_tag)

    container = cli.containers.create(
        image=image_tag,
        entrypoint=entrypoint,
        command=command,
        detach=True,
        volumes=volumes,
        working_dir="/workspace",
    )

    logging_thread = Thread(target=logging_worker, args=(log_queue, container))
    container_temp_path = os.path.join("/tmp", container.name)
    os.makedirs(container_temp_path, exist_ok=False)
    dstats_tmppath = os.path.join(container_temp_path, "docker_stats")
    stats_thread = DockerStatsCollectorThread(container, dstats_tmppath)

    # Job output must come from stdout/stderr
    container.start()
    stats_thread.start()
    logging_thread.start()

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

    if task.canceled:
        ret = {"StatusCode": -123}
    else:
        ret = container.wait()

    # Dump run std{out,err} and entrypoint used.
    meta = {}
    for stdout, stderr, key in [
        (True, False, "stdout"),
        (False, True, "stderr"),
    ]:
        with open(os.path.join(container_temp_path, key), "wb") as fp:
            fp.write(container.logs(stdout=stdout, stderr=stderr))

        with open(os.path.join(container_temp_path, key), "rb") as fp:
            fobj = Upload().uploadFromFile(
                fp,
                os.path.getsize(fp.name),
                os.path.basename(fp.name),
                parentType="folder",
                parent=submission_folder,
                user=admin,
            )
            meta[key + "_file_id"] = str(fobj["_id"])
    Folder().setMetadata(submission_folder, meta)
    try:
        container.remove()
    except docker.errors.NotFound:
        pass

    if not task.canceled and ret["StatusCode"] != 0:
        raise ValueError(
            "Error executing recorded run. Check stdout/stderr for details."
        )

    return ret
