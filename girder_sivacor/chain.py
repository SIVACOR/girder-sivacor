"""What "this submission's chain" is, and what survives deleting it.

**One definition of the chain.** Until girder-sivacor#129 there were two.
``cleanup_submissions`` matched ``{"args.0.job_id": job_id}``, while
``_fail_stranded`` and ``cancel_jobs`` matched that *or* ``{"args.3": job_id}``
-- and the narrow one was the one doing the deleting. The difference is the
head of the chain: ``prepare_submission`` is published with positional
arguments and carries the submission's job id at ``args[3]``, while every step
after it receives the previous step's return value as ``args[0]`` and carries
it at ``args[0]["job_id"]``. So the retention sweep has been leaving the head
job of every expired submission behind since the pipeline was written.
:func:`chain_query` is now the only spelling of the question.

**Why a chain job is redacted rather than deleted.** Measured on production on
2026-10-14: 1,636 celery chain jobs, 1,450 of them (89 %) belonging to
submissions that no longer exist, the oldest from 2026-08-05. All 1,450 carried
a ``Girder-Token``; 343 carried the AES-GCM secrets envelope; 90 % of a
120-job sample held a non-empty log, and 13 distinct user ids were recoverable
from ``docker:logs:<userId>`` lines in that sample alone. Pressing Delete was
therefore *strictly worse for erasure than doing nothing*: the visible half
went immediately and the chain became permanent, because
``cleanup_submissions`` finds work by iterating submission folders and the
folder was gone.

Deleting the chain outright was rejected for the reason the orphan sweep had to
be armed before volumes were: it is where post-mortems have actually been read
from, and removing it with no replacement makes the next incident harder.
Checked rather than assumed -- the Evidence sections of all nine write-ups were
read, and the appendix of
``../development_notes/incidents/2026-10-12-dns-pull-kills-run.md`` records
which fields each diagnosis depended on. What is kept below is every column
that table marks load-bearing, plus the narration and the traceback, which it
found were being lost on this path today.

**The boundary.** Allow-list, never deny-list -- the same rule, and for the
same reason, as :mod:`girder_sivacor.telemetry`. :func:`redact_job` names the
top-level fields that survive and ``$unset``\\ s everything else the document
happens to have, so a field a future girder_worker invents is dropped without
anybody remembering to drop it. ``meta`` is rebuilt rather than edited. Free
text goes through :mod:`girder_sivacor.narration` or
:func:`girder_sivacor.telemetry.sanitize_failure`; nothing else does.

**Pseudonyms, and the cost of them.** 2026-09-17 established *same researcher
as 09-11* by decoding ``docker:logs:6aa11162...``. Dropping the id outright has
a real diagnostic cost and pretending otherwise would be dishonest, so the id
is replaced by ``HMAC(salt, id)`` -- "the same account appears in two
incidents" survives, the reversible identifier does not. The salt lives in
:attr:`~girder_sivacor.settings.PluginSettings.PSEUDONYM_SALT` and is generated
on first use. **Deleting that setting is a kill switch**: every pseudonym ever
written becomes unlinkable to every other, which is the property that makes
keeping them defensible.

Not done here, deliberately: the skeletons are not yet subject to the retention
window, because the sweep finds work by folder and a redacted chain has none.
The issue's own plan puts that last -- once the skeleton has been read in anger
at least once, its content moves into the execution record and the chain is
deleted with everything else. Until then it stays.
"""

import datetime
import hashlib
import hmac
import logging
import re
import secrets

from girder.models.setting import Setting
from girder_jobs.models.job import Job

from . import narration
from .settings import PluginSettings
from .telemetry import safe_image_name, safe_image_tag, safe_title

logger = logging.getLogger(__name__)

#: Where a step's structured traceback lives, on the chain job it belongs to.
#: Written by ``PUT /sivacor/failure/:id`` and kept by :func:`redact_job`.
FAILURE_KEY = "sivacorFailure"

#: Where the skeleton lives, on a redacted chain job.
REDACTED_KEY = "sivacorRedacted"

