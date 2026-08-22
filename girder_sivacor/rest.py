import datetime
import json
import logging
import math
import os
from zoneinfo import ZoneInfo

import requests
import yaml
from girder.api import access
from girder.api.describe import Description, autoDescribeRoute
from girder.api.rest import Resource, boundHandler, filtermodel
from girder.constants import AccessType, TokenScope
from girder.exceptions import AccessException, RestException, ValidationException
from girder.models.collection import Collection
from girder.models.file import File
from girder.models.folder import Folder
from girder.models.group import Group
from girder.models.setting import Setting
from girder.models.token import Token
from girder.models.user import User
from girder_jobs.constants import REST_CREATE_JOB_TOKEN_SCOPE, JobStatus
from girder_jobs.models.job import Job
from girder_plugin_worker.utils import getWorkerApiUrl

from .errors import FailureCode
from .models.execution_record import ExecutionRecord
from .settings import PluginSettings
from .telemetry import sanitize_record
from .utils import encrypt_job_secrets
from .worker_plugin.routing import DISPATCH_QUEUE
from .worker_plugin.run_submission import (
    create_workspace,
    execute_workflow,
    finalize_job,
    prepare_submission,
    prune_workspace,
    run_tro,
    sign_tro,
    upload_workspace,
)

logger = logging.getLogger(__name__)

#: How long the token handed to the worker stays valid. It has to outlive the
#: longest replication run, but it grants admin access, so not by much.
WORKER_TOKEN_DAYS = 7


def worker_sizes():
    """The size catalogue, as the operator configured it.

    Sorted by ``memory_gb`` so "the smallest" is well defined however the
    setting was written.
    """
    sizes = Setting().get(PluginSettings.WORKER_SIZES) or []
    return sorted(sizes, key=lambda entry: entry["memory_gb"])


def default_worker_size():
    """The size a submission gets when it does not ask for one.

    Derived -- the smallest ungated entry -- rather than configured, so there is
    no second field that can contradict the catalogue. The validator guarantees
    at least one ungated entry exists.
    """
    catalogue = worker_sizes()
    if not catalogue:
        # The validator refuses an empty catalogue, so this is a deployment
        # whose setting was written before it existed. Say which setting, rather
        # than raising an IndexError that reads as a bug in the submission path.
        raise ValidationException(
            f"No worker sizes are configured ({PluginSettings.WORKER_SIZES})."
        )
    for entry in catalogue:
        if not entry["gated"]:
            return entry["memory_gb"]
    # Likewise unreachable while the validator holds. Falling back to the
    # smallest rung beats refusing every submission on the deployment.
    return catalogue[0]["memory_gb"]


def may_select_gated_sizes(user):
    """Whether ``user`` is allowed to ask for a ``gated`` rung (S5 guard 2).

    Membership of the group named by ``sivacor.worker_size_group_name``, or site
    admin. Admins bypass because they can add themselves to that group anyway,
    so refusing them buys nothing and takes away the operator's ability to
    exercise a gated rung while testing it.

    Fails closed: an anonymous caller, a missing group, or a user who is not a
    member all get ``False``, because the gated rungs are the expensive ones.
    """
    if user is None:
        return False
    if user.get("admin"):
        return True
    group_name = Setting().get(PluginSettings.WORKER_SIZE_GROUP_NAME)
    group = Group().findOne({"name": group_name})
    if group is None:
        # Refusing everyone is the safe direction, but to the researcher it
        # looks exactly like not being a member -- and to an operator who
        # mistyped the setting it looks like a broken gate. Say which it is
        # here, since nothing user-facing can.
        logger.warning(
            "No group named %r (%s), so no user may select a gated worker size.",
            group_name,
            PluginSettings.WORKER_SIZE_GROUP_NAME,
        )
        return False
    return group["_id"] in (user.get("groups") or [])


def _gated_access(user, catalogue):
    """``may_select_gated_sizes``, asked only when the catalogue gates anything.

    Skipping it matters: the check is a database lookup, and on a deployment
    whose catalogue is entirely ungated -- which is every deployment until the
    upper rungs are added -- it would log a missing-group warning on every
    submission, for a group nobody has any reason to have created.
    """
    if not any(entry["gated"] for entry in catalogue):
        return False
    return may_select_gated_sizes(user)


def resolve_worker_size(workflow, user):
    """Return the ``memory_gb`` a workflow asked for, validated.

    Raises :class:`ValidationException` naming the selectable sizes, rather than
    saying "invalid": the catalogue is a published contract -- an exported
    workflow carries a bare number -- so a submission rejected because a rung
    was withdrawn has to be told what it may ask for instead.

    ``user`` is required rather than defaulted, even though ``None`` is a
    meaningful value here: a caller that forgets to pass it would otherwise
    silently refuse every gated rung, which is the safe direction but is also
    invisible.
    """
    requested = (workflow.get("resources") or {}).get("memory_gb")
    if requested is None:
        return default_worker_size()
    catalogue = worker_sizes()
    # Once per call, not once per entry: it is a database lookup, and both the
    # message and the refusal below have to agree about the answer.
    gated_ok = _gated_access(user, catalogue)
    selectable = [
        entry["memory_gb"] for entry in catalogue if gated_ok or not entry["gated"]
    ]
    match = next(
        (entry for entry in catalogue if entry["memory_gb"] == requested), None
    )
    if match is None:
        raise ValidationException(
            f"Unknown worker size: {requested} GB. "
            f"Available sizes: {', '.join(str(size) for size in selectable)}."
        )
    if match["gated"] and not gated_ok:
        raise ValidationException(
            f"The {requested} GB worker is not self-service. "
            "Contact support@sivacor.org to request it. "
            f"Available sizes: {', '.join(str(size) for size in selectable)}."
        )
    return requested


def targeted_assignment():
    """Whether the fleet controller, rather than a shared queue, routes work."""
    return bool(Setting().get(PluginSettings.TARGETED_ASSIGNMENT))


#: Field on the Girder user document holding that user's scratch-volume ceiling,
#: in GB. Absent or ``0`` means *not approved*, which is every user until an
#: administrator says otherwise.
#:
#: **One field carries all three of the feature's access rules** -- off by
#: default, approved users only, and a per-user maximum -- which is why it is a
#: field and not a group. ``sivacor.worker_size_group_name`` gates a *boolean*
#: capability, and a Girder group has no per-member payload, so a group cannot
#: express "Alice may have 200 GB, Bob 50". See V3 in
#: development_notes/cinder_volumes_plan.md.
#:
#: camelCase to match the other plugin-owned user fields (``lastJobId``,
#: ``lastProjectId``); a dotted key would be a Mongo field-name hazard.
USER_VOLUME_QUOTA_FIELD = "sivacorMaxVolumeGb"

#: Volume sizes are rounded up to a multiple of this, in GB.
#:
#: Cinder accepts any whole number of gigabytes, so this is for legibility, not
#: for the API: a reservation spent in 10 GB units is one an operator can reason
#: about against :attr:`PluginSettings.VOLUME_TOTAL_GB`, where a ledger of 37s
#: and 113s is not. Rounding *up* so the researcher never gets less than they
#: asked for.
VOLUME_GRANULARITY_GB = 10


def volumes_enabled():
    """Whether this deployment offers scratch volumes at all."""
    return bool(Setting().get(PluginSettings.VOLUMES_ENABLED))


def volume_total_gb():
    """This deployment's reserved slice of the Cinder gigabytes quota."""
    return int(Setting().get(PluginSettings.VOLUME_TOTAL_GB) or 0)


