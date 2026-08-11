import datetime
import logging
from pathlib import Path

from bson import ObjectId
from girder import events
from girder.api import access
from girder.api.rest import boundHandler, filtermodel
from girder.constants import AccessType, TokenScope
from girder.exceptions import ValidationException
from girder.models.file import File
from girder.models.folder import Folder
from girder.models.setting import Setting
from girder.models.user import User
from girder.plugin import GirderPlugin, getPlugin, registerPluginStaticContent
from girder.utility import mail_utils, setting_utilities
from girder.utility.model_importer import ModelImporter
from girder_jobs.constants import JobStatus
from girder_jobs.models.job import Job as JobModel
from girder_oauth.providers import addProvider
from girder_oauth.settings import PluginSettings as OAuthSettings

from .auth.orcid import ORCID
from .notifications import (
    _createMessage,
    _sendmail,
    email_urls,
    set_submission_status,
)
from .rest import SIVACOR, get_submission_child_jobs
from .settings import PluginSettings

logger = logging.getLogger(__name__)


@setting_utilities.validator(PluginSettings.UPLOADS_FOLDER_NAME)
def _validate_uploads_folder_name(value):
    if not isinstance(value, str) or not value:
        raise ValidationException("Uploads folder name must be a non-empty string.")
    return value


@setting_utilities.validator(PluginSettings.RETENTION_DAYS)
def _validate_retention_days(doc):
    value = doc.get("value")
    if not isinstance(value, float) or value < 0.0:
        raise ValidationException("Retention days must be a non-negative number.")
    return value


@setting_utilities.validator(
    {PluginSettings.HEARTBEAT_TIMEOUT, PluginSettings.MAX_RUNTIME}
)
def _validate_reaper_thresholds(doc):
    value = doc.get("value")
    # Zero would fail every running submission on the next sweep, so unlike
    # the retention window these have to be strictly positive.
    if not isinstance(value, float) or value <= 0.0:
        raise ValidationException("This setting must be a positive number.")
    return value


@setting_utilities.validator(
    {
        PluginSettings.SUBMISSION_COLLECTION_NAME,
        PluginSettings.EDITORS_GROUP_NAME,
        PluginSettings.TRO_GPG_FINGERPRINT,
        PluginSettings.TRO_GPG_PASSPHRASE,
        PluginSettings.STATA_LICENSE,
    }
)
def _validate_string_settings(doc):
    value = doc.get("value")
    if not isinstance(value, str) or not value:
        raise ValidationException("This setting must be a non-empty string.")
    return value


@setting_utilities.validator(PluginSettings.TRO_PROFILE)
def _validate_tro_profile(doc):
    value = doc.get("value")
    if not isinstance(value, dict):
        raise ValidationException("TRO profile must be a dictionary.")
    return value


@setting_utilities.validator(PluginSettings.IMAGE_TAGS)
def _validate_image_tags(doc):
    value = doc.get("value")
    if not isinstance(value, dict):
        raise ValidationException("Image tags must be a dictionary.")
    return value


@setting_utilities.validator(PluginSettings.BANNER_ENABLED)
def _validate_banner_enabled(doc):
    value = doc.get("value")
    if not isinstance(value, bool):
        raise ValidationException("Banner enabled must be a boolean.")
    return value


@setting_utilities.validator(PluginSettings.BANNER_MESSAGE)
def _validate_banner_message(doc):
    value = doc.get("value")
    if not isinstance(value, str):
        raise ValidationException("Banner message must be a string.")
    return value


@setting_utilities.validator({"oauth.orcid_client_id", "oauth.orcid_client_secret"})
def validateOrcidSettings(doc):
    pass


@setting_utilities.default({"oauth.orcid_client_id", "oauth.orcid_client_secret"})
def defaultOrcidSettings():
    return ""


def create_uploads_folder(event: events.Event) -> None:
    user = event.info
    folderModel = ModelImporter.model("folder")
    uploads_folder = folderModel.findOne(
        {
            "parentId": user["_id"],
            "parentCollection": "user",
            "name": Setting().get(PluginSettings.UPLOADS_FOLDER_NAME),
        }
    )
    if not uploads_folder:
        uploads_folder = folderModel.createFolder(
            parent=user, name="Uploads", parentType="user", public=False, creator=user
        )
    folderModel.setUserAccess(uploads_folder, user, level=AccessType.ADMIN, save=True)


def send_approval_email(event: events.Event) -> None:
    user = event.info["user"]
    context = {
        "user": user,
        **email_urls(),
        "current_year": datetime.datetime.now().year,
    }
    text_content = (
        f"Hello Admin,\n\n"
        "Someone has registered a new account that needs admin approval.\n"
        f"Login: {user['login']}\n"
        f"Email: {user['email']}\n"
        f"Name: {user['firstName']} {user['lastName']}"
    )
    rendered_html = mail_utils.renderTemplate("account_approval.mako", context)
    to = [u["email"] for u in User().getAdmins()]
    msg, recipients = _createMessage(
        "Account pending approval", text_content, rendered_html, to, None
    )
    events.trigger("_sendmail", info={"message": msg, "recipients": recipients})
    event.preventDefault()


