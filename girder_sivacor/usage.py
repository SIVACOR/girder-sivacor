"""Per-user resource accounting: who spent the allocation, and on what.

The design is ``development_notes/09_user_usage_accounting_plan.md``; the
decision numbers cited throughout (``09-U3``, ``09-U10`` …) are its. This module
is the whole of the arithmetic and none of the wiring: it knows how to turn one
instance's lifetime into counters, and nothing about when that happens.

**Two constraints shape everything here.**

1. *The fleet controller stays thin.* It contributes the only two facts no other
   process can observe -- when an instance was created and when it was reaped --
   via :func:`record_lifetime`, and holds no rate, no policy and no user
   identity. Everything else runs in a Girder process (09-U8, 09-U16).
2. *A researcher is never charged for a failure they could not have caused.*
   :data:`USER_PAYS` is that rule, enumerated against every member of
   ``FailureCode`` so a new code cannot default silently (09-U10).

**Three stores, and only one of them is permanent.** The counters live on the
Girder user document, because that is the only place where deleting the user
already erases them -- a side collection keyed by user id would be a new
personal-data store with its own erasure obligation (09-U1). The other two are
transient joins, TTL-indexed, and the privacy section of the plan treats them as
a different thing from the counters for that reason. All three are Girder
models; see :mod:`girder_sivacor.models.usage`, including why the one the fleet
controller writes is a model too.

Nothing here retries. A failed accrual under-counts and says so in the log
(09-U9): an under-charged user complains to nobody, while an over-charged one is
a support incident and a credibility problem.
"""

from __future__ import annotations

import datetime
import logging

from girder.models.setting import Setting
from girder.models.user import User

from .errors import FailureCode
from .models.usage import (
    InstanceLifetime,
    UsageAttribution,
    UsageTotals,
)
from .settings import PluginSettings

logger = logging.getLogger(__name__)


# --- where things live -----------------------------------------------------

#: The counters, as one subdocument on the Girder user.
#:
#: camelCase to match the other plugin-owned user fields (``lastJobId``,
#: ``lastProjectId``, ``sivacorMaxVolumeGb``). Deliberately *not* added to
#: ``User().exposeFields``, so it is invisible to other users the same way
#: ``sivacorMaxVolumeGb`` is -- see ``__init__.py``'s ``exposeFields`` call, and
#: note that adding it there would publish every researcher's spend to every
#: other researcher.
USER_USAGE_FIELD = "sivacorUsage"

#: The house bucket's ``_id``. One document, addressed by a literal.
TOTALS_ID = "house"


# --- rates and caps --------------------------------------------------------

#: Hours added to ``sivacor.max_runtime`` to get the longest life a single
#: instance may bill a user for.
#:
#: Covers boot grace, the idle clock and the signing window -- measured at ~15.5
#: minutes of an ~18-minute instance life (``01_autoscaling_plan.md``:807), so
#: half an hour is generous rather than tuned. **The normal path never reaches
#: the cap**, and that is the test that this constant is right: a cap firing on
#: an ordinary submission is a bug here, not a finding about the submission,
#: which is why :func:`split_at_cap` logs when it fires.
BILLABLE_OVERHEAD_HOURS = 0.5


def max_billable_hours() -> float:
    """The longest single-instance life a user can be charged for (09-U15).

    Derived from ``sivacor.max_runtime`` rather than configured separately, so
    there is no second knob that can contradict the runtime ceiling. An instance
    only outlives this when nothing reaped it -- the controller is down, the tick
    is wedged -- and charging a researcher for hours of our outage is what this
    prevents.
    """
    return float(Setting().get(PluginSettings.MAX_RUNTIME)) + BILLABLE_OVERHEAD_HOURS


def split_at_cap(hours: float) -> tuple[float, float]:
    """Split a lifetime into (billable to the user, absorbed by the house)."""
    cap = max_billable_hours()
    if hours <= cap:
        return hours, 0.0
    logger.warning(
        "instance lifetime %.2f h exceeds the %.2f h cap; charging the user %.2f h "
        "and the house %.2f h. On a healthy fleet this never fires -- a reap that "
        "did not happen is the usual cause.",
        hours,
        cap,
        cap,
        hours - cap,
    )
    return cap, hours - cap


