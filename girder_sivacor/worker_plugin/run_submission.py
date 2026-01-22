import datetime
import json
import os
import pathlib
import shutil
import tarfile
import tempfile
import zipfile
from functools import wraps
from importlib.metadata import version
from zoneinfo import ZoneInfo

import randomname
from girder.constants import AccessType
from girder.models.collection import Collection
from girder.models.file import File
from girder.models.folder import Folder
from girder.models.group import Group
from girder.models.item import Item
from girder.models.setting import Setting
from girder.models.upload import Upload
from girder.models.user import User
from girder_jobs.constants import JobStatus
from girder_jobs.models.job import Job
from girder_worker.app import app
from tro_utils.tro_utils import TRO

from ..settings import PluginSettings
from .lib import (
    _dump_from_fileobj,
    _update_file_from_path,
    annotate_item_type,
    get_project_dir,
    recorded_run,
    zip_symlink,
)

IGNORE_DIRS = [".git", "__pycache__"]


def timestamp():
    zone = ZoneInfo("America/Chicago")
    return f"[{datetime.datetime.now().astimezone(zone).replace(microsecond=0).isoformat()}]"


def job_check(task):
    @wraps(task)
    def inner(self, *args, **kwargs):
        if len(args) > 0 and isinstance(args[0], dict) and args[0].get("job_id"):
            job = Job().load(args[0]["job_id"], force=True)
            if job["status"] != JobStatus.RUNNING:
                if self.request.chain:
                    self.request.chain = None
                return {"job_id": str(args[0]["job_id"])}
        return task(self, *args, **kwargs)

    return inner


def safe_tar_extract(tar, path):
    root = os.path.abspath(path)
    for member in tar.getmembers():
        target = os.path.abspath(os.path.join(root, member.name))
        if not target.startswith(root):
            raise Exception("Attempted Path Traversal in Tar File: " + member.name)

        if member.issym() or member.islnk():
            raise Exception("Tar File contains unsafe links: " + member.name)

    tar.extractall(root, members=tar.getmembers(), filter="data")


def _create_submission_folder(user):
    # Logic to create submission directory based on fileId and image_tag
    admin = User().findOne({"admin": True})
    root_collection = Collection().createCollection(
        Setting().get(PluginSettings.SUBMISSION_COLLECTION_NAME),
        creator=admin,
        public=True,
        reuseExisting=True,
    )
    editors_group_name = Setting().get(PluginSettings.EDITORS_GROUP_NAME)
    editors_group = Group().findOne({"name": editors_group_name})
    if editors_group is None:
        editors_group = Group().createGroup(editors_group_name, admin, public=True)

    Collection().setGroupAccess(
        root_collection, editors_group, AccessType.READ, save=True, currentUser=admin
    )

    submission_folder = Folder().createFolder(
        root_collection,
        randomname.get_name(),
        parentType="collection",
        public=False,
        creator=admin,
        reuseExisting=False,
    )
    submission_folder = Folder().setMetadata(
        submission_folder,
        {"creator_id": str(user["_id"])},
    )
    return Folder().setUserAccess(
        submission_folder,
        user,
        AccessType.READ,
        save=True,
    )


@app.task(queue="local")
def cleanup_submission(submission_folder_id):
    folder = Folder().load(submission_folder_id, force=True)
    for item in Folder().childItems(
        folder, filters={"size": {"$gt": Setting().get(PluginSettings.MAX_ITEM_SIZE)}}
    ):
        Item().remove(item)


