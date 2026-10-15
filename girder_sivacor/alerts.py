"""Telling an operator that a submission failed for a reason that is ours.

Nothing used to. Failures were found by browsing ``#sivacor/telemetry`` by
hand, from time to time -- which is how the 2026-10-12 failure surfaced two
days after a 50 h 39 m run burned 199.9 CPU-hours and 3,254.54 SU-hours and
died at 01:29Z with nothing said. The gap has been open since 2026-09-18, which
named it exactly: *"the absence of any alert on ``unexpected`` climbing"*.

**Two rules shape everything here.**

1. *Silence is the bug being fixed, so a code with no entry must not be able to
   arrive un-alerted.* :data:`ALERT_POLICY` is enumerated against every member
   of ``FailureCode`` and :func:`_assert_table_is_exhaustive` refuses to import
   if one is missing -- the same guard, for the same reason, as
   ``usage.USER_PAYS``.
2. *An alert nobody reads is worse than none.* ``stata_error`` and
   ``nonzero_exit`` are the bulk of all failures and are the researcher's own
   code; mailing them would bury the ones that matter. That is the failure mode
   to avoid, not a nicety.

**This is a third table and not a reuse of ``usage.USER_PAYS``.** That one
answers *who pays*; this one answers *does an operator need to look*. They
overlap heavily and disagree in both directions: ``UNSAFE_ARCHIVE`` is billed
to the researcher and is a **security event**, so it alerts; ``SUBMISSION_DELETED``
is billed to the researcher and is documented as *"Not a defect"*, so it does
not.
"""

from __future__ import annotations

import datetime
import logging
from enum import Enum

from girder import events
from girder.models.collection import Collection
from girder.models.folder import Folder
from girder.models.setting import Setting
from girder.models.user import User
from girder.utility import mail_utils
from girder_jobs.constants import JobStatus
from girder_jobs.models.job import Job

from .errors import FailureCode
from .models.alerts import AlertCounter
from .telemetry import safe_detail
from .usage import is_billable

logger = logging.getLogger(__name__)


class Alert(str, Enum):
    """What an operator should be told about one failure code."""

    #: One email, as it happens. For failures that mean something is wrong with
    #: the platform and somebody should look now.
    EMAIL = "email"
    #: Counted, and reported once a day in a single message. For failures that
    #: are the researcher's to pay for but are still the most expensive outcome
    #: in their class, each with a control behind it -- the size picker, the
    #: scratch volume, the runtime ceiling. A trend worth seeing; not a thing to
    #: wake anyone for.
    DIGEST = "digest"
    #: Nothing. The researcher's own code or package, or an outcome documented
    #: as not a defect.
    NEVER = "never"


