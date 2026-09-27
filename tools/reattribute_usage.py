#!/usr/bin/env python3
"""Move one instance's accrued usage from the house bucket to a researcher.

Why this exists
---------------
``usage.accrue`` decides who pays from the failure code stamped on an instance,
and that decision is final: the transient join rows are deleted at accrual, so
nothing downstream can revisit it. When the stamp itself was wrong, the bill
stays wrong forever and there is no way back.

That is not hypothetical. A production submission ran to completion and was
declared failed anyway, because the reaper's 30-minute silence threshold
elapsed while ``upload_workspace`` was uploading a 28.7 GiB package. The run
was corrected by hand everywhere a researcher can see it -- folder status, job
status, the notification -- but the accrual had already charged 5,246 SU-hours
to the house as ``reaped_no_heartbeat``, which is 98.6% of every SU the
platform has ever accounted for, attributed to nobody. This script is the
missing way back.

What it moves, and what it refuses to compute
---------------------------------------------
**The su-hours come from the house's own ``byReason`` entry, never from a
recomputed rate.** The house holds what was actually charged; multiplying hours
by a rate looked up today would silently substitute today's catalogue for the
one in force at accrual, and a reattribution that does not balance is worse
than no reattribution at all. The credit is the debit, to the last bit.

Everything the house cannot break down per reason -- instance-hours, the
instance count -- must be stated, and may only be defaulted when the house
holds exactly one reason and there is nothing to disambiguate.

``volume_gb_hours`` is the one figure genuinely absent from both sides: the
house never records it, because ``accrue`` only computes it on the path that
charges a user. Pass ``--volume-gb`` and it is derived the same way the accrual
would have (hours x gigabytes); omit it and the researcher is credited none,
which is right for a run with no volume and wrong for one with a volume, so it
is worth looking up.

Where to run it
---------------
Anywhere pymongo can reach Girder's database. Inside the girder container::

    docker exec -i $(docker ps -qf name=wt_girder) \\
        python3 - --login jane.doe --reason reaped_no_heartbeat --volume-gb 470 \\
        < tools/reattribute_usage.py

**Dry run by default.** It prints both documents before and after and changes
nothing until ``--apply``.

What it deliberately does not touch
-----------------------------------
The execution record. That store is anonymous by construction and carries no
user, job or instance id, so there is no key to find the run by, and rewriting
telemetry is a different decision from correcting a bill. A run corrected here
still reads as ``reaped_no_heartbeat`` in the failure-code breakdown, and that
is the intended state, not an oversight.
"""

from __future__ import annotations

import argparse
import datetime
import os
import sys

#: The house bucket's ``_id`` -- ``girder_sivacor.usage.TOTALS_ID``. Hardcoded,
#: with the collection names below, so this runs anywhere pymongo does rather
#: than needing the whole girder import to move two numbers.
TOTALS_ID = "house"
TOTALS_COLLECTION = "sivacor_usage_totals"
USER_USAGE_FIELD = "sivacorUsage"

#: Counters at or below this are treated as spent. Subtracting equal floats is
#: exact, but the house's total may have been accumulated from several accruals
#: whose sum no longer round-trips, and leaving 1e-13 SU-hours behind would keep
#: a document alive that should have been deleted.
EPSILON = 1e-9


def _fmt(value):
    return "none" if value is None else value