def su_per_hour_for(memory_gb: int | None) -> float | None:
    """The allocation's rate for the rung ``memory_gb`` names, or ``None``.

    ``None`` means the catalogue has no such rung -- an instance booted before
    the rung was retired, or one whose size tag never resolved. The caller
    decides what to do; see :func:`accrue`, which declines to guess.
    """
    # Imported here rather than at module scope: rest.py will import this module
    # for GET /sivacor/usage, and a top-level import back into rest.py would be
    # circular.
    from .rest import worker_sizes

    for entry in worker_sizes():
        if entry["memory_gb"] == memory_gb:
            return float(entry["su_per_hour"])
    return None


# --- who pays --------------------------------------------------------------

#: Whether the *researcher* pays for a run that ended with this code.
#:
#: Enumerated against every member of ``FailureCode``, and
#: :func:`_assert_table_is_exhaustive` refuses to import if one is missing. That
#: is not ceremony: a code with no entry would otherwise fall through to "house
#: pays", which is the safe direction but also a silent one -- the deployment
#: would quietly stop charging for a whole class of run and nothing would say so.
#:
#: The three entries worth arguing about are all ``True``, and all for the same
#: reason: they are the most expensive outcomes in the system and every one of
#: them is a property of the submitted code. Making an infinite loop free makes
#: it the cheapest route to unmetered compute (09-U12).
USER_PAYS: dict[FailureCode, bool] = {
    # Their code ran and returned an error.
    FailureCode.STATA_ERROR: True,
    FailureCode.NONZERO_EXIT: True,
    # The run exceeded the size they chose; the picker exists for this.
    FailureCode.OUT_OF_MEMORY: True,
    # Their package filled the disk; the scratch-volume control exists for this.
    FailureCode.OUT_OF_DISK: True,
    # 09-U12. The single most expensive outcome there is.
    FailureCode.REAPED_MAX_RUNTIME: True,
    # Their package; cheap anyway, it fails early.
    FailureCode.MAIN_FILE_MISSING: True,
    FailureCode.MAIN_FILE_AMBIGUOUS: True,
    # Retired -- nothing raises either -- but still enum members, because an
    # execution record outlives the release that wrote it. They keep their
    # entries so the exhaustiveness check below stays meaningful.
    FailureCode.PROJECT_FILE_MISSING: True,
    FailureCode.DEPENDENCY_RESOLUTION_FAILED: True,
    # Their upload.
    FailureCode.UNSUPPORTED_ARCHIVE: True,
    FailureCode.UNSAFE_ARCHIVE: True,
    # The run happened; they deleted it mid-write-back.
    FailureCode.SUBMISSION_DELETED: True,
    # Deployment misconfiguration.
    FailureCode.NO_STATA_LICENSE: False,
    # Registry or network, not the researcher.
    FailureCode.IMAGE_PULL_FAILED: False,
    # An image the operator allow-listed that our engine cannot run.
    FailureCode.NO_ENTRYPOINT: False,
    # We lost the worker.
    FailureCode.REAPED_NO_HEARTBEAT: False,
    # Fleet or config fault -- and moot, no instance ever booted, so no
    # attribution row exists to read these off.
    FailureCode.REAPED_NO_WORKER: False,
    FailureCode.SIZE_UNAVAILABLE: False,
    # A defect. This is the case that motivated 09-U10.
    FailureCode.UNEXPECTED: False,
}


def _assert_table_is_exhaustive() -> None:
    """Fail at import if a ``FailureCode`` has no billability entry.

    Specified as *enumerate the enum* rather than *count the rows* on purpose. A
    count passes on the day a member is added and is wrong from then on, and the
    failure it hides is the quiet kind: runs stop being charged and the totals
    merely look low.
    """
    missing = sorted(code.value for code in FailureCode if code not in USER_PAYS)
    if missing:
        raise RuntimeError(
            "girder_sivacor.usage.USER_PAYS is missing an entry for: "
            + ", ".join(missing)
            + ". Every FailureCode needs one -- see 'Who pays for a failure' in "
            "development_notes/09_user_usage_accounting_plan.md. Absent means "
            "'house pays', which is safe but silent, so the table is required "
            "to be explicit."
        )