#: Every ``FailureCode``, and what it is worth telling an operator.
#:
#: Enumerated rather than defaulted: a code with no entry would fall through to
#: *no alert*, which is the safe direction but also a silent one -- and silence
#: is the bug this module exists to fix. ``_assert_table_is_exhaustive`` makes
#: adding a code without deciding this a startup failure rather than a quiet
#: omission discovered during the incident it should have reported.
ALERT_POLICY: dict[FailureCode, Alert] = {
    # --- ours, and somebody should look now --------------------------------
    #: The code reserved for our own defects. A climbing `unexpected` is a bug
    #: report, and this alert is the thing that was missing when it climbed to
    #: eighteen unnoticed.
    FailureCode.UNEXPECTED: Alert.EMAIL,
    FailureCode.IMAGE_PULL_FAILED: Alert.EMAIL,
    FailureCode.NO_ENTRYPOINT: Alert.EMAIL,
    #: A deployment misconfiguration, not a submission problem: the researcher
    #: picked a Stata image this deployment has no license for.
    FailureCode.NO_STATA_LICENSE: Alert.EMAIL,
    FailureCode.REAPED_NO_HEARTBEAT: Alert.EMAIL,
    FailureCode.REAPED_NO_WORKER: Alert.EMAIL,
    #: A size left `sivacor.worker_sizes` while a submission waited for it --
    #: the one reaper outcome that is a configuration fault.
    FailureCode.SIZE_UNAVAILABLE: Alert.EMAIL,
    #: Billed to the researcher and alerted anyway, which is exactly why this
    #: table is not USER_PAYS: an archive that tried to escape the extraction
    #: root is a **security event**, whoever pays for the compute.
    FailureCode.UNSAFE_ARCHIVE: Alert.EMAIL,
    # --- theirs to pay for, but worth watching ------------------------------
    #: The three most expensive outcomes in their classes, each with a control
    #: behind it. Per-event mail would be noise at production's volume; a daily
    #: count is the trend an operator would act on.
    FailureCode.OUT_OF_MEMORY: Alert.DIGEST,
    FailureCode.OUT_OF_DISK: Alert.DIGEST,
    FailureCode.REAPED_MAX_RUNTIME: Alert.DIGEST,
    # --- the researcher's own code or package -------------------------------
    #: The bulk of all failures. Alerting on these would bury everything above.
    FailureCode.STATA_ERROR: Alert.NEVER,
    FailureCode.NONZERO_EXIT: Alert.NEVER,
    FailureCode.MAIN_FILE_MISSING: Alert.NEVER,
    FailureCode.MAIN_FILE_AMBIGUOUS: Alert.NEVER,
    FailureCode.UNSUPPORTED_ARCHIVE: Alert.NEVER,
    #: Documented as "Not a defect": the researcher cancelled and then deleted
    #: inside the ~12 s the write-back takes.
    FailureCode.SUBMISSION_DELETED: Alert.NEVER,
    #: Retired with the Julia resolve phase. Nothing raises either any more, but
    #: records written under them outlive the release, so the members stay --
    #: and an exhaustive table has to carry them.
    FailureCode.PROJECT_FILE_MISSING: Alert.NEVER,
    FailureCode.DEPENDENCY_RESOLUTION_FAILED: Alert.NEVER,
}


def _assert_table_is_exhaustive() -> None:
    """Refuse to import if a ``FailureCode`` has no alert policy.

    Lifted wholesale from ``usage._assert_table_is_exhaustive``, and the
    rationale transfers word for word: a missing entry would read as "do not
    alert", which is the safe direction and also a silent one. A new failure
    code must not be able to arrive un-alerted by omission -- least of all in
    the module whose entire purpose is that nothing fails quietly.
    """
    missing = sorted(code.value for code in FailureCode if code not in ALERT_POLICY)
    if missing:
        raise RuntimeError(
            "girder_sivacor.alerts.ALERT_POLICY is missing an entry for: "
            + ", ".join(missing)
            + ". Decide whether an operator needs to look when this happens; "
            "there is deliberately no default."
        )


_assert_table_is_exhaustive()


def policy_for(code) -> Alert:
    """What to do about ``code``. An unrecognised one is treated as a defect.

    A code this server does not know came from a worker newer than it, which is
    a deployment that is half upgraded -- worth an email in its own right. The
    alternative, returning ``NEVER``, would make a version skew invisible in
    precisely the way this module exists to prevent.
    """
    try:
        return ALERT_POLICY[FailureCode(code)]
    except (ValueError, KeyError):
        logger.warning("No alert policy for failure code %r; treating as a defect", code)
        return Alert.EMAIL


def _now():
    return datetime.datetime.now(datetime.UTC)


#: The label for a failure that reached a terminal status without ever saying
#: why. Not a ``FailureCode``: those are codes a raise site *chose*, and this is
#: the absence of one. Kept out of the enum deliberately -- adding it would put
#: a value into the permanent execution-record vocabulary that no raise site
#: can ever produce.
UNKNOWN_CODE = "unknown"

#: Field on the job marking that an alert decision has been taken.
#:
#: "Decided", not "emailed": a ``NEVER`` code sets it too. The sweep that
#: catches failures whose code never arrived reads its *absence*, so a code
#: that was considered and deliberately not mailed has to be distinguishable
#: from one that was never considered at all. Getting this wrong would mail an
#: operator about every ``stata_error`` on the deployment.
DECIDED_FIELD = "meta.sivacorAlertDecidedAt"


