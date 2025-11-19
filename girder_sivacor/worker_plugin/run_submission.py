import datetime
import io
import json
import os
import shutil
import tarfile
import tempfile
import zipfile
import pathlib

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
from girder.utility import RequestBodyStream
from girder_jobs.constants import JobStatus
from girder_jobs.models.job import Job
from girder_worker.app import app
from tro_utils.tro_utils import TRO

from ..settings import PluginSettings
from .lib import recorded_run, zip_symlink

IGNORE_DIRS = [".git", "__pycache__"]


def _create_submission_directory(user):
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
    return Folder().setUserAccess(
        submission_folder,
        user,
        AccessType.READ,
        save=True,
    )


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


@app.task(queue="local")
def prepare_submission(userId, fileId, image_tag, main_file, job_id):
    # Create a submission directory
    job = Job().load(job_id, force=True)
    try:
        user = User().load(userId, force=True)
        submission_folder = _create_submission_directory(user)
        # Move file to the submission directory
        fobj = File().load(fileId, user=user, level=AccessType.READ)
        item = Item().load(fobj["itemId"], user=user, level=AccessType.READ)
        Item().move(item, submission_folder)
        Folder().setMetadata(
            submission_folder,
            {"image_tag": image_tag, "status": "submitted", "job_id": job_id},
        )
        return {
            "folder_id": str(submission_folder["_id"]),
            "file_id": str(fobj["_id"]),
            "job_id": str(job_id),
            "main_file": main_file,
        }
    except Exception as exc:
        Job().updateJob(
            job,
            "Failed to prepare submission: \n\t" + str(exc) + "\n",
            status=JobStatus.ERROR,
        )
        raise exc
    return {
        "folder_id": None,
        "file_id": None,
        "main_file": main_file,
        "job_id": str(job_id),
    }


@app.task(queue="local")
def create_workspace(submission):
    job = Job().load(submission["job_id"], force=True)
    job = Job().updateJob(
        job,
        "Creating workspace from source folder.\n",
        status=JobStatus.RUNNING,
    )

    try:
        temp_dir = f"/tmp/workspace-{submission['folder_id']}"
        submission["temp_dir"] = temp_dir
        os.makedirs(temp_dir, exist_ok=False)
        fobj = File().load(submission["file_id"], force=True)
        with File().open(fobj) as f:
            with open(os.path.join(temp_dir, fobj["name"]), "wb") as out_f:
                out_f.write(f.read())
        # File is either a zip or tar archive; extract accordingly
        if fobj["name"].endswith(".zip"):
            with zipfile.ZipFile(os.path.join(temp_dir, fobj["name"]), "r") as zip_ref:
                zip_ref.extractall(temp_dir)
        elif fobj["name"].endswith((".tar.gz", ".tgz")):
            with tarfile.open(os.path.join(temp_dir, fobj["name"]), "r:gz") as tar_ref:
                tar_ref.extractall(temp_dir)
        else:
            raise ValueError("Unsupported file format for workspace creation.")
        os.remove(
            os.path.join(temp_dir, fobj["name"])
        )  # remove the archive file after extraction
    except Exception as exc:
        Job().updateJob(
            job,
            "Failed to create workspace: \n\t" + str(exc) + "\n",
            status=JobStatus.ERROR,
        )
        raise exc

    return submission