_assert_table_is_exhaustive()


def is_billable(code: FailureCode | str | None) -> bool:
    """Whether a run that ended with ``code`` is charged to the researcher.

    ``None`` -- success, or a cancel -- is billable: the compute was spent on
    their behalf either way, and charging a cancel is what keeps "cancel" from
    becoming a way to run twenty-five minutes of analysis for free.

    An *unrecognised* code is not billable. Nothing classified the outcome, so
    nobody can claim it was the user's fault (09-U13).
    """
    if code is None:
        return True
    try:
        code = FailureCode(code)
    except ValueError:
        logger.warning(
            "unrecognised failure code %r; charging the house per 09-U13", code
        )
        return False
    return USER_PAYS[code]


# --- writing the transient rows --------------------------------------------


def record_lifetime(instance, deleted_at=None) -> None:
    """The fleet controller's entire contribution to this feature (09-U16).

    Call it in the reap loop **strictly before** ``fleet.delete_instance``,
    alongside the diagnostics capture and the volume-attachment read that are
    already ordered that way: ``created_at`` is on the ``Instance`` only until
    the server is gone.

    Raw observation only -- two timestamps and two tags. No rate, no catalogue
    read, no user lookup, no billability branch. If a future change adds any of
    those here it is in the wrong process; the accrual runs in Girder, on this.

    Goes through Girder's model layer even though the caller is the fleet
    controller, which is not a Girder server: that process already reaches
    Girder this way for ``dispatch.publish`` and ``catalogue.load``, so
    threading a raw pymongo handle in would be a second mechanism for something
    it already does -- and one that hardcodes a collection name.

    Best effort: a lifetime that cannot be written is an under-count, which
    09-U9 permits, and a fleet that stops reaping because a counter failed is
    not a trade worth making.
    """
    deleted_at = deleted_at or _now()
    try:
        InstanceLifetime().collection.update_one(
            {"instanceId": instance.id},
            {
                "$setOnInsert": {
                    "instanceId": instance.id,
                    "createdAt": instance.created_at,
                    "deletedAt": deleted_at,
                    "sizeGb": instance.size,
                    "volumeGb": instance.volume_gb,
                    "at": _now(),
                }
            },
            upsert=True,
        )
    except Exception:
        logger.warning(
            "could not record the lifetime of instance %s; its usage will not be "
            "accrued to anybody",
            instance.id,
            exc_info=True,
        )


def record_attribution(instance_id, user_id, memory_gb, volume_gb, billable) -> None:
    """Snapshot who a reaped instance's usage belongs to (09-U5).

    Written when the submission's job reaches a terminal status, which is the
    last moment the job document -- and so the user id -- is guaranteed to exist:
    a user may delete their submission at any time, routinely before the ~18
    minutes it takes for the instance to be reaped.

    ``billable`` starts false for a failure and is raised later by
    :func:`stamp_outcome` if the code turns out to be one the researcher pays
    for. The default is the fail-safe: a stamp that never arrives leaves the
    house paying (09-U13), which is the direction 09-U9 wants to err in.

    **``billable`` is written with ``$max``, never ``$set``, so it can only ever
    go up.** The hook that calls this fires on *every* job update, not only the
    one that made the status terminal -- so a failed submission that has already
    had its outcome stamped will be re-attributed with ``billable=False`` the
    next time anything touches the job, and a ``$set`` would silently un-charge
    it. ``$max`` makes that impossible rather than leaving it to depend on
    whether any call path happens to log after a terminal status. (BSON orders
    ``False`` before ``True``, so this is exactly "raise, never lower".)
    """
    UsageAttribution().collection.update_one(
        {"instanceId": instance_id},
        {
            "$set": {
                "instanceId": instance_id,
                "userId": user_id,
                "memoryGb": memory_gb,
                "volumeGb": volume_gb,
                "at": _now(),
            },
            "$max": {"billable": bool(billable)},
        },
        upsert=True,
    )


