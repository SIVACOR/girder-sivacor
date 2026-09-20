"""Per-user resource accounting: the arithmetic, wired to nothing.

09-A1 of ``development_notes/09_user_usage_accounting_plan.md``. Nothing in the
pipeline calls any of this yet -- these are the unit tests for the module that
later phases hang off, and several of them are boundary tests in the same sense
as ``test_execution_records.py``: they assert what must *not* happen to a number
nobody can repair afterwards, because 09-U4 forbids a backfill.

The two that matter most if you are skimming:
``test_every_failure_code_has_a_billability_entry`` is the drift guard, and
``test_the_drain_claims_the_lifetime_before_the_attribution`` is 09-U19, the
ordering that decides whether a crash under-counts or silently charges the wrong
party.
"""

import datetime
from types import SimpleNamespace

import pytest
from girder.models.folder import Folder
from girder.models.setting import Setting
from girder.models.user import User
from girder_jobs.constants import JobStatus
from girder_jobs.models.job import Job
from pymongo.collection import Collection
from girder_sivacor import usage
from girder_sivacor.errors import FailureCode
from girder_sivacor.settings import PluginSettings

LADDER = [
    {"memory_gb": 30, "flavor": "m3.medium", "vcpus": 8, "gated": False},
    {"memory_gb": 60, "flavor": "m3.large", "vcpus": 16, "gated": False},
]


def utc(*args):
    return datetime.datetime(*args, tzinfo=datetime.timezone.utc)


def fake_instance(instance_id="i-1", created=None, size=60, volume_gb=None):
    return SimpleNamespace(
        id=instance_id,
        created_at=created or utc(2026, 9, 20, 12, 0),
        size=size,
        volume_gb=volume_gb,
    )


@pytest.fixture
def db(server):
    """The raw pymongo database, which is what the controller passes in.

    ``record_lifetime`` deliberately takes this rather than a Girder model, so
    the fleet controller can call it with no Girder request context (09-U16).
    """
    handle = User().collection.database
    for name in (usage.ATTRIBUTION_COLLECTION, usage.LIFETIME_COLLECTION):
        handle[name].delete_many({})
    handle[usage.TOTALS_COLLECTION].delete_many({})
    return handle


# --- the billability table -------------------------------------------------


@pytest.mark.plugin("sivacor")
def test_every_failure_code_has_a_billability_entry():
    """The drift guard, and the reason it is written this way.

    Enumerating the enum rather than counting rows is the whole point: a count
    passes on the day a member is added and is wrong from then on, and the
    failure it hides is quiet -- a class of run stops being charged and the
    totals merely look low.
    """
    missing = [code.value for code in FailureCode if code not in usage.USER_PAYS]
    assert missing == []


@pytest.mark.plugin("sivacor")
def test_the_guard_actually_fires_when_a_code_is_missing(monkeypatch):
    """A guard nobody has seen fail is a guard nobody knows works."""
    trimmed = dict(usage.USER_PAYS)
    del trimmed[FailureCode.UNEXPECTED]
    monkeypatch.setattr(usage, "USER_PAYS", trimmed)
    with pytest.raises(RuntimeError) as excinfo:
        usage._assert_table_is_exhaustive()
    assert "unexpected" in str(excinfo.value)


@pytest.mark.plugin("sivacor")
@pytest.mark.parametrize(
    "code",
    [
        FailureCode.STATA_ERROR,
        FailureCode.NONZERO_EXIT,
        FailureCode.OUT_OF_MEMORY,
        FailureCode.OUT_OF_DISK,
        FailureCode.REAPED_MAX_RUNTIME,
        FailureCode.MAIN_FILE_MISSING,
        FailureCode.UNSAFE_ARCHIVE,
        FailureCode.SUBMISSION_DELETED,
    ],
)
def test_the_researcher_pays_for_their_own_package(code):
    assert usage.is_billable(code) is True