@app.task(queue="local")
def run_tro(submission, action):
    job = Job().load(submission["job_id"], force=True)
    job = Job().updateJob(
        job,
        f"Running TRO utilities in the workspace. ({action})\n",
        status=JobStatus.RUNNING,
    )
    try:
        admin = User().findOne({"admin": True})
        submission_folder = Folder().load(submission["folder_id"], force=True)
        tro_file = f"/tmp/tro-{submission['job_id']}.jsonld"
        tro_obj = None
        if submission.get("troId") is not None:
            tro_obj = File().load(submission["troId"], force=True)
            with open(f"/tmp/{tro_obj['name']}", "wb") as out_f:
                with File().open(tro_obj) as f:
                    out_f.write(f.read())
            tro_file = f"/tmp/{tro_obj['name']}"

        temp_dir = submission["temp_dir"]

        with tempfile.NamedTemporaryFile(delete=True) as profile:
            profile.write(
                json.dumps(Setting().get(PluginSettings.TRO_PROFILE)).encode()
            )
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

        if action == "add_arrangement":
            arrangements = tro.list_arrangements()
            if not arrangements:
                tro.add_arrangement(
                    temp_dir,
                    comment="Before executing workflow",
                    ignore_dirs=IGNORE_DIRS,
                )
            else:
                tro.add_arrangement(
                    temp_dir,
                    comment="After executing workflow",
                    ignore_dirs=IGNORE_DIRS,
                    resolve_symlinks=False,
                )
        elif action == "add_performance":
            tro.add_performance(
                datetime.datetime.fromisoformat(submission["run_start_time"]),
                datetime.datetime.fromisoformat(submission["run_end_time"]),
                comment=f"SIVACOR workflow execution ({submission['main_file']})",
                accessed_arrangement="arrangement/0",
                modified_arrangement="arrangement/1",
                caps=submission.get("run_caps", ["trov:InternetIsolation"]),
            )
        elif action == "sign":
            tro.request_timestamp()

            for meta_key, filename in zip(
                ("sig_file_id", "tsr_file_id"), (tro.sig_filename, tro.tsr_filename)
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
            submission["troId"] = str(tro_obj["_id"])
        os.remove(tro.tro_filename)
        meta["tro_file_id"] = str(tro_obj["_id"])
        Folder().setMetadata(submission_folder, meta)
    except Exception as exc:
        Job().updateJob(
            job,
            "Failed to run TRO utilities: \n\t" + str(exc) + "\n",
            status=JobStatus.ERROR,
        )
        raise exc

    return submission


@app.task(queue="local", bind=True)
def execute_workflow(task, submission):
    job = Job().load(submission["job_id"], force=True)
    job = Job().updateJob(
        job,
        "Executing workflow on workspace.\n",
        status=JobStatus.RUNNING,
    )
    try:
        # Placeholder for actual workflow execution logic

        start_time = datetime.datetime.now()
        ret = recorded_run(submission, task)
        if ret["StatusCode"] != 0:
            raise RuntimeError(
                f"Workflow execution failed with code {ret['StatusCode']}"
            )
        end_time = datetime.datetime.now()

        submission["run_start_time"] = start_time.isoformat()
        submission["run_end_time"] = end_time.isoformat()
        submission["run_caps"] = ["trov:InternetIsolation"]
    except Exception as exc:
        Job().updateJob(
            job,
            "Failed to execute workflow: \n\t" + str(exc) + "\n",
            status=JobStatus.ERROR,
        )
        raise exc

    return submission


@app.task(queue="local")
def upload_workspace(submission):
    # Upload the modified workspace back to Girder as a zip file
    # called 'executed_replication_package.zip'
    job = Job().load(submission["job_id"], force=True)
    job = Job().updateJob(
        job,
        "Uploading executed replication package to Girder.\n",
        status=JobStatus.RUNNING,
    )
    try:
        admin = User().findOne({"admin": True})
        submission_folder = Folder().load(submission["folder_id"], force=True)

        submission_fobj = File().load(submission["file_id"], force=True)
        zip_basename = (
            pathlib.Path(submission_fobj["name"]).stem + f"-{submission['job_id']}.zip"
        )

        zip_path = os.path.join(submission["temp_dir"], zip_basename)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(submission["temp_dir"]):
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
                    with File().open(fobj) as fp:
                        zipf.writestr("tro/" + fobj["name"], fp.read())

            for ext in (".jsonld", ".sig", ".tsr"):
                basename = f"tro-{submission['job_id']}.{ext}"
                tro_file_path = os.path.join("/tmp", basename)
                arcname = "tro/" + basename
                if os.path.exists(tro_file_path):
                    zipf.write(tro_file_path, arcname)

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
        os.remove(zip_path)
        Folder().setMetadata(submission_folder, {"replpack_file_id": str(fobj["_id"])})
        submission["replpack_file_id"] = str(fobj["_id"])
    except Exception as exc:
        Job().updateJob(
            job,
            "Failed to upload executed replication package: \n\t" + str(exc) + "\n",
            status=JobStatus.ERROR,
        )
        raise exc

    return submission


@app.task(queue="local")
def finalize_job(submission):
    shutil.rmtree(submission["temp_dir"], ignore_errors=True)
    job = Job().load(submission["job_id"], force=True)
    job = Job().updateJob(
        job,
        "Submission job finalized successfully.\n",
        status=JobStatus.SUCCESS,
    )
    return submission
