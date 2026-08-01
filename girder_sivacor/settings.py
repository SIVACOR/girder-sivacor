from girder.settings import SettingDefault
from tro_utils import TROVCapability as Caps


class PluginSettings:
    UPLOADS_FOLDER_NAME = "sivacor.uploads_folder_name"
    SUBMISSION_COLLECTION_NAME = "sivacor.submission_collection_name"
    EDITORS_GROUP_NAME = "sivacor.editors_group_name"
    IMAGE_TAGS = "sivacor.image_tags"
    TRO_GPG_FINGERPRINT = "sivacor.tro_gpg_fingerprint"
    TRO_GPG_PASSPHRASE = "sivacor.tro_gpg_passphrase"
    TRO_PROFILE = "sivacor.tro_profile"
    RETENTION_DAYS = "sivacor.retention_days"  # in days
    BANNER_ENABLED = "sivacor.banner_enabled"
    BANNER_MESSAGE = "sivacor.banner_message"
    HEARTBEAT_TIMEOUT = "sivacor.heartbeat_timeout"  # in minutes
    MAX_RUNTIME = "sivacor.max_runtime"  # in hours
    #: Contents of ``stata.lic``, served to workers that run a Stata image.
    #:
    #: Ephemeral workers are stock VMs with no license on disk (D7: nothing
    #: provider-specific, so no baked image and no cloud secret manager), and
    #: cloud-init has no Girder credential -- the admin-scoped token only
    #: arrives with the task. So the license is fetched at run time, by the one
    #: step that needs it, using the token already in hand. Deliberately NOT in
    #: user-data: that would persist a vendor license in Nova's DB and put it on
    #: every worker rather than only the ones running Stata.
    STATA_LICENSE = "sivacor.stata_license"


SettingDefault.defaults.update(
    {
        PluginSettings.UPLOADS_FOLDER_NAME: "Uploads",
        PluginSettings.SUBMISSION_COLLECTION_NAME: "Submissions",
        PluginSettings.EDITORS_GROUP_NAME: "Editors",
        PluginSettings.RETENTION_DAYS: 7,
        PluginSettings.BANNER_ENABLED: False,
        PluginSettings.BANNER_MESSAGE: "",
        # Generous relative to the one-minute heartbeat: only the container run
        # ticks it, so the quiet stretches are whole pipeline steps -- zipping
        # and uploading a multi-gigabyte package chief among them. Better to
        # notice a dead worker late than to fail a live submission.
        PluginSettings.HEARTBEAT_TIMEOUT: 30.0,
        PluginSettings.MAX_RUNTIME: 24.0,
        # Empty by default: a deployment with no Stata license set simply cannot
        # run Stata images, and says so at container-create time.
        PluginSettings.STATA_LICENSE: "",
        PluginSettings.IMAGE_TAGS: {
            "dataeditors/stata15": ["latest", "2023-01-27"],
            "dataeditors/stata16": ["latest", "2023-06-13", "2022-10-14"],
            "dataeditors/stata17": ["latest", "2024-05-21", "2024-02-13"],
            "dataeditors/stata18-mp": ["2025-11-12", "2025-08-12", "2025-02-26"],
            "dataeditors/stata18_5-mp": ["2025-02-26", "2024-12-18", "2024-10-16"],
            "dataeditors/stata19_5-mp": ["2025-11-12", "2025-08-14", "2025-08-13"],
            "rocker/geospatial": [
                "4.5.2",
                "4.5.1",
                "4.5.0",
                "4.4.3",
                "4.4.2",
                "4.4.1",
                "4.4.0",
                "4.3.3",
                "4.3.2",
                "4.3.1",
                "4.3.0",
                "4.2.3",
                "4.2.2",
                "4.2.1",
                "4.2.0",
            ],
            "rocker/r-ver": [
                "4.5.2",
                "4.5.1",
                "4.5.0",
                "4.4.3",
                "4.4.2",
                "4.4.1",
                "4.4.0",
                "4.3.3",
                "4.3.2",
                "4.3.1",
                "4.3.0",
                "4.2.3",
                "4.2.2",
                "4.2.1",
                "4.2.0",
            ],
            "rocker/tidyverse": [
                "4.5.2",
                "4.5.1",
                "4.5.0",
                "4.4.3",
                "4.4.2",
                "4.4.1",
                "4.4.0",
                "4.3.3",
                "4.3.2",
                "4.3.1",
                "4.3.0",
                "4.2.3",
                "4.2.2",
                "4.2.1",
                "4.2.0",
            ],
            "rocker/verse": [
                "4.5.2",
                "4.5.1",
                "4.5.0",
                "4.4.3",
                "4.4.2",
                "4.4.1",
                "4.4.0",
                "4.3.3",
                "4.3.2",
                "4.3.1",
                "4.3.0",
                "4.2.3",
                "4.2.2",
                "4.2.1",
                "4.2.0",
            ],
        },
        PluginSettings.TRO_GPG_FINGERPRINT: "fingerprint",
        PluginSettings.TRO_GPG_PASSPHRASE: "passphrase",
        PluginSettings.TRO_PROFILE: {
            "rdfs:comment": "SIVACOR TRO profile",
            "trov:hasCapability": [
                {"@id": "trs/capability/1", "@type": Caps.ENV_ISOLATION.value},
                {
                    "@id": "trs/capability/2",
                    "@type": Caps.NET_ISOLATION.value,
                },
                {
                    "@id": "trs/capability/3",
                    "@type": Caps.NON_INTERACTIVE.value,
                },
                {
                    "@id": "trs/capability/4",
                    "@type": Caps.MACHINE_ENFORCEMENT.value,
                },
            ],
            "trov:owner": "SIVACOR Team",
            "trov:description": "SIVACOR AEA Infrastructure",
            "trov:contact": "admin@sivacor.org",
            "trov:url": "https://sivacor.org/",
            "trov:name": "sivacor",
        },
    }
)