@pytest.mark.plugin("sivacor")
@pytest.mark.parametrize(
    "code",
    [
        FailureCode.NO_STATA_LICENSE,
        FailureCode.IMAGE_PULL_FAILED,
        FailureCode.NO_ENTRYPOINT,
        FailureCode.REAPED_NO_HEARTBEAT,
        FailureCode.SIZE_UNAVAILABLE,
        FailureCode.UNEXPECTED,
    ],
)
def test_the_house_pays_for_our_own_defects(code):
    """Constraint 2. A ceiling that counts our bad week against a researcher's
    allowance produces a support ticket nobody can answer."""
    assert usage.is_billable(code) is False


@pytest.mark.plugin("sivacor")
def test_the_three_expensive_outcomes_are_charged():
    """09-U12, spelled out because it is the entry to argue about.

    All three are properties of the submitted code, and they are the most
    expensive things a submission can do. Make them free and a program that
    never terminates is the cheapest route to unmetered compute.
    """
    assert usage.is_billable(FailureCode.REAPED_MAX_RUNTIME) is True
    assert usage.is_billable(FailureCode.OUT_OF_MEMORY) is True
    assert usage.is_billable(FailureCode.OUT_OF_DISK) is True


@pytest.mark.plugin("sivacor")
def test_success_and_cancel_are_billable():
    """No code means the run succeeded or the user stopped it. Either way the
    SU were spent on their behalf -- and charging a cancel keeps it from
    becoming a way to run twenty-five minutes of analysis for free."""
    assert usage.is_billable(None) is True


@pytest.mark.plugin("sivacor")
def test_an_unrecognised_code_is_not_billable():
    """09-U13. Nothing classified the outcome, so nobody can claim it was the
    user's fault."""
    assert usage.is_billable("something_from_a_future_release") is False


@pytest.mark.plugin("sivacor")
def test_a_bare_string_code_is_understood():
    """Mongo hands the code back as a string, not an enum member."""
    assert usage.is_billable("stata_error") is True
    assert usage.is_billable("image_pull_failed") is False


# --- rates and the cap -----------------------------------------------------


@pytest.mark.plugin("sivacor")
def test_the_cap_is_derived_from_max_runtime(server):
    """Derived rather than configured, so no second knob can contradict the
    runtime ceiling."""
    Setting().set(PluginSettings.MAX_RUNTIME, 24.0)
    assert usage.max_billable_hours() == 24.0 + usage.BILLABLE_OVERHEAD_HOURS
    Setting().set(PluginSettings.MAX_RUNTIME, 2.0)
    assert usage.max_billable_hours() == 2.0 + usage.BILLABLE_OVERHEAD_HOURS


@pytest.mark.plugin("sivacor")
def test_an_ordinary_lifetime_never_reaches_the_cap(server):
    """The test that the constant is set right. A cap firing on an ordinary
    submission is a bug in BILLABLE_OVERHEAD_HOURS, not a finding about the
    submission."""
    Setting().set(PluginSettings.MAX_RUNTIME, 24.0)
    billable, over = usage.split_at_cap(18 / 60)  # the measured ~18 minutes
    assert billable == pytest.approx(0.3)
    assert over == 0.0


@pytest.mark.plugin("sivacor")
def test_time_past_the_cap_goes_to_the_house(server):
    """09-U15: a researcher must not pay for an instance that outlived its work
    because our reaper was down."""
    Setting().set(PluginSettings.MAX_RUNTIME, 2.0)
    billable, over = usage.split_at_cap(10.0)
    assert billable == 2.5
    assert over == 7.5


@pytest.mark.plugin("sivacor")
def test_the_rate_comes_from_the_catalogue(server):
    Setting().set(PluginSettings.WORKER_SIZES, LADDER)
    assert usage.su_per_hour_for(30) == 8.0
    assert usage.su_per_hour_for(60) == 16.0


