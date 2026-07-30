import copy
import datetime
import json
import logging
import os
import pathlib
import shutil
import tarfile
import tempfile
import zipfile
from functools import wraps
from importlib.metadata import version
from zoneinfo import ZoneInfo

import pathspec
import posix1e
import randomname
from celery.signals import worker_ready
from girder.constants import AccessType
from girder_worker.app import app
from girder_worker.utils import JobStatus
from tro_utils import TRPAttribute
from tro_utils.tro_utils import TRO

from ..settings import PluginSettings
from .girder_api import GirderApi, dump_to_zip
from .lib import (
    get_project_dir,
    reap_orphaned_containers,
    recorded_run,
    zip_symlink,
)
from .routing import DISPATCH_QUEUE, LOCAL_QUEUE, pin_chain, worker_queue

IGNORE_DIRS = [".git", "__pycache__"]
DEFAULT_SIVACOR_IGNORE = [
    # Python
    "__pycache__/",
    "*.py[cod]",
    "*$py.class",
    ".venv/",
    "venv/",
    ".env",
    # Version Control & IDEs
    ".git/",
    ".svn/",
    ".idea/",
    ".vscode/",
    "*.swp",
    ".DS_Store",  # macOS noise
    "Thumbs.db",  # Windows noise
    # Common Research/Data temporary files
    "*.tmp",
    "node_modules/",
    # Jupyter Notebook checkpoints
    ".ipynb_checkpoints/",
    # Stata/R temporary files
    "*.smcl",
    ".Rhistory",
    ".RData",
    ".Ruserdata",
]
logger = logging.getLogger(__name__)


def timestamp():
    zone = ZoneInfo("America/Chicago")
    return f"[{datetime.datetime.now().astimezone(zone).replace(microsecond=0).isoformat()}]"


def report(api, job_id, message, status=JobStatus.RUNNING):
    """Append a timestamped line to the submission's Girder job log."""
    api.update_job(job_id, log=f"{timestamp()} {message}\n", status=status)


def report_failure(api, job_id, failure, exc):
    """Mark the job failed, without letting that replace ``exc``.

    Reporting is itself a REST call and has its own ways to fail -- most
    routinely, a job the user cancelled rejects the transition to ERROR. The
    original exception is the one worth propagating.
    """
    try:
        report(api, job_id, f"{failure}: \n\t{exc}", status=JobStatus.ERROR)
    except Exception:
        logger.exception("Could not mark job %s as failed", job_id)


def discard_workspace(submission):
    """Delete the scratch directories of a submission that will not continue.

    While the worker shared Girder's database this was a side effect of the
    ``jobs.job.update.after`` handler. That handler now runs on the server,
    which cannot see these paths, so every exit from the pipeline has to do it
    itself.
    """
    for key in ("tmp_dir", "workspace_dir"):
        if path := submission.get(key):
            shutil.rmtree(path, ignore_errors=True)


def submission_task(failure):
    """Wrap a pipeline step in its Girder job bookkeeping.

    Each step used to open with a job lookup and close with a near-identical
    ``except`` clause that logged ``failure`` and re-raised. Doing it once here
    also gives the two exits from a submission -- a job that is no longer
    running, and a step that raised -- a single place to drop the workspace.

    The wrapped function is called as ``func(task, api, submission, ...)``.
    """

    def decorator(func):
        @wraps(func)
        def inner(task, submission, *args, **kwargs):
            api = GirderApi.for_task(task)
            job_id = submission["job_id"]
            if api.job(job_id)["status"] != JobStatus.RUNNING:
                return abandon(task, submission)
            try:
                return func(task, api, submission, *args, **kwargs)
            except Exception as exc:
                report_failure(api, job_id, failure, exc)
                discard_workspace(submission)
                raise

        return inner

    return decorator


def abandon(task, submission):
    """Drop the rest of the chain and clean up after a cancelled submission."""
    if task.request.chain:
        task.request.chain = None
    discard_workspace(submission)
    return {"job_id": str(submission["job_id"])}


