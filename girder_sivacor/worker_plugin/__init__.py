import os
from email.message import EmailMessage
from email.policy import SMTP
import datetime
import logging
from pathlib import Path
from girder import events
from girder.models.collection import Collection
from girder.models.folder import Folder
from girder.models.setting import Setting
from girder.models.user import User
from girder_jobs.constants import JobStatus
from girder_jobs.models.job import Job
from girder_worker import GirderWorkerPluginABC
from girder.settings import SettingKey
from girder.utility import mail_utils

logger = logging.getLogger(__name__)
_HERE = Path(__file__).parent


def format_timestamp(timestamp):
    """Format timestamp for display"""
    # Implement based on your timestamp format
    return timestamp.strftime("%Y-%m-%d %H:%M:%S EST")


def calculate_duration(start, end):
    """Calculate human-readable duration"""
    duration = end - start
    hours, remainder = divmod(int(duration.total_seconds()), 3600)
    minutes, seconds = divmod(remainder, 60)

    parts = []
    if hours > 0:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    if minutes > 0:
        parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    if seconds > 0 or not parts:
        parts.append(f"{seconds} second{'s' if seconds != 1 else ''}")

    return " ".join(parts)


def _createMessage(subject: str, text_content: str, rendered_html: str, to, bcc):
    # Normalize recipients
    if isinstance(to, str):
        to = [to]
    if isinstance(bcc, str):
        bcc = [bcc]
    elif bcc is None:
        bcc = []

    if not to and not bcc:
        raise ValueError("At least one recipient (to or bcc) must be specified.")
    if not subject:
        raise ValueError("Email subject cannot be empty.")

    # 1. Create the modern EmailMessage object with SMTP policy
    # The SMTP policy automatically uses Quoted-Printable for UTF-8/long lines
    msg = EmailMessage(policy=SMTP)

    msg["Subject"] = subject
    msg["From"] = Setting().get(SettingKey.EMAIL_FROM_ADDRESS)
    if to:
        msg["To"] = ", ".join(to)
    if bcc:
        msg["Bcc"] = ", ".join(bcc)

    # 2. Set the plain text as the main content
    msg.set_content(text_content)

    # 3. Add the HTML version as an alternative
    # EmailMessage handles the 'alternative' container creation for you
    msg.add_alternative(rendered_html, subtype="html")

    recipients = list(set(to) | set(bcc))
    return msg, recipients


def notify_user(job, submission_folder, success: bool) -> None:
    job = Job().load(job["_id"], force=True)
    meta = submission_folder.get("meta", {})
    if not meta.get("creator_id"):
        logger.error(
            "Submission folder %s has no creator_id in metadata.",
            str(submission_folder["_id"]),
        )
        return
    user = User().load(meta["creator_id"], force=True)
    if not user:
        logger.error("User with ID %s not found.", str(meta["creator_id"]))
        return

    context = {
        "base_url": "https://submit.sivacor.org",
        "docs_url": "https://docs.sivacor.org",
        "current_year": datetime.datetime.now().year,
        "logo_url": "https://submit.sivacor.org/sivacor_logo_notext_trans.png",
        "user_name": f"{user['firstName']} {user['lastName']}",
        "job_id": str(job["_id"]),
        "is_success": success,
        "status_text": "SUCCESS" if success else "FAILED",
        "submission_time": format_timestamp(job["created"]),
        "completion_time": format_timestamp(job["updated"]),
        "execution_time": calculate_duration(job["created"], job["updated"]),
        "stages": meta.get("stages", []),
        "submission_url": "https://submit.sivacor.org/",
    }

    # 2. Create the Plain Text version (Very important for spam scores)
    text_content = (
        f"Hello {context['user_name']},\n\n"
        f"Your SIVACOR job {context['job_id']} has finished with "
        f"status {context['status_text']}.\n"
        f"View details here: {context['submission_url']}"
    )

    if not success:
        context["error_message"] = "".join(
            job.get("log", ["No error message available."])
        )

    subject = (
        "Your SIVACOR submission has completed successfully"
        if success
        else "Your SIVACOR submission has failed"
    )

    rendered_html = mail_utils.renderTemplate("submission_notification.mako", context)
    msg, recipients = _createMessage(
        subject, text_content, rendered_html, [user["email"]], None
    )

    if os.environ.get("GIRDER_EMAIL_TO_CONSOLE"):
        print("Redirecting email to console:")
        print(msg.as_string())
        return

    setting = Setting()
    smtp = mail_utils._SMTPConnection(
        host=setting.get(SettingKey.SMTP_HOST),
        port=setting.get(SettingKey.SMTP_PORT),
        encryption=setting.get(SettingKey.SMTP_ENCRYPTION),
        username=setting.get(SettingKey.SMTP_USERNAME),
        password=setting.get(SettingKey.SMTP_PASSWORD),
    )

    logger.info("Sending email to %s through %s", ", ".join(recipients), smtp.host)

    with smtp:
        smtp.send(msg["From"], recipients, msg.as_bytes())


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
        try:
            notify_user(job, submission_folder, success=True)
        except Exception as e:
            logger.exception(
                "Failed to send success notification for job %s: %s",
                str(job["_id"]),
                str(e),
            )
    elif status in (JobStatus.ERROR, JobStatus.CANCELED):
        submission_status = "failed"
        try:
            notify_user(job, submission_folder, success=False)
        except Exception as e:
            logger.exception(
                "Failed to send failure notification for job %s: %s",
                str(job["_id"]),
                str(e),
            )
    else:
        submission_status = "processing"
    Folder().collection.update_one(
        {"_id": submission_folder["_id"]},
        {"$set": {"meta.status": submission_status}},
    )


class SIVACORWorkerPlugin(GirderWorkerPluginABC):
    def __init__(self, app, *args, **kwargs):
        self.app = app
        mail_utils.addTemplateDirectory((_HERE.parent / "mail_templates").as_posix())
        events.bind("jobs.job.update.after", "sivacor", set_submission_status)

    def task_imports(self):
        return [
            "girder_sivacor.worker_plugin.run_submission",
        ]