@pytest.mark.plugin("sivacor")
def test_a_declared_rate_beats_vcpus(server):
    """09-U3. On r3 the two differ by a factor of two, and nothing downstream
    would notice the error."""
    Setting().set(
        PluginSettings.WORKER_SIZES,
        [{"memory_gb": 60, "flavor": "r3.large", "vcpus": 16, "gated": False, "su_per_hour": 32}],
    )
    assert usage.su_per_hour_for(60) == 32.0


@pytest.mark.plugin("sivacor")
def test_an_unknown_rung_has_no_rate(server):
    Setting().set(PluginSettings.WORKER_SIZES, LADDER)
    assert usage.su_per_hour_for(250) is None
    assert usage.su_per_hour_for(None) is None


# --- the transient rows ----------------------------------------------------


@pytest.mark.plugin("sivacor")
def test_record_lifetime_writes_what_the_controller_saw(db):
    usage.record_lifetime(
        db, fake_instance(size=30, volume_gb=100), deleted_at=utc(2026, 9, 20, 12, 30)
    )
    row = db[usage.LIFETIME_COLLECTION].find_one({"instanceId": "i-1"})
    assert row["createdAt"] == utc(2026, 9, 20, 12, 0).replace(tzinfo=None)
    assert row["deletedAt"] == utc(2026, 9, 20, 12, 30).replace(tzinfo=None)
    assert row["sizeGb"] == 30
    assert row["volumeGb"] == 100


@pytest.mark.plugin("sivacor")
def test_record_lifetime_never_raises():
    """Best effort on purpose: a fleet that stops reaping because a counter
    failed is not a trade worth making (09-U9).

    A stub rather than a monkeypatched collection, because ``db[name]`` builds a
    *new* ``Collection`` object on every call -- patching one instance patches
    something ``record_lifetime`` will never see, and the test passes without
    having exercised anything.
    """

    class ExplodingDb:
        def __getitem__(self, name):
            raise RuntimeError("mongo is having a day")

    usage.record_lifetime(ExplodingDb(), fake_instance())  # must not raise


@pytest.mark.plugin("sivacor")
def test_a_second_reap_of_one_instance_does_not_overwrite_the_first(db):
    """``$setOnInsert``: the first observation is the true one. A controller
    that re-decides the same reap must not move the timestamps."""
    usage.record_lifetime(db, fake_instance(), deleted_at=utc(2026, 9, 20, 12, 30))
    usage.record_lifetime(db, fake_instance(), deleted_at=utc(2026, 9, 20, 23, 0))
    row = db[usage.LIFETIME_COLLECTION].find_one({"instanceId": "i-1"})
    assert row["deletedAt"] == utc(2026, 9, 20, 12, 30).replace(tzinfo=None)


@pytest.mark.plugin("sivacor")
def test_a_failure_starts_unbillable(db):
    """The fail-safe is the column default, not a code path someone has to
    remember to take (09-U13)."""
    usage.record_attribution(db, "i-1", "u-1", 60, None, billable=False)
    row = db[usage.ATTRIBUTION_COLLECTION].find_one({"instanceId": "i-1"})
    assert row["billable"] is False


@pytest.mark.plugin("sivacor")
def test_stamping_a_user_fault_makes_it_billable(db):
    usage.record_attribution(db, "i-1", "u-1", 60, None, billable=False)
    usage.stamp_outcome(db, "i-1", FailureCode.STATA_ERROR)
    row = db[usage.ATTRIBUTION_COLLECTION].find_one({"instanceId": "i-1"})
    assert row["billable"] is True
    assert row["code"] == "stata_error"


@pytest.mark.plugin("sivacor")
def test_stamping_our_own_fault_leaves_it_unbillable(db):
    usage.record_attribution(db, "i-1", "u-1", 60, None, billable=False)
    usage.stamp_outcome(db, "i-1", FailureCode.IMAGE_PULL_FAILED)
    row = db[usage.ATTRIBUTION_COLLECTION].find_one({"instanceId": "i-1"})
    assert row["billable"] is False
    assert row["code"] == "image_pull_failed"