def _claim(job) -> bool:
    """Take the alert decision for this job, and say whether we got it.

    Exactly-once, atomically. Two sites stamp an outcome -- the worker's
    ``PUT /sivacor/outcome/:id`` and the reaper's in-process ``_stamp_outcome``
    -- plus the sweep, and a retried REST call would otherwise mail twice about
    one failure. The filter is the guard: whoever's update modifies a document
    wins, and everyone else is told no.
    """
    result = Job().collection.update_one(
        {"_id": job["_id"], DECIDED_FIELD: {"$exists": False}},
        {"$set": {DECIDED_FIELD: _now()}},
    )
    return result.modified_count == 1


def consider(job, code=None, detail=None) -> str:
    """Decide what this failure is worth telling an operator, and do it.

    Called from wherever a failure code is *stamped*, which is deliberately not
    where the issue proposed. ``notifications.set_submission_status`` fires on
    the terminal transition and would be the natural seam, but it cannot work:
    **both** failure paths transition the job first and stamp the code second,
    because the transition is what creates the usage attribution row the stamp
    writes onto. Both say so in their own comments. Hooking the transition
    would therefore mean every email read "unknown code", and the one case that
    reading is reserved for -- *we failed and could not say why* -- would stop
    meaning anything.

    Never raises. An alert that kills the failure path it is reporting on is
    strictly worse than no alert, and this runs inside handlers that are
    already dealing with the researcher's real problem.

    Returns what it did, for the caller's log and for tests.
    """
    try:
        return _consider(job, code, detail)
    except Exception:
        logger.exception(
            "Could not raise an admin alert for job %s; the submission's own "
            "outcome is unaffected",
            str(job.get("_id")),
        )
        return "errored"


def _consider(job, code, detail) -> str:
    # Claimed *before* the policy is read, so the field means "an alert decision
    # was taken" rather than "an email was sent". A NEVER code claims too --
    # otherwise the sweep below cannot tell a failure we decided to stay quiet
    # about from one that never reached a decision, and would mail an operator
    # about every stata_error on the deployment.
    if not _claim(job):
        return "already-alerted"

    if code is None:
        # The state the sweep exists for. "We failed and could not say why" is
        # the most alert-worthy thing there is, so it is never suppressed by
        # policy -- only by the storm control every email path goes through.
        name, detail, policy = UNKNOWN_CODE, None, Alert.EMAIL
    else:
        policy = policy_for(code)
        name = str(getattr(code, "value", code))
        detail = safe_detail(code, detail)

    if policy is Alert.NEVER:
        return "never"

    count, first = AlertCounter().note(name, detail)
    if policy is Alert.DIGEST:
        return "digested"
    if not first:
        # The storm, collapsed. Nothing is lost: the counter this just
        # incremented is what the next digest reports, so a signature that
        # fired ten times is one email plus a line saying so.
        logger.info(
            "Suppressing a repeat admin alert for %s (%d so far)", code, count
        )
        return "suppressed"

    send_failure_email(job, name, detail)
    return "emailed"


#: Trailing job-log lines to quote in the email.
#:
#: The thing an admin would otherwise open the job for -- and, after #129, the
#: email may be the only copy: a researcher pressing Delete takes the job and
#: its log with it.
LOG_TAIL_LINES = 25


