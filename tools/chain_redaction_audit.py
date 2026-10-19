#!/usr/bin/env python3
"""Census the redacted celery chains, and prove the backfill of #129 landed.

Why this exists
---------------
girder-sivacor#129: ``DELETE /sivacor/submission/:id`` removed a submission's
folder and job but not its celery chain, and ``cleanup_submissions`` finds
work by iterating folders -- so pressing Delete made the chain permanent,
tokens, researcher container output, filenames and user ids included. The fix
redacts the chain on the delete path and clears the backlog with
``POST /sivacor/redact_orphans``.

Running that backfill proves nothing by itself, and *re-running* it proves
less than it looks: its query is ``meta.sivacorRedacted.version != N``, so
every document it has already rewritten has left the population. A second pass
returns ``redacted: 0`` whether the first cleared three thousand documents or
none. It can answer "is anything left", never "what did it do".

This script answers both, after the fact, from the database:

* **What it did** -- ``redact_job`` stamps ``meta.sivacorRedacted.at`` on every
  skeleton it writes, so the backfill is recoverable as a burst in a histogram
  of that field long after the shell scrollback is gone. That is the figure
  the 2026-10-17 production run never captured.
* **Whether anything is left** -- it re-derives, read-only, exactly the
  population ``redact_orphans`` would still find: a celery job that names a
  submission job which no longer exists and carries no current skeleton.
* **Whether redaction actually redacted** -- the criterion #129 was closed on
  is not "the endpoint ran" but "no chain job retains a log, a token, a
  secrets envelope, a filename or a reversible user id". A skeleton that still
  has ``log``, ``jobInfoSpec``, ``args``, ``kwargs`` or a non-null ``userId``
  means ``chain.KEEP_FIELDS`` has a hole and the whole exercise was cosmetic.
  That check is the point of this script; the counts are the interesting part.

**Read-only.** ``find``, ``count_documents`` and ``aggregate`` only: no writes,
no REST calls, no repair. Fixing a finding means running the backfill, which
is a different, deliberate act.

Where to run it
---------------
Inside the ``girder`` container, which already has pymongo and the mongo
network::

    docker exec -i $(docker ps -qf name=wt_girder) \
        python3 - < tools/chain_redaction_audit.py

Arguments survive that form -- ``python3 -`` passes everything after the
dash through to ``sys.argv`` -- so there is no need to copy the file in::

    docker exec -i $(docker ps -qf name=wt_girder) \
        python3 - --by-hour < tools/chain_redaction_audit.py

Telling the backfill apart from the delete path
-----------------------------------------------
Both write a skeleton, so a document alone cannot say which wrote it. A
*chain* can: ``redact_chain`` attaches the submission job's narration to the
head of the chain and ``redact_orphans`` never can, because by then the job
that held it is gone. So a chain with a narration was deleted through the
fixed path, and a chain without one was almost certainly backfilled.

"Almost": a delete whose submission job had already vanished also writes no
narration. That is rare and it is the honest caveat on the split -- it inflates
the backfill's share rather than hiding it, and the ``at`` histogram
disambiguates anyway, since the backfill is a burst inside one or two hours
and deletes trickle.

``narration_dropped`` is not a defect signal
-------------------------------------------
A skeleton records how many of the submission job's log lines matched no
template in :mod:`girder_sivacor.narration`, and it is tempting to read a
non-zero count as that module having rotted. It is not. A failure's headline
(``Failed to run TRO utilities:``) is kept and the exception it interpolates
lands on the next line, tab-indented, where it is **deliberately** matched by
nothing -- it is where ``r(601)``, a ``renv`` path under ``/tmp/workspace-...``
and a ``UnicodeDecodeError``'s raw bytes turn up. Keeping the headline and
dropping the detail is the design, and since #129 that detail has a structured
home of its own in ``meta.sivacorFailure``.

So every failed submission contributes drops, and a count here tracks how many
of the deleted runs had failed. Measured on the restored dump: of 128 dropped
lines across 227 submission logs, **127 are exactly that indented detail**.

``--narration`` is the check that *would* catch rot, and it needs the live
logs rather than the skeletons, because a skeleton keeps only the count. It
replays every surviving submission job's log through the real module and
reports the drops that are **not** a failure's detail -- a line the platform
writes that nobody templated. On the restored dump that is 1: a bare
``Failed`` from a release no longer running, in a log from 2026-02-17.

What this cannot see
--------------------
Anything about the 611 ``type: "celery"`` jobs that belong to no chain and
still hold a token (girder-sivacor#142). It counts them, because a number
materially above 611 would mean chain jobs are escaping ``chain_query``
rather than that #142 is merely still open, but it does not look inside them.

Output note
-----------
Unlike ``assetstore_integrity.py``, this report is anonymous by construction:
everything it prints is a count, an hour, or a salted pseudonym. That is a
property of what it reads, not a filter it applies -- if a count here is
non-zero in the "still holding" row, the *documents* are not anonymous, which
is the finding.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter

from bson.errors import InvalidId
from bson.objectid import ObjectId
from pymongo import MongoClient
from pymongo.errors import PyMongoError

#: ``girder_sivacor.chain.REDACTION_VERSION``. Hardcoded rather than imported
#: so this runs anywhere pymongo does, including a plain python:3.12 container
#: pointed at the database -- a diagnostic should not need the plugin
#: importable. ``--version`` overrides it, which is what to use after a bump:
#: documents at the *old* version are then correctly reported as work left.
REDACTION_VERSION = 1

#: ``girder_sivacor.chain.REDACTED_KEY`` and ``FAILURE_KEY``, same reasoning.
REDACTED_KEY = "sivacorRedacted"
FAILURE_KEY = "sivacorFailure"

#: Fields a skeleton must not have. The first four are ``$unset`` by
#: ``redact_job``'s allow-list; ``userId`` is blanked rather than unset,
#: because job listing reads it unguarded, so it is checked by value.
#: ``jobInfoSpec`` carries the Girder-Token, ``args`` the secrets envelope and
#: the workspace paths, ``log`` the researcher's container output.
FORBIDDEN = ("log", "jobInfoSpec", "args", "kwargs")

#: What girder-sivacor#142 counted on 2026-10-14, as a floor rather than as an
#: expectation. **This number grows on its own**: ``delete_submission`` ends in
#: ``deleteFolderTask.delay(...)``, which is a celery job carrying a
#: ``jobInfoSpec`` and args Girder owns (``[folderId, progress, userId]``), so
#: it is unattributable by construction and every deletion adds one. Measured:
#: 8 folder deletions on ``deploy-dev`` on 2026-10-19 added exactly 8. So a
#: count above this is the ordinary passage of time; what would be a surprise
#: is a jump much larger than the number of deletions since, which would mean
#: chain jobs are escaping ``chain_query`` rather than that #142 is still open.
EXPECTED_UNATTRIBUTED_TOKENS = 611


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Census redacted celery chains and verify the #129 backfill.",
        epilog="Read-only. Exits 1 if anything was found, 0 if clean.",
    )
    parser.add_argument(
        "--mongo-uri",
        default=os.environ.get("GIRDER_MONGO_URI", "mongodb://mongo:27017/girder"),
        help="Mongo connection string (default: $GIRDER_MONGO_URI).",
    )
    parser.add_argument(
        "--version",
        type=int,
        default=REDACTION_VERSION,
        metavar="N",
        help=f"Redaction version to treat as current (default {REDACTION_VERSION}).",
    )
    parser.add_argument(
        "--by-hour",
        action="store_true",
        help="Print the full hour-by-hour histogram of when skeletons were "
        "written, not just the busiest hour. This is how the backfill's own "
        "count is recovered.",
    )
    parser.add_argument(
        "--narration",
        action="store_true",
        help="Replay every surviving submission job's log through "
        "girder_sivacor.narration and report lines it drops that are not a "
        "failure's interpolated detail. This is the rot check; the skeletons' "
        "own 'dropped' counter is not. Needs the plugin importable.",
    )
    parser.add_argument(
        "--json",
        metavar="PATH",
        help="Also write the report as one JSON object here. Counts only.",
    )
    parser.add_argument("--quiet", action="store_true", help="Findings only.")
    return parser.parse_args(argv)


def skeleton_of(job):
    return (job.get("meta") or {}).get(REDACTED_KEY) or {}


def submission_id_of(job):
    """The submission job id a chain job names, or ``None``.

    The same two spellings as ``girder_sivacor.chain.submission_id_of``: the
    head step is published positionally and carries it at ``args[3]``, every
    step after it receives the previous step's return value at ``args[0]``.
    """
    args = job.get("args") or []
    if args and isinstance(args[0], dict) and args[0].get("job_id"):
        return str(args[0]["job_id"])
    if len(args) > 3 and isinstance(args[3], str):
        return args[3]
    return None


def census(db, version):
    """Count the skeletons and when they were written. Returns a dict."""
    jobs = db.job
    current = {f"meta.{REDACTED_KEY}.version": version}
    report = {
        "celery_jobs": jobs.count_documents({"type": "celery"}),
        "skeletons": jobs.count_documents(current),
        "stale_version": jobs.count_documents(
            {
                f"meta.{REDACTED_KEY}": {"$exists": True},
                f"meta.{REDACTED_KEY}.version": {"$ne": version},
            }
        ),
        "with_failure": jobs.count_documents(
            {**current, f"meta.{FAILURE_KEY}": {"$exists": True}}
        ),
    }

    by_hour = Counter()
    narrated_chains, all_chains = set(), set()
    narration_dropped = 0
    docs_in_narrated_chain = Counter()
    for job in jobs.find(current, {f"meta.{REDACTED_KEY}": 1}):
        skeleton = skeleton_of(job)
        when = skeleton.get("at")
        by_hour[when.strftime("%Y-%m-%d %H:00") if when else "(no timestamp)"] += 1
        chain = skeleton.get("chain")
        if chain:
            all_chains.add(chain)
            docs_in_narrated_chain[chain] += 1
            if "narration" in skeleton:
                narrated_chains.add(chain)
        narration_dropped += int(skeleton.get("narration_dropped") or 0)

    report["by_hour"] = dict(sorted(by_hour.items()))
    report["chains"] = len(all_chains)
    # A chain with a narration was deleted through the fixed path; one without
    # was backfilled. See the module docstring for the caveat on "without".
    report["chains_delete_path"] = len(narrated_chains)
    report["chains_backfilled"] = len(all_chains - narrated_chains)
    report["docs_delete_path"] = sum(
        n for chain, n in docs_in_narrated_chain.items() if chain in narrated_chains
    )
    report["docs_backfilled"] = report["skeletons"] - report["docs_delete_path"]
    report["narration_dropped"] = narration_dropped
    return report


def leftovers(db, version):
    """What ``redact_orphans`` would still find, derived without writing.

    Mirrors its loop exactly: a chain job it cannot attribute to a submission
    is left alone and counted ``unknown``, a submission that still exists is
    ``live``, and a malformed id cannot name a live submission so it is work.
    """
    jobs = db.job
    counts = {"orphaned": 0, "live": 0, "unknown": 0}
    query = {"type": "celery", f"meta.{REDACTED_KEY}.version": {"$ne": version}}
    for job in jobs.find(query, {"args": 1}):
        submission_id = submission_id_of(job)
        if not submission_id:
            counts["unknown"] += 1
            continue
        try:
            exists = jobs.find_one({"_id": ObjectId(submission_id)}, {"_id": 1})
        except (InvalidId, TypeError):
            exists = None
        counts["orphaned" if exists is None else "live"] += 1
    return counts


def dirty_skeletons(db, version):
    """Skeletons that kept something the allow-list should have dropped.

    The acceptance criterion of #129, checked against the database rather than
    against the test suite. Returns a per-field count: which field leaked is
    the first thing to know, and ``_id``\\ s are deliberately not collected --
    the answer to a non-zero count here is to read ``chain.KEEP_FIELDS``, not
    to go and look at one document.
    """
    current = {f"meta.{REDACTED_KEY}.version": version}
    found = {
        field: db.job.count_documents({**current, field: {"$exists": True}})
        for field in FORBIDDEN
    }
    found["userId"] = db.job.count_documents({**current, "userId": {"$ne": None}})
    return {field: count for field, count in found.items() if count}


def token_census(db, version):
    """Celery jobs still holding a ``jobInfoSpec``, split by why.

    ``unattributed`` is girder-sivacor#142's population -- Girder's own
    ``deleteFolderTask`` and the retired ``Cleanup submission folder``, neither
    of which is "this submission's chain" by ``chain_query``'s definition.
    ``attributed`` is the row that matters here: a token-holding job that
    *does* name a submission is either a live chain, which is correct, or a
    miss, which this script's ``leftovers`` has already counted.
    """
    counts = {"total": 0, "attributed": 0, "unattributed": 0, "redacted": 0}
    query = {"type": "celery", "jobInfoSpec": {"$exists": True}}
    for job in db.job.find(query, {"args": 1, f"meta.{REDACTED_KEY}.version": 1}):
        counts["total"] += 1
        if skeleton_of(job).get("version") == version:
            # A skeleton with a token is a contradiction: redact_job unsets
            # jobInfoSpec. Counted separately so it cannot hide in a bucket.
            counts["redacted"] += 1
        elif submission_id_of(job):
            counts["attributed"] += 1
        else:
            counts["unattributed"] += 1
    return counts


def narration_rot(db):
    """Dropped submission-log lines that are *not* a failure's detail.

    The rot check. ``girder_sivacor.narration`` is imported here rather than
    at module scope so the rest of this script keeps running anywhere pymongo
    does; a deployment where the plugin is not importable gets a note instead
    of a traceback.

    A dropped line is explained if it is the tab-indented exception text
    following a kept ``Failed to ...:`` headline -- the one drop the module
    makes on purpose. Anything else is a line the platform writes that nobody
    templated, and it is being lost from every future post-mortem.
    """
    try:
        from girder_sivacor import narration
    except ImportError as exc:
        return {"available": False, "why": str(exc)}

    shapes, explained, scanned = Counter(), 0, 0
    query = {"type": "sivacor_submission", "log": {"$exists": True}}
    for job in db.job.find(query, {"log": 1}):
        scanned += 1
        log = job.get("log") or []
        if isinstance(log, str):
            log = [log]
        lines = [
            line
            for chunk in log
            if isinstance(chunk, str)
            for line in chunk.splitlines()
            if line.strip()
        ]
        for index, line in enumerate(lines):
            if narration._keep(line) is not None:
                continue
            previous = lines[index - 1] if index else ""
            detail_of_a_failure = (
                line[:1] in ("\t", " ")
                and previous.rstrip().endswith(":")
                and narration._keep(previous) is not None
            )
            if detail_of_a_failure:
                explained += 1
            else:
                shapes[line.strip()[:120]] += 1
    return {
        "available": True,
        "logs_scanned": scanned,
        "explained": explained,
        "unexplained": dict(shapes.most_common()),
    }


def print_report(report, args):
    out = sys.stdout
    if not args.quiet:
        print(f"celery jobs                {report['celery_jobs']:>7,}", file=out)
        print(f"  redacted skeletons       {report['skeletons']:>7,}", file=out)
        print(f"    via the delete path    {report['docs_delete_path']:>7,}"
              f"  ({report['chains_delete_path']:,} chains)", file=out)
        print(f"    via the backfill       {report['docs_backfilled']:>7,}"
              f"  ({report['chains_backfilled']:,} chains)", file=out)
        print(f"  carrying a traceback     {report['with_failure']:>7,}", file=out)
        print(f"  narration lines dropped  {report['narration_dropped']:>7,}"
              "   expected on a failed run, see --narration", file=out)
        print(file=out)

        hours = report["by_hour"]
        if hours:
            print("when the skeletons were written (UTC):", file=out)
            shown = hours if args.by_hour else dict([max(hours.items(), key=lambda kv: kv[1])])
            for hour, count in shown.items():
                print(f"  {hour}  {count:>7,}", file=out)
            if not args.by_hour and len(hours) > 1:
                print(f"  ... and {len(hours) - 1} other hour(s); --by-hour for all", file=out)
            print("  The backfill is the burst; deletes trickle one chain at a time.", file=out)
            print(file=out)

        left = report["leftovers"]
        print(f"chain jobs not redacted    {sum(left.values()):>7,}", file=out)
        print(f"  submission still exists  {left['live']:>7,}"
              "   correct: redaction is what deleting one does", file=out)
        print(f"  submission is gone       {left['orphaned']:>7,}"
              "   must be 0; this is the backfill's remaining work", file=out)
        print(f"  unattributable           {left['unknown']:>7,}"
              "   left alone on purpose, see girder-sivacor#142", file=out)
        print(file=out)

        tokens = report["tokens"]
        print(f"celery jobs still holding a Girder-Token  {tokens['total']:>7,}", file=out)
        print(f"  outside any chain (girder-sivacor#142)  {tokens['unattributed']:>7,}"
              f"   {EXPECTED_UNATTRIBUTED_TOKENS:,} on 2026-10-14, +1 per deletion since", file=out)
        print(f"  belonging to a live submission          {tokens['attributed']:>7,}"
              "   goes when that submission does", file=out)
        print(file=out)

    rot = report.get("narration")
    if rot and not args.quiet:
        if not rot["available"]:
            print(f"--narration needs the plugin importable: {rot['why']}", file=out)
        else:
            print(f"submission logs replayed    {rot['logs_scanned']:>7,}", file=out)
            print(f"  drops explained as a failure's detail  {rot['explained']:>7,}", file=out)
            print(f"  drops nobody templated                 "
                  f"{sum(rot['unexplained'].values()):>7,}", file=out)
            for line, count in rot["unexplained"].items():
                print(f"      {count:>4}  {line}", file=out)
        print(file=out)

    findings = report["findings"]
    if not findings:
        print("No findings: every orphaned chain is redacted and every skeleton is clean.")
        return
    print("FINDINGS")
    for finding in findings:
        print(f"  * {finding}")


def main(argv=None):
    args = parse_args(argv)
    client = MongoClient(args.mongo_uri, serverSelectionTimeoutMS=10000)
    db = client.get_default_database()

    # The first query is also the connection test: MongoClient constructs
    # lazily, so a wrong URI only surfaces here -- and it should surface as one
    # line, not as a driver traceback the operator has to read past.
    try:
        report = census(db, args.version)
    except PyMongoError as exc:
        print(f"Could not query {args.mongo_uri}: {exc}", file=sys.stderr)
        return 2

    report["leftovers"] = leftovers(db, args.version)  # printed, and a finding
    report["dirty"] = dirty_skeletons(db, args.version)
    report["tokens"] = token_census(db, args.version)
    if args.narration:
        report["narration"] = narration_rot(db)

    findings = []
    if left := report["leftovers"]["orphaned"]:
        findings.append(
            f"{left:,} orphaned chain job(s) are not redacted. The backfill was "
            "not run to completion: page POST /sivacor/redact_orphans until "
            "'next' comes back null."
        )
    for field, count in report["dirty"].items():
        findings.append(
            f"{count:,} skeleton(s) still carry '{field}'. chain.KEEP_FIELDS has "
            "a hole and the redaction is cosmetic -- this is the one finding "
            "here that is a defect rather than unfinished work."
        )
    if stale := report["stale_version"]:
        findings.append(
            f"{stale:,} skeleton(s) are at an older redaction version. Re-run "
            "the backfill: redact_job rewrites them, which is what the version "
            "exists for."
        )
    rot = report.get("narration") or {}
    if rot.get("unexplained"):
        findings.append(
            f"{sum(rot['unexplained'].values()):,} submission log line(s) in "
            f"{len(rot['unexplained'])} shape(s) match no template in "
            "girder_sivacor.narration and are not a failure's detail, so they "
            "are lost from every post-mortem of a deleted run. Add the "
            "template; see that module's docstring."
        )
    if report["tokens"]["redacted"]:
        findings.append(
            f"{report['tokens']['redacted']:,} redacted skeleton(s) still hold a "
            "jobInfoSpec, which redact_job unsets. Treat as the 'log' row above."
        )
    report["findings"] = findings

    print_report(report, args)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
        print(f"\nWrote {args.json}")

    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
