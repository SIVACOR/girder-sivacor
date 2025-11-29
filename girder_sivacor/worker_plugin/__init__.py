import logging
from girder import events
from girder.models.collection import Collection
from girder.models.folder import Folder
from girder.models.setting import Setting
from girder_jobs.constants import JobStatus
from girder_worker import GirderWorkerPluginABC

logger = logging.getLogger(__name__)


def set_submission_status(event: events.Event) -> None:
    from ..settings import PluginSettings

    job = event.info.get("job")
    if not job or job.get("type") != "sivacor_submission":
        return

    root_collection = Collection().findOne(
        {"name": Setting().get(PluginSettings.SUBMISSION_COLLECTION_NAME)}
    )
    if not root_collection:
        logger.error(
            "Submission collection '%s' not found.",
            Setting().get(PluginSettings.SUBMISSION_COLLECTION_NAME),
        )
        return

    submission_folder = Folder().findOne(
        {
            "parentId": root_collection["_id"],
            "parentCollection": "collection",
            "meta.job_id": str(job["_id"]),
        }
    )
    if not submission_folder:
        logger.error("!!! Submission folder for job %s not found.", str(job["_id"]))
        return

    status = job.get("status")
    if status == JobStatus.SUCCESS:
        submission_status = "completed"
    elif status in (JobStatus.ERROR, JobStatus.CANCELED):
        submission_status = "failed"
    else:
        submission_status = "processing"
    Folder().collection.update_one(
        {"_id": submission_folder["_id"]},
        {"$set": {"meta.status": submission_status}},
    )


class SIVACORWorkerPlugin(GirderWorkerPluginABC):
    def __init__(self, app, *args, **kwargs):
        self.app = app
        events.bind("jobs.job.update.after", "sivacor", set_submission_status)

    def task_imports(self):
        return [
            "girder_sivacor.worker_plugin.run_submission",
        ]
