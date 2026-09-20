#!/usr/bin/env python3
"""Verify per-user resource accounting against a real deployment.

09_user_usage_accounting_plan.md's evidence lines, as one command. Run it after
at least one submission has been created *and its worker reaped* -- the reap is
what produces the lifetime row, and it happens ~18 minutes after the submission
finishes, not when it finishes.

    export GIRDER_API_URL=https://girder.test.sivacor.org/api/v1
    export GIRDER_API_KEY=...          # an admin key on that deployment
    python3 tools/usage_check.py

**Never point this at production casually.** It is read-only apart from one
``POST /sivacor/usage/drain``, which is idempotent by construction -- each
lifetime row is claimed with ``find_one_and_delete`` -- but draining on
production moves real numbers onto real user documents, which is the beat task's
job and not yours. Pass ``--no-drain`` to observe without touching anything.

What it can and cannot prove, stated plainly because the difference matters:

* It **can** prove the two halves of the join met, that the rate applied is the
  catalogue's, and that the lifetime is a plausible instance life. The strongest
  internal check is ``suHours / instanceHours``, which must equal the rung's
  ``su_per_hour`` exactly -- nothing else in the pipeline can make that true by
  accident.
* It **cannot** prove the lifetime matches what Nova saw. That needs
  ``openstack server show <uuid> -f json | jq .created`` taken *before* the
  reap, from a host with cloud credentials. The plan's playbook has it.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

#: A trivial submission on the smallest rung costs about this much wall clock,
#: nearly all of it boot grace and the idle clock rather than analysis
#: (01_autoscaling_plan.md:807). Used only to say "this looks like an instance
#: life" rather than to assert a value.
PLAUSIBLE_HOURS = (0.05, 1.5)


def call(path, method="GET"):
    url = os.environ["GIRDER_API_URL"].rstrip("/") + path
    req = urllib.request.Request(url, method=method)
    req.add_header("Girder-Token", TOKEN)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        sys.exit(f"{method} {path} failed: {exc.code} {exc.read()[:300]!r}")


def authenticate():
    url = os.environ["GIRDER_API_URL"].rstrip("/") + "/api_key/token"
    data = f"key={os.environ['GIRDER_API_KEY']}".encode()
    req = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)["authToken"]["token"]


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" -- {detail}" if detail else ""))
    return bool(ok)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-drain", action="store_true", help="observe only")
    args = ap.parse_args()

    global TOKEN
    for var in ("GIRDER_API_URL", "GIRDER_API_KEY"):
        if not os.environ.get(var):
            sys.exit(f"{var} is not set")
    TOKEN = authenticate()

    rungs = {e["memory_gb"]: e for e in call("/sivacor/worker_sizes")["sizes"]}
    print(f"catalogue: {sorted(rungs)} GB")

    before = call("/sivacor/usage")
    print(f"pending before drain: {before['pending']}")

    if not args.no_drain:
        print(f"drained: {call('/sivacor/usage/drain', method='POST')['accrued']}")

    report = call("/sivacor/usage")
    print(json.dumps(report, indent=2, default=str))

    ok = True
    print("\nchecks:")

    ok &= check(
        "at least one user has accrued usage",
        report["users"],
        "none yet -- has an instance actually been REAPED? that is ~18 min "
        "after the submission finishes, not when it finishes",
    )
    if not report["users"]:
        # Everything below reads a user row; say which half is missing instead
        # of a cascade of failures about it.
        p = report["pending"]
        print(
            f"\n  pending lifetimes={p['lifetimes']} attributions={p['attributions']}\n"
            "  both zero      -> nothing has been reaped yet\n"
            "  attributions>0 -> W1 works, no reap has happened (or 09-A3 is not "
            "in the controller's image)\n"
            "  lifetimes>0    -> W3 works, the drain did not run or found no partner"
        )
        return 1

    for row in report["users"]:
        print(f"\n  user {row['login']}:")
        hours = row["instance_hours"]
        ok &= check("submissions counted", row["submissions"] >= 1, row["submissions"])
        ok &= check("since is set", row["since"], row["since"])
        ok &= check(
            "instance hours look like an instance life",
            PLAUSIBLE_HOURS[0] <= hours <= PLAUSIBLE_HOURS[1],
            f"{hours:.3f} h",
        )
        # The load-bearing one. Nothing else in the pipeline can make this
        # ratio equal a catalogue rate by accident, so it is what says the
        # right rung's rate was applied to the right lifetime.
        sizes = [int(k) for k in row["by_size"]]
        if len(sizes) == 1 and hours:
            expected = rungs.get(sizes[0], {}).get("vcpus")
            actual = row["su_hours"] / hours
            ok &= check(
                f"su_hours/instance_hours equals the {sizes[0]} GB rung's rate",
                expected is not None and abs(actual - expected) < 1e-6,
                f"{actual:.4f} vs {expected} "
                "(vcpus, which equals su_per_hour on m3 -- the endpoint does "
                "not expose the rate itself, by design)",
            )
        else:
            print(f"    (skipped rate check: by_size is {row['by_size']})")

    house = report["house"]
    print(f"\n  house: {house['su_hours']:.3f} SU-h, byReason={house['by_reason']}")
    ok &= check(
        "nothing landed in over_cap",
        not house["by_reason"].get("over_cap"),
        "a cap firing on an ordinary submission is a bug in "
        "BILLABLE_OVERHEAD_HOURS, not a finding about the submission",
    )
    ok &= check(
        "nothing landed in unexpected",
        not house["by_reason"].get("unexpected"),
        "a growing `unexpected` is a bug report",
    )
    if house["by_reason"].get("unstamped"):
        print(
            "    NOTE unstamped burn is present: a run failed and nothing "
            "classified it. Conservative (the house paid, not the user), but "
            "it means 09-A2b did not reach that run."
        )

    print(f"\ntotal accounted: {report['total_su_hours']:.3f} SU-hours")
    print(
        "This will be SMALLER than the ACCESS dashboard, always -- the manager "
        "VM, the other deployment and the SHUTOFF half-rate are all outside it. "
        "The dashboard also lags 12-24 h, so never compare same-day."
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