@pytest.mark.plugin("sivacor")
def test_a_stamp_never_makes_a_charged_run_free(db):
    """Only ever raises. A success is billable from the moment its attribution
    row is written, and no later code should be able to reverse that."""
    usage.record_attribution(db, "i-1", "u-1", 60, None, billable=True)
    usage.stamp_outcome(db, "i-1", FailureCode.UNEXPECTED)
    row = db[usage.ATTRIBUTION_COLLECTION].find_one({"instanceId": "i-1"})
    assert row["billable"] is True


# --- accrual ---------------------------------------------------------------


@pytest.fixture
def priced(server):
    Setting().set(PluginSettings.WORKER_SIZES, LADDER)
    Setting().set(PluginSettings.MAX_RUNTIME, 24.0)


def lifetime_row(instance_id="i-1", hours=1.0, size=60, volume_gb=None):
    start = utc(2026, 9, 20, 12, 0)
    return {
        "instanceId": instance_id,
        "createdAt": start,
        "deletedAt": start + datetime.timedelta(hours=hours),
        "sizeGb": size,
        "volumeGb": volume_gb,
    }


@pytest.mark.plugin("sivacor")
def test_a_billable_run_lands_on_the_user(db, priced, user):
    usage.accrue(
        db,
        lifetime_row(hours=2.0, size=60, volume_gb=50),
        {"instanceId": "i-1", "userId": user["_id"], "memoryGb": 60, "billable": True},
    )
    counters = User().load(user["_id"], force=True)[usage.USER_USAGE_FIELD]
    assert counters["suHours"] == pytest.approx(32.0)  # 2 h x 16 SU/hr
    assert counters["instanceHours"] == pytest.approx(2.0)
    assert counters["volumeGbHours"] == pytest.approx(100.0)  # 2 h x 50 GB
    assert counters["submissions"] == 1
    assert counters["bySize"] == {"60": 1}
    assert counters["since"] is not None


@pytest.mark.plugin("sivacor")
def test_instance_hours_makes_a_wrong_rate_visible(db, priced, user):
    """The redundancy earns its bytes: suHours / instanceHours has to equal the
    rung's rate, so a catalogue that drifts says so instead of quietly
    mis-billing."""
    usage.accrue(
        db,
        lifetime_row(hours=3.0, size=30),
        {"instanceId": "i-1", "userId": user["_id"], "memoryGb": 30, "billable": True},
    )
    counters = User().load(user["_id"], force=True)[usage.USER_USAGE_FIELD]
    assert counters["suHours"] / counters["instanceHours"] == pytest.approx(8.0)


@pytest.mark.plugin("sivacor")
def test_two_submissions_accumulate(db, priced, user):
    for i in (1, 2):
        usage.accrue(
            db,
            lifetime_row(instance_id=f"i-{i}", hours=1.0, size=60),
            {"userId": user["_id"], "memoryGb": 60, "billable": True},
        )
    counters = User().load(user["_id"], force=True)[usage.USER_USAGE_FIELD]
    assert counters["submissions"] == 2
    assert counters["suHours"] == pytest.approx(32.0)
    assert counters["bySize"] == {"60": 2}


@pytest.mark.plugin("sivacor")
def test_since_is_written_once_and_never_moves(db, priced, user):
    """09-U4: an honest `since` is the whole substitute for a backfill. If it
    advanced with each accrual it would claim to cover a window it does not."""
    usage.accrue(
        db,
        lifetime_row(instance_id="i-1"),
        {"userId": user["_id"], "memoryGb": 60, "billable": True},
    )
    first = User().load(user["_id"], force=True)[usage.USER_USAGE_FIELD]["since"]
    usage.accrue(
        db,
        lifetime_row(instance_id="i-2"),
        {"userId": user["_id"], "memoryGb": 60, "billable": True},
    )
    assert User().load(user["_id"], force=True)[usage.USER_USAGE_FIELD]["since"] == first