def _job_facts(job) -> dict:
    """Everything the email needs, read from the job and its folder.

    **Never from ``sivacor_execution_record``.** ``record_outcome``'s docstring
    already draws that line: the telemetry store is anonymous by design and
    documents that no identity flows through it. An admin email naming a
    researcher is fine and operational; a code path that reads identity *out
    of* the anonymous store is not, and that store's lawfulness rests on there
    being no such path.
    """
    from .settings import PluginSettings

    job = Job().load(job["_id"], includeLog=True, force=True) or job
    meta = job.get("meta") or {}
    facts = {
        "job_id": str(job["_id"]),
        "step": meta.get("sivacorFailedStep"),
        "login": None,
        "submission_name": None,
        "stage": None,
        "stages": None,
        "requested_memory_gb": meta.get("requested_memory_gb"),
        "requested_disk_gb": meta.get("requested_disk_gb"),
        "elapsed": None,
        "log_tail": "".join((job.get("log") or [])[-LOG_TAIL_LINES:]).strip(),
    }
    if job.get("created") and job.get("updated"):
        facts["elapsed"] = str(job["updated"] - job["created"])
    user_id = job.get("userId")
    if user_id and (user := User().load(user_id, force=True)):
        facts["login"] = user.get("login")
    try:
        root = Collection().findOne(
            {"name": Setting().get(PluginSettings.SUBMISSION_COLLECTION_NAME)}
        )
        folder = root and Folder().findOne(
            {"parentId": root["_id"], "meta.job_id": str(job["_id"])}
        )
    except Exception:
        folder = None
    if folder:
        facts["submission_name"] = folder.get("name")
        stages = (folder.get("meta") or {}).get("stages") or []
        facts["stages"] = len(stages) or None
    return facts


def send_failure_email(job, code, detail=None) -> None:
    """Mail every admin about one failure. Best effort, like every other send.

    SMTP being down must not change a submission's outcome -- the same
    never-raise rule the heartbeat follows, and for the same reason: the run
    already failed, and failing it differently because we could not talk about
    it helps nobody.
    """
    from .notifications import _createMessage, email_urls

    recipients = [u["email"] for u in User().getAdmins() if u.get("email")]
    if not recipients:
        logger.warning("No admin has an email address; cannot raise an alert")
        return

    facts = _job_facts(job)
    name = str(getattr(code, "value", code))
    billable = is_billable(code)
    context = {
        **email_urls(),
        **facts,
        "code": name,
        "detail": detail,
        # The cost of the failure in one word. `billable` false means the house
        # is paying, i.e. we called it our own fault.
        "house_pays": not billable,
        "current_year": _now().year,
    }
    subject = f"[SIVACOR] submission failed: {name}"
    if facts["login"]:
        subject += f" ({facts['login']})"

    stage_line = (
        f"Stage: {facts['stage']} of {facts['stages']}\n"
        if facts["stage"] and facts["stages"]
        else f"Stages: {facts['stages']}\n"
        if facts["stages"]
        else ""
    )
    text = (
        f"A SIVACOR submission terminated abnormally.\n\n"
        f"Code: {name}{f' ({detail})' if detail is not None else ''}\n"
        f"Step: {facts['step'] or 'unknown'}\n"
        f"Job: {facts['job_id']}\n"
        f"User: {facts['login'] or 'unknown'}\n"
        f"{stage_line}"
        f"Elapsed: {facts['elapsed'] or 'unknown'}\n"
        f"Paid by: {'the house (our fault)' if not billable else 'the researcher'}\n\n"
        f"Last {LOG_TAIL_LINES} log lines:\n{facts['log_tail'] or '(none)'}\n"
    )
    rendered = mail_utils.renderTemplate("submission_failure_admin.mako", context)
    msg, to = _createMessage(subject, text, rendered, recipients, None)
    events.trigger("_sendmail", info={"message": msg, "recipients": to})


#: How long after a job goes ERROR the sweep waits before calling its code lost.
#:
#: The stamp follows the terminal transition by one REST call, so this only has
#: to outlast that round trip -- but a worker retrying through a blip can take
#: longer, and alerting "unknown" about a failure whose code arrives a minute
#: later is a false alarm about the alarm system itself.
ALERT_GRACE = datetime.timedelta(minutes=10)

#: How far back the sweep looks. Bounds the blast radius on first deploy, where
#: every historical failure is undecided by definition: without it the first
#: sweep would consider the whole of a deployment's history at once.
SWEEP_WINDOW = datetime.timedelta(hours=24)