def _as_utc(value):
    """Coerce a stored datetime to aware UTC for comparison.

    Whether Mongo hands back a naive or an aware datetime is a property of the
    *client* (``tz_aware``), not of the stored instant -- BSON has no timezone.
    This script builds its own client and Girder builds another, so the two
    disagree, and comparing across them raises ``TypeError`` rather than
    returning a wrong answer. Normalising at the comparison keeps the stored
    instant untouched, which is what matters: a correction that shifted a
    timestamp by a UTC offset would misdate the accrual it is repairing.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)


def house_state(db):
    return db[TOTALS_COLLECTION].find_one({"_id": TOTALS_ID}) or {}


def user_state(db, login):
    user = db["user"].find_one({"login": login})
    if user is None:
        raise SystemExit(f"no account with login {login!r}")
    return user


def plan(db, login, reason, instance_hours=None, instances=None, volume_gb=0, at=None):
    """Work out the move, or raise ``SystemExit`` explaining why it cannot be made."""
    house = house_state(db)
    by_reason = house.get("byReason") or {}
    if reason not in by_reason:
        raise SystemExit(
            f"the house holds nothing under {reason!r}; it has "
            f"{sorted(by_reason) or 'no charges at all'}"
        )

    su_hours = float(by_reason[reason])
    single = len(by_reason) == 1

    if instance_hours is None:
        if not single:
            raise SystemExit(
                "--instance-hours is required: the house is carrying "
                f"{len(by_reason)} reasons and does not break instance-hours "
                "down by any of them"
            )
        instance_hours = float(house.get("instanceHours", 0.0))
    if instances is None:
        instances = int(house.get("instances", 0)) if single else None
        if instances is None:
            raise SystemExit("--instances is required when the house holds several reasons")

    if instance_hours > float(house.get("instanceHours", 0.0)) + EPSILON:
        raise SystemExit(
            f"the house holds {house.get('instanceHours', 0.0)} instance-hours, "
            f"fewer than the {instance_hours} being moved"
        )

    user = user_state(db, login)
    at = at or house.get("lastAt")
    if at is None:
        raise SystemExit("--at is required: the house row carries no timestamp to inherit")

    return {
        "user": user,
        "reason": reason,
        "su_hours": su_hours,
        "instance_hours": instance_hours,
        "instances": instances,
        "volume_gb_hours": instance_hours * float(volume_gb or 0),
        "at": at,
        "house": house,
    }


def apply_plan(db, move, memory_gb=None):
    """Credit the researcher and debit the house by exactly the same figures."""
    user_id = move["user"]["_id"]
    field = USER_USAGE_FIELD

    inc = {
        f"{field}.submissions": move["instances"],
        f"{field}.suHours": move["su_hours"],
        f"{field}.instanceHours": move["instance_hours"],
        f"{field}.volumeGbHours": move["volume_gb_hours"],
    }
    if memory_gb is not None:
        inc[f"{field}.bySize.{int(memory_gb)}"] = move["instances"]
    db["user"].update_one({"_id": user_id}, {"$inc": inc})

    # `since` is the *earliest* accrual and `lastAt` the latest, and a correction
    # lands in the past -- so neither may simply be set. An account whose first
    # real run came after this one would otherwise claim its accounting began
    # later than the spend it is now carrying.
    current = (db["user"].find_one({"_id": user_id}) or {}).get(field) or {}
    at = _as_utc(move["at"])
    since, last_at = _as_utc(current.get("since")), _as_utc(current.get("lastAt"))
    stamps = {}
    if since is None or at < since:
        stamps[f"{field}.since"] = move["at"]
    if last_at is None or at > last_at:
        stamps[f"{field}.lastAt"] = move["at"]
    if stamps:
        db["user"].update_one({"_id": user_id}, {"$set": stamps})

    db[TOTALS_COLLECTION].update_one(
        {"_id": TOTALS_ID},
        {
            "$inc": {
                "suHours": -move["su_hours"],
                "instanceHours": -move["instance_hours"],
                "instances": -move["instances"],
                f"byReason.{move['reason']}": -move["su_hours"],
            }
        },
    )

    # A reason drained to zero is not a reason with no spend -- it is a reason
    # that no longer applies to anything, and leaving it at 0.0 would put a row
    # in the operator's house breakdown that means nothing.
    house = house_state(db)
    drained = [
        key for key, value in (house.get("byReason") or {}).items()
        if abs(float(value)) <= EPSILON
    ]
    if drained:
        db[TOTALS_COLLECTION].update_one(
            {"_id": TOTALS_ID},
            {"$unset": {f"byReason.{key}": "" for key in drained}},
        )
    house = house_state(db)
    spent = all(
        abs(float(house.get(key, 0.0))) <= EPSILON
        for key in ("suHours", "instanceHours", "instances")
    ) and not (house.get("byReason") or {})
    if spent:
        # Deleted rather than zeroed: `report()` defaults a missing house to
        # zeroes anyway, and an empty document would keep claiming a `since`
        # for spend that is no longer there.
        db[TOTALS_COLLECTION].delete_one({"_id": TOTALS_ID})

    return house_state(db), (db["user"].find_one({"_id": user_id}) or {}).get(field) or {}


def describe(label, house, user_counters):
    print(f"  {label}")
    print(f"    house        : su={_fmt(house.get('suHours'))} "
          f"inst_h={_fmt(house.get('instanceHours'))} "
          f"instances={_fmt(house.get('instances'))} "
          f"reasons={sorted(house.get('byReason') or {}) or 'none'}")
    print(f"    researcher   : su={_fmt(user_counters.get('suHours'))} "
          f"inst_h={_fmt(user_counters.get('instanceHours'))} "
          f"vol_gb_h={_fmt(user_counters.get('volumeGbHours'))} "
          f"submissions={_fmt(user_counters.get('submissions'))} "
          f"sizes={user_counters.get('bySize') or 'none'}")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Move one instance's accrued usage from the house to a researcher.",
        epilog="Dry run unless --apply is given.",
    )
    parser.add_argument("--login", required=True, help="the researcher to charge")
    parser.add_argument("--reason", required=True,
                        help="the house byReason key to move, e.g. reaped_no_heartbeat")
    parser.add_argument("--instance-hours", type=float, default=None,
                        help="defaults to the house's total when it holds one reason")
    parser.add_argument("--instances", type=int, default=None,
                        help="defaults to the house's count when it holds one reason")
    parser.add_argument("--memory-gb", type=int, default=None,
                        help="the worker rung, for the researcher's bySize tally")
    parser.add_argument("--volume-gb", type=int, default=0,
                        help="scratch volume size, to derive volume GB-hours")
    parser.add_argument("--at", default=None,
                        help="ISO-8601 accrual time; defaults to the house's lastAt")
    parser.add_argument("--mongo-uri", default=os.environ.get(
        "GIRDER_MONGO_URI", "mongodb://mongo:27017/girder"))
    parser.add_argument("--apply", action="store_true",
                        help="write the change; without it nothing is modified")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    from pymongo import MongoClient

    client = MongoClient(args.mongo_uri)
    db = client.get_default_database()

    at = None
    if args.at:
        at = _as_utc(datetime.datetime.fromisoformat(args.at.replace("Z", "+00:00")))

    move = plan(db, args.login, args.reason, args.instance_hours, args.instances,
                args.volume_gb, at)

    print(f"moving {move['su_hours']} SU-hours ({move['reason']}) to {args.login}")
    print(f"  instance-hours {move['instance_hours']}  instances {move['instances']}  "
          f"volume GB-hours {move['volume_gb_hours']}  at {move['at']}")
    describe("before:", move["house"],
             (move["user"].get(USER_USAGE_FIELD) or {}))

    if not args.apply:
        print("\ndry run -- nothing written. Re-run with --apply.")
        return 0

    house, counters = apply_plan(db, move, args.memory_gb)
    describe("after:", house, counters)
    print("\napplied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