@pytest.mark.plugin("sivacor")
def test_an_unclaimed_instance_is_house_burn_not_an_error(db, priced):
    """09-U7. Real SU belonging to nobody -- and its size is the interesting
    number, because it is the fleet controller's efficiency in SU."""
    usage.accrue(db, lifetime_row(hours=1.0, size=60), None)
    house = db[usage.TOTALS_COLLECTION].find_one({"_id": usage.TOTALS_ID})
    assert house["suHours"] == pytest.approx(16.0)
    assert house["byReason"]["unclaimed"] == pytest.approx(16.0)


@pytest.mark.plugin("sivacor")
def test_our_defect_is_charged_to_the_house_under_its_own_reason(db, priced, user):
    """A growing `unexpected` is a bug report, which is why byReason exists."""
    usage.accrue(
        db,
        lifetime_row(hours=1.0, size=60),
        {"userId": user["_id"], "memoryGb": 60, "billable": False, "code": "unexpected"},
    )
    house = db[usage.TOTALS_COLLECTION].find_one({"_id": usage.TOTALS_ID})
    assert house["byReason"]["unexpected"] == pytest.approx(16.0)
    assert usage.USER_USAGE_FIELD not in User().load(user["_id"], force=True)


@pytest.mark.plugin("sivacor")
def test_an_unstamped_failure_is_charged_to_the_house(db, priced, user):
    usage.accrue(
        db,
        lifetime_row(hours=1.0, size=60),
        {"userId": user["_id"], "memoryGb": 60, "billable": False},
    )
    house = db[usage.TOTALS_COLLECTION].find_one({"_id": usage.TOTALS_ID})
    assert house["byReason"]["unstamped"] == pytest.approx(16.0)


@pytest.mark.plugin("sivacor")
def test_the_user_pays_up_to_the_cap_and_the_house_pays_the_rest(db, server, user):
    """The outage case, end to end: 09-U15 splitting one lifetime two ways."""
    Setting().set(PluginSettings.WORKER_SIZES, LADDER)
    Setting().set(PluginSettings.MAX_RUNTIME, 2.0)  # cap is 2.5 h
    usage.accrue(
        db,
        lifetime_row(hours=10.0, size=60),
        {"userId": user["_id"], "memoryGb": 60, "billable": True},
    )
    counters = User().load(user["_id"], force=True)[usage.USER_USAGE_FIELD]
    house = db[usage.TOTALS_COLLECTION].find_one({"_id": usage.TOTALS_ID})
    assert counters["instanceHours"] == pytest.approx(2.5)
    assert counters["suHours"] == pytest.approx(40.0)
    assert house["byReason"]["over_cap"] == pytest.approx(7.5 * 16)
    # The over-cap half is not a submission of its own.
    assert house["instances"] == 0


@pytest.mark.plugin("sivacor")
def test_a_rung_that_left_the_catalogue_charges_nothing_rather_than_guessing(
    db, priced, user
):
    """Guessing a rate is how a number nobody can repair gets written."""
    usage.accrue(
        db,
        lifetime_row(hours=1.0, size=999),
        {"userId": user["_id"], "memoryGb": 999, "billable": True},
    )
    counters = User().load(user["_id"], force=True)[usage.USER_USAGE_FIELD]
    assert counters["suHours"] == 0.0
    # The hours are still real and still recorded, so the gap is visible.
    assert counters["instanceHours"] == pytest.approx(1.0)


@pytest.mark.plugin("sivacor")
def test_a_backwards_lifetime_is_skipped_not_credited(db, priced, user):
    """Charging a negative would *credit* the user, which is worse than
    skipping: it is the one error that can make usage go down."""
    row = lifetime_row(hours=1.0)
    row["createdAt"], row["deletedAt"] = row["deletedAt"], row["createdAt"]
    usage.accrue(db, row, {"userId": user["_id"], "memoryGb": 60, "billable": True})
    assert usage.USER_USAGE_FIELD not in User().load(user["_id"], force=True)


# --- the drain -------------------------------------------------------------