def stamp_outcome(instance_id, code) -> None:
    """Record why a run failed, and whether that makes it the user's to pay for.

    Separate from :func:`record_attribution` so the two are order-independent:
    the row is created with the safe default and this upgrades it if it arrives.
    Making the code ride the status transition instead would mean getting the
    ordering right on five separate terminal paths.

    **Only ever raises ``billable``, never lowers it**, by the same ``$max`` as
    :func:`record_attribution`. A success is billable from the moment its
    attribution row is written, and nothing about a later code should be able to
    make an already-charged run free.
    """
    UsageAttribution().collection.update_one(
        {"instanceId": instance_id},
        {
            "$set": {"code": getattr(code, "value", code)},
            "$max": {"billable": bool(is_billable(code))},
        },
    )


# --- the accrual -----------------------------------------------------------


def accrue(lifetime, attribution) -> None:
    """Charge one reaped instance's lifetime to a user, the house, or both.

    ``attribution`` of ``None`` is not an error: the instance booted and was
    reaped without ever claiming a submission, which is real burn belonging to
    nobody and is exactly what the house bucket is for (09-U7).

    Called only from the drain, which has already claimed both rows -- see
    :func:`drain` for why the order matters.
    """
    hours = _hours_between(lifetime.get("createdAt"), lifetime.get("deletedAt"))
    if hours is None:
        logger.warning(
            "instance %s has no usable lifetime (%r -> %r); skipping",
            lifetime.get("instanceId"),
            lifetime.get("createdAt"),
            lifetime.get("deletedAt"),
        )
        return

    billable_hours, over_cap_hours = split_at_cap(hours)

    # The instance is what burned, so the instance's own size tag is what sets
    # the rate -- not the size the researcher asked for. They agree in every
    # normal case; when they do not, that is a fleet bug, and the attribution
    # row's memoryGb is still what the histogram counts because that is the
    # choice the user actually made.
    rate = su_per_hour_for(lifetime.get("sizeGb"))
    if rate is None:
        logger.warning(
            "instance %s has no catalogue rung for size %r; charging the house at "
            "no rate rather than guessing one",
            lifetime.get("instanceId"),
            lifetime.get("sizeGb"),
        )
        rate = 0.0

    volume_gb = lifetime.get("volumeGb") or 0

    if over_cap_hours:
        accrue_house(
            "over_cap",
            su_hours=over_cap_hours * rate,
            instance_hours=over_cap_hours,
            instances=0,
        )

    if attribution is None:
        accrue_house(
            "unclaimed",
            su_hours=billable_hours * rate,
            instance_hours=billable_hours,
        )
        return

    if not attribution.get("billable"):
        accrue_house(
            attribution.get("code") or "unstamped",
            su_hours=billable_hours * rate,
            instance_hours=billable_hours,
        )
        return

    accrue_user(
        attribution["userId"],
        su_hours=billable_hours * rate,
        instance_hours=billable_hours,
        volume_gb_hours=billable_hours * volume_gb,
        memory_gb=attribution.get("memoryGb"),
    )


