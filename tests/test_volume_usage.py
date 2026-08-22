"""Per-user scratch-volume accounting: C5.2 of cinder_volumes_plan.md.

The gate on approving a *second* account, because the first question a second
approved user creates is "who is spending the storage grant". These tests pin the
two properties that make the answer usable rather than merely present:

  - It is **derived**, not stored. Nothing here writes a ledger, so the figures
    reach back exactly as far as the submissions do (``sivacor.retention_days``)
    and no further. That is deliberate: a permanent per-user record of storage
    use would be a new store of personal data, and the one store that outlives a
    submission (``sivacor_execution_record``) is lawful precisely because it
    carries no identifier. This inherits retention instead of arguing that open.
  - An **approved account that has spent nothing still appears**. An unspent
    allowance stands against the shared Cinder quota, so leaving it out would
    make the grant look smaller than it is -- which is the exact mistake that
    would let a second grant be approved over the top of a first.
"""

import datetime

import pytest
from girder.models.setting import Setting
from girder.models.user import User
from girder_jobs.constants import JobStatus
from girder_jobs.models.job import Job
from girder_sivacor.rest import USER_VOLUME_QUOTA_FIELD, volume_usage
from girder_sivacor.settings import PluginSettings
from pytest_girder.assertions import assertStatus, assertStatusOk


def _approve(user, max_gb):
    User().update({"_id": user["_id"]}, {"$set": {USER_VOLUME_QUOTA_FIELD: max_gb}})
    return User().load(user["_id"], force=True)


def _submission(user, disk_gb, hours=None, status=JobStatus.SUCCESS, started_ago=None):
    """A finished (or in-flight) submission with a volume, as Mongo holds it.

    Written straight to the collection rather than driven through ``submit_job``:
    the arithmetic under test is over the *stored* shape -- naive UTC timestamps
    and a status timeline -- and a real submission cannot be made to have
    finished two hours ago.
    """
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    start = now - datetime.timedelta(hours=started_ago if started_ago is not None else (hours or 0))
    job = Job().createJob(
        title="accounting fixture",
        type="sivacor_submission",
        public=False,
        user=user,
    )
    timestamps = [{"status": JobStatus.RUNNING, "time": start}]
    if hours is not None and status in (JobStatus.SUCCESS, JobStatus.ERROR, JobStatus.CANCELED):
        timestamps.append({"status": status, "time": start + datetime.timedelta(hours=hours)})
    Job().collection.update_one(
        {"_id": job["_id"]},
        {
            "$set": {
                "status": status,
                "created": start,
                "updated": start,
                "timestamps": timestamps,
                "meta": {} if disk_gb is None else {"requested_disk_gb": disk_gb},
            }
        },
    )
    return Job().load(job["_id"], force=True)


@pytest.mark.plugin("sivacor")
def test_an_untouched_deployment_accounts_for_nothing(server):
    usage = volume_usage()
    assert usage["users"] == []
    assert usage["totals"] == {
        "approved_users": 0,
        "granted_gb": 0,
        "submissions": 0,
        "gb_hours": 0,
        "live_gb": 0,
    }
    # The window the figures cover, so a small total is not read as a quiet month
    # when it is really a short retention window.
    assert usage["retention_days"] == Setting().get(PluginSettings.RETENTION_DAYS)


@pytest.mark.plugin("sivacor")
def test_an_approved_account_that_has_spent_nothing_is_still_listed(server, user):
    _approve(user, 100)
    usage = volume_usage()
    assert [row["login"] for row in usage["users"]] == [user["login"]]
    row = usage["users"][0]
    assert row["ceiling_gb"] == 100
    assert (row["submissions"], row["gb_hours"], row["live_gb"]) == (0, 0.0, 0)
    # The grant is 100 GB whether or not it has been spent -- that is the number
    # a second grant has to be weighed against.
    assert usage["totals"]["granted_gb"] == 100
    assert usage["totals"]["approved_users"] == 1


@pytest.mark.plugin("sivacor")
def test_gb_hours_is_size_times_duration(server, user):
    _approve(user, 100)
    _submission(user, 20, hours=2)
    row = volume_usage()["users"][0]
    assert row["gb_hours"] == 40.0
    assert row["submissions"] == 1
    assert row["largest_gb"] == 20
    assert row["live_gb"] == 0, "a finished submission holds nothing"
    assert row["last_at"].endswith("Z")


@pytest.mark.plugin("sivacor")
def test_a_submission_that_asked_for_no_volume_is_invisible_here(server, user):
    """The ~90% case, and it must not appear at all.

    Not as a zero row either: this endpoint is about volume spend, and counting
    every ordinary submission would bury the handful that matter.
    """
    _submission(user, None, hours=5)
    assert volume_usage()["users"] == []


@pytest.mark.plugin("sivacor")
def test_an_in_flight_submission_holds_gigabytes_now(server, user):
    """live_gb is the figure that says whether the next request can be honoured."""
    _approve(user, 100)
    _submission(user, 30, status=JobStatus.RUNNING, started_ago=1)
    row = volume_usage()["users"][0]
    assert row["live_gb"] == 30
    # Billed up to now rather than not at all, because the volume exists now.
    assert row["gb_hours"] >= 29.0
    assert volume_usage()["totals"]["live_gb"] == 30


@pytest.mark.plugin("sivacor")
def test_the_biggest_spender_comes_first(server, user, admin):
    _approve(user, 100)
    _approve(admin, 100)
    _submission(user, 10, hours=1)
    _submission(admin, 50, hours=4)
    logins = [row["login"] for row in volume_usage()["users"]]
    assert logins == [admin["login"], user["login"]]


@pytest.mark.plugin("sivacor")
def test_a_nonsense_stored_size_is_skipped_rather_than_summed(server, user):
    """Fails closed the same way user_volume_quota does.

    A value someone wrote straight into Mongo must not become a NaN or a
    TypeError inside an operator's budget answer.
    """
    for bad in (0, -10, True, 1.5, "20", [20], {}):
        _submission(user, bad, hours=1)
    assert volume_usage()["users"] == []


@pytest.mark.plugin("sivacor")
def test_the_endpoint_is_admin_only(server, user, admin):
    """User-attributed by design, unlike the anonymous execution record."""
    resp = server.request(path="/sivacor/volume_usage", method="GET", user=user)
    assertStatus(resp, 403)
    resp = server.request(path="/sivacor/volume_usage", method="GET", user=admin)
    assertStatusOk(resp)
    assert set(resp.json) == {
        "enabled",
        "deployment_gb",
        "granularity_gb",
        "retention_days",
        "users",
        "totals",
    }


@pytest.mark.plugin("sivacor")
def test_the_answer_carries_no_submission_internals(server, user, admin):
    """Only what a budget question needs.

    The job documents this reads from carry ``kwargs`` (the encrypted secret
    envelope) and ``sivacorChain``. The projection excludes them; this is the
    assertion that keeps it that way.
    """
    _approve(user, 100)
    _submission(user, 20, hours=1)
    resp = server.request(path="/sivacor/volume_usage", method="GET", user=admin)
    assertStatusOk(resp)
    assert set(resp.json["users"][0]) == {
        "user_id",
        "login",
        "ceiling_gb",
        "submissions",
        "gb_hours",
        "largest_gb",
        "live_gb",
        "last_at",
    }