@pytest.mark.plugin("sivacor")
def test_the_drain_pairs_a_lifetime_with_its_attribution(db, priced, user):
    usage.record_attribution(db, "i-1", user["_id"], 60, None, billable=True)
    db[usage.LIFETIME_COLLECTION].insert_one(lifetime_row(hours=1.0, size=60))
    assert usage.drain(db) == 1
    counters = User().load(user["_id"], force=True)[usage.USER_USAGE_FIELD]
    assert counters["suHours"] == pytest.approx(16.0)
    # Both rows consumed, so a second drain is a no-op rather than a double charge.
    assert usage.drain(db) == 0
    assert db[usage.LIFETIME_COLLECTION].count_documents({}) == 0
    assert db[usage.ATTRIBUTION_COLLECTION].count_documents({}) == 0


@pytest.mark.plugin("sivacor")
def test_the_drain_claims_the_lifetime_before_the_attribution(db, priced, user, monkeypatch):
    """09-U19, and the reason the order is written down rather than incidental.

    A crash between the two deletes must lose the lifetime -- an under-count,
    which 09-U9 permits -- and not the attribution. Losing the attribution
    instead leaves the lifetime to be re-processed as house burn on the next
    pass: the total stays plausible and the wrong party pays, which is the
    failure nobody notices.
    """
    usage.record_attribution(db, "i-1", user["_id"], 60, None, billable=True)
    db[usage.LIFETIME_COLLECTION].insert_one(lifetime_row(hours=1.0, size=60))

    # Patched on the class, not an instance: ``db[name]`` builds a new
    # ``Collection`` every call, so an instance patch would never be seen.
    real = Collection.find_one_and_delete

    def crash_on_the_second_delete(self, *args, **kwargs):
        if self.name == usage.ATTRIBUTION_COLLECTION:
            raise RuntimeError("killed between the two deletes")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Collection, "find_one_and_delete", crash_on_the_second_delete)
    with pytest.raises(RuntimeError):
        usage.drain(db)
    monkeypatch.undo()

    # The lifetime is gone -- claimed -- so nothing will re-charge it.
    assert db[usage.LIFETIME_COLLECTION].count_documents({}) == 0
    # The attribution survives and expires by TTL. Nobody was charged.
    assert db[usage.ATTRIBUTION_COLLECTION].count_documents({}) == 1
    assert usage.USER_USAGE_FIELD not in User().load(user["_id"], force=True)


@pytest.mark.plugin("sivacor")
def test_one_bad_row_does_not_stop_the_drain(db, priced, user, monkeypatch):
    """A lost accrual is lost, not retried -- but it must not take the rest of
    the batch with it."""
    usage.record_attribution(db, "i-1", user["_id"], 60, None, billable=True)
    usage.record_attribution(db, "i-2", user["_id"], 60, None, billable=True)
    db[usage.LIFETIME_COLLECTION].insert_one(lifetime_row("i-1", hours=1.0))
    db[usage.LIFETIME_COLLECTION].insert_one(lifetime_row("i-2", hours=1.0))

    calls = {"n": 0}
    real_accrue = usage.accrue

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("one bad row")
        return real_accrue(*args, **kwargs)

    monkeypatch.setattr(usage, "accrue", flaky)
    assert usage.drain(db) == 1
    assert db[usage.LIFETIME_COLLECTION].count_documents({}) == 0


@pytest.mark.plugin("sivacor")
def test_draining_nothing_is_not_an_error(db, priced):
    assert usage.drain(db) == 0


# --- storage shape ---------------------------------------------------------


@pytest.mark.plugin("sivacor")
def test_the_counters_survive_a_user_save(db, priced, user):
    """Open item 4: `sivacorUsage` is a plain subdocument on a model Girder
    itself owns, so an unrelated user update must not drop it."""
    usage.accrue(
        db,
        lifetime_row(hours=1.0, size=60),
        {"userId": user["_id"], "memoryGb": 60, "billable": True},
    )
    reloaded = User().load(user["_id"], force=True)
    reloaded["firstName"] = "Renamed"
    User().save(reloaded)
    assert User().load(user["_id"], force=True)[usage.USER_USAGE_FIELD]["submissions"] == 1