#: Bumped when :func:`redact_job` keeps or drops something different. It is
#: what makes the backfill idempotent, and what makes re-running it after a
#: change actually do something.
REDACTION_VERSION = 1

#: Top-level fields of a chain job document that survive redaction.
#:
#: Every one is in the appendix's load-bearing set. ``status`` and
#: ``timestamps`` are 09-11's and 09-25's whole diagnosis -- which step was
#: running, and for how long; ``celeryTaskId``/``celeryParentTaskId`` are what
#: let the chain still be walked in order once ``args`` is gone.
#:
#: ``title`` is **not** here even though it is kept, because it is not kept
#: verbatim: :func:`~girder_sivacor.telemetry.safe_title` rewrites it. The head
#: step of every chain is titled ``f"Moving {file['name']} to submission
#: collection"``, so the researcher's uploaded filename is in the title of 881
#: of the 5,175 chain jobs in a restored production database. It is set below
#: rather than kept, which is why it is absent from this list.
#:
#: ``args`` and ``kwargs`` are absent on purpose and carry the most: workspace
#: and tmp paths, the main file's name, the original file, folder and job ids,
#: and -- on ``execute_workflow`` -- the AES-GCM secrets envelope and the
#: wrapped job key. ``jobInfoSpec`` is absent because it carries a
#: ``Girder-Token``. ``log`` is absent because it is a mixed stream: the
#: platform's lines and the researcher's container stdout in one field, which
#: is exactly what :data:`FAILURE_KEY` exists to undo.
KEEP_FIELDS = frozenset(
    {
        "_id",
        "type",
        "handler",
        "status",
        "progress",
        "created",
        "updated",
        "when",
        "interval",
        "timestamps",
        "public",
        "parentId",
        "celeryTaskId",
        "celeryParentTaskId",
        "asynchronous",
        "async",
        "meta",
    }
)

#: The log line :class:`~girder_sivacor.worker_plugin.lib.LogPublisher` prints
#: when it starts, naming the redis channel it publishes on -- which is keyed
#: on the submission's creator. Read out before ``log`` is dropped, as the
#: *fallback* source for the pseudonym.
#:
#: The issue reported this as the only surviving copy, on the grounds that the
#: job document's own ``userId`` is ``None``. That is not what a restored
#: production database shows: ``userId`` is set on all 5,175 chain jobs there.
#: So this covers only the older or stranger documents, and the straightforward
#: field covers the rest -- see :func:`redact_job`.
_CREATOR_IN_LOG = re.compile(r"docker:logs:([0-9a-fA-F]{24})")


def chain_query(job_id):
    """Every celery job belonging to the submission job ``job_id``.

    The single definition; callers add their own ``status`` filter. See the
    module docstring for why there are two branches.
    """
    job_id = str(job_id)
    return {
        "type": "celery",
        "$or": [{"args.0.job_id": job_id}, {"args.3": job_id}],
    }


def submission_id_of(job):
    """The submission job id a chain job belongs to, or ``None``.

    The inverse of :func:`chain_query`, for the backfill: it starts from the
    chain job and has to find out whether the submission still exists.
    """
    args = job.get("args") or []
    if args and isinstance(args[0], dict) and args[0].get("job_id"):
        return str(args[0]["job_id"])
    if len(args) > 3 and isinstance(args[3], str):
        return args[3]
    return None


def pseudonym(value, prefix=""):
    """A stable, salted, one-way name for ``value``. ``None`` passes through.

    Keyed HMAC rather than a bare hash: a Girder object id is 24 hex characters
    from a space small enough to enumerate, so an unkeyed digest would be
    reversible by anyone who could list the users.
    """
    if not value:
        return None
    digest = hmac.new(_salt(), str(value).encode(), hashlib.sha256).hexdigest()
    return f"{prefix}{digest[:16]}"


def _salt():
    """The pseudonym key, generated on first use.

    Two concurrent first uses can generate two salts and the loser's writes
    land in a second cohort -- harmless, and strictly in the safe direction: it
    weakens linkage, which is the thing being rationed here anyway.
    """
    salt = Setting().get(PluginSettings.PSEUDONYM_SALT)
    if not salt:
        Setting().set(PluginSettings.PSEUDONYM_SALT, secrets.token_hex(32))
        salt = Setting().get(PluginSettings.PSEUDONYM_SALT)
    return salt.encode()


