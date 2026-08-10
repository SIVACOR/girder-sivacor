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

from .errors import FailureCode
from .models.execution_record import ExecutionRecord
from .settings import PluginSettings
from .telemetry import sanitize_record
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


def _iso8601(value):
    """Format a Mongo timestamp as RFC 3339 with a literal ``Z``.

    Girder's own JSON encoder emits ``+00:00`` instead
    (``girder/utility/__init__.py:130``), so anything that has to match the
    manifest's documented ``...Z`` shape has to format itself rather than hand a
    datetime back and let the encoder do it.
    """
    if value is None:
        return None
    return (
        _as_utc(value)
        .astimezone(datetime.timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


#: The manifest schema version. sivacor-girderfs refuses to mount against a
#: version it does not know, so bump this for any incompatible change to the
#: shape emitted by get_fs_manifest -- adding a field is not one.
FS_MANIFEST_VERSION = 1

#: How many items' files to fetch per Mongo query when building a manifest.
#: Trades round trips against the size of a single $in; 1000 puts a 100k-node
#: tree at ~50 queries instead of ~50 000.
_MANIFEST_FILE_BATCH = 1000


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
        self.route("POST", ("execution_record",), self.record_execution)
        self.route("GET", ("execution_record",), self.list_execution_records)
        self.route("GET", ("execution_record", "summary"), self.summarise_execution_records)
        self.route("GET", ("image_tags",), self.get_image_tags)
        self.route("GET", ("workflow_schema",), self.get_workflow_schema)
        self.route("GET", ("fs", "manifest"), self.get_fs_manifest)
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
        Description("Store an anonymous record of how a submission executed.")
        .notes(
            "Called once by the worker when a submission reaches a terminal "
            "state. These records outlive the submission -- they are what the "
            "deployment can still report on after the user deletes their data "
            "or the retention sweep does.\n\n"
            "Nothing stored here identifies a person: no user, job or folder "
            "id, no filename, no worker instance. The body is not stored as "
            "sent -- it is rebuilt field by field from an allow-list, so a "
            "worker cannot widen what is kept by sending more."
        )
        .jsonParam(
            "record",
            "The execution record, as built by the worker.",
            paramType="body",
            requireObject=True,
        )
    )
    def record_execution(self, record):
        return self.store_execution_record(record)

    @staticmethod
    def store_execution_record(payload):
        """Filter and persist one execution record. Returns what was stored."""
        # The date is stamped here, not taken from the payload, and is
        # deliberately only a date: at pilot scale a wall-clock submission time
        # is close enough to an identifier to undo the rest of this.
        today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        document = sanitize_record(payload, today)
        ExecutionRecord().save(document)
        return document

    @access.admin
    @autoDescribeRoute(
        Description("List anonymous execution records.")
        .notes(
            "Admin-only. Not because the records are sensitive -- they contain "
            "no personal data -- but because they are operational reporting "
            "data with no meaning to a researcher, and an endpoint nobody needs "
            "publicly is better closed."
        )
        .param("status", "Only records in this terminal state.", required=False)
        .param("errorCode", "Only failures with this code.", required=False)
        .param("imageName", "Only runs that used this image.", required=False)
        .param("since", "Earliest date to include (YYYY-MM-DD).", required=False)
        .param("until", "Latest date to include (YYYY-MM-DD).", required=False)
        .pagingParams(defaultSort="date", defaultSortDir=-1)
    )
    def list_execution_records(self, status, errorCode, imageName, since, until, limit, offset, sort):
        query = self._execution_record_query(status, errorCode, imageName, since, until)
        cursor = ExecutionRecord().find(query, limit=limit, offset=offset, sort=sort)
        return {
            # The total is what makes the table's paging honest; a page of
            # results says nothing about how much matched.
            "count": ExecutionRecord().collection.count_documents(query),
            "records": list(cursor),
        }

    @access.admin
    @autoDescribeRoute(
        Description("Summarise execution records for reporting.")
        .notes(
            "The aggregation the browse view opens with. Computed in Mongo "
            "rather than by paging every record into the client, which stops "
            "working the moment there are more records than one page."
        )
        .param("status", "Only records in this terminal state.", required=False)
        .param("errorCode", "Only failures with this code.", required=False)
        .param("imageName", "Only runs that used this image.", required=False)
        .param("since", "Earliest date to include (YYYY-MM-DD).", required=False)
        .param("until", "Latest date to include (YYYY-MM-DD).", required=False)
    )
    def summarise_execution_records(self, status, errorCode, imageName, since, until):
        query = self._execution_record_query(status, errorCode, imageName, since, until)
        collection = ExecutionRecord().collection

        def grouped(pipeline):
            return list(collection.aggregate([{"$match": query}] + pipeline))

        durations = grouped(
            [
                {
                    "$group": {
                        "_id": None,
                        "avg": {"$avg": "$total_duration_seconds"},
                        "max": {"$max": "$total_duration_seconds"},
                    }
                }
            ]
        )
        return {
            "total": collection.count_documents(query),
            "byStatus": grouped(
                [{"$group": {"_id": "$status", "count": {"$sum": 1}}}, {"$sort": {"count": -1}}]
            ),
            "byErrorCode": grouped(
                [
                    {"$match": {"error.code": {"$ne": None}}},
                    {"$group": {"_id": "$error.code", "count": {"$sum": 1}}},
                    {"$sort": {"count": -1}},
                ]
            ),
            # One record can have several stages, so unwind first -- otherwise a
            # two-stage run counts once against whichever image happens to be
            # first, and the totals quietly disagree with byStatus.
            "byImage": grouped(
                [
                    {"$unwind": "$stages"},
                    {
                        "$group": {
                            "_id": {
                                "$concat": [
                                    {"$ifNull": ["$stages.image_name", "unknown"]},
                                    ":",
                                    {"$ifNull": ["$stages.image_tag", "unknown"]},
                                ]
                            },
                            "count": {"$sum": 1},
                            "failed": {
                                "$sum": {"$cond": [{"$eq": ["$status", "completed"]}, 0, 1]}
                            },
                        }
                    },
                    {"$sort": {"count": -1}},
                ]
            ),
            "byDate": grouped(
                [
                    {
                        "$group": {
                            "_id": "$date",
                            "count": {"$sum": 1},
                            "failed": {
                                "$sum": {"$cond": [{"$eq": ["$status", "completed"]}, 0, 1]}
                            },
                        }
                    },
                    {"$sort": {"_id": 1}},
                ]
            ),
            # Deliberately mean and max rather than percentiles: $percentile
            # needs MongoDB 7, and this deployment runs 4.4.
            "duration": {
                "avg": durations[0]["avg"] if durations else None,
                "max": durations[0]["max"] if durations else None,
            },
        }

    @staticmethod
    def _execution_record_query(status, errorCode, imageName, since, until):
        query = {}
        if status:
            query["status"] = status
        if errorCode:
            query["error.code"] = errorCode
        if imageName:
            query["stages.image_name"] = imageName
        # 'date' is stored as a YYYY-MM-DD string, which orders lexicographically
        # the same way it orders chronologically -- so a plain range works.
        if since or until:
            bounds = {}
            if since:
                bounds["$gte"] = since
            if until:
                bounds["$lte"] = until
            query["date"] = bounds
        return query

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
                code = FailureCode.REAPED_MAX_RUNTIME
            elif now - last_seen > stale_after:
                reason = (
                    f"no sign of life for {now - last_seen}; the worker "
                    "running it is presumed lost"
                )
                code = FailureCode.REAPED_NO_HEARTBEAT
            else:
                continue

            logger.warning("Reaping stranded submission %s: %s", job["_id"], reason)
            self._fail_stranded(job, reason)
            self._record_reaped(job, code, now - created)
            reaped.append(str(job["_id"]))

        return {"reaped": reaped}

    @classmethod
    def _record_reaped(cls, job, code, elapsed):
        """Write the execution record for a submission the worker lost.

        The worker records its own outcome, but a reaped submission is by
        definition one whose worker is not coming back -- so a run that died
        this way would otherwise be the one kind of failure missing from the
        reporting data, which is the opposite of what you want.
        """
        stages = []
        folder = Folder().findOne({"meta.job_id": str(job["_id"])})
        for stage in (folder or {}).get("meta", {}).get("stages", []) or []:
            stages.append(
                {
                    "image_name": stage.get("image_name"),
                    "image_tag": stage.get("image_tag"),
                    "network_isolation": bool(stage.get("network_isolation", False)),
                }
            )
        try:
            cls.store_execution_record(
                {
                    "status": "reaped",
                    "stages": stages,
                    "total_duration_seconds": elapsed.total_seconds(),
                    "error": {"step": "reaper", "code": code.value},
                }
            )
        except Exception:
            # Same call as the worker's: reporting data is never worth failing
            # the sweep that is cleaning up after a lost worker.
            logger.warning(
                "Could not record execution telemetry for reaped job %s",
                job["_id"],
                exc_info=True,
            )

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

    # DATA_READ rather than the bare @access.user every other handler here uses,
    # and that is deliberate: sivacor-girderfs authenticates with a DATA_READ
    # API key exchanged for a long-lived token, so an analysis container that
    # somehow reaches the daemon's config still cannot mutate Girder. Do not
    # "tidy" this into consistency with its neighbours.
    @access.user(scope=TokenScope.DATA_READ)
    @autoDescribeRoute(
        Description("Get a complete metadata manifest for a folder subtree.")
        .notes(
            "Serves sivacor-girderfs, which mounts a Girder folder read-only "
            "into an analysis container. It needs the whole subtree up front: "
            "walking it over stock REST costs one request per node, and it "
            "needs every file's content hash without reading the file, since "
            "hashing a mounted dataset defeats the point of mounting it.\n\n"
            "The walk uses the requesting user's own permissions, so a "
            "subfolder they cannot read is simply absent -- the rest of the "
            "tree is still returned.\n\n"
            "Contract, not implementation detail: 'folders', 'items' and "
            "'files' are each sorted by 'id', and stay so between calls. The "
            "client derives inode numbers from manifest order, so an unstable "
            "order would renumber the same folder between mounts. Timestamps "
            "are RFC 3339 UTC with a 'Z' suffix; 'updated' is null on a file "
            "never modified since upload. 'root.parent' is null even when the "
            "folder has a parent in Girder -- a subtree mount must not be told "
            "about anything above it. 'sha512' is '' when Girder never recorded "
            "one (imported and link files), which means the client fetches over "
            "HTTP rather than locating the blob on disk; it is not an error. "
            "Clients must refuse a 'manifestVersion' they do not know."
        )
        .modelParam(
            "folderId",
            "The folder whose subtree to describe.",
            model=Folder,
            level=AccessType.READ,
            required=True,
            paramType="query",
        )
        .param(
            "maxNodes",
            "Refuse trees larger than this many folders, items and files.",
            required=False,
            dataType="integer",
            default=1000000,
        )
        .errorResponse("The subtree exceeds maxNodes.", 413)
    )
    def get_fs_manifest(self, folder, maxNodes):
        user = self.getCurrentUser()
        is_admin = bool(user.get("admin"))

        folders = []
        items = []
        files = []
        seen = 0

        def budget():
            nonlocal seen
            seen += 1
            if seen > maxNodes:
                # Abort during the walk, not after: the guard exists to keep a
                # pathological tree from OOM-ing the web process, which a
                # check on the finished response would not do.
                raise RestException(
                    f"This subtree has more than maxNodes ({maxNodes}) nodes. "
                    "Mount a smaller folder, or raise maxNodes.",
                    code=413,
                )

        # Iterative rather than recursive: nothing bounds Girder's folder depth,
        # and a REST handler should not be able to exhaust the Python stack.
        # Order of traversal does not matter -- every array is sorted at the end.
        budget()
        stack = [(folder, None)]
        while stack:
            current, parent_id = stack.pop()
            folders.append(
                {
                    "id": str(current["_id"]),
                    "parent": parent_id,
                    "name": current["name"],
                    "created": _iso8601(current.get("created")),
                    "updated": _iso8601(current.get("updated")),
                }
            )

            # childItems and childFiles take no `user`: items and files are
            # AccessControlMixin resources that inherit the containing folder's
            # ACL (girder/utility/acl_mixin.py), so a READ-permitted folder
            # settles them. Passing user= would not filter -- childItems
            # forwards **kwargs straight into the pymongo query and would
            # corrupt it. childFolders, by contrast, does filter by user, and
            # that is what keeps unreadable branches out of the manifest.
            # Never force=True here: `girder mount` walks that way and thereby
            # exposes the whole database to anyone who can read the mountpoint.
            folder_items = list(Folder().childItems(current))
            for item in folder_items:
                budget()
                items.append(
                    {
                        "id": str(item["_id"]),
                        "folder": str(current["_id"]),
                        "name": item["name"],
                        "created": _iso8601(item.get("created")),
                        "updated": _iso8601(item.get("updated")),
                    }
                )

            # Files are fetched per batch of items rather than per item.
            # Item().childFiles(item) is exactly File().find({"itemId": id}),
            # so calling it in a loop costs one Mongo round trip per item: at
            # 50 000 items that measured 18.7s of a 26s response, against 0.55s
            # for the same rows fetched with $in (M1-BACKEND-CHANGES.md §7).
            # Identical query, identical access semantics -- files inherit the
            # containing folder's ACL either way -- and the response is
            # byte-for-byte the same, since every array is sorted at the end.
            #
            # Chunked rather than one query per folder so that a single
            # enormous folder cannot pull an unbounded number of file
            # documents into memory before budget() gets a chance to refuse
            # them. The items themselves are already budget-checked above, so
            # this is bounded by maxNodes regardless.
            by_item_id = {item["_id"]: item for item in folder_items}
            item_ids = list(by_item_id)
            for start in range(0, len(item_ids), _MANIFEST_FILE_BATCH):
                chunk = item_ids[start:start + _MANIFEST_FILE_BATCH]
                for file in File().find({"itemId": {"$in": chunk}}):
                    budget()
                    files.append(
                        self._manifest_file(file, by_item_id[file["itemId"]], is_admin)
                    )

            for child in list(Folder().childFolders(current, "folder", user=user)):
                budget()
                stack.append((child, str(current["_id"])))

        for array in (folders, items, files):
            array.sort(key=lambda entry: entry["id"])

        return {
            "manifestVersion": FS_MANIFEST_VERSION,
            "root": {"type": "folder", "id": str(folder["_id"])},
            "generatedAt": _iso8601(datetime.datetime.now(datetime.timezone.utc)),
            "folders": folders,
            "items": items,
            "files": files,
        }

    @staticmethod
    def _manifest_file(file, item, is_admin):
        imported = bool(file.get("imported"))
        return {
            "id": str(file["_id"]),
            "item": str(item["_id"]),
            "name": file["name"],
            # linkUrl files may carry no size at all.
            "size": int(file.get("size") or 0),
            # Client-asserted at upload; deliberately not sniffed here.
            "mimeType": file.get("mimeType"),
            "created": _iso8601(file.get("created")),
            "updated": _iso8601(file.get("updated")),
            # Set only on upload finalisation, so imported and linkUrl files
            # have none. "" means "cannot be located by hash"; never null.
            "sha512": file.get("sha512") or "",
            "imported": imported,
            # Only an imported file's `path` is an absolute host path. A normal
            # filesystem-assetstore file also has one, but it is relative to the
            # assetstore root and derivable from sha512
            # (filesystem_assetstore_adapter.py:234), so emitting it would just
            # be an absolute-looking value that is not absolute.
            "path": file["path"] if (is_admin and imported and "path" in file) else None,
            # Non-null means there is no assetstore blob at all.
            "linkUrl": file.get("linkUrl"),
        }

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