@pytest.mark.plugin("sivacor")
def test_the_counters_are_not_exposed_to_other_users(server, db, priced, user, admin):
    """The whole store is per-user spend. Girder exposes user fields by
    allow-list and this is deliberately not on it -- adding it would publish
    every researcher's usage to every other researcher."""
    usage.accrue(
        db,
        lifetime_row(hours=1.0, size=60),
        {"userId": user["_id"], "memoryGb": 60, "billable": True},
    )
    resp = server.request(path=f"/user/{user['_id']}", method="GET", user=admin)
    assert usage.USER_USAGE_FIELD not in resp.json


@pytest.mark.plugin("sivacor")
def test_the_house_bucket_carries_no_identifier(db, priced, user):
    """Permanent *because* it is not personal data -- the same basis as
    sivacor_execution_record. A user or job id here would give it an erasure
    obligation it has no machinery for."""
    usage.accrue(db, lifetime_row(hours=1.0, size=60), None)
    house = db[usage.TOTALS_COLLECTION].find_one({"_id": usage.TOTALS_ID})
    flat = str(house)
    assert str(user["_id"]) not in flat
    assert "i-1" not in flat
    assert set(house) <= {
        "_id",
        "since",
        "lastAt",
        "suHours",
        "instanceHours",
        "instances",
        "byReason",
    }


@pytest.mark.plugin("sivacor")
def test_the_transient_rows_expire(db):
    """Without the TTL index the "transient" rows are permanent, and the
    privacy argument for keeping a user id in the attribution row is not true.
    Expiry has to be a property of the collection, not a sweep somebody
    remembers to run."""
    usage.ensure_indices(db)
    for name in (usage.ATTRIBUTION_COLLECTION, usage.LIFETIME_COLLECTION):
        ttls = [
            idx
            for idx in db[name].list_indexes()
            if idx.get("expireAfterSeconds") is not None
        ]
        assert len(ttls) == 1
        assert ttls[0]["expireAfterSeconds"] == usage.TRANSIENT_TTL_SECONDS


# --- W1: the attribution hook (09-A2) --------------------------------------
#
# These drive the real Girder event, not `record_attribution` directly: the
# whole claim of 09-A2 is that *one* hook covers every terminal path, and only
# an actual job transition tests that.


def submission_job(user, queue="sivacor.i-abc", memory_gb=60, disk_gb=None):
    """A sivacor_submission job shaped the way an assigned one really is."""
    job = Job().createJob(
        title="usage-test", type="sivacor_submission", user=user, public=False
    )
    Job().collection.update_one(
        {"_id": job["_id"]},
        {
            "$set": {
                "meta.worker_queue": queue,
                "meta.requested_memory_gb": memory_gb,
                "meta.requested_disk_gb": disk_gb,
            }
        },
    )
    # Girder enforces the job state machine, so a terminal status can only be
    # reached from RUNNING -- which is also what a real submission does.
    job = Job().load(job["_id"], force=True)
    return Job().updateJob(job, status=JobStatus.RUNNING)


def attribution(db, instance_id="i-abc"):
    return db[usage.ATTRIBUTION_COLLECTION].find_one({"instanceId": instance_id})


@pytest.mark.plugin("sivacor")
def test_a_finished_submission_is_attributed_to_its_submitter(db, user, submission_collection):
    job = submission_job(user, disk_gb=100)
    Job().updateJob(job, status=JobStatus.SUCCESS)
    row = attribution(db)
    assert row["userId"] == user["_id"]
    assert row["memoryGb"] == 60
    assert row["volumeGb"] == 100
    assert row["billable"] is True