@app.task(queue="local", bind=True)
@job_check
def prepare_submission(task, userId, fileId, stages, job_id):
    # Create a submission directory
    job = Job().load(job_id, force=True)
    try:
        user = User().load(userId, force=True)
        submission_folder = _create_submission_folder(user)
        # Move file to the submission directory
        fobj = File().load(fileId, user=user, level=AccessType.READ)
        annotate_item_type(fobj, "submission_file")
        item = Item().load(fobj["itemId"], user=user, level=AccessType.READ)
        Item().move(item, submission_folder)
        Folder().setMetadata(
            submission_folder,
            {
                "stages": stages,
                "status": "submitted",
                "job_id": str(job["_id"]),
            },
        )
        cleanup_submission.apply_async(
            args=(str(submission_folder["_id"]),),
            kwargs={
                "girder_job_title": "Cleanup submission folder",
            },
            countdown=Setting().get(PluginSettings.RETENTION_DAYS) * 86400,
        )
        Job().updateJob(
            job,
            f"{timestamp()} New submission: '" + submission_folder["name"] + "' created.\n",
            status=JobStatus.RUNNING,
        )
        return {
            "folder_id": str(submission_folder["_id"]),
            "file_id": str(fobj["_id"]),
            "job_id": str(job_id),
            "stages": stages,
        }
    except Exception as exc:
        Job().updateJob(
            job,
            f"{timestamp()} Failed to prepare submission: \n\t" + str(exc) + "\n",
            status=JobStatus.ERROR,
        )
        raise exc
    return {
        "folder_id": None,
        "file_id": None,
        "stages": None,
        "job_id": str(job_id),
    }


@app.task(queue="local", bind=True)
@job_check
def create_workspace(task, submission):
    job = Job().load(submission["job_id"], force=True)
    job = Job().updateJob(
        job,
        f"{timestamp()} Creating workspace from source folder.\n",
        status=JobStatus.RUNNING,
    )

    try:
        temp_dir = f"/tmp/workspace-{submission['folder_id']}"
        submission["temp_dir"] = temp_dir
        project_dir = get_project_dir(submission)
        os.makedirs(project_dir, exist_ok=False)
        # Ensure R library directory for user install.packages exists
        for stage in submission.get("stages", []):
            if stage["image_name"].startswith("rocker/"):
                os.makedirs(os.path.join(temp_dir, "R", "library"), exist_ok=True)

        fobj = File().load(submission["file_id"], force=True)
        temp_filename = os.path.join(temp_dir, fobj["name"])
        with File().open(fobj) as f:
            with open(temp_filename, "wb") as out_f:
                _dump_from_fileobj(f, out_f)
        # File is either a zip or tar archive; extract accordingly
        extracted = False
        try:
            if zipfile.is_zipfile(temp_filename):
                with zipfile.ZipFile(temp_filename, "r") as zip_ref:
                    zip_ref.extractall(project_dir)
                extracted = True
                print("Extracted as a zip file.")
        except zipfile.BadZipFile:
            print("Not a zip file, trying tar...")

        try:
            if tarfile.is_tarfile(temp_filename) and not extracted:
                with tarfile.open(temp_filename, "r:*") as tar_ref:
                    safe_tar_extract(tar_ref, project_dir)
                extracted = True
                print("Extracted as a tar file.")
        except tarfile.TarError as e:
            print(f"Not a tar file either... Reason: {e}")
            raise ValueError("Unsupported file format for workspace creation.")

    except Exception as exc:
        os.remove(temp_filename)
        Job().updateJob(
            job,
            f"{timestamp()} Failed to create workspace: \n\t" + str(exc) + "\n",
            status=JobStatus.ERROR,
        )
        raise exc

    os.remove(temp_filename)
    return submission