def send_approved_email(event: events.Event) -> None:
    user = event.info["user"]
    context = {
        "user": user,
        **email_urls(),
        "current_year": datetime.datetime.now().year,
    }
    text_content = (
        f"Hello {user['firstName']} {user['lastName']},\n\n"
        "Your account has been approved. You may now login."
    )
    rendered_html = mail_utils.renderTemplate("account_approved.mako", context)
    to = [user["email"]]
    msg, recipients = _createMessage(
        "SIVACOR account approved", text_content, rendered_html, to, None
    )
    events.trigger("_sendmail", info={"message": msg, "recipients": recipients})
    event.preventDefault()


@access.public(scope=TokenScope.DATA_READ)
@filtermodel(model=Folder)
@boundHandler
def search_with_job_id(self, event):
    params = event.info["params"]
    jobId = params.get("jobId")
    if jobId:
        parentType = params.get("parentType")
        parentId = params.get("parentId")
        if not parentType or not parentId:
            raise ValidationException(
                "Both parentType and parentId must be provided when filtering by jobId."
            )
        query = {
            "parentCollection": parentType,
            "parentId": ObjectId(parentId),
            "meta.job_id": jobId,
        }
        user = self.getCurrentUser()
        folders = [
            Folder().filter(obj, user)
            for obj in Folder().findWithPermissions(
                query,
                sort=[("created", -1)],
                user=self.getCurrentUser(),
                level=AccessType.READ,
                limit=1,
                offset=0,
            )
        ]
        event.preventDefault().addResponse(folders)


@access.public
@boundHandler
def add_public_settings(self, event):
    """Expose selected SIVACOR settings to unauthenticated clients.

    Bound to ``rest.get.system/public_settings.after`` so that the frontend can
    read them (e.g. the maintenance banner) without authenticating.
    """
    settings = event.info["returnVal"]
    public_settings = [
        PluginSettings.BANNER_ENABLED,
        PluginSettings.BANNER_MESSAGE,
        PluginSettings.UPLOADS_FOLDER_NAME,
    ]
    settings.update({key: Setting().get(key) for key in public_settings})


def cancel_jobs(event):
    job = event.info
    if job["type"] != "sivacor_submission":
        return

    q = {
        "type": "celery",
        "$or": [
            {"args.0.job_id": str(job["_id"])},
            {"args.3": str(job["_id"])},
        ],
        "status": {
            "$in": [
                JobStatus.INACTIVE,
                JobStatus.QUEUED,
                JobStatus.RUNNING,
            ]
        },
    }
    for child_job in JobModel().find(q):
        JobModel().cancelJob(child_job)


class SIVACORPlugin(GirderPlugin):
    DISPLAY_NAME = "SIVACOR"

    def load(self, info):
        from girder.api.v1.folder import Folder as FolderResource

        events.unbind("_sendmail", "core.email")
        events.bind("_sendmail", "sivacor", _sendmail)
        events.bind("email.approval", "sivacor", send_approval_email)
        events.bind("email.approved", "sivacor", send_approved_email)
        events.bind("model.user.save.created", "sivacor", create_uploads_folder)
        events.bind("rest.get.folder.before", "sivacor", search_with_job_id)
        events.bind(
            "rest.get.system/public_settings.after", "sivacor", add_public_settings
        )
        ModelImporter.model("user").exposeFields(
            level=AccessType.READ, fields=("lastJobId", "lastProjectId")
        )
        # sivacor-girderfs mounts a Girder folder read-only into an analysis
        # container and needs each blob's content hash *as metadata* -- hashing
        # a mounted dataset by reading it defeats the point of not copying it.
        # Girder computes sha512 at upload but exposes it at no access level
        # (girder/models/file.py:37-41), so widen that here.
        #
        # The split: sha512 is a hash of data the caller can already download in
        # full, and 'imported' is a boolean selecting which storage layout
        # applies -- neither is sensitive. 'path' is an absolute host path for
        # imported files, i.e. infrastructure layout, so site admins only.
        #
        # exposeFields is global to this Girder instance: every File response
        # site-wide grows these fields, including the ones aea-sivacor consumes.
        # That is accepted, but it is a site-wide change made from a plugin.
        File().exposeFields(level=AccessType.READ, fields=("sha512", "imported"))
        File().exposeFields(level=AccessType.SITE_ADMIN, fields=("path",))

        getPlugin("oauth").load(info)
        OAuthSettings.ORCID_CLIENT_ID = "oauth.orcid_client_id"
        OAuthSettings.ORCID_CLIENT_SECRET = "oauth.orcid_client_secret"
        addProvider(ORCID)
        User().exposeFields(level=AccessType.ADMIN, fields=("oauth"))

        info["apiRoot"].sivacor = SIVACOR()
        getPlugin("jobs").load(info)
        events.bind("jobs.cancel", "sivacor", cancel_jobs)
        # Workers report progress over REST, so this fires here rather than in
        # the worker process as it used to.
        events.bind("jobs.job.update.after", "sivacor", set_submission_status)
        info["apiRoot"].job.route("GET", (":id", "children"), get_submission_child_jobs)

        FolderResource.find.description.param(
            "jobId",
            (
                "Optional job ID to filter folders by those associated with "
                "the given job."
            ),
            required=False,
            dataType="string",
        )
        template_dir = (Path(__file__).parent / "mail_templates").as_posix()
        logger.warning(f"Adding {template_dir} as template directory")
        mail_utils.addTemplateDirectory(template_dir)
        registerPluginStaticContent(
            plugin="sivacor",
            css=["/style.css"],
            js=["/girder-plugin-sivacor.umd.cjs"],
            staticDir=Path(__file__).parent / "web_client" / "dist",
            tree=info["serverRoot"],
        )