def safe_tar_extract(tar, path):
    root = os.path.abspath(path)
    for member in tar.getmembers():
        target = os.path.abspath(os.path.join(root, member.name))
        if not target.startswith(root):
            raise Exception("Attempted Path Traversal in Tar File: " + member.name)

        if member.issym() or member.islnk():
            raise Exception("Tar File contains unsafe links: " + member.name)

    tar.extractall(root, members=tar.getmembers(), filter="data")


def _create_submission_folder(api, user_id):
    # Logic to create submission directory based on fileId and image_tag
    settings = api.settings(
        [
            PluginSettings.SUBMISSION_COLLECTION_NAME,
            PluginSettings.EDITORS_GROUP_NAME,
        ]
    )
    root_collection = api.find_or_create_collection(
        settings[PluginSettings.SUBMISSION_COLLECTION_NAME]
    )
    editors_group = api.find_or_create_group(
        settings[PluginSettings.EDITORS_GROUP_NAME]
    )
    api.grant_group_access(
        root_collection["_id"], editors_group["_id"], AccessType.READ
    )

    submission_folder = api.create_folder(
        root_collection["_id"], randomname.get_name(), public=False
    )
    api.set_folder_metadata(submission_folder["_id"], {"creator_id": str(user_id)})
    api.grant_user_access(submission_folder["_id"], user_id, AccessType.READ)
    return submission_folder


def _matlab_perms(target_path, uid=1001):
    for is_default in [False, True]:
        try:
            acl_type = (
                posix1e.ACL_TYPE_DEFAULT if is_default else posix1e.ACL_TYPE_ACCESS
            )
            acl = (
                posix1e.ACL(filedef=target_path)
                if is_default
                else posix1e.ACL(file=target_path)
            )

            # Check if ACL is empty without using len()
            entries = list(acl)

            # If creating a Default ACL for the first time,
            # you MUST include the base (minimal) entries.
            if is_default and not entries:
                access_acl = posix1e.ACL(file=target_path)
                for entry in access_acl:
                    if entry.tag_type in [
                        posix1e.ACL_USER_OBJ,
                        posix1e.ACL_GROUP_OBJ,
                        posix1e.ACL_OTHER,
                    ]:
                        new_e = acl.append()
                        new_e.tag_type = entry.tag_type
                        new_e.permset = entry.permset

            # Now find or add user {uid}
            found = False
            for entry in acl:
                if entry.tag_type == posix1e.ACL_USER and entry.qualifier == uid:
                    new_entry = entry
                    found = True
                    break

            if not found:
                new_entry = acl.append()
                new_entry.tag_type = posix1e.ACL_USER
                new_entry.qualifier = uid

            new_entry.permset.clear()
            for perm in [posix1e.ACL_READ, posix1e.ACL_WRITE, posix1e.ACL_EXECUTE]:
                new_entry.permset.add(perm)

            acl.calc_mask()
            acl.applyto(target_path, acl_type)

        except OSError:
            if is_default:
                continue
            raise


@app.task(queue=DISPATCH_QUEUE, bind=True)
def prepare_submission(task, userId, fileId, stages, job_id):
    # Create a submission directory
    api = GirderApi.for_task(task)
    # Every later step works out of a directory on this machine, so claim the
    # rest of the chain for this worker before doing anything else.
    queue = worker_queue(task)
    pin_chain(task, queue)
    try:
        submission_folder = _create_submission_folder(api, userId)
        # Move file to the submission directory
        fobj = api.file(fileId)
        api.annotate_item_type(fobj, "submission_file")
        api.move_item(fobj["itemId"], submission_folder["_id"])
        api.set_folder_metadata(
            submission_folder["_id"],
            {
                "stages": stages,
                "status": "submitted",
                "job_id": str(job_id),
            },
        )
        report(api, job_id, f"New submission: '{submission_folder['name']}' created.")
        return {
            "folder_id": str(submission_folder["_id"]),
            "file_id": str(fobj["_id"]),
            "job_id": str(job_id),
            "stages": stages,
            "queue": queue,
        }
    except Exception as exc:
        report_failure(api, job_id, "Failed to prepare submission", exc)
        raise


