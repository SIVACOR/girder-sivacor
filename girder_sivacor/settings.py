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
    #: How long a submission may wait for the controller to give it a worker
    #: before it is failed. In minutes.
    #:
    #: Only meaningful under TARGETED_ASSIGNMENT, where a submission is RUNNING
    #: but genuinely idle until an instance is picked for it -- so neither the
    #: heartbeat nor the runtime rule applies, and without a bound of its own a
    #: submission an assigner never reaches would sit forever, holding the
    #: one-submission-per-user gate shut.
    ASSIGNMENT_TIMEOUT = "sivacor.assignment_timeout"  # in minutes
    #: The catalogue of worker sizes a submission may ask for.
    #:
    #: One object per rung: ``memory_gb`` (the advertised RAM figure, which is
    #: also the value that travels on the wire and in an exported workflow),
    #: ``flavor`` (the OpenStack name, server-side only -- nothing
    #: provider-specific may become visible to a researcher), ``vcpus`` and
    #: ``gated``.
    #:
    #: A Girder setting rather than a file or a fetched YAML because two
    #: processes need it and they share no other config channel: this plugin
    #: validates submissions against it, and the fleet controller reads it out
    #: of Mongo to know which flavour to boot. See P0.3 in
    #: development_notes/worker_sizing_plan.md.
    #:
    #: ``vcpus`` is duplicated here deliberately. This plugin holds no
    #: OpenStack credential, so it cannot ask Nova for a flavour's shape, yet
    #: it has to render "16 cores" in the picker. The controller -- the one
    #: process that *does* have credentials -- is what checks the duplicate
    #: against Nova at startup. Nothing else derivable is stored: root disk is
    #: flat at 60 GB across the ladder, SU/hr equals vCPU on Jetstream2, and
    #: usable memory is an approximation that must not be frozen into config.
    WORKER_SIZES = "sivacor.worker_sizes"

    #: Whether a submission is handed to a worker the fleet controller picked
    #: for it, instead of being published to the shared dispatch queue.
    #:
    #: A Girder setting, and specifically *one* setting, because two processes
    #: have to agree about it: this plugin decides whether ``submit_job``
    #: publishes, and the controller decides whether it assigns. If Girder
    #: publishes while the controller also assigns, two workers end up on one
    #: workspace; if neither does, nothing runs at all and the fleet looks like
    #: a healthy idle system. Two environment variables in two services can
    #: disagree about that; one setting cannot. See P2 in
    #: development_notes/worker_sizing_plan.md.
    #:
    #: A submission already in flight is never picked up by the other path:
    #: each one records which way it was routed in ``meta.awaiting_assignment``,
    #: and the controller assigns only what is marked there.
    #:
    #: **Disarming is only safe while something still consumes the shared queue.**
    #: Once a deployment sets ``SIVACOR_WORKER_QUEUES=private`` (P2 rollout step 4)
    #: its workers no longer subscribe to ``sivacor``, so turning this off makes
    #: ``submit_job`` publish to a queue with no consumer. Those submissions are
    #: marked ``awaiting_assignment: False``, so they miss the longer
    #: ``REAPED_NO_WORKER`` bound and are failed at ``sivacor.heartbeat_timeout``
    #: as ``REAPED_NO_HEARTBEAT`` -- telling the researcher their worker was lost
    #: when none was ever requested. Check what the fleet's workers subscribe to
    #: before flipping this off, not just what is in flight.
    TARGETED_ASSIGNMENT = "sivacor.targeted_assignment"

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
        # An hour: long enough to cover a full fleet working through a queue at
        # ~2 min a boot, short enough that a broken assigner tells the
        # researcher something within one sitting rather than at the 30-minute
        # heartbeat timeout, which would name the wrong cause.
        PluginSettings.ASSIGNMENT_TIMEOUT: 60.0,
        # Empty by default: a deployment with no Stata license set simply cannot
        # run Stata images, and says so at container-create time.
        PluginSettings.STATA_LICENSE: "",
        # Off: the shared dispatch queue is still how a submission reaches a
        # worker. Turn it on only against a controller that assigns -- an older
        # one ignores the setting, so nothing would ever be published.
        PluginSettings.TARGETED_ASSIGNMENT: False,
        # ONE entry, matching what production runs today (SIVACOR_OS_FLAVOR is
        # m3.large). A single rung means no submission can ask for anything
        # different, so recording the size and, later, assigning on it are both
        # exercised before any user can choose. Rungs are added once the
        # controller can boot a heterogeneous fleet.
        PluginSettings.WORKER_SIZES: [
            {"memory_gb": 60, "flavor": "m3.large", "vcpus": 16, "gated": False},
        ],
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