@pytest.mark.plugin("sivacor")
def test_a_failure_is_attributed_but_not_yet_billable(db, user, submission_collection):
    """The fail-safe: the row exists so the user *can* be charged, and is not
    charged until something classifies the outcome (09-U13)."""
    job = submission_job(user)
    Job().updateJob(job, status=JobStatus.ERROR)
    row = attribution(db)
    assert row["userId"] == user["_id"]
    assert row["billable"] is False


@pytest.mark.plugin("sivacor")
def test_a_cancel_is_billable_from_the_status_alone(db, user, submission_collection):
    """No failure code is involved and nothing has to be reported by the
    worker: the SU were spent on the researcher's behalf before they stopped
    the run, and making a cancel free makes it a way to get free compute."""
    job = submission_job(user)
    Job().updateJob(job, status=JobStatus.CANCELED)
    assert attribution(db)["billable"] is True


@pytest.mark.plugin("sivacor")
def test_a_running_submission_is_not_attributed_yet(db, user, submission_collection):
    """The hook fires on every job update, including every log line. Only a
    terminal status means the instance is finished with."""
    job = submission_job(user)
    Job().updateJob(job, log="still going\n")
    assert attribution(db) is None


@pytest.mark.plugin("sivacor")
def test_a_submission_that_never_got_an_instance_is_not_attributed(
    db, user, submission_collection
):
    """No private queue means nothing booted -- refused at submit_job, reaped as
    REAPED_NO_WORKER, or a deployment on the shared-queue path. No SU to
    attribute and no row to write."""
    job = submission_job(user, queue=None)
    Job().updateJob(job, status=JobStatus.SUCCESS)
    assert db[usage.ATTRIBUTION_COLLECTION].count_documents({}) == 0


@pytest.mark.plugin("sivacor")
def test_a_later_job_update_cannot_un_charge_a_stamped_failure(
    db, user, submission_collection
):
    """The reason `billable` is written with $max and not $set.

    The hook fires on *every* job update. A failed submission whose outcome has
    already been stamped as the researcher's fault gets re-attributed the next
    time anything touches the job -- and with $set that second write would
    silently make a charged run free. Rather than trace every call path that
    might log after a terminal status, the invariant is structural.
    """
    job = submission_job(user)
    Job().updateJob(job, status=JobStatus.ERROR)
    usage.stamp_outcome(db, "i-abc", FailureCode.STATA_ERROR)
    assert attribution(db)["billable"] is True

    Job().updateJob(job, log="a line that arrives after the failure\n")
    assert attribution(db)["billable"] is True


@pytest.mark.plugin("sivacor")
def test_attribution_failure_never_breaks_the_folder_status(
    db, user, submission_collection, monkeypatch
):
    """The folder write is the 2026-09-04 delete guard. An accounting failure
    must not be able to leave a submission transitional and undeletable --
    counters are the least important thing that handler does, and they run
    first, so a raise there would otherwise take the folder update with it."""
    from girder_sivacor import notifications

    def explode(*args, **kwargs):
        raise RuntimeError("no room at the inn")

    monkeypatch.setattr(notifications.usage, "record_attribution", explode)
    job = submission_job(user)
    folder = Folder().createFolder(
        submission_collection, f"sub-{job['_id']}", parentType="collection"
    )
    Folder().setMetadata(folder, {"job_id": str(job["_id"]), "creator_id": user["_id"]})

    Job().updateJob(job, status=JobStatus.ERROR)

    assert Folder().load(folder["_id"], force=True)["meta"]["status"] == "failed"


@pytest.mark.plugin("sivacor")
def test_the_indexes_exist_after_a_plugin_load(db):
    """Wired at load, not left to whoever writes the first row. Without the TTL
    index the transient rows are permanent and the privacy argument for holding
    a user id in one of them is not true."""
    for name in (usage.ATTRIBUTION_COLLECTION, usage.LIFETIME_COLLECTION):
        ttls = [
            idx
            for idx in db[name].list_indexes()
            if idx.get("expireAfterSeconds") is not None
        ]
        assert len(ttls) == 1, f"{name} has no TTL index after plugin load"