@app.task(queue=DISPATCH_QUEUE, bind=True)
@submission_task("Failed to create workspace")
def create_workspace(task, api, submission):
    report(api, submission["job_id"], "Creating workspace from source folder.")

    submission["tmp_dir"] = f"/tmp/tmp-{submission['folder_id']}"
    os.makedirs(submission["tmp_dir"], exist_ok=False)
    # add sticky bit to tmp_dir
    os.chmod(submission["tmp_dir"], 0o1777)

    workspace_dir = f"/tmp/workspace-{submission['folder_id']}"
    submission["workspace_dir"] = workspace_dir
    project_dir = get_project_dir(submission)
    os.makedirs(project_dir, exist_ok=False)
    # Give read/write/execute permissions to myself
    _matlab_perms(project_dir, uid=os.getuid())
    # Give read/write/execute permissions to Matlab user
    _matlab_perms(project_dir, uid=1001)
    # Ensure R library directory for user install.packages exists
    for stage in submission.get("stages", []):
        if stage["image_name"].startswith("rocker/"):
            os.makedirs(os.path.join(workspace_dir, "R", "library"), exist_ok=True)

    fobj = api.file(submission["file_id"])
    temp_filename = os.path.join(workspace_dir, fobj["name"])
    try:
        api.download_file(fobj["_id"], temp_filename)
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
    finally:
        if os.path.exists(temp_filename):
            os.remove(temp_filename)

    return submission


def skip_condition(condition, submission):
    if condition == "is_pruned":
        return not submission.get("pruned", False)
    return False