def redact_chain(job_id, *, creator_id=None, narration_log=None):
    """Redact every chain job of one submission. Returns how many were rewritten.

    Called from ``delete_submission`` **before** the submission job is removed,
    because ``narration_log`` is that job's log and there is no second copy of
    it.

    :param creator_id: the submission's creator, for the pseudonym. Taken from
        the folder's ``meta.creator_id`` on the delete path; recovered from the
        chain's own log text on the backfill path.
    :param narration_log: the submission job's ``log``, scrubbed and attached
        to the head of the chain. ``None`` leaves the narration out.
    """
    jobs = list(Job().collection.find(chain_query(job_id)).sort("created", 1))
    if not jobs:
        return 0
    chain_key = pseudonym(job_id, prefix="c-")
    user_key = pseudonym(creator_id, prefix="u-")
    redacted = 0
    for index, job in enumerate(jobs):
        # The narration goes on the head of the chain only. Copying it onto
        # every step would multiply one submission's log by its step count for
        # no extra answer.
        log = narration_log if index == 0 else None
        if redact_job(job, chain_key=chain_key, user_key=user_key, narration_log=log):
            redacted += 1
    logger.info(
        "Redacted %d of %d chain jobs for submission %s",
        redacted,
        len(jobs),
        job_id,
    )
    return redacted


def redact_job(job, *, chain_key=None, user_key=None, narration_log=None):
    """Rewrite one chain job as its skeleton. Returns whether it was rewritten.

    Idempotent: a document already at :data:`REDACTION_VERSION` is left alone,
    which is what lets the backfill be run repeatedly and in pieces.
    """
    meta = job.get("meta") or {}
    if (meta.get(REDACTED_KEY) or {}).get("version") == REDACTION_VERSION:
        return False

    if user_key is None:
        # ``userId`` first. The issue reported it as ``None`` on these
        # documents -- "they look anonymous and are not" -- but a restored
        # production database has it set on all 5,175 of them, so the log scrape
        # is the fallback rather than the only source. It matters: scraping only
        # works for a job that actually started a container, which is a minority
        # of the chain, while this is on every document.
        #
        # From the collection when `job` came from ``Job().load``, which
        # projects ``log`` away unless asked (``_computeFields``) -- so reading
        # it off the document would silently lose the creator rather than fail.
        creator = job.get("userId")
        if not creator:
            log = job["log"] if "log" in job else _log_of(job["_id"])
            creator = _creator_from_log(log)
        user_key = pseudonym(creator, prefix="u-")

    skeleton = {
        "version": REDACTION_VERSION,
        "at": datetime.datetime.now(datetime.UTC),
        # What groups these documents now that `args` is gone. The chain can
        # still be *ordered* by celeryParentTaskId; this is what finds it.
        "chain": chain_key,
        "user": user_key,
        "stage": _stage_of(job),
    }
    if narration_log is not None:
        lines, dropped = narration.scrub(narration_log)
        skeleton["narration"] = lines
        # Reported, not swallowed: a non-zero count here means a log line
        # exists that `narration` has no template for, and every future
        # post-mortem silently loses it. See that module's docstring.
        skeleton["narration_dropped"] = dropped

    new_meta = {REDACTED_KEY: skeleton}
    # Already sanitized when it was written, by `PUT /sivacor/failure/:id`.
    if failure := meta.get(FAILURE_KEY):
        new_meta[FAILURE_KEY] = failure

    # Blanked rather than unset: `userId` is read unguarded by job listing and
    # filtering, and it is the value rather than the key that identifies
    # anyone. It is excluded from the unset below because Mongo refuses an
    # update that both sets and unsets one path.
    update = {
        "$set": {
            "meta": new_meta,
            # Rewritten, never kept: the head step's title is generated from
            # the uploaded file's name. See KEEP_FIELDS.
            "title": safe_title(job.get("title")),
            "userId": None,
            # Not unset: an access-controlled model with no `access` at all is
            # a shape Girder does not expect. Empty is the same answer stated
            # in the form every reader already handles.
            "access": {"users": [], "groups": []},
        }
    }
    # The allow-list, applied as an unset of its complement: a field this
    # module has never heard of goes, which is the direction a boundary has to
    # fail in.
    if drop := set(job) - KEEP_FIELDS - set(update["$set"]):
        update["$unset"] = {field: "" for field in drop}
    Job().collection.update_one({"_id": job["_id"]}, update)
    return True


