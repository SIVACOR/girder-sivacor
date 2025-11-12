from pathlib import Path

from girder import events
from girder.constants import AccessType
from girder.exceptions import ValidationException
from girder.plugin import GirderPlugin, getPlugin, registerPluginStaticContent
from girder.utility import setting_utilities
from girder.utility.model_importer import ModelImporter
from girder_oauth.providers import addProvider
from girder_oauth.settings import PluginSettings as OAuthSettings

from .auth.orcid import ORCID
from .settings import PluginSettings


@setting_utilities.validator(PluginSettings.UPLOADS_FOLDER_NAME)
def _validate_uploads_folder_name(value):
    if not isinstance(value, str) or not value:
        raise ValidationException("Uploads folder name must be a non-empty string.")
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


class SIVACORPlugin(GirderPlugin):
    DISPLAY_NAME = "SIVACOR"

    def load(self, info):
        events.bind("model.user.save.created", "sivacor", create_uploads_folder)
        ModelImporter.model("user").exposeFields(
            level=AccessType.READ, fields=("lastJobId", "lastProjectId")
        )

        getPlugin("oauth").load(info)
        OAuthSettings.ORCID_CLIENT_ID = "oauth.orcid_client_id"
        OAuthSettings.ORCID_CLIENT_SECRET = "oauth.orcid_client_secret"
        addProvider(ORCID)

        registerPluginStaticContent(
            plugin="sivacor",
            css=["/style.css"],
            js=["/girder-plugin-sivacor.umd.cjs"],
            staticDir=Path(__file__).parent / "web_client" / "dist",
            tree=info["serverRoot"],
        )