def accrue_user(user_id, su_hours, instance_hours, volume_gb_hours, memory_gb) -> None:
    """Add one instance's spend to a user's cumulative counters.

    ``bySize`` is keyed by ``memory_gb`` as a string, never by flavour: S1 of
    ``03_worker_sizing_plan.md`` keeps the provider's name for a machine shape
    out of everything downstream, and a counter is exactly the kind of thing a
    report later reads. Mongo keys cannot contain dots and the rungs are
    integers, so string keys are safe.
    """
    users = User().collection
    now = _now()

    # `since` first, and in an update of its own, because **``$setOnInsert``
    # does not work here**: it fires when a *document* is inserted, and the user
    # document already exists, so the field would simply never be written. The
    # `$exists: False` filter is the equivalent that does -- it matches nothing
    # once the field is there, so this is idempotent rather than a race.
    #
    # Before the counters rather than after, so that a failure between the two
    # leaves "accounting began at X, nothing spent yet", which is honest, rather
    # than a total with no start date, which claims to cover everything (09-U4).
    users.update_one(
        {"_id": user_id, f"{USER_USAGE_FIELD}.since": {"$exists": False}},
        {"$set": {f"{USER_USAGE_FIELD}.since": now}},
    )

    inc = {
        f"{USER_USAGE_FIELD}.submissions": 1,
        f"{USER_USAGE_FIELD}.suHours": su_hours,
        f"{USER_USAGE_FIELD}.instanceHours": instance_hours,
        f"{USER_USAGE_FIELD}.volumeGbHours": volume_gb_hours,
    }
    if memory_gb is not None:
        inc[f"{USER_USAGE_FIELD}.bySize.{int(memory_gb)}"] = 1
    users.update_one(
        {"_id": user_id},
        {"$inc": inc, "$set": {f"{USER_USAGE_FIELD}.lastAt": now}},
    )


def accrue_house(reason, su_hours, instance_hours, instances=1) -> None:
    """Add spend that belongs to nobody, or to us, to the anonymous singleton.

    ``reason`` separates three different bills with three different owners: the
    fleet booted something nobody used (``unclaimed``), we charged ourselves for
    a defect (a ``FailureCode`` value), or our reaper was down (``over_cap``).
    A growing ``unexpected`` is a bug report.

    Not a real admin account, deliberately: that would mix platform waste with
    that admin's genuine submissions, and a future quota would lock out the one
    account that must never be locked out (09-U11).
    """
    now = _now()
    UsageTotals().collection.update_one(
        {"_id": TOTALS_ID},
        {
            "$inc": {
                "suHours": su_hours,
                "instanceHours": instance_hours,
                "instances": instances,
                f"byReason.{_reason_key(reason)}": su_hours,
            },
            "$set": {"lastAt": now},
            "$setOnInsert": {"since": now},
        },
        upsert=True,
    )


def drain(limit=500) -> int:
    """Accrue every lifetime waiting to be counted. Returns how many were.

    Runs on the beat, not in the reap loop and not in ``submit_job`` (09-U18).
    The rows are durable, so how often this runs affects freshness and never
    correctness.

    **Claims the lifetime row first, and that order is load-bearing** (09-U19). A
    crash between the two deletes loses one lifetime and leaves an attribution to
    expire -- an under-count, which 09-U9 permits. The reverse order loses the
    attribution and re-processes the lifetime as house burn on the next pass,
    which is a silent mis-attribution: the number stays plausible and the wrong
    party pays.
    """
    accrued = 0
    for _ in range(limit):
        lifetime = InstanceLifetime().collection.find_one_and_delete({})
        if lifetime is None:
            break
        attribution = UsageAttribution().collection.find_one_and_delete(
            {"instanceId": lifetime["instanceId"]}
        )
        try:
            accrue(lifetime, attribution)
        except Exception:
            # Both rows are already gone, so this is a lost accrual rather than
            # a retryable one -- which is the trade 09-U9 chose deliberately.
            logger.warning(
                "could not accrue instance %s; its usage is lost rather than "
                "double-counted",
                lifetime.get("instanceId"),
                exc_info=True,
            )
            continue
        accrued += 1
    return accrued


# --- small helpers ---------------------------------------------------------


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _hours_between(start, end) -> float | None:
    if not start or not end:
        return None
    start = _as_utc(start)
    end = _as_utc(end)
    seconds = (end - start).total_seconds()
    # A negative or zero lifetime means clocks disagree or the row is malformed.
    # Charging a negative would *credit* the user, which is worse than skipping.
    return seconds / 3600.0 if seconds > 0 else None


def _as_utc(value):
    """Mongo hands back naive datetimes; everything written here is UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)


def _reason_key(reason) -> str:
    """Mongo field names cannot contain dots, and a reason reaches this as a
    ``FailureCode`` value, a bare string, or the two literals this module coins.
    """
    return str(getattr(reason, "value", reason)).replace(".", "_")