@app.task(queue="local", bind=True)
@job_check
def run_tro(task, submission, action, inumber):
    job = Job().load(submission["job_id"], force=True)
    job = Job().updateJob(
        job,
        f"{timestamp()} Running TRO utilities in the workspace. ({action})\n",
        status=JobStatus.RUNNING,
    )
    try:
        admin = User().findOne({"admin": True})
        submission_folder = Folder().load(submission["folder_id"], force=True)
        temp_dir = submission["temp_dir"]
        tro_file = os.path.join(temp_dir, f"tro-{submission['job_id']}.jsonld")
        tro_obj = None
        if submission.get("troId") is not None:
            tro_obj = File().load(submission["troId"], force=True)
            with File().open(tro_obj) as f:
                with open(tro_file, "wb") as out_f:
                    _dump_from_fileobj(f, out_f)

        with tempfile.NamedTemporaryFile(delete=True) as profile:
            trs_profile = Setting().get(PluginSettings.TRO_PROFILE)
            trs_profile["sivacor_version"] = version("girder_sivacor")
            profile.write(json.dumps(trs_profile).encode())
            profile.seek(0)
            tro = TRO(
                filepath=tro_file,
                gpg_fingerprint=Setting().get(PluginSettings.TRO_GPG_FINGERPRINT),
                gpg_passphrase=Setting().get(PluginSettings.TRO_GPG_PASSPHRASE),
                profile=profile.name,
                tro_creator="SIVACOR/tro_utils",
                tro_name=submission_folder["name"],
                tro_description="SIVACOR Run",
            )

        meta = {}
        project_dir = get_project_dir(submission)
        if action == "add_arrangement":
            arrangements = tro.list_arrangements()
            if not arrangements:
                tro.add_arrangement(
                    project_dir,
                    comment="Before executing workflow",
                    ignore_dirs=IGNORE_DIRS,
                )
            else:
                tro.add_arrangement(
                    project_dir,
                    comment=f"After executing workflow step {inumber}",
                    ignore_dirs=IGNORE_DIRS,
                    resolve_symlinks=False,
                )
        elif action == "add_performance":
            stages = submission.get("stages", [])
            main_file = stages[inumber].get("main_file", "unknown")
            runs = submission.get("runs", [])
            run = runs[-1] if runs else {}
            extra_attributes = None
            for item in Folder().childItems(
                submission_folder,
                filters={"name": f"performance_data_stage_{inumber + 1}.json"},
                limit=1,
            ):
                with Item().childFiles(item) as files:
                    for fobj in files:
                        with File().open(fobj) as f:
                            extra_attributes = json.load(f)
            tro.add_performance(
                datetime.datetime.fromisoformat(run["run_start_time"]),
                datetime.datetime.fromisoformat(run["run_end_time"]),
                comment=f"SIVACOR workflow execution ({main_file}) step {inumber + 1}",
                accessed_arrangement=f"arrangement/{inumber}",
                modified_arrangement=f"arrangement/{inumber + 1}",
                caps=run.get("run_caps", []),
                extra_attributes=extra_attributes,
            )
        elif action == "sign":
            tro.request_timestamp()

            for meta_key, filename, nice_name in zip(
                ("sig_file_id", "tsr_file_id"),
                (tro.sig_filename, tro.tsr_filename),
                ("tro_signature", "tro_timestamp"),
            ):
                with open(filename, "rb") as f:
                    fobj = Upload().uploadFromFile(
                        f,
                        os.path.getsize(filename),
                        os.path.basename(filename),
                        parentType="folder",
                        parent=submission_folder,
                        user=admin,
                        mimeType="text/plain",
                    )
                    annotate_item_type(fobj, nice_name)
                    meta[meta_key] = str(fobj["_id"])
                os.remove(filename)

        tro.save()
        if tro_obj:
            _update_file_from_path(tro_obj, tro.tro_filename, admin)
        else:
            tro_obj = Upload().uploadFromFile(
                open(tro.tro_filename, "rb"),
                os.path.getsize(tro.tro_filename),
                os.path.basename(tro.tro_filename),
                parentType="folder",
                parent=submission_folder,
                user=admin,
                mimeType="application/ld+json",
            )
            annotate_item_type(tro_obj, "tro_declaration")
            submission["troId"] = str(tro_obj["_id"])
        os.remove(tro.tro_filename)
        meta["tro_file_id"] = str(tro_obj["_id"])
        Folder().setMetadata(submission_folder, meta)
    except Exception as exc:
        Job().updateJob(
            job,
            f"{timestamp()} Failed to run TRO utilities: \n\t" + str(exc) + "\n",
            status=JobStatus.ERROR,
        )
        raise exc

    return submission


