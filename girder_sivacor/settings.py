from girder.settings import SettingDefault

class PluginSettings:
    UPLOADS_FOLDER_NAME = "sivacor.uploads_folder_name"

SettingDefault.defaults.update(
    {
        PluginSettings.UPLOADS_FOLDER_NAME: "Uploads",
    }
)
