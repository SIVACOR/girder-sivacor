import logging
from pathlib import Path

from bson import ObjectId
from girder import events
from girder.api import access
from girder.api.rest import boundHandler, filtermodel
from girder.constants import AccessType, TokenScope
from girder.exceptions import ValidationException
from girder.models.folder import Folder
from girder.models.user import User
from girder.plugin import GirderPlugin, getPlugin, registerPluginStaticContent
from girder.utility import setting_utilities
from girder.utility.model_importer import ModelImporter
from girder_oauth.providers import addProvider
from girder_oauth.settings import PluginSettings as OAuthSettings

from .auth.orcid import ORCID
from .rest import SIVACOR
from .settings import PluginSettings

logger = logging.getLogger(__name__)


@setting_utilities.validator(PluginSettings.UPLOADS_FOLDER_NAME)
def _validate_uploads_folder_name(value):
    if not isinstance(value, str) or not value:
        raise ValidationException("Uploads folder name must be a non-empty string.")
    return value


@setting_utilities.validator(PluginSettings.MAX_ITEM_SIZE)
def _validate_max_item_size(doc):
    value = doc.get("value")
    if not isinstance(value, int) or value <= 0:
        raise ValidationException("Max item size must be a positive integer.")
    return value


@setting_utilities.validator(PluginSettings.RETENTION_DAYS)
def _validate_retention_days(doc):
    value = doc.get("value")
    if not isinstance(value, float) or value < 0.0:
        raise ValidationException("Retention days must be a non-negative number.")
    return value


@setting_utilities.validator(
    {
        PluginSettings.SUBMISSION_COLLECTION_NAME,
        PluginSettings.EDITORS_GROUP_NAME,
        PluginSettings.TRO_GPG_FINGERPRINT,
        PluginSettings.TRO_GPG_PASSPHRASE,
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
            "parentType": "user",
            "name": PluginSettings.UPLOADS_FOLDER_NAME,
        }
    )
    if not uploads_folder:
        uploads_folder = folderModel.createFolder(
            parent=user, name="Uploads", parentType="user", public=False, creator=user
        )
    folderModel.setUserAccess(uploads_folder, user, level=AccessType.ADMIN, save=True)


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
                "Both parentType and parentId must be provided when "
                "filtering by jobId."
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


class SIVACORPlugin(GirderPlugin):
    DISPLAY_NAME = "SIVACOR"

    def load(self, info):
        from girder.api.v1.folder import Folder as FolderResource

        events.bind("model.user.save.created", "sivacor", create_uploads_folder)
        events.bind("rest.get.folder.before", "sivacor", search_with_job_id)
        ModelImporter.model("user").exposeFields(
            level=AccessType.READ, fields=("lastJobId", "lastProjectId")
        )

        getPlugin("oauth").load(info)
        OAuthSettings.ORCID_CLIENT_ID = "oauth.orcid_client_id"
        OAuthSettings.ORCID_CLIENT_SECRET = "oauth.orcid_client_secret"
        addProvider(ORCID)
        User().exposeFields(level=AccessType.ADMIN, fields=("oauth"))

        info["apiRoot"].sivacor = SIVACOR()

        FolderResource.find.description.param(
            "jobId",
            (
                "Optional job ID to filter folders by those associated with "
                "the given job."
            ),
            required=False,
            dataType="string",
        )

        registerPluginStaticContent(
            plugin="sivacor",
            css=["/style.css"],
            js=["/girder-plugin-sivacor.umd.cjs"],
            staticDir=Path(__file__).parent / "web_client" / "dist",
            tree=info["serverRoot"],
        )