def user_volume_quota(user):
    """The scratch-volume ceiling for ``user``, in GB. ``0`` means not approved.

    Fails closed on every uncertain input -- anonymous, a missing field, a
    negative or non-integer value someone wrote straight into Mongo. The
    resource being guarded is a shared quota that production's assetstore draws
    on, so "we could not establish a ceiling" has to mean zero.

    **Site admins are not special here, unlike the worker-size gate.** There, an
    admin bypasses because they could add themselves to the group anyway, so
    refusing them only cost the operator the ability to test a gated rung. A
    ceiling is a *number*, and there is no equivalent "they could grant it to
    themselves anyway" argument that produces one -- an admin who wants a volume
    sets their own field, which is the same operator action as for anyone else,
    and leaves a record of what was granted. It also means the value an admin
    tests with is the value a researcher would get.
    """
    if not user:
        return 0
    value = user.get(USER_VOLUME_QUOTA_FIELD)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return 0
    return value


#: Terminal job statuses, i.e. the point after which the volume is on its way out.
_TERMINAL_JOB_STATUSES = (JobStatus.SUCCESS, JobStatus.ERROR, JobStatus.CANCELED)


def _naive_utc(value):
    """Girder writes job timestamps as naive UTC; normalise anything else to match."""
    if not isinstance(value, datetime.datetime):
        return None
    return value.replace(tzinfo=None) if value.tzinfo else value


def _volume_hours(job, now):
    """Hours a submission's volume plausibly existed for.

    Measured from the job's first RUNNING stamp (falling back to ``created``) to
    its first terminal stamp (falling back to ``updated``, or to *now* while it
    is still in flight).

    **This brackets the submission, not the volume, and the error is known in
    both directions.** The volume is created moments before its instance and so
    starts a little *after* RUNNING -- under targeted assignment `submit_job`
    marks the job RUNNING and leaves the placement to the controller, which can
    take until ``sivacor.assignment_timeout`` -- and it is destroyed at the
    *reap*, up to ~20 minutes after the run ends (10 min boot grace + 5 idle +
    the timer; measured at ~18 min in CV4). So a figure here is good to within a
    reap tail, which is what "who is spending the storage grant" needs. It is
    not a billing record and must not be presented as one.
    """
    stamps = job.get("timestamps") or []

    def first(statuses):
        times = [
            _naive_utc(stamp.get("time"))
            for stamp in stamps
            if stamp.get("status") in statuses and _naive_utc(stamp.get("time"))
        ]
        return min(times) if times else None

    start = first((JobStatus.RUNNING,)) or _naive_utc(job.get("created"))
    end = first(_TERMINAL_JOB_STATUSES)
    if end is None:
        end = (
            now
            if job.get("status") in ACTIVE_JOB_STATUSES
            else _naive_utc(job.get("updated"))
        )
    if start is None or end is None or end <= start:
        return 0.0
    return (end - start).total_seconds() / 3600.0


def volume_usage():
    """Per-user scratch-volume accounting: C5.2 of cinder_volumes_plan.md.

    The precondition for approving a second account, because the first question a
    second approved user creates is "who is spending the storage grant" -- and
    until this existed there was no way to answer it.

    **Derived from job documents rather than from a ledger, deliberately.** A
    permanent per-user record of who used how much storage would be a *new*
    store of personal data, and the one store that outlives a submission here
    (``sivacor_execution_record``) is lawful precisely because it carries no
    identifier at all. Rather than argue that exception open, this reads what is
    already there and inherits the submissions' own retention: the figures below
    reach back exactly as far as ``sivacor.retention_days``, and no further. An
    operator who needs a longer history should export this, not make the server
    remember it.

    Approved accounts with no volume submissions are listed too, with zeroes. An
    allowance nobody has spent is still an allowance against the quota, and
    leaving it out would make the grant look smaller than it is.
    """
    # Naive UTC, to match what pymongo hands back: Girder writes aware UTC
    # timestamps, but reads them from Mongo without a timezone, so a job just
    # written and a job loaded later disagree about tzinfo. _naive_utc settles
    # that for the stored values; this is the same convention for "now".
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    users = {}

    def slot(user_id, user=None):
        key = str(user_id)
        if key not in users:
            user = user or User().load(user_id, force=True)
            users[key] = {
                "user_id": key,
                "login": (user or {}).get("login"),
                "ceiling_gb": user_volume_quota(user),
                "submissions": 0,
                "gb_hours": 0.0,
                "largest_gb": 0,
                "live_gb": 0,
                "last_at": None,
            }
        return users[key]

    for user in User().find({USER_VOLUME_QUOTA_FIELD: {"$gt": 0}}):
        slot(user["_id"], user)

    jobs = Job().collection.find(
        {
            "type": "sivacor_submission",
            "meta.requested_disk_gb": {"$exists": True, "$ne": None},
        },
        # Never pull kwargs: they carry the submission's encrypted secrets, and
        # an aggregation has no business holding them even in memory.
        projection={
            "userId": 1,
            "status": 1,
            "created": 1,
            "updated": 1,
            "timestamps": 1,
            "meta.requested_disk_gb": 1,
        },
    )
    for job in jobs:
        if not job.get("userId"):
            continue
        gb = (job.get("meta") or {}).get("requested_disk_gb")
        if not isinstance(gb, int) or isinstance(gb, bool) or gb <= 0:
            continue
        entry = slot(job["userId"])
        entry["submissions"] += 1
        entry["gb_hours"] += gb * _volume_hours(job, now)
        entry["largest_gb"] = max(entry["largest_gb"], gb)
        if job.get("status") in ACTIVE_JOB_STATUSES:
            entry["live_gb"] += gb
        created = _naive_utc(job.get("created"))
        if created and (entry["last_at"] is None or created > entry["last_at"]):
            entry["last_at"] = created

    rows = []
    for entry in users.values():
        entry["gb_hours"] = round(entry["gb_hours"], 2)
        entry["last_at"] = entry["last_at"].isoformat() + "Z" if entry["last_at"] else None
        rows.append(entry)
    # Biggest spender first: the operator's question is who, not when.
    rows.sort(key=lambda row: (-row["gb_hours"], row["login"] or ""))

    return {
        "enabled": volumes_enabled(),
        "deployment_gb": volume_total_gb(),
        "granularity_gb": VOLUME_GRANULARITY_GB,
        # The window these figures cover, so a small total is not mistaken for a
        # quiet month when it is really a short retention window.
        "retention_days": Setting().get(PluginSettings.RETENTION_DAYS),
        "users": rows,
        "totals": {
            "approved_users": sum(1 for row in rows if row["ceiling_gb"] > 0),
            "granted_gb": sum(row["ceiling_gb"] for row in rows),
            "submissions": sum(row["submissions"] for row in rows),
            "gb_hours": round(sum(row["gb_hours"] for row in rows), 2),
            # What is out right now, against the deployment's reservation: the
            # one figure that says whether the next request can be honoured.
            "live_gb": sum(row["live_gb"] for row in rows),
        },
    }