def _stage_of(job):
    """The image a step ran, pulled out of ``args`` before ``args`` is dropped.

    Only ``execute_workflow`` has one: it is published as
    ``execute_workflow.s(stage, secrets)`` and so receives
    ``(submission, stage, secrets)``. Everything else in the chain is
    bookkeeping and has no image of its own.

    Both halves go through the same allow-list execution records use, which is
    not merely tidiness: ``image_name`` is the one value here that a reader
    would reasonably treat as safe because it "comes from the allow-list", and
    by this point it has been through a worker and a Mongo document.
    """
    args = job.get("args") or []
    if len(args) < 2 or not isinstance(args[1], dict):
        return None
    stage = args[1]
    name = safe_image_name(stage.get("image_name"))
    tag = safe_image_tag(stage.get("image_tag"))
    if not name:
        return None
    return {
        "image_name": name,
        "image_tag": tag,
        "network_isolation": bool(stage.get("network_isolation", False)),
    }


def _log_of(job_id):
    document = Job().collection.find_one({"_id": job_id}, {"log": 1}) or {}
    return document.get("log")


def _creator_from_log(log):
    """Recover the submission's creator from a chain job's log, or ``None``."""
    if isinstance(log, str):
        log = [log]
    for chunk in log or []:
        if not isinstance(chunk, str):
            continue
        if match := _CREATOR_IN_LOG.search(chunk):
            return match.group(1)
    return None


def redact_orphans(limit=500, after=None):
    """Redact chain jobs whose submission is already gone. Bounded, idempotent.

    The backfill for the 1,450 documents that predate
    :func:`redact_chain` being called on the delete path. Safe to run while the
    platform is live, and safe to run twice: a redacted document leaves the
    query, and a chain whose submission job still exists is skipped rather than
    touched.

    Paged by ``_id`` rather than by a growing offset, so a run that redacts
    nothing (every document in the page was live) still advances. ``next`` in
    the result is what to pass back as ``after``; ``None`` means the end.

    Neither narration nor a failure record can be recovered here -- the
    submission job is already gone, which is the whole problem the forward path
    fixes. What *is* recovered is the creator, from the log's
    ``docker:logs:<userId>`` line, so the backlog keeps the one capability
    (finding C) that its text was carrying.
    """
    query = {
        "type": "celery",
        f"meta.{REDACTED_KEY}.version": {"$ne": REDACTION_VERSION},
    }
    if after:
        query["_id"] = {"$gt": _object_id(after)}
    counts = {"examined": 0, "redacted": 0, "live": 0, "unknown": 0, "next": None}
    last = None
    for job in Job().collection.find(query).sort("_id", 1).limit(int(limit)):
        counts["examined"] += 1
        last = job["_id"]
        submission_id = submission_id_of(job)
        if not submission_id:
            # Not provably an orphan, so not touched. A chain job we cannot
            # attribute is the one case where doing nothing is right: it is
            # also the case where we cannot tell whether a live run is reading
            # it.
            counts["unknown"] += 1
            continue
        if _submission_exists(submission_id):
            counts["live"] += 1
            continue
        if redact_job(job, chain_key=pseudonym(submission_id, prefix="c-")):
            counts["redacted"] += 1
    if counts["examined"] == int(limit) and last is not None:
        counts["next"] = str(last)
    return counts


def _submission_exists(job_id):
    try:
        return Job().load(job_id, force=True) is not None
    except Exception:
        # A malformed id cannot name a live submission.
        return False


def _object_id(value):
    from bson.objectid import ObjectId

    return value if isinstance(value, ObjectId) else ObjectId(str(value))
