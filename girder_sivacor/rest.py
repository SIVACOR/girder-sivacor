import datetime
import json
import logging
import os
from zoneinfo import ZoneInfo

import requests
import yaml
from girder.api import access
from girder.api.describe import Description, autoDescribeRoute
from girder.api.rest import Resource, boundHandler, filtermodel
from girder.constants import AccessType, TokenScope
from girder.exceptions import AccessException, RestException, ValidationException
from girder.models.collection import Collection
from girder.models.file import File
from girder.models.folder import Folder
from girder.models.setting import Setting
from girder.models.token import Token
from girder.models.user import User
from girder_jobs.constants import REST_CREATE_JOB_TOKEN_SCOPE, JobStatus
from girder_jobs.models.job import Job
from girder_plugin_worker.utils import getWorkerApiUrl

from .settings import PluginSettings
from .utils import encrypt_job_secrets
from .worker_plugin.routing import DISPATCH_QUEUE
from .worker_plugin.run_submission import (
    create_workspace,
    execute_workflow,
    finalize_job,
    prepare_submission,
    prune_workspace,
    run_tro,
    sign_tro,
    upload_workspace,
)

logger = logging.getLogger(__name__)

#: How long the token handed to the worker stays valid. It has to outlive the
#: longest replication run, but it grants admin access, so not by much.
WORKER_TOKEN_DAYS = 7