def resolve_volume_gb(workflow, user):
    """Return the scratch-volume size this workflow may have, in GB, or ``None``.

    ``None`` means *no volume*, and it is what a workflow that does not mention
    ``resources.disk_gb`` gets. It is deliberately distinct from ``0``: the
    absent case must take a code path with no volume in it at all, which is what
    makes this feature inert by default and hard to regress into.

    Raises :class:`ValidationException` for the four refusals in V8. Each names
    a different thing because they are different problems: two of them the
    researcher can do nothing about, and one -- over their own ceiling -- they
    can act on alone, so it names the ceiling rather than saying "too large".

    ``user`` is required rather than defaulted, for the same reason
    :func:`resolve_worker_size`'s is: a caller that forgot it would silently
    refuse everyone, which is the safe direction but an invisible one.
    """
    requested = (workflow.get("resources") or {}).get("disk_gb")
    if requested is None:
        return None

    # Shape first, so a nonsense value is not reported as a permissions problem.
    # The schema already bounds this; submit_job re-checks because the root
    # object has no additionalProperties: False, so an unvalidated field would
    # otherwise be silently ignored rather than rejected.
    if isinstance(requested, bool) or not isinstance(requested, int) or requested <= 0:
        raise ValidationException(
            "resources.disk_gb must be a positive whole number of gigabytes."
        )

    if not volumes_enabled():
        raise ValidationException(
            "Extra scratch disk is not available on this deployment."
        )

    # **The fleet cannot honour this unless it is placing submissions itself.** The
    # volume's size comes from this submission's own record, and the controller only
    # reads that when it assigns; demand derived from queue *depth* carries no
    # submission at all, so on the shared-queue path a volume is never created however
    # large a disk was asked for. Accepting the request anyway is the exact shape of
    # promise-silently-broken that one-setting-for-two-processes exists to prevent, so
    # it is refused here rather than discovered as an out_of_disk failure an hour later.
    #
    # A capacity message, not a permissions one: the researcher did nothing wrong and
    # can do nothing about it.
    if not targeted_assignment():
        raise ValidationException(
            "Extra scratch disk cannot be provided on this deployment right now. "
            "Contact support@sivacor.org."
        )

    ceiling = user_volume_quota(user)
    if ceiling <= 0:
        raise ValidationException(
            "Extra scratch disk needs approval. Contact support@sivacor.org to "
            "request it."
        )

    # Round before comparing, or a request of ceiling-minus-one is accepted and
    # then silently rounded past the ceiling it was checked against.
    granted = (
        math.ceil(requested / VOLUME_GRANULARITY_GB) * VOLUME_GRANULARITY_GB
    )

    if granted > ceiling:
        raise ValidationException(
            f"{requested} GB of extra scratch disk is more than your "
            f"{ceiling} GB limit. Ask for {ceiling} GB or less, or contact "
            "support@sivacor.org to raise it."
        )

    reservation = volume_total_gb()
    if granted > reservation:
        # A capacity message, not a permissions one: this user *is* approved for
        # the size they asked for, and the deployment is what cannot supply it.
        # Telling an approved user to "request access" would send them to ask
        # for something they already have.
        raise ValidationException(
            f"{requested} GB of extra scratch disk is more than this deployment "
            f"currently has available ({reservation} GB). Contact "
            "support@sivacor.org."
        )
    return granted


def build_submission_chain(job, file, stages, secrets):
    """Assemble the celery chain that runs one submission.

    Split out of :meth:`SIVACOR.submit_job` so the fleet controller can build
    the same chain at assignment time, once it has picked an instance, and
    publish it straight to that instance's private queue. There must be exactly
    one builder: a second copy in another repo is a scheduling policy that can
    drift, which is the thing S2/S3 exist to prevent. See P2 in
    development_notes/worker_sizing_plan.md.

    Builds only -- the caller publishes. Under targeted assignment the claim and
    the publish have to be ordered against each other (claim first, always: the
    other order publishes the chain twice if the tick dies between them), and
    that ordering belongs where the claim is.
    """
    # The worker has no database of its own; it reaches back over REST as
    # an administrator. girder_worker copies these headers from a running
    # task onto the next one it publishes, but the chain is built here, so
    # set them on every step rather than relying on that.
    admin = User().findOne({"admin": True})
    # REST_CREATE_JOB_TOKEN_SCOPE is not optional. girder_worker's
    # girder_before_task_publish POSTs to /job to record a child job for every
    # step it publishes, and that endpoint is
    # @access.token(scope=REST_CREATE_JOB_TOKEN_SCOPE, required=True) -- which a
    # plain USER_AUTH token does not satisfy even for an administrator. Without
    # it every step logs "Failed to post job: HTTP error 403 ... Invalid token
    # scope" and carries on, so the submission still runs but no child job is
    # ever created: GET /job/:id/children comes back empty, per-step progress
    # vanishes from the UI, and the reaper has no child jobs to settle.
    worker_token = str(
        Token().createToken(
            user=admin,
            days=WORKER_TOKEN_DAYS,
            scope=[TokenScope.USER_AUTH, REST_CREATE_JOB_TOKEN_SCOPE],
        )["_id"]
    )
    api_url = getWorkerApiUrl()

    def step(signature, title):
        return signature.set(
            girder_job_title=title,
            girder_api_url=api_url,
            girder_client_token=worker_token,
        )

    workflow = step(
        prepare_submission.s(
            str(job["userId"]),
            str(file["_id"]),
            stages,
            str(job["_id"]),
            job.get("meta", {}).get("requested_memory_gb"),
            job.get("meta", {}).get("requested_disk_gb"),
        ),
        f"Moving {file['name']} to submission collection",
    )
    workflow |= step(create_workspace.s(), "Create Workspace")
    workflow |= step(run_tro.s("add_arrangement", 0, None), "Record initial arrangement")
    for i, stage in enumerate(stages):
        workflow |= step(
            execute_workflow.s(stage, secrets), "Execute SIVACOR Workflow"
        )
        workflow |= step(
            run_tro.s("add_arrangement", i + 1, None), "Record final arrangement"
        )
        workflow |= step(
            run_tro.s("add_performance", i, None), "Record user workflow TRP"
        )
    workflow |= step(prune_workspace.s(), "Prune Workspace")
    workflow |= step(
        run_tro.s("add_arrangement", len(stages) + 1, "is_pruned"),
        "Record final pruned arrangement",
    )
    workflow |= step(
        run_tro.s("prune_performance", len(stages), "is_pruned"),
        "Record workspace prune TRP",
    )
    # Signing runs on the manager, not on the worker holding the workspace --
    # sign_tro is declared on LOCAL_QUEUE and is exempt from pin_chain. See
    # routing.UNPINNED_TASKS.
    workflow |= step(sign_tro.s(), "Sign TRO")
    workflow |= step(upload_workspace.s(), "Upload Replicated Package")
    workflow |= step(finalize_job.s(), "Finalize Job Submission")
    return workflow