@app.task(queue=DISPATCH_QUEUE, bind=True)
@submission_task("Failed to run TRO utilities")
def run_tro(task, api, submission, action, inumber, condition):
    report(
        api,
        submission["job_id"],
        f"Running TRO utilities in the workspace. ({action})",
    )
    if skip_condition(condition, submission):
        logger.info(
            f"Skipping TRO action '{action}' for submission {submission['folder_id']} "
            f"due to condition '{condition}'."
        )
        return submission

    folder_id = submission["folder_id"]
    submission_folder = api.folder(folder_id)
    settings = api.settings(
        [
            PluginSettings.TRO_PROFILE,
            PluginSettings.TRO_GPG_FINGERPRINT,
            PluginSettings.TRO_GPG_PASSPHRASE,
        ]
    )

    tro_file = f"/tmp/tro-{submission['job_id']}.jsonld"
    tro_obj = None
    if submission.get("troId") is not None:
        tro_obj = api.file(submission["troId"])
        api.download_file(tro_obj["_id"], tro_file)

    with tempfile.NamedTemporaryFile(delete=True) as profile:
        trs_profile = settings[PluginSettings.TRO_PROFILE]
        trs_profile["sivacor:stackVersion"] = version("girder_sivacor")
        profile.write(json.dumps(trs_profile).encode())
        profile.seek(0)
        tro = TRO(
            filepath=tro_file,
            gpg_fingerprint=settings[PluginSettings.TRO_GPG_FINGERPRINT],
            gpg_passphrase=settings[PluginSettings.TRO_GPG_PASSPHRASE],
            profile=profile.name,
            extra_context={"sivacor": "https://vocabulary.sivacor.org/0.1/"},
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
    elif action == "prune_performance":
        runs = submission.get("runs", [])
        run = runs[-1] if runs else {}
        tro.add_performance(
            datetime.datetime.fromisoformat(run["run_start_time"]),
            datetime.datetime.fromisoformat(run["run_end_time"]),
            comment="SIVACOR Workspace pruning step",
            accessed_arrangement=(f"arrangement/{inumber}", "/workspace"),
            modified_arrangement=(f"arrangement/{inumber + 1}", "/workspace"),
            attrs=run.get("run_attrs", []),
        )
    elif action == "add_performance":
        stages = submission.get("stages", [])
        main_file = stages[inumber].get("main_file", "unknown")
        runs = submission.get("runs", [])
        run = runs[-1] if runs else {}
        extra_attributes = _performance_attributes(api, folder_id, inumber + 1)

        tro.add_performance(
            datetime.datetime.fromisoformat(run["run_start_time"]),
            datetime.datetime.fromisoformat(run["run_end_time"]),
            comment=f"SIVACOR workflow execution ({main_file}) step {inumber + 1}",
            accessed_arrangement=(f"arrangement/{inumber}", "/workspace"),
            modified_arrangement=(f"arrangement/{inumber + 1}", "/workspace"),
            attrs=run.get("run_attrs", []),
            extra_attributes=extra_attributes,
        )
    elif action == "sign":
        tro.request_timestamp()

        for meta_key, filename, nice_name in zip(
            ("sig_file_id", "tsr_file_id"),
            (tro.sig_filename, tro.tsr_filename),
            ("tro_signature", "tro_timestamp"),
        ):
            fobj = api.upload_file(
                folder_id, filename, mime_type="text/plain", item_type=nice_name
            )
            meta[meta_key] = str(fobj["_id"])
            os.remove(filename)

    tro.save()
    if tro_obj:
        api.replace_file(tro_obj["_id"], tro.tro_filename)
    else:
        tro_obj = api.upload_file(
            folder_id,
            tro.tro_filename,
            mime_type="application/ld+json",
            item_type="tro_declaration",
        )
        submission["troId"] = str(tro_obj["_id"])
    os.remove(tro.tro_filename)
    meta["tro_file_id"] = str(tro_obj["_id"])
    api.set_folder_metadata(folder_id, meta)

    return submission


def _performance_attributes(api, folder_id, stage_num):
    """Read back the performance data recorded_run uploaded for a stage."""
    item = api.find_child_item(folder_id, f"performance_data_stage_{stage_num}.json")
    if not item:
        return None
    files = api.item_files(item["_id"])
    if not files:
        return None
    data = json.loads(b"".join(api.file_chunks(files[0]["_id"])))
    # Namespace the bare keys; anything already prefixed is left alone.
    return {f"sivacor:{key}": value for key, value in data.items() if ":" not in key}


@app.task(queue=DISPATCH_QUEUE, bind=True)
@submission_task("Failed to execute workflow")
def execute_workflow(task, api, submission, stage, env_vars):
    report(api, submission["job_id"], "Executing workflow on workspace.")

    # Placeholder for actual workflow execution logic
    start_time = datetime.datetime.now()
    ret = recorded_run(api, submission, stage, env_vars, task=task)
    if ret["StatusCode"] == -123:
        print("Termination requested, stopping execution.")
        return abandon(task, submission)

    if ret["StatusCode"] != 0:
        raise RuntimeError(f"Workflow execution failed with code {ret['StatusCode']}")
    end_time = datetime.datetime.now()

    if submission.get("runs") is None:
        submission["runs"] = []
    attrs = [
        TRPAttribute.ENV_ISOLATION.value,
        TRPAttribute.NON_INTERACTIVE.value,
        TRPAttribute.MACHINE_ENFORCEMENT.value,
    ]
    if stage.get("network_isolation", False):
        attrs.append(TRPAttribute.NET_ISOLATION.value)
    submission["runs"].append(
        {
            "run_start_time": start_time.isoformat(),
            "run_end_time": end_time.isoformat(),
            "run_attrs": attrs,
        }
    )

    return submission


@app.task(queue=DISPATCH_QUEUE, bind=True)
@submission_task("Failed to prune workspace")
def prune_workspace(task, api, submission):
    logger.info(f"Pruning workspace for submission {submission['folder_id']}")
    start_time = datetime.datetime.now()
    project_dir = pathlib.Path(get_project_dir(submission))
    patterns = copy.deepcopy(DEFAULT_SIVACOR_IGNORE)
    # Check if user provided a custom .sivacorignore
    if ignore_path := next(project_dir.rglob(".sivacorignore"), None):
        logger.info(
            f"Found custom .sivacorignore at {ignore_path}, applying user patterns."
        )
        ignore_base_dir = ignore_path.parent
        with open(ignore_path, "r") as f:
            user_patterns = [
                line.strip() for line in f if line.strip() and not line.startswith("#")
            ]
            patterns += user_patterns
    else:
        logger.info("No custom .sivacorignore found, using default ignore patterns.")
        ignore_base_dir = project_dir

    removed_paths = []
    spec = pathspec.PathSpec.from_lines("gitignore", list(patterns))

    for root, dirs, files in os.walk(ignore_base_dir, topdown=True):
        root_path = pathlib.Path(root)

        kept_dirs = []
        for d in dirs:
            dir_p = root_path / d
            relative_dir_str = str(dir_p.relative_to(ignore_base_dir)) + "/"
            if spec.match_file(relative_dir_str):
                removed_paths.append(relative_dir_str)
                shutil.rmtree(dir_p, ignore_errors=True)
            else:
                kept_dirs.append(d)

        dirs[:] = kept_dirs
        for f in files:
            file_p = root_path / f
            relative_file_str = str(file_p.relative_to(ignore_base_dir))
            if spec.match_file(relative_file_str):
                removed_paths.append(relative_file_str)
                file_p.unlink()

    end_time = datetime.datetime.now()
    if removed_paths:
        logger.info(
            "Pruned the following paths from the workspace:\n - "
            + "\n - ".join(removed_paths)
        )
    submission["pruned"] = len(removed_paths) > 0
    submission["runs"].append(
        {
            "run_start_time": start_time.isoformat(),
            "run_end_time": end_time.isoformat(),
            "run_attrs": [],
        }
    )
    return submission


@app.task(queue=DISPATCH_QUEUE, bind=True)
@submission_task("Failed to upload executed replication package")
def upload_workspace(task, api, submission):
    # Upload the modified workspace back to Girder as a zip file
    # called 'executed_replication_package.zip'
    report(
        api,
        submission["job_id"],
        "Uploading executed replication package to Girder.",
    )

    folder_id = submission["folder_id"]
    folder_meta = api.folder(folder_id).get("meta", {})

    submission_fobj = api.file(submission["file_id"])
    zip_basename = (
        pathlib.Path(submission_fobj["name"]).stem + f"-{submission['job_id']}.zip"
    )
    project_dir = get_project_dir(submission)

    zip_path = os.path.join(submission["workspace_dir"], zip_basename)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk(project_dir):
            # ignore contents of dirs from IGNORE_DIRS
            dirs[:] = [d for d in dirs if d not in IGNORE_DIRS]
            for file in files:
                if file == zip_basename:
                    continue
                file_path = os.path.join(root, file)
                arcname = os.path.relpath(file_path, submission["workspace_dir"])
                if os.path.islink(file_path):
                    zip_symlink(zipf, file_path, arcname=arcname)
                else:
                    zipf.write(file_path, arcname)

        # Store TRO files in a separate 'tro/' directory within the zip
        for key in ("tro_file_id", "sig_file_id", "tsr_file_id"):
            if fobj := api.file(folder_meta.get(key)):
                dump_to_zip(
                    api.file_chunks(fobj["_id"]), zipf, "tro/" + fobj["name"]
                )

        # Store stdout and stderr logs
        for key in ("stderr_file_id", "stdout_file_id"):
            if fobj := api.file(folder_meta.get(key)):
                dump_to_zip(api.file_chunks(fobj["_id"]), zipf, fobj["name"])

    fobj = api.upload_file(
        folder_id,
        zip_path,
        mime_type="application/zip",
        item_type="replicated_package",
    )
    os.remove(zip_path)
    api.set_folder_metadata(folder_id, {"replpack_file_id": str(fobj["_id"])})
    submission["replpack_file_id"] = str(fobj["_id"])

    return submission


@app.task(queue=DISPATCH_QUEUE, bind=True)
def finalize_job(task, submission):
    api = GirderApi.for_task(task)
    discard_workspace(submission)
    if api.job(submission["job_id"])["status"] == JobStatus.RUNNING:
        report(
            api,
            submission["job_id"],
            "Submission job finalized successfully.",
            status=JobStatus.SUCCESS,
        )
    return submission


@worker_ready.connect
def _sweep_orphaned_containers(sender=None, **kwargs):
    """Kill analysis containers a previous incarnation of this worker left behind.

    A container is a *sibling* started through the docker socket, so killing the
    worker -- or losing its VM -- leaves the analysis running with nobody to
    collect its output. Verified in the P0 kill test: the R container was still
    burning a core after the job had been failed and the worker had restarted.

    Startup is the one moment when this is unambiguous: a fresh worker process has
    no task in flight, so any container still bearing its queue label must be a
    leftover. No Girder query needed, which matters because the worker has neither
    a database nor a standing credential.
    """
    try:
        reap_orphaned_containers()
    except Exception:
        logger.warning("Orphan container sweep failed", exc_info=True)


@app.on_after_configure.connect
def setup_periodic_tasks(sender, **kwargs):
    # ``add_periodic_task`` builds the message itself, so the queue has to be
    # named here too -- the task's own default only applies to ``apply_async``.
    sender.add_periodic_task(
        12 * 60 * 60,
        cleanup_submissions.s(),
        name="Clean up old submissions",
        options={"queue": LOCAL_QUEUE},
    )
    sender.add_periodic_task(
        10 * 60,
        reap_stranded_submissions.s(),
        name="Reap stranded submissions",
        options={"queue": LOCAL_QUEUE},
    )


def _local_admin_token():
    """Mint a short-lived admin token through the model layer.

    Only the co-located worker consumes ``sivacor.maintenance``, and that
    worker has ``GIRDER_MONGO_URI`` and the model layer -- so it can issue its
    own credential instead of being handed a standing API key. Nothing is
    persisted beyond the token itself, which is scoped to a single day because
    it is used for exactly one POST.

    Returns ``None`` on any failure. This is a *fallback*, and the whole point
    of the REST conversion was that a worker need not reach MongoDB: a remote
    worker importing this must degrade to a skip, not raise.
    """
    try:
        from girder.models.token import Token
        from girder.models.user import User

        # Sorted so a multi-admin deployment attributes the sweeps to the same
        # account every time, which matters when reading the job log later.
        admin = User().findOne({"admin": True}, sort=[("created", 1)])
        if admin is None:
            return None
        return Token().createToken(admin, days=1)["_id"]
    except Exception:
        logger.debug("No local Girder model layer for maintenance", exc_info=True)
        return None


def _maintenance_api():
    """Client for a task that has no submission to inherit a token from.

    Periodic tasks arrive without ``girder_client_token`` headers, so the
    credential has to come from somewhere else. Prefer an explicit
    ``GIRDER_API_KEY``; failing that, mint one locally. The caller reports
    having neither as a skip rather than a failure.
    """
    api_url = os.environ.get("GIRDER_API_URL")
    if not api_url:
        return None
    if api_key := os.environ.get("GIRDER_API_KEY"):
        return GirderApi(api_url, api_key=api_key)
    if token := _local_admin_token():
        return GirderApi(api_url, token=token)
    return None


@app.task(queue=LOCAL_QUEUE)
def cleanup_submissions():
    """Trigger the retention sweep.

    The sweep itself runs on the Girder server: it removes jobs by query, which
    has no REST equivalent.
    """
    if not (api := _maintenance_api()):
        logger.info(
            "Skipping submission retention sweep: set GIRDER_API_URL on a "
            "worker (plus GIRDER_API_KEY, unless it can reach MongoDB) to "
            "enable it."
        )
        return
    api.client.post("sivacor/cleanup")


@app.task(queue=LOCAL_QUEUE)
def reap_stranded_submissions():
    """Ask Girder to fail submissions whose worker went away.

    Same shape as :func:`cleanup_submissions`, and for the same reason: the
    sweep is a query over the job collection. It has to be driven from outside
    the submission it might be failing, so it cannot live on the chain.

    .. note::

       This stays an HTTP call even though the only consumer of
       :data:`~.routing.LOCAL_QUEUE` has the model layer and could sweep
       in-process. Failing a submission works by ``updateJob()`` firing
       ``jobs.job.update.after``, and that handler is bound in
       ``SIVACORPlugin.load()`` -- which runs in the Girder *server*, not here:
       a celery worker loads ``girder_worker_plugins`` entry points only, so in
       this process the topic has zero handlers. Sweeping locally would
       transition the job and silently skip the folder status update and the
       user's failure email. The test suite cannot catch that, because
       ``pytest-girder`` loads the full plugin in-process.
    """
    if not (api := _maintenance_api()):
        logger.info(
            "Skipping stranded-submission reap: set GIRDER_API_URL on a "
            "worker (plus GIRDER_API_KEY, unless it can reach MongoDB) to "
            "enable it."
        )
        return
    api.client.post("sivacor/reap")
