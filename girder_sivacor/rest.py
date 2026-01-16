import datetime
import json
import os

import requests
import yaml
from girder.api import access
from girder.api.describe import Description, autoDescribeRoute
from girder.api.rest import Resource, boundHandler, filtermodel
from girder.constants import AccessType
from girder.exceptions import ValidationException
from girder.models.file import File as FileModel
from girder.models.setting import Setting as SettingModel
from girder.models.user import User as UserModel
from girder_jobs.constants import JobStatus
from girder_jobs.models.job import Job as JobModel
from zoneinfo import ZoneInfo

from .settings import PluginSettings
from .worker_plugin.run_submission import (
    create_workspace,
    execute_workflow,
    finalize_job,
    prepare_submission,
    run_tro,
    upload_workspace,
)

stage_schema = {
    "$schema": "http://json-schema.org/draft-04/schema#",
    "title": "Workflow",
    "description": "A SIVACOR replication workflow.",
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "image_name": {"type": "string"},
            "main_file": {"type": "string"},
            "image_tag": {"type": "string"},
        },
        "required": ["image_name", "main_file", "image_tag"],
        "additionalProperties": False,
    },
    "minItems": 1,
}


class SIVACOR(Resource):
    def __init__(self):
        super(SIVACOR, self).__init__()
        self.resourceName = "sivacor"
        self.route("POST", ("submit_job",), self.submit_job)
        self.route("GET", ("image_tags",), self.get_image_tags)

    @access.user
    @autoDescribeRoute(
        Description("Submit a job to SIVACOR.")
        .modelParam(
            "id",
            "The ID of the file to process.",
            model=FileModel,
            level=AccessType.ADMIN,
            required=True,
            paramType="query",
        )
        .jsonParam(
            "stages",
            "The defintion of replication workflow stages to execute.",
            requireArray=True,
            required=True,
            schema=stage_schema,
        )
    )
    @filtermodel(model=JobModel)
    def submit_job(self, file, stages):
        tags = self._get_tags()
        for stage in stages:
            image_name = stage.get("image_name")
            tag = stage.get("image_tag")
            image_reference = f"{image_name}:{tag}"
            if image_name not in tags or tag not in tags.get(image_name, []):
                raise ValidationException(f"Invalid image: {image_reference}")

        # Job submission logic goes here
        user = self.getCurrentUser()
        job = JobModel().createJob(
            title=f"SIVACOR Run for {file['name']} by {user['firstName']} {user['lastName']}",
            type="sivacor_submission",
            public=False,
            user=user,
        )
        UserModel().collection.update_one(
            {"_id": user["_id"]}, {"$set": {"lastJobId": job["_id"]}}
        )
        zone = ZoneInfo("America/Chicago")
        timestamp = f"[{datetime.datetime.now().astimezone(zone).replace(microsecond=0).isoformat()}]"
        job = JobModel().updateJob(
            job, f"{timestamp} Preparing SIVACOR submission\n", status=JobStatus.RUNNING
        )

        workflow = prepare_submission.s(
            str(user["_id"]),
            str(file["_id"]),
            stages,
            str(job["_id"]),
        ).set(
            girder_job_title=f"Moving {file['name']} to submission collection",
        )
        workflow |= create_workspace.s().set(girder_job_title="Create Workspace")
        workflow |= run_tro.s("add_arrangement", 0).set(
            girder_job_title="Record initial arrangement"
        )
        for i, stage in enumerate(stages):
            workflow |= execute_workflow.s(stage).set(
                girder_job_title="Execute SIVACOR Workflow"
            )
            workflow |= run_tro.s("add_arrangement", i + 1).set(
                girder_job_title="Record final arrangement"
            )
            workflow |= run_tro.s("add_performance", i).set(
                girder_job_title="Record Performance Metrics"
            )
        workflow |= run_tro.s("sign", 0).set(girder_job_title="Sign TRO")
        workflow |= upload_workspace.s().set(
            girder_job_title="Upload Replicated Package"
        )
        workflow |= finalize_job.s().set(girder_job_title="Finalize Job Submission")
        try:
            workflow.apply_async(queue="local")
        except Exception:
            pass  # Exceptions are handled in the job steps
        return job

    @access.public
    @autoDescribeRoute(Description("Get available Docker image tags for SIVACOR."))
    def get_image_tags(self):
        tags = self._get_tags()
        return tags

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
            tags = SettingModel().get(PluginSettings.IMAGE_TAGS)
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


@access.public
@autoDescribeRoute(
    Description("Get submission child jobs for a given job.").modelParam(
        "id",
        "The ID of the parent job.",
        model=JobModel,
        level=AccessType.READ,
        required=True,
    )
)
@filtermodel(model=JobModel)
@boundHandler
def get_submission_child_jobs(self, job):
    child_jobs = []
    workspace_job = JobModel().findOne({"type": "celery", "args.3": str(job["_id"])})
    if workspace_job:
        child_jobs.append(workspace_job)
    child_jobs += list(
        JobModel().find({"type": "celery", "args.0.job_id": str(job["_id"])})
    )
    return child_jobs
