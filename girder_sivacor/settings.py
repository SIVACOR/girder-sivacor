from girder.settings import SettingDefault


class PluginSettings:
    UPLOADS_FOLDER_NAME = "sivacor.uploads_folder_name"
    SUBMISSION_COLLECTION_NAME = "sivacor.submission_collection_name"
    EDITORS_GROUP_NAME = "sivacor.editors_group_name"
    IMAGE_TAGS = "sivacor.image_tags"
    TRO_GPG_FINGERPRINT = "sivacor.tro_gpg_fingerprint"
    TRO_GPG_PASSPHRASE = "sivacor.tro_gpg_passphrase"
    TRO_PROFILE = "sivacor.tro_profile"


SettingDefault.defaults.update(
    {
        PluginSettings.UPLOADS_FOLDER_NAME: "Uploads",
        PluginSettings.SUBMISSION_COLLECTION_NAME: "Submissions",
        PluginSettings.EDITORS_GROUP_NAME: "Editors",
        PluginSettings.IMAGE_TAGS: ["latest", "stable", "experimental"],
        PluginSettings.TRO_GPG_FINGERPRINT: "fingerprint",
        PluginSettings.TRO_GPG_PASSPHRASE: "passphrase",
        PluginSettings.TRO_PROFILE: {
            "rdfs:comment": "SIVACOR TRO profile",
            "trov:hasCapability": [
                {"@id": "trs/capability/1", "@type": "trov:CanRecordInternetAccess"},
                {
                    "@id": "trs/capability/2",
                    "@type": "trov:CanProvideInternetIsolation",
                },
            ],
            "trov:owner": "SIVACOR Team",
            "trov:description": "SIVACOR AEA Infrastructure",
            "trov:contact": "email@goes.here",
            "trov:url": "https://sivacor.org/",
            "trov:name": "sivacor",
        },
    }
)