def sweep_undecided() -> dict:
    """Alert on failures that reached a terminal status without saying why.

    Every path that fails a submission stamps a code immediately afterwards, so
    this normally finds nothing. It exists for when that second call does not
    happen -- ``report_outcome`` swallows its own exceptions by design, because
    a counter must not replace the researcher's real error -- and the result is
    a run that failed with no recorded reason. The issue this implements is
    blunt about it: *"we failed and could not say why" is the single most
    alert-worthy state there is*, so it must not be the quiet one.

    Bounded at both ends. :data:`ALERT_GRACE` keeps it from racing the stamp it
    is checking for; :data:`SWEEP_WINDOW` keeps a first run from considering a
    deployment's entire history. Storm control does the rest: every failure it
    finds shares one signature, so a backlog is one email and a count.
    """
    now = _now()
    cursor = Job().collection.find(
        {
            "type": "sivacor_submission",
            "status": JobStatus.ERROR,
            "updated": {"$gt": now - SWEEP_WINDOW, "$lt": now - ALERT_GRACE},
            DECIDED_FIELD: {"$exists": False},
        },
        projection={"_id": 1, "userId": 1, "meta": 1, "created": 1, "updated": 1},
    )
    found = alerted = 0
    for job in cursor:
        found += 1
        if consider(job) in ("emailed", "suppressed"):
            alerted += 1
    if found:
        logger.warning(
            "Swept %d submission(s) that failed without recording a reason", found
        )
    return {"found": found, "alerted": alerted}


def send_digest() -> dict:
    """Report every counted failure since the last digest, in one email.

    Two things end up here, and keeping them in one message is the point of
    having a digest at all:

    * the ``DIGEST`` codes -- out of memory, out of disk, past the runtime
      ceiling. Each is the researcher's to pay for, and each is the most
      expensive outcome in its class with a control behind it, so the number
      per day is the thing an operator acts on rather than any single one.
    * the ``EMAIL`` codes that were **suppressed as repeats**. The first of a
      storm was mailed as it happened; the rest are a count here, which is what
      makes collapsing them lossless rather than merely quieter.

    Counts only, with no job ids and no logins: this is built from
    :class:`~girder_sivacor.models.alerts.AlertCounter`, which holds no
    identifier, and there is no reason to go and fetch some. A reader who needs
    to know *which* submissions can follow the link to ``#sivacor/telemetry``.

    Returns a summary for the caller; sends nothing when there is nothing to
    say, because a daily email that usually reads "nothing happened" trains
    people to stop opening it.
    """
    from .notifications import _createMessage, email_urls

    rows = AlertCounter().drain()
    if not rows:
        return {"sent": False, "signatures": 0, "failures": 0}

    recipients = [u["email"] for u in User().getAdmins() if u.get("email")]
    if not recipients:
        logger.warning("No admin has an email address; dropping the alert digest")
        return {"sent": False, "signatures": len(rows), "failures": 0}

    rows.sort(key=lambda row: (-row.get("count", 0), row.get("code") or ""))
    total = sum(row.get("count", 0) for row in rows)
    lines = []
    for row in rows:
        code = row.get("code")
        detail = row.get("detail")
        label = f"{code}" + (f" ({detail})" if detail is not None else "")
        suffix = " -- already mailed once" if policy_for(code) is Alert.EMAIL else ""
        lines.append(f"  {row.get('count', 0):>4}  {label}{suffix}")

    context = {
        **email_urls(),
        "rows": rows,
        "total": total,
        "current_year": _now().year,
    }
    text = (
        f"SIVACOR failures since the last digest: {total} across "
        f"{len(rows)} signature(s).\n\n" + "\n".join(lines) + "\n"
    )
    rendered = mail_utils.renderTemplate("submission_failure_digest.mako", context)
    msg, to = _createMessage(
        f"[SIVACOR] daily failure digest: {total} failure(s)", text, rendered,
        recipients, None,
    )
    events.trigger("_sendmail", info={"message": msg, "recipients": to})
    return {"sent": True, "signatures": len(rows), "failures": total}