@app.task(queue="local", bind=True)
@job_check
def execute_workflow(task, submission, stage):
    job = Job().load(submission["job_id"], force=True)
    job = Job().updateJob(
        job,
        f"{timestamp()} Executing workflow on workspace.\n",
        status=JobStatus.RUNNING,
    )
    try:
        # Placeholder for actual workflow execution logic

        start_time = datetime.datetime.now()
        ret = recorded_run(submission, stage, task=task)
        if ret["StatusCode"] == -123:
            print("Termination requested, stopping execution.")
            if task.request.chain:
                task.request.chain = None
            return {"job_id": submission["job_id"]}

        if ret["StatusCode"] != 0:
            raise RuntimeError(
                f"Workflow execution failed with code {ret['StatusCode']}"
            )
        end_time = datetime.datetime.now()

        if submission.get("runs") is None:
            submission["runs"] = []
        submission["runs"].append(
            {
                "run_start_time": start_time.isoformat(),
                "run_end_time": end_time.isoformat(),
                "run_caps": ["trov:InternetIsolation"],
            }
        )
    except Exception as exc:
        Job().updateJob(
            job,
            f"{timestamp()} Failed to execute workflow: \n\t" + str(exc) + "\n",
            status=JobStatus.ERROR,
        )
        raise exc

    return submission


@app.task(queue="local", bind=True)
@job_check
def upload_workspace(task, submission):
    # Upload the modified workspace back to Girder as a zip file
    # called 'executed_replication_package.zip'
    job = Job().load(submission["job_id"], force=True)
    job = Job().updateJob(
        job,
        f"{timestamp()} Uploading executed replication package to Girder.\n",
        status=JobStatus.RUNNING,
    )
    try:
        admin = User().findOne({"admin": True})
        submission_folder = Folder().load(submission["folder_id"], force=True)

        submission_fobj = File().load(submission["file_id"], force=True)
        zip_basename = (
            pathlib.Path(submission_fobj["name"]).stem + f"-{submission['job_id']}.zip"
        )
        project_dir = get_project_dir(submission)

        zip_path = os.path.join(submission["temp_dir"], zip_basename)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(project_dir):
                # ignore contents of dirs from IGNORE_DIRS
                dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
                for file in files:
                    if file == zip_basename:
                        continue
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, submission["temp_dir"])
                    if os.path.islink(file_path):
                        zip_symlink(zipf, file_path, arcname=arcname)
                    else:
                        zipf.write(file_path, arcname)

            # Store TRO files in a separate 'tro/' directory within the zip
            for key in ("tro_file_id", "sig_file_id", "tsr_file_id"):
                if fobj := File().load(
                    submission_folder.get("meta", {}).get(key), force=True
                ):
                    with File().open(fobj) as fp:  # TODO: use _dump_from_fileobj?
                        _dump_from_fileobj(
                            fp, zipf, is_zip=True, arcname="tro/" + fobj["name"]
                        )

            for ext in (".jsonld", ".sig", ".tsr"):
                basename = f"tro-{submission['job_id']}.{ext}"
                tro_file_path = os.path.join("/tmp", basename)
                arcname = "tro/" + basename
                if os.path.exists(tro_file_path):
                    zipf.write(tro_file_path, arcname)

            # Store stdout and stderr logs
            for key in ("stderr_file_id", "stdout_file_id"):
                if fobj := File().load(
                    submission_folder.get("meta", {}).get(key), force=True
                ):
                    with File().open(fobj) as fp:
                        _dump_from_fileobj(fp, zipf, is_zip=True)

        with open(zip_path, "rb") as f:
            fobj = Upload().uploadFromFile(
                f,
                os.path.getsize(zip_path),
                zip_basename,
                parentType="folder",
                parent=submission_folder,
                user=admin,
                mimeType="application/zip",
            )
            annotate_item_type(fobj, "replicated_package")
        os.remove(zip_path)
        Folder().setMetadata(submission_folder, {"replpack_file_id": str(fobj["_id"])})
        submission["replpack_file_id"] = str(fobj["_id"])
    except Exception as exc:
        Job().updateJob(
            job,
            f"{timestamp()} Failed to upload executed replication package: \n\t" + str(exc) + "\n",
            status=JobStatus.ERROR,
        )
        raise exc

    return submission


@app.task(queue="local")
def finalize_job(submission):
    shutil.rmtree(submission["temp_dir"], ignore_errors=True)
    job = Job().load(submission["job_id"], force=True)
    if job["status"] == JobStatus.RUNNING:
        job = Job().updateJob(
            job,
            f"{timestamp()} Submission job finalized successfully.\n",
            status=JobStatus.SUCCESS,
        )
    return submission