def _as_utc(value):
    """Make a Mongo timestamp safe to compare against an aware ``now``.

    Whether pymongo hands back aware datetimes depends on how the client was
    built, and a naive one turns the reaper's comparisons into a TypeError
    rather than a wrong answer -- silent in a periodic task.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value


stage_schema = {
    "$schema": "http://json-schema.org/draft-04/schema#",
    "title": "Workflow",
    "description": "A SIVACOR replication workflow.",
    "type": "object",
    "properties": {
        "stages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "image_name": {"type": "string"},
                    "main_file": {"type": "string"},
                    "image_tag": {"type": "string"},
                    "network_isolation": {"type": "boolean", "default": False},
                },
                "required": ["image_name", "main_file", "image_tag"],
                "additionalProperties": False,
            },
            "minItems": 1,
        },
        "env_secrets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["key", "value"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["stages"],
}

# Statuses in which a submission is still in flight. A user is allowed at most
# one submission in one of these states at a time.
ACTIVE_JOB_STATUSES = (JobStatus.INACTIVE, JobStatus.QUEUED, JobStatus.RUNNING)


class SIVACOR(Resource):
    def __init__(self):
        super(SIVACOR, self).__init__()
        self.resourceName = "sivacor"
        self.route("POST", ("submit_job",), self.submit_job)
        self.route("POST", ("cleanup",), self.cleanup_submissions)
        self.route("POST", ("reap",), self.reap_submissions)
        self.route("POST", ("heartbeat", ":id"), self.heartbeat)
        self.route("POST", ("claim", ":id"), self.claim)
        self.route("GET", ("image_tags",), self.get_image_tags)
        self.route("GET", ("workflow_schema",), self.get_workflow_schema)
        self.route("DELETE", ("submission", ":id"), self.delete_submission)

    @access.user
    @autoDescribeRoute(
        Description("Submit a job to SIVACOR.")
        .modelParam(
            "id",
            "The ID of the file to process.",
            model=File,
            level=AccessType.ADMIN,
            required=True,
            paramType="query",
        )
        .jsonParam(
            "workflow",
            "The defintion of replication workflow stages to execute.",
            paramType="body",
            requireObject=True,
            required=True,
            schema=stage_schema,
        )
        .errorResponse("You already have a submission in progress.", 409)
    )
    @filtermodel(model=Job)
    def submit_job(self, file, workflow):
        # Only one submission per user may be in flight at a time.
        user = self.getCurrentUser()
        if active := self._active_submission(user):
            raise self._active_submission_error(active)

        stages = workflow.get("stages", [])
        env_secrets = encrypt_job_secrets(workflow.get("env_secrets", []))
        tags = self._get_tags()
        for stage in stages:
            image_name = stage.get("image_name")
            tag = stage.get("image_tag")
            image_reference = f"{image_name}:{tag}"
            if image_name not in tags or tag not in tags.get(image_name, []):
                raise ValidationException(f"Invalid image: {image_reference}")

        job = Job().createJob(
            title=f"SIVACOR Run for {file['name']} by {user['firstName']} {user['lastName']}",
            type="sivacor_submission",
            public=False,
            user=user,
        )
        # Close the window between the check above and the job creation: two
        # concurrent requests can both pass it, so whoever ends up with the
        # older job wins and the other one is rolled back. Nothing has been
        # dispatched yet, so removing the job is enough to undo it.
        if active := self._active_submission(user, older_than=job["_id"]):
            Job().remove(job)
            raise self._active_submission_error(active)

        User().collection.update_one(
            {"_id": user["_id"]}, {"$set": {"lastJobId": job["_id"]}}
        )
        zone = ZoneInfo("America/Chicago")
        timestamp = f"[{datetime.datetime.now().astimezone(zone).replace(microsecond=0).isoformat()}]"
        job = Job().updateJob(
            job, f"{timestamp} Preparing SIVACOR submission\n", status=JobStatus.RUNNING
        )

        # The worker has no database of its own; it reaches back over REST as
        # an administrator. girder_worker copies these headers from a running
        # task onto the next one it publishes, but the chain is built here, so
        # set them on every step rather than relying on that.
        admin = User().findOne({"admin": True})
        # REST_CREATE_JOB_TOKEN_SCOPE is not optional. girder_worker's
        # girder_before_task_publish POSTs to /job to record a child job for every
        # step it publishes, and that endpoint is
        # @access.token(scope=REST_CREATE_JOB_TOKEN_SCOPE, required=True) -- which a
        # plain USER_AUTH token does not satisfy even for an administrator. Without
        # it every step logs "Failed to post job: HTTP error 403 ... Invalid token
        # scope" and carries on, so the submission still runs but no child job is
        # ever created: GET /job/:id/children comes back empty, per-step progress
        # vanishes from the UI, and the reaper has no child jobs to settle.
        worker_token = str(
            Token().createToken(
                user=admin,
                days=WORKER_TOKEN_DAYS,
                scope=[TokenScope.USER_AUTH, REST_CREATE_JOB_TOKEN_SCOPE],
            )["_id"]
        )
        api_url = getWorkerApiUrl()

        def step(signature, title):
            return signature.set(
                girder_job_title=title,
                girder_api_url=api_url,
                girder_client_token=worker_token,
            )

        workflow = step(
            prepare_submission.s(
                str(user["_id"]),
                str(file["_id"]),
                stages,
                str(job["_id"]),
            ),
            f"Moving {file['name']} to submission collection",
        )
        workflow |= step(create_workspace.s(), "Create Workspace")
        workflow |= step(
            run_tro.s("add_arrangement", 0, None), "Record initial arrangement"
        )
        for i, stage in enumerate(stages):
            workflow |= step(
                execute_workflow.s(stage, env_secrets), "Execute SIVACOR Workflow"
            )
            workflow |= step(
                run_tro.s("add_arrangement", i + 1, None), "Record final arrangement"
            )
            workflow |= step(
                run_tro.s("add_performance", i, None), "Record user workflow TRP"
            )
        workflow |= step(prune_workspace.s(), "Prune Workspace")
        workflow |= step(
            run_tro.s("add_arrangement", len(stages) + 1, "is_pruned"),
            "Record final pruned arrangement",
        )
        workflow |= step(
            run_tro.s("prune_performance", len(stages), "is_pruned"),
            "Record workspace prune TRP",
        )
        # Signing runs on the manager, not on the worker holding the workspace --
        # sign_tro is declared on LOCAL_QUEUE and is exempt from pin_chain. See
        # routing.UNPINNED_TASKS.
        workflow |= step(sign_tro.s(), "Sign TRO")
        workflow |= step(upload_workspace.s(), "Upload Replicated Package")
        workflow |= step(finalize_job.s(), "Finalize Job Submission")
        try:
            workflow.apply_async(queue=DISPATCH_QUEUE)
        except Exception:
            logger.exception("Failed to dispatch submission %s", str(job["_id"]))
        return job

    @staticmethod
    def _active_submission(user, older_than=None):
        """Return the user's oldest unfinished submission job, if any.

        :param older_than: only consider jobs created before this job id.
        """
        query = {
            "userId": user["_id"],
            "type": "sivacor_submission",
            "status": {"$in": list(ACTIVE_JOB_STATUSES)},
        }
        if older_than is not None:
            query["_id"] = {"$lt": older_than}
        return Job().findOne(query, sort=[("created", 1)])

    @staticmethod
    def _active_submission_error(job):
        return RestException(
            f"You already have a submission in progress ('{job['title']}'). "
            "Please wait for it to finish, or cancel it, before submitting "
            "a new one.",
            code=409,
            extra=str(job["_id"]),
        )

    @access.admin
    @autoDescribeRoute(
        Description("Remove submissions older than the retention window.").notes(
            "Called periodically by the celery worker. It runs here rather than "
            "on the worker because it deletes jobs by query, which the REST API "
            "cannot express."
        )
    )
    def cleanup_submissions(self):
        root_collection = Collection().findOne(
            {"name": Setting().get(PluginSettings.SUBMISSION_COLLECTION_NAME)}
        )
        if not root_collection:
            return {"removed": 0}

        admin = self.getCurrentUser()
        cutoff_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            days=Setting().get(PluginSettings.RETENTION_DAYS)
        )
        removed = 0
        # Materialize the cursor: removing folders while iterating it can make
        # Mongo skip documents.
        folders = list(Folder().childFolders(root_collection, "collection", user=admin))
        for folder in folders:
            if folder["created"] > cutoff_time:
                continue
            if job_id := folder.get("meta", {}).get("job_id"):
                if main_job := Job().load(job_id, force=True):
                    Job().remove(main_job)
                Job().collection.delete_many({"args.0.job_id": job_id})
            logger.info(
                "Cleaning up submission folder %s created at %s",
                str(folder["_id"]),
                folder["created"],
            )
            Folder().remove(folder)
            removed += 1
        return {"removed": removed}

    @access.admin
    @autoDescribeRoute(
        Description("Record a liveness heartbeat for a running submission.")
        .notes(
            "Called by the worker while a container runs. A submission can "
            "execute for hours without logging anything, so the job's "
            "'updated' timestamp is not by itself a liveness signal -- this is "
            "what tells /sivacor/reap the worker is still there."
        )
        .modelParam(
            "id",
            "The ID of the submission job.",
            model=Job,
            force=True,
            required=True,
        )
    )
    def heartbeat(self, job):
        now = datetime.datetime.now(datetime.timezone.utc)
        # Straight to the collection rather than through updateJob(): that
        # fires jobs.job.update.after, and re-running the status handler --
        # folder write, possibly an email -- once a minute per submission is
        # not what a heartbeat should cost.
        Job().collection.update_one(
            {"_id": job["_id"]}, {"$set": {"meta.heartbeat": now}}
        )
        return {"heartbeat": now}

    @access.admin
    @autoDescribeRoute(
        Description("Record which worker queue a submission was claimed by.")
        .notes(
            "Called once by prepare_submission, right after the chain is pinned. "
            "This is what lets the autoscaler tell a *spent* worker from an "
            "available one: an ephemeral worker stops consuming the dispatch "
            "queue the moment it claims a submission, so counting it as capacity "
            "makes the controller refuse to create the instance the next "
            "submission needs. See D8 in autoscaling_plan.md.\n\n"
            "Deliberately recorded server-side rather than read back over the "
            "broker: 'celery inspect active_queues' would answer the same "
            "question, but a worker whose broker connection has died cannot "
            "answer it -- and that is exactly the case where the number matters."
        )
        .modelParam(
            "id",
            "The ID of the submission job.",
            model=Job,
            force=True,
            required=True,
        )
        .param("queue", "The worker's private queue name.", required=True)
    )
    def claim(self, job, queue):
        # Same reasoning as heartbeat: bypass updateJob() so this cannot trigger
        # the status handler's folder write and email.
        Job().collection.update_one(
            {"_id": job["_id"]}, {"$set": {"meta.worker_queue": queue}}
        )
        return {"worker_queue": queue}

    @access.admin
    @autoDescribeRoute(
        Description("Fail submissions whose worker stopped reporting.").notes(
            "Called periodically by the celery worker. A chain publishes each "
            "step only after the previous one returns, so a worker that dies "
            "mid-submission leaves no message to retry and no step to fail: "
            "the job stays RUNNING forever. Retrying is not an option either, "
            "since the workspace died with the worker -- so fail loudly."
        )
    )
    def reap_submissions(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        stale_after = datetime.timedelta(
            minutes=Setting().get(PluginSettings.HEARTBEAT_TIMEOUT)
        )
        max_runtime = datetime.timedelta(
            hours=Setting().get(PluginSettings.MAX_RUNTIME)
        )

        # Materialize the cursor: this loop moves jobs out of the status the
        # query selects on, and Mongo can skip documents that shift under a
        # live cursor. Same reason cleanup_submissions() lists its folders.
        # Job().find() drops the log from the projection, which is what we want
        # -- these documents are only read for their timestamps.
        stranded = list(
            Job().find({"type": "sivacor_submission", "status": JobStatus.RUNNING})
        )

        reaped = []
        for job in stranded:
            created = _as_utc(job["created"])
            # Any of the three counts as a sign of life: the heartbeat covers a
            # long container run, 'updated' covers every step that logs, and
            # 'created' keeps a submission that has not reached its first step
            # from being reaped before it starts.
            last_seen = max(
                _as_utc(t)
                for t in (
                    created,
                    job.get("updated") or created,
                    job.get("meta", {}).get("heartbeat") or created,
                )
            )
            if now - created > max_runtime:
                reason = (
                    f"exceeded the maximum runtime of {max_runtime}; "
                    "started at " + created.isoformat()
                )
            elif now - last_seen > stale_after:
                reason = (
                    f"no sign of life for {now - last_seen}; the worker "
                    "running it is presumed lost"
                )
            else:
                continue

            logger.warning("Reaping stranded submission %s: %s", job["_id"], reason)
            self._fail_stranded(job, reason)
            reaped.append(str(job["_id"]))

        return {"reaped": reaped}

    @staticmethod
    def _fail_stranded(job, reason):
        """Transition a stranded job to ERROR and settle its children.

        Going through ``updateJob`` is the point: it fires
        ``jobs.job.update.after``, which is what marks the submission folder
        failed and emails the user.
        """
        zone = ZoneInfo("America/Chicago")
        stamp = (
            datetime.datetime.now().astimezone(zone).replace(microsecond=0).isoformat()
        )
        try:
            Job().updateJob(
                job,
                log=f"[{stamp}] Submission abandoned: {reason}.\n",
                status=JobStatus.ERROR,
            )
        except Exception:
            logger.exception("Could not fail stranded submission %s", job["_id"])
            return

        # The per-step celery jobs are stranded too, and nothing else will ever
        # touch them. They carry no submission status of their own, so update
        # the collection directly rather than replaying the event handler.
        Job().collection.update_many(
            {
                "type": "celery",
                "$or": [
                    {"args.0.job_id": str(job["_id"])},
                    {"args.3": str(job["_id"])},
                ],
                "status": {
                    "$in": [JobStatus.INACTIVE, JobStatus.QUEUED, JobStatus.RUNNING]
                },
            },
            {"$set": {"status": JobStatus.ERROR}},
        )

    @access.public
    @autoDescribeRoute(Description("Get available Docker image tags for SIVACOR."))
    def get_image_tags(self):
        tags = self._get_tags()
        return tags

    @access.public
    @autoDescribeRoute(
        Description(
            "Get the JSON schema a replication workflow definition must satisfy."
        ).notes(
            "The same schema submit_job validates its body against, so clients "
            "(e.g. the submission UI importing a workflow from a YAML/JSON file) "
            "can check a definition before submitting it."
        )
    )
    def get_workflow_schema(self):
        return stage_schema

    @staticmethod
    def _get_tags():
        now = datetime.datetime.now(datetime.UTC)
        cutoff = now - datetime.timedelta(hours=4)

        fetch = not os.path.exists("/tmp/sivacor_image_tags.json") or (
            os.path.getmtime("/tmp/sivacor_image_tags.json") < cutoff.timestamp()
        )

        if fetch:
            source_url = (
                "https://raw.githubusercontent.com/SIVACOR/sivacor-repo-choice"
                "/refs/heads/main/allowed_repos.yaml"
            )
            tags = Setting().get(PluginSettings.IMAGE_TAGS)
            try:
                response = requests.get(source_url, timeout=10)
                response.raise_for_status()
                tags = yaml.safe_load(response.text)
            except Exception as e:
                print(f"Error fetching image tags: {e}")
            with open("/tmp/sivacor_image_tags.json", "w") as f:
                json.dump(tags, f)

        with open("/tmp/sivacor_image_tags.json", "r") as f:
            return json.load(f)

        return tags

    @access.user
    @autoDescribeRoute(
        Description("Delete a SIVACOR submission job and its associated data.")
        .modelParam(
            "id",
            "The ID of the submission job to delete.",
            model=Folder,
            level=AccessType.READ,
            required=True,
        )
        .param(
            "progress",
            "Whether to report progress during deletion.",
            required=False,
            dataType="boolean",
            default=False,
        )
    )
    def delete_submission(self, folder, progress):
        from girder.tasks import deleteFolderTask

        # ensure that user has read access to the folder, but meta confirms he was creator
        user = self.getCurrentUser()
        meta = folder.get("meta", {})
        creator_id = meta.get("creator_id")
        if not (creator_id and str(creator_id) == str(user["_id"])):
            raise AccessException(
                "You do not have permission to delete '%s' submission." % folder["name"]
            )
        if meta.get("status") not in ("completed", "failed"):
            raise ValidationException(
                "Only completed or failed submissions can be deleted."
            )
        root = self.submission_collection(user)
        if not root or folder["parentId"] != root["_id"]:
            raise ValidationException("Invalid submission folder.")

        job = Job().load(meta.get("job_id"), force=True)
        if job:
            Job().remove(job)
        deleteFolderTask.delay(
            str(folder["_id"]),
            progress,
            str(User().findOne({"admin": True})["_id"]),
        )
        return {"message": f"Marked submission '{folder['name']}' for deletion."}

    @staticmethod
    def submission_collection(user):
        return Collection().filter(
            Collection().findOne(
                {"name": Setting().get(PluginSettings.SUBMISSION_COLLECTION_NAME)}
            ),
            user=user,
        )


@access.public
@autoDescribeRoute(
    Description("Get submission child jobs for a given job.").modelParam(
        "id",
        "The ID of the parent job.",
        model=Job,
        level=AccessType.READ,
        required=True,
    )
)
@filtermodel(model=Job)
@boundHandler
def get_submission_child_jobs(self, job):
    child_jobs = []
    workspace_job = Job().findOne({"type": "celery", "args.3": str(job["_id"])})
    if workspace_job:
        child_jobs.append(workspace_job)
    child_jobs += list(Job().find({"type": "celery", "args.0.job_id": str(job["_id"])}))
    return child_jobs