def _as_utc(value):
    """Make a Mongo timestamp safe to compare against an aware ``now``.

    Whether pymongo hands back aware datetimes depends on how the client was
    built, and a naive one turns the reaper's comparisons into a TypeError
    rather than a wrong answer -- silent in a periodic task.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value


def _iso8601(value):
    """Format a Mongo timestamp as RFC 3339 with a literal ``Z``.

    Girder's own JSON encoder emits ``+00:00`` instead
    (``girder/utility/__init__.py:130``), so anything that has to match the
    manifest's documented ``...Z`` shape has to format itself rather than hand a
    datetime back and let the encoder do it.
    """
    if value is None:
        return None
    return (
        _as_utc(value)
        .astimezone(datetime.timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


#: The manifest schema version. sivacor-girderfs refuses to mount against a
#: version it does not know, so bump this for any incompatible change to the
#: shape emitted by get_fs_manifest -- adding a field is not one.
FS_MANIFEST_VERSION = 1

#: How many items' files to fetch per Mongo query when building a manifest.
#: Trades round trips against the size of a single $in; 1000 puts a 100k-node
#: tree at ~50 queries instead of ~50 000.
_MANIFEST_FILE_BATCH = 1000


stage_schema = {
    "$schema": "http://json-schema.org/draft-04/schema#",
    "title": "Workflow",
    "description": "A SIVACOR replication workflow.",
    "type": "object",
    "properties": {
        "stages": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "image_name": {"type": "string"},
                    "main_file": {"type": "string"},
                    "image_tag": {"type": "string"},
                    "network_isolation": {"type": "boolean", "default": False},
                },
                "required": ["image_name", "main_file", "image_tag"],
                "additionalProperties": False,
            },
            "minItems": 1,
        },
        "env_secrets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["key", "value"],
                "additionalProperties": False,
            },
        },
        # Workflow-level, not per-stage: pin_chain binds every stage of a
        # submission to one machine, so a per-stage size would be a lie this
        # schema was endorsing.
        #
        # Typed only, not an enum of the catalogue's figures. The catalogue is a
        # Girder setting, so an enum here would either be a second hardcoded
        # copy of it or would make this schema dynamic -- and
        # get_workflow_schema serves it verbatim precisely so the UI validates
        # against the same object the server does. The value is checked against
        # the live catalogue in submit_job instead, which is mandatory rather
        # than belt-and-braces: the root object has no additionalProperties
        # False, so an unknown size would otherwise be silently ignored rather
        # than rejected. Clients read the selectable values from
        # GET /sivacor/worker_sizes.
        "resources": {
            "type": "object",
            "properties": {
                "memory_gb": {"type": "integer", "minimum": 1},
                # Absent means no volume, which is every submission until an
                # approved user asks. Deliberately not an enum and not bounded
                # here: the real ceiling is per user (V3), so the schema can
                # only say "a positive whole number of gigabytes" and
                # resolve_volume_gb owns the rest. A bound here would either
                # duplicate the per-user ceiling or contradict it.
                "disk_gb": {"type": "integer", "minimum": 1},
            },
            "additionalProperties": False,
        },
    },
    "required": ["stages"],
}

# Statuses in which a submission is still in flight. A user is allowed at most
# one submission in one of these states at a time.
ACTIVE_JOB_STATUSES = (JobStatus.INACTIVE, JobStatus.QUEUED, JobStatus.RUNNING)


class SIVACOR(Resource):
    def __init__(self):
        super(SIVACOR, self).__init__()
        self.resourceName = "sivacor"
        self.route("POST", ("submit_job",), self.submit_job)
        self.route("POST", ("cleanup",), self.cleanup_submissions)
        self.route("POST", ("reap",), self.reap_submissions)
        self.route("POST", ("heartbeat", ":id"), self.heartbeat)
        self.route("POST", ("claim", ":id"), self.claim)
        self.route("POST", ("execution_record",), self.record_execution)
        self.route("GET", ("execution_record",), self.list_execution_records)
        self.route(
            "GET", ("execution_record", "summary"), self.summarise_execution_records
        )
        self.route("GET", ("image_tags",), self.get_image_tags)
        self.route("GET", ("worker_sizes",), self.get_worker_sizes)
        self.route("GET", ("volume_quota",), self.get_volume_quota)
        self.route("GET", ("volume_usage",), self.get_volume_usage)
        self.route("PUT", ("user", ":id", "volume_quota"), self.set_volume_quota)
        self.route("GET", ("workflow_schema",), self.get_workflow_schema)
        self.route("GET", ("fs", "manifest"), self.get_fs_manifest)
        self.route("DELETE", ("submission", ":id"), self.delete_submission)

    @access.user
    @autoDescribeRoute(
        Description("Submit a job to SIVACOR.")
        .modelParam(
            "id",
            "The ID of the file to process.",
            model=File,
            level=AccessType.ADMIN,
            required=True,
            paramType="query",
        )
        .jsonParam(
            "workflow",
            "The defintion of replication workflow stages to execute.",
            paramType="body",
            requireObject=True,
            required=True,
            schema=stage_schema,
        )
        .errorResponse("You already have a submission in progress.", 409)
    )
    @filtermodel(model=Job)
    def submit_job(self, file, workflow):
        # Only one submission per user may be in flight at a time.
        user = self.getCurrentUser()
        if active := self._active_submission(user):
            raise self._active_submission_error(active)

        stages = workflow.get("stages", [])
        env_secrets = encrypt_job_secrets(workflow.get("env_secrets", []))
        tags = self._get_tags()
        for stage in stages:
            image_name = stage.get("image_name")
            tag = stage.get("image_tag")
            image_reference = f"{image_name}:{tag}"
            if image_name not in tags or tag not in tags.get(image_name, []):
                raise ValidationException(f"Invalid image: {image_reference}")

        # Resolved server-side, before anything is created: an unknown size must
        # never reach the controller, where the flavour it names does not exist,
        # create_instance raises, and three of those trip the circuit breaker
        # and stop the whole fleet.
        # Re-validated here even though the picker only offers what the user may
        # have: the gate is server-side or it is not a gate, and the value can
        # also arrive from an imported workflow file that was exported by
        # somebody who *is* a member.
        requested_memory_gb = resolve_worker_size(workflow, user)
        # Same reasoning as the size above, and the same place: server-side,
        # before anything is created. Unlike the size this can be None, meaning
        # no volume -- which is every submission on a deployment that has not
        # turned the feature on. C1 in development_notes/cinder_volumes_plan.md.
        requested_disk_gb = resolve_volume_gb(workflow, user)

        assigned = targeted_assignment()
        other_fields = {
            "meta": {
                # Recorded at submit time, unlike meta.worker_queue, which only
                # exists once a worker has been chosen. The pair is what tells
                # "asked for N" from "ran on a box that had N".
                "requested_memory_gb": requested_memory_gb,
                # None means no volume. Recorded either way so that "did not
                # ask" and "asked and got it" are distinguishable in the job
                # document without inferring from the absence of a field.
                "requested_disk_gb": requested_disk_gb,
                # Which of the two routes this submission took, recorded per
                # submission rather than read from the setting later. The
                # setting can be flipped while this one is in flight, and a
                # submission already published to the dispatch queue must never
                # then be assigned as well -- that is two workers on one
                # workspace. The controller assigns only what is marked here.
                "awaiting_assignment": assigned,
            }
        }
        if assigned:
            # What the controller needs to build the chain once it has picked an
            # instance. Deliberately not under meta: filtermodel drops unexposed
            # top-level fields, so the secret envelope cannot leave the server
            # in a job document. (It is the same ciphertext that otherwise sits
            # in the broker message, so this is not a new exposure -- but there
            # is no reason to widen it either.)
            other_fields["sivacorChain"] = {
                "fileId": file["_id"],
                "stages": stages,
                "secrets": env_secrets,
            }

        job = Job().createJob(
            title=f"SIVACOR Run for {file['name']} by {user['firstName']} {user['lastName']}",
            type="sivacor_submission",
            public=False,
            user=user,
            otherFields=other_fields,
        )
        # Close the window between the check above and the job creation: two
        # concurrent requests can both pass it, so whoever ends up with the
        # older job wins and the other one is rolled back. Nothing has been
        # dispatched yet, so removing the job is enough to undo it.
        if active := self._active_submission(user, older_than=job["_id"]):
            Job().remove(job)
            raise self._active_submission_error(active)

        User().collection.update_one(
            {"_id": user["_id"]}, {"$set": {"lastJobId": job["_id"]}}
        )
        zone = ZoneInfo("America/Chicago")
        timestamp = f"[{datetime.datetime.now().astimezone(zone).replace(microsecond=0).isoformat()}]"
        job = Job().updateJob(
            job, f"{timestamp} Preparing SIVACOR submission\n", status=JobStatus.RUNNING
        )

        if assigned:
            # Nothing is published: the controller picks an instance and
            # publishes the chain to that instance's private queue. Until it
            # does, the submission is RUNNING with no activity of its own, which
            # the reaper is taught to leave alone.
            return job

        try:
            build_submission_chain(job, file, stages, env_secrets).apply_async(
                queue=DISPATCH_QUEUE
            )
        except Exception:
            logger.exception("Failed to dispatch submission %s", str(job["_id"]))
        return job

    @staticmethod
    def _active_submission(user, older_than=None):
        """Return the user's oldest unfinished submission job, if any.

        :param older_than: only consider jobs created before this job id.
        """
        query = {
            "userId": user["_id"],
            "type": "sivacor_submission",
            "status": {"$in": list(ACTIVE_JOB_STATUSES)},
        }
        if older_than is not None:
            query["_id"] = {"$lt": older_than}
        return Job().findOne(query, sort=[("created", 1)])

    @staticmethod
    def _active_submission_error(job):
        return RestException(
            f"You already have a submission in progress ('{job['title']}'). "
            "Please wait for it to finish, or cancel it, before submitting "
            "a new one.",
            code=409,
            extra=str(job["_id"]),
        )

    @access.admin
    @autoDescribeRoute(
        Description("Remove submissions older than the retention window.").notes(
            "Called periodically by the celery worker. It runs here rather than "
            "on the worker because it deletes jobs by query, which the REST API "
            "cannot express."
        )
    )
    def cleanup_submissions(self):
        root_collection = Collection().findOne(
            {"name": Setting().get(PluginSettings.SUBMISSION_COLLECTION_NAME)}
        )
        if not root_collection:
            return {"removed": 0}

        admin = self.getCurrentUser()
        cutoff_time = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
            days=Setting().get(PluginSettings.RETENTION_DAYS)
        )
        removed = 0
        # Materialize the cursor: removing folders while iterating it can make
        # Mongo skip documents.
        folders = list(Folder().childFolders(root_collection, "collection", user=admin))
        for folder in folders:
            if folder["created"] > cutoff_time:
                continue
            if job_id := folder.get("meta", {}).get("job_id"):
                if main_job := Job().load(job_id, force=True):
                    Job().remove(main_job)
                Job().collection.delete_many({"args.0.job_id": job_id})
            logger.info(
                "Cleaning up submission folder %s created at %s",
                str(folder["_id"]),
                folder["created"],
            )
            Folder().remove(folder)
            removed += 1
        return {"removed": removed}

    @access.admin
    @autoDescribeRoute(
        Description("Record a liveness heartbeat for a running submission.")
        .notes(
            "Called by the worker while a container runs. A submission can "
            "execute for hours without logging anything, so the job's "
            "'updated' timestamp is not by itself a liveness signal -- this is "
            "what tells /sivacor/reap the worker is still there."
        )
        .modelParam(
            "id",
            "The ID of the submission job.",
            model=Job,
            force=True,
            required=True,
        )
    )
    def heartbeat(self, job):
        now = datetime.datetime.now(datetime.timezone.utc)
        # Straight to the collection rather than through updateJob(): that
        # fires jobs.job.update.after, and re-running the status handler --
        # folder write, possibly an email -- once a minute per submission is
        # not what a heartbeat should cost.
        Job().collection.update_one(
            {"_id": job["_id"]}, {"$set": {"meta.heartbeat": now}}
        )
        return {"heartbeat": now}

    @access.admin
    @autoDescribeRoute(
        Description("Record which worker queue a submission was claimed by.")
        .notes(
            "Called once by prepare_submission, right after the chain is pinned. "
            "This is what lets the autoscaler tell a *spent* worker from an "
            "available one: an ephemeral worker stops consuming the dispatch "
            "queue the moment it claims a submission, so counting it as capacity "
            "makes the controller refuse to create the instance the next "
            "submission needs. See D8 in autoscaling_plan.md.\n\n"
            "Deliberately recorded server-side rather than read back over the "
            "broker: 'celery inspect active_queues' would answer the same "
            "question, but a worker whose broker connection has died cannot "
            "answer it -- and that is exactly the case where the number matters."
        )
        .modelParam(
            "id",
            "The ID of the submission job.",
            model=Job,
            force=True,
            required=True,
        )
        .param("queue", "The worker's private queue name.", required=True)
    )
    def claim(self, job, queue):
        # Same reasoning as heartbeat: bypass updateJob() so this cannot trigger
        # the status handler's folder write and email.
        Job().collection.update_one(
            {"_id": job["_id"]}, {"$set": {"meta.worker_queue": queue}}
        )
        return {"worker_queue": queue}

    @access.admin
    @autoDescribeRoute(
        Description("Store an anonymous record of how a submission executed.")
        .notes(
            "Called once by the worker when a submission reaches a terminal "
            "state. These records outlive the submission -- they are what the "
            "deployment can still report on after the user deletes their data "
            "or the retention sweep does.\n\n"
            "Nothing stored here identifies a person: no user, job or folder "
            "id, no filename, no worker instance. The body is not stored as "
            "sent -- it is rebuilt field by field from an allow-list, so a "
            "worker cannot widen what is kept by sending more."
        )
        .jsonParam(
            "record",
            "The execution record, as built by the worker.",
            paramType="body",
            requireObject=True,
        )
    )
    def record_execution(self, record):
        return self.store_execution_record(record)

    @staticmethod
    def store_execution_record(payload):
        """Filter and persist one execution record. Returns what was stored."""
        # The date is stamped here, not taken from the payload, and is
        # deliberately only a date: at pilot scale a wall-clock submission time
        # is close enough to an identifier to undo the rest of this.
        today = datetime.datetime.now(datetime.timezone.utc).date().isoformat()
        document = sanitize_record(
            payload,
            today,
            allowed_sizes=[entry["memory_gb"] for entry in worker_sizes()],
        )
        ExecutionRecord().save(document)
        return document

    @access.admin
    @autoDescribeRoute(
        Description("List anonymous execution records.")
        .notes(
            "Admin-only. Not because the records are sensitive -- they contain "
            "no personal data -- but because they are operational reporting "
            "data with no meaning to a researcher, and an endpoint nobody needs "
            "publicly is better closed."
        )
        .param("status", "Only records in this terminal state.", required=False)
        .param("errorCode", "Only failures with this code.", required=False)
        .param("imageName", "Only runs that used this image.", required=False)
        .param("since", "Earliest date to include (YYYY-MM-DD).", required=False)
        .param("until", "Latest date to include (YYYY-MM-DD).", required=False)
        .pagingParams(defaultSort="date", defaultSortDir=-1)
    )
    def list_execution_records(
        self, status, errorCode, imageName, since, until, limit, offset, sort
    ):
        query = self._execution_record_query(status, errorCode, imageName, since, until)
        cursor = ExecutionRecord().find(query, limit=limit, offset=offset, sort=sort)
        return {
            # The total is what makes the table's paging honest; a page of
            # results says nothing about how much matched.
            "count": ExecutionRecord().collection.count_documents(query),
            "records": list(cursor),
        }

    @access.admin
    @autoDescribeRoute(
        Description("Summarise execution records for reporting.")
        .notes(
            "The aggregation the browse view opens with. Computed in Mongo "
            "rather than by paging every record into the client, which stops "
            "working the moment there are more records than one page."
        )
        .param("status", "Only records in this terminal state.", required=False)
        .param("errorCode", "Only failures with this code.", required=False)
        .param("imageName", "Only runs that used this image.", required=False)
        .param("since", "Earliest date to include (YYYY-MM-DD).", required=False)
        .param("until", "Latest date to include (YYYY-MM-DD).", required=False)
    )
    def summarise_execution_records(self, status, errorCode, imageName, since, until):
        query = self._execution_record_query(status, errorCode, imageName, since, until)
        collection = ExecutionRecord().collection

        def grouped(pipeline):
            return list(collection.aggregate([{"$match": query}] + pipeline))

        durations = grouped(
            [
                {
                    "$group": {
                        "_id": None,
                        "avg": {"$avg": "$total_duration_seconds"},
                        "max": {"$max": "$total_duration_seconds"},
                    }
                }
            ]
        )
        return {
            "total": collection.count_documents(query),
            "byStatus": grouped(
                [
                    {"$group": {"_id": "$status", "count": {"$sum": 1}}},
                    {"$sort": {"count": -1}},
                ]
            ),
            "byErrorCode": grouped(
                [
                    {"$match": {"error.code": {"$ne": None}}},
                    {"$group": {"_id": "$error.code", "count": {"$sum": 1}}},
                    {"$sort": {"count": -1}},
                ]
            ),
            # One record can have several stages, so unwind first -- otherwise a
            # two-stage run counts once against whichever image happens to be
            # first, and the totals quietly disagree with byStatus.
            "byImage": grouped(
                [
                    {"$unwind": "$stages"},
                    {
                        "$group": {
                            "_id": {
                                "$concat": [
                                    {"$ifNull": ["$stages.image_name", "unknown"]},
                                    ":",
                                    {"$ifNull": ["$stages.image_tag", "unknown"]},
                                ]
                            },
                            "count": {"$sum": 1},
                            "failed": {
                                "$sum": {
                                    "$cond": [{"$eq": ["$status", "completed"]}, 0, 1]
                                }
                            },
                        }
                    },
                    {"$sort": {"count": -1}},
                ]
            ),
            "byDate": grouped(
                [
                    {
                        "$group": {
                            "_id": "$date",
                            "count": {"$sum": 1},
                            "failed": {
                                "$sum": {
                                    "$cond": [{"$eq": ["$status", "completed"]}, 0, 1]
                                }
                            },
                        }
                    },
                    {"$sort": {"_id": 1}},
                ]
            ),
            # Deliberately mean and max rather than percentiles: $percentile
            # needs MongoDB 7, and this deployment runs 4.4.
            "duration": {
                "avg": durations[0]["avg"] if durations else None,
                "max": durations[0]["max"] if durations else None,
            },
        }

    @staticmethod
    def _execution_record_query(status, errorCode, imageName, since, until):
        query = {}
        if status:
            query["status"] = status
        if errorCode:
            query["error.code"] = errorCode
        if imageName:
            query["stages.image_name"] = imageName
        # 'date' is stored as a YYYY-MM-DD string, which orders lexicographically
        # the same way it orders chronologically -- so a plain range works.
        if since or until:
            bounds = {}
            if since:
                bounds["$gte"] = since
            if until:
                bounds["$lte"] = until
            query["date"] = bounds
        return query

    @access.admin
    @autoDescribeRoute(
        Description("Fail submissions whose worker stopped reporting.").notes(
            "Called periodically by the celery worker. A chain publishes each "
            "step only after the previous one returns, so a worker that dies "
            "mid-submission leaves no message to retry and no step to fail: "
            "the job stays RUNNING forever. Retrying is not an option either, "
            "since the workspace died with the worker -- so fail loudly."
        )
    )
    def reap_submissions(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        stale_after = datetime.timedelta(
            minutes=Setting().get(PluginSettings.HEARTBEAT_TIMEOUT)
        )
        max_runtime = datetime.timedelta(
            hours=Setting().get(PluginSettings.MAX_RUNTIME)
        )
        unassigned_after = datetime.timedelta(
            minutes=Setting().get(PluginSettings.ASSIGNMENT_TIMEOUT)
        )
        # Read once, not per job: the sweep can span many submissions and the
        # catalogue cannot change underneath a single pass in any way that
        # matters. Sizes only, since that is all the comparison below needs.
        catalogue_sizes = {entry["memory_gb"] for entry in worker_sizes()}

        # Materialize the cursor: this loop moves jobs out of the status the
        # query selects on, and Mongo can skip documents that shift under a
        # live cursor. Same reason cleanup_submissions() lists its folders.
        # Job().find() drops the log from the projection, which is what we want
        # -- these documents are only read for their timestamps.
        stranded = list(
            Job().find({"type": "sivacor_submission", "status": JobStatus.RUNNING})
        )

        reaped = []
        for job in stranded:
            created = _as_utc(job["created"])
            meta = job.get("meta", {})
            # Under targeted assignment a submission is RUNNING from the moment
            # it is accepted but genuinely idle until the controller picks an
            # instance for it -- there is no worker to have a heartbeat and no
            # run to overrun. Judging that wait by either of the rules below
            # fails a healthy submission and names the wrong cause; it gets its
            # own, longer bound instead. See P2 in worker_sizing_plan.md.
            assigned_at = meta.get("assigned_at")
            if meta.get("awaiting_assignment") and not assigned_at:
                # A rung withdrawn from the catalogue while this waited. The
                # controller already refuses to downgrade it onto hardware the
                # run was never sized for -- it logs `not in the catalogue` and
                # creates nothing -- but on its own that leaves the submission
                # to age out an hour later as REAPED_NO_WORKER, which names the
                # fleet when the cause is a config edit. Fail it now instead,
                # with the size in the message. Open item 3, answered 2026-08-20.
                #
                # Girder does this rather than the controller because S3 forbids
                # the controller calling updateJob(), and updateJob is what marks
                # the folder failed and emails the researcher.
                requested = meta.get("requested_memory_gb")
                if requested is not None and requested not in catalogue_sizes:
                    self._reap(
                        job,
                        f"the {requested} GB worker size is no longer offered, so "
                        "no machine of that size will be created for this "
                        "submission. Please resubmit choosing one of: "
                        + ", ".join(f"{s} GB" for s in sorted(catalogue_sizes)),
                        FailureCode.SIZE_UNAVAILABLE,
                        now - created,
                        reaped,
                        detail=requested,
                    )
                    continue
                if now - created > unassigned_after:
                    self._reap(
                        job,
                        f"no worker could be assigned to it within "
                        f"{unassigned_after}",
                        FailureCode.REAPED_NO_WORKER,
                        now - created,
                        reaped,
                    )
                continue
            # Any of these counts as a sign of life: the heartbeat covers a
            # long container run, 'updated' covers every step that logs,
            # 'assigned_at' covers the gap between a controller publishing the
            # chain and the first step logging, and 'created' keeps a submission
            # that has not reached its first step from being reaped before it
            # starts.
            last_seen = max(
                _as_utc(t)
                for t in (
                    created,
                    job.get("updated") or created,
                    meta.get("heartbeat") or created,
                    assigned_at or created,
                )
            )
            # An assigned submission's clock starts when it was given a worker,
            # not when it was accepted: the wait was not its run.
            started = _as_utc(assigned_at) if assigned_at else created
            if now - started > max_runtime:
                reason = (
                    f"exceeded the maximum runtime of {max_runtime}; "
                    "started at " + created.isoformat()
                )
                code = FailureCode.REAPED_MAX_RUNTIME
            elif now - last_seen > stale_after:
                reason = (
                    f"no sign of life for {now - last_seen}; the worker "
                    "running it is presumed lost"
                )
                code = FailureCode.REAPED_NO_HEARTBEAT
            else:
                continue

            self._reap(job, reason, code, now - created, reaped)

        return {"reaped": reaped}

    @classmethod
    def _reap(cls, job, reason, code, elapsed, reaped, detail=None):
        """Fail one submission and note it, however it was selected."""
        logger.warning("Reaping stranded submission %s: %s", job["_id"], reason)
        cls._fail_stranded(job, reason)
        cls._record_reaped(job, code, elapsed, detail)
        reaped.append(str(job["_id"]))

    @classmethod
    def _record_reaped(cls, job, code, elapsed, detail=None):
        """Write the execution record for a submission the worker lost.

        The worker records its own outcome, but a reaped submission is by
        definition one whose worker is not coming back -- so a run that died
        this way would otherwise be the one kind of failure missing from the
        reporting data, which is the opposite of what you want.
        """
        stages = []
        folder = Folder().findOne({"meta.job_id": str(job["_id"])})
        for stage in (folder or {}).get("meta", {}).get("stages", []) or []:
            stages.append(
                {
                    "image_name": stage.get("image_name"),
                    "image_tag": stage.get("image_tag"),
                    "network_isolation": bool(stage.get("network_isolation", False)),
                }
            )
        try:
            cls.store_execution_record(
                {
                    "status": "reaped",
                    "stages": stages,
                    "total_duration_seconds": elapsed.total_seconds(),
                    "error": {
                        "step": "reaper",
                        "code": code.value,
                        # sanitize_record validates this per code, so an
                        # unexpected value is dropped rather than stored.
                        **({"detail": detail} if detail is not None else {}),
                    },
                }
            )
        except Exception:
            # Same call as the worker's: reporting data is never worth failing
            # the sweep that is cleaning up after a lost worker.
            logger.warning(
                "Could not record execution telemetry for reaped job %s",
                job["_id"],
                exc_info=True,
            )

    @staticmethod
    def _fail_stranded(job, reason):
        """Transition a stranded job to ERROR and settle its children.

        Going through ``updateJob`` is the point: it fires
        ``jobs.job.update.after``, which is what marks the submission folder
        failed and emails the user.
        """
        zone = ZoneInfo("America/Chicago")
        stamp = (
            datetime.datetime.now().astimezone(zone).replace(microsecond=0).isoformat()
        )
        try:
            Job().updateJob(
                job,
                log=f"[{stamp}] Submission abandoned: {reason}.\n",
                status=JobStatus.ERROR,
            )
        except Exception:
            logger.exception("Could not fail stranded submission %s", job["_id"])
            return

        # The per-step celery jobs are stranded too, and nothing else will ever
        # touch them. They carry no submission status of their own, so update
        # the collection directly rather than replaying the event handler.
        Job().collection.update_many(
            {
                "type": "celery",
                "$or": [
                    {"args.0.job_id": str(job["_id"])},
                    {"args.3": str(job["_id"])},
                ],
                "status": {
                    "$in": [JobStatus.INACTIVE, JobStatus.QUEUED, JobStatus.RUNNING]
                },
            },
            {"$set": {"status": JobStatus.ERROR}},
        )

    @access.public
    @autoDescribeRoute(Description("Get available Docker image tags for SIVACOR."))
    def get_image_tags(self):
        tags = self._get_tags()
        return tags

    @access.public
    @autoDescribeRoute(
        Description("Get the worker sizes a submission may ask for.")
        .notes(
            "The same catalogue submit_job validates 'resources.memory_gb' "
            "against, so the picker offers exactly what the server accepts. "
            "'default' is the size a submission gets if it asks for none.\n\n"
            "'gated' is a property of the catalogue -- this rung is by request "
            "-- while 'selectable' is the answer for the caller making this "
            "request, so a picker can show a gated rung it cannot choose "
            "rather than hiding the ladder's top half from everyone. Both are "
            "reported because they say different things: an unauthenticated "
            "caller sees 'gated' true and 'selectable' false, and so does a "
            "logged-in non-member.\n\n"
            "'flavor' is deliberately absent: the cloud provider's name for a "
            "machine shape must not become visible to a researcher or "
            "load-bearing in an exported workflow. 'vcpus' is here because the "
            "label needs it; usable memory is not, because it is an "
            "approximation that would go stale in a cache."
        )
    )
    def get_worker_sizes(self):
        catalogue = worker_sizes()
        gated_ok = _gated_access(self.getCurrentUser(), catalogue)
        return {
            "sizes": [
                {
                    "memory_gb": entry["memory_gb"],
                    "vcpus": entry["vcpus"],
                    "gated": entry["gated"],
                    "selectable": gated_ok or not entry["gated"],
                }
                for entry in catalogue
            ],
            "default": default_worker_size(),
        }

    @access.public
    @autoDescribeRoute(
        Description("Get the caller's own scratch-volume allowance.").notes(
            "'max_gb' is 0 for anyone not approved, which is the default for "
            "every account -- so 0 and 'enabled: false' are both ordinary "
            "answers, not errors.\n\n"
            "Public rather than @access.user so an unauthenticated client gets "
            "the deployment's answer ('enabled') without a login, the way "
            "/sivacor/worker_sizes does. An anonymous caller is never approved, "
            "so 'max_gb' is 0 for them.\n\n"
            "'granularity_gb' is reported because the server rounds a request "
            "*up* to a multiple of it before checking it against 'max_gb'. A "
            "client that does not round the same way can offer a value it will "
            "then be refused for."
        )
    )
    def get_volume_quota(self):
        return {
            "enabled": volumes_enabled(),
            "max_gb": user_volume_quota(self.getCurrentUser()),
            "granularity_gb": VOLUME_GRANULARITY_GB,
            # The deployment's reservation, so a client can explain a refusal it
            # would otherwise have to describe as "too large" with no number.
            "deployment_gb": volume_total_gb(),
        }

    @access.admin
    @autoDescribeRoute(
        Description("Per-user scratch-volume accounting, in GB-hours.").notes(
            "C5.2 of cinder_volumes_plan.md, and the precondition for approving "
            "a second account: it answers 'who is spending the storage grant'.\n\n"
            "Admin-only and deliberately user-attributed -- unlike "
            "/sivacor/execution_record, which is anonymous by design and must "
            "stay that way. Nothing here is stored: it is derived from job "
            "documents on each call, so it reaches back exactly as far as "
            "'sivacor.retention_days' and no further. Export it if you need a "
            "longer history; do not make the server remember one.\n\n"
            "'gb_hours' brackets the *submission*, not the volume: the volume "
            "appears moments before its instance and is destroyed at the reap, "
            "up to ~20 minutes after the run ends. Good to within a reap tail, "
            "which is what a budget question needs -- not a billing record.\n\n"
            "Approved accounts that have spent nothing are listed with zeroes, "
            "because an unspent allowance still stands against the quota.\n\n"
            "Volume gigabytes come out of the storage allocation, not the SU "
            "budget that pays for worker instance-hours (open item 6)."
        )
    )
    def get_volume_usage(self):
        return volume_usage()

    @access.admin
    @autoDescribeRoute(
        Description("Set a user's scratch-volume allowance, in GB.")
        .modelParam("id", "The user to grant or revoke.", model=User, level=AccessType.ADMIN)
        .param(
            "maxGb",
            "Ceiling in GB. 0 revokes approval.",
            dataType="integer",
            required=True,
        )
        .notes(
            "The only way to approve a user for scratch volumes; nothing else "
            "creates or raises this. Deliberately an explicit endpoint rather "
            "than a field on the user PUT: it spends a shared OpenStack quota "
            "that production's assetstore also draws on, so granting it is an "
            "operator action that should be hard to do by accident.\n\n"
            "Not bounded by 'sivacor.volume_total_gb' here on purpose -- an "
            "operator may reasonably grant a ceiling ahead of funding it, and "
            "submit_job enforces both independently. The refusals read "
            "differently, which is the point: over your own ceiling is "
            "actionable by the researcher, over the deployment's is not."
        )
    )
    def set_volume_quota(self, user, maxGb):
        if maxGb < 0:
            raise ValidationException("maxGb must be zero or more.")
        User().update({"_id": user["_id"]}, {"$set": {USER_VOLUME_QUOTA_FIELD: maxGb}})
        logger.info(
            "Scratch-volume allowance for user %s set to %d GB by an administrator",
            user["_id"],
            maxGb,
        )
        return {"userId": str(user["_id"]), "max_gb": maxGb}

    @access.public
    @autoDescribeRoute(
        Description(
            "Get the JSON schema a replication workflow definition must satisfy."
        ).notes(
            "The same schema submit_job validates its body against, so clients "
            "(e.g. the submission UI importing a workflow from a YAML/JSON file) "
            "can check a definition before submitting it."
        )
    )
    def get_workflow_schema(self):
        return stage_schema

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
            tags = Setting().get(PluginSettings.IMAGE_TAGS)
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

    # DATA_READ rather than the bare @access.user every other handler here uses,
    # and that is deliberate: sivacor-girderfs authenticates with a DATA_READ
    # API key exchanged for a long-lived token, so an analysis container that
    # somehow reaches the daemon's config still cannot mutate Girder. Do not
    # "tidy" this into consistency with its neighbours.
    @access.user(scope=TokenScope.DATA_READ)
    @autoDescribeRoute(
        Description("Get a complete metadata manifest for a folder subtree.")
        .notes(
            "Serves sivacor-girderfs, which mounts a Girder folder read-only "
            "into an analysis container. It needs the whole subtree up front: "
            "walking it over stock REST costs one request per node, and it "
            "needs every file's content hash without reading the file, since "
            "hashing a mounted dataset defeats the point of mounting it.\n\n"
            "The walk uses the requesting user's own permissions, so a "
            "subfolder they cannot read is simply absent -- the rest of the "
            "tree is still returned.\n\n"
            "Contract, not implementation detail: 'folders', 'items' and "
            "'files' are each sorted by 'id', and stay so between calls. The "
            "client derives inode numbers from manifest order, so an unstable "
            "order would renumber the same folder between mounts. Timestamps "
            "are RFC 3339 UTC with a 'Z' suffix; 'updated' is null on a file "
            "never modified since upload. 'root.parent' is null even when the "
            "folder has a parent in Girder -- a subtree mount must not be told "
            "about anything above it. 'sha512' is '' when Girder never recorded "
            "one (imported and link files), which means the client fetches over "
            "HTTP rather than locating the blob on disk; it is not an error. "
            "Clients must refuse a 'manifestVersion' they do not know."
        )
        .modelParam(
            "folderId",
            "The folder whose subtree to describe.",
            model=Folder,
            level=AccessType.READ,
            required=True,
            paramType="query",
        )
        .param(
            "maxNodes",
            "Refuse trees larger than this many folders, items and files.",
            required=False,
            dataType="integer",
            default=1000000,
        )
        .errorResponse("The subtree exceeds maxNodes.", 413)
    )
    def get_fs_manifest(self, folder, maxNodes):
        user = self.getCurrentUser()
        is_admin = bool(user.get("admin"))

        folders = []
        items = []
        files = []
        seen = 0

        def budget():
            nonlocal seen
            seen += 1
            if seen > maxNodes:
                # Abort during the walk, not after: the guard exists to keep a
                # pathological tree from OOM-ing the web process, which a
                # check on the finished response would not do.
                raise RestException(
                    f"This subtree has more than maxNodes ({maxNodes}) nodes. "
                    "Mount a smaller folder, or raise maxNodes.",
                    code=413,
                )

        # Iterative rather than recursive: nothing bounds Girder's folder depth,
        # and a REST handler should not be able to exhaust the Python stack.
        # Order of traversal does not matter -- every array is sorted at the end.
        budget()
        stack = [(folder, None)]
        while stack:
            current, parent_id = stack.pop()
            folders.append(
                {
                    "id": str(current["_id"]),
                    "parent": parent_id,
                    "name": current["name"],
                    "created": _iso8601(current.get("created")),
                    "updated": _iso8601(
                        current.get("updated") or current.get("created")
                    ),
                }
            )

            # childItems and childFiles take no `user`: items and files are
            # AccessControlMixin resources that inherit the containing folder's
            # ACL (girder/utility/acl_mixin.py), so a READ-permitted folder
            # settles them. Passing user= would not filter -- childItems
            # forwards **kwargs straight into the pymongo query and would
            # corrupt it. childFolders, by contrast, does filter by user, and
            # that is what keeps unreadable branches out of the manifest.
            # Never force=True here: `girder mount` walks that way and thereby
            # exposes the whole database to anyone who can read the mountpoint.
            folder_items = list(Folder().childItems(current))
            for item in folder_items:
                budget()
                items.append(
                    {
                        "id": str(item["_id"]),
                        "folder": str(current["_id"]),
                        "name": item["name"],
                        "created": _iso8601(item.get("created")),
                        "updated": _iso8601(item.get("updated") or item.get("created")),
                    }
                )

            # Files are fetched per batch of items rather than per item.
            # Item().childFiles(item) is exactly File().find({"itemId": id}),
            # so calling it in a loop costs one Mongo round trip per item: at
            # 50 000 items that measured 18.7s of a 26s response, against 0.55s
            # for the same rows fetched with $in (M1-BACKEND-CHANGES.md §7).
            # Identical query, identical access semantics -- files inherit the
            # containing folder's ACL either way -- and the response is
            # byte-for-byte the same, since every array is sorted at the end.
            #
            # Chunked rather than one query per folder so that a single
            # enormous folder cannot pull an unbounded number of file
            # documents into memory before budget() gets a chance to refuse
            # them. The items themselves are already budget-checked above, so
            # this is bounded by maxNodes regardless.
            by_item_id = {item["_id"]: item for item in folder_items}
            item_ids = list(by_item_id)
            for start in range(0, len(item_ids), _MANIFEST_FILE_BATCH):
                chunk = item_ids[start : start + _MANIFEST_FILE_BATCH]
                for file in File().find({"itemId": {"$in": chunk}}):
                    budget()
                    files.append(
                        self._manifest_file(file, by_item_id[file["itemId"]], is_admin)
                    )

            for child in list(Folder().childFolders(current, "folder", user=user)):
                budget()
                stack.append((child, str(current["_id"])))

        for array in (folders, items, files):
            array.sort(key=lambda entry: entry["id"])

        return {
            "manifestVersion": FS_MANIFEST_VERSION,
            "root": {"type": "folder", "id": str(folder["_id"])},
            "generatedAt": _iso8601(datetime.datetime.now(datetime.timezone.utc)),
            "folders": folders,
            "items": items,
            "files": files,
        }

    @staticmethod
    def _manifest_file(file, item, is_admin):
        imported = bool(file.get("imported"))
        return {
            "id": str(file["_id"]),
            "item": str(item["_id"]),
            "name": file["name"],
            # linkUrl files may carry no size at all.
            "size": int(file.get("size") or 0),
            # Client-asserted at upload; deliberately not sniffed here.
            "mimeType": file.get("mimeType"),
            "created": _iso8601(file.get("created")),
            "updated": _iso8601(file.get("updated") or file.get("created")),
            # Set only on upload finalisation, so imported and linkUrl files
            # have none. "" means "cannot be located by hash"; never null.
            "sha512": file.get("sha512") or "",
            "imported": imported,
            # Only an imported file's `path` is an absolute host path. A normal
            # filesystem-assetstore file also has one, but it is relative to the
            # assetstore root and derivable from sha512
            # (filesystem_assetstore_adapter.py:234), so emitting it would just
            # be an absolute-looking value that is not absolute.
            "path": file["path"]
            if (is_admin and imported and "path" in file)
            else None,
            # Non-null means there is no assetstore blob at all.
            "linkUrl": file.get("linkUrl"),
        }

    @access.user
    @autoDescribeRoute(
        Description("Delete a SIVACOR submission job and its associated data.")
        .modelParam(
            "id",
            "The ID of the submission job to delete.",
            model=Folder,
            level=AccessType.READ,
            required=True,
        )
        .param(
            "progress",
            "Whether to report progress during deletion.",
            required=False,
            dataType="boolean",
            default=False,
        )
    )
    def delete_submission(self, folder, progress):
        from girder.tasks import deleteFolderTask

        # ensure that user has read access to the folder, but meta confirms he was creator
        user = self.getCurrentUser()
        meta = folder.get("meta", {})
        creator_id = meta.get("creator_id")
        if not (creator_id and str(creator_id) == str(user["_id"])):
            raise AccessException(
                "You do not have permission to delete '%s' submission." % folder["name"]
            )
        if meta.get("status") not in ("completed", "failed"):
            raise ValidationException(
                "Only completed or failed submissions can be deleted."
            )
        root = self.submission_collection(user)
        if not root or folder["parentId"] != root["_id"]:
            raise ValidationException("Invalid submission folder.")

        job = Job().load(meta.get("job_id"), force=True)
        if job:
            Job().remove(job)
        deleteFolderTask.delay(
            str(folder["_id"]),
            progress,
            str(User().findOne({"admin": True})["_id"]),
        )
        return {"message": f"Marked submission '{folder['name']}' for deletion."}

    @staticmethod
    def submission_collection(user):
        return Collection().filter(
            Collection().findOne(
                {"name": Setting().get(PluginSettings.SUBMISSION_COLLECTION_NAME)}
            ),
            user=user,
        )


@access.public
@autoDescribeRoute(
    Description("Get submission child jobs for a given job.").modelParam(
        "id",
        "The ID of the parent job.",
        model=Job,
        level=AccessType.READ,
        required=True,
    )
)
@filtermodel(model=Job)
@boundHandler
def get_submission_child_jobs(self, job):
    child_jobs = []
    workspace_job = Job().findOne({"type": "celery", "args.3": str(job["_id"])})
    if workspace_job:
        child_jobs.append(workspace_job)
    child_jobs += list(Job().find({"type": "celery", "args.0.job_id": str(job["_id"])}))
    return child_jobs
