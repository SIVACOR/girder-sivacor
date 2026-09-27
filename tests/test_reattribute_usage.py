"""``tools/reattribute_usage.py``: moving a bill from the house to a researcher.

``usage.accrue`` decides who pays from a failure code and then deletes the rows
it decided from, so a wrong stamp is a permanent wrong bill. This is the way
back, and these tests exist because it writes to the one store that is supposed
to outlive every submission -- the counters a quota or an invoice would read.

The property that matters is in
:func:`test_the_credit_equals_the_debit_to_the_last_bit`: a reattribution that
does not balance is worse than none, because the deployment's total silently
changes and nothing compares it against anything.
"""

import datetime
import importlib.util
from pathlib import Path

import pytest
from girder.models.user import User
from girder_sivacor import usage
from girder_sivacor.models.usage import UsageTotals

TOOL = Path(__file__).resolve().parent.parent / "tools" / "reattribute_usage.py"

UTC = datetime.timezone.utc

#: Read back through Girder's tz_aware client, so the expectations are aware
#: too. The stored instant is the same either way -- BSON has no timezone.
AT = datetime.datetime(2026, 9, 25, 1, 7, 35, 683000, tzinfo=UTC)


def _load_tool():
    """Load the operator script by path; ``tools/`` is not an importable package."""
    spec = importlib.util.spec_from_file_location("reattribute_usage", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def tool():
    return _load_tool()


@pytest.fixture
def db(server):
    UsageTotals().collection.delete_many({})
    return User().collection.database


def _house(su_hours=5246.08, instance_hours=81.97, instances=1, reasons=None):
    UsageTotals().collection.replace_one(
        {"_id": "house"},
        {
            "_id": "house",
            "since": AT,
            "lastAt": AT,
            "instances": instances,
            "suHours": su_hours,
            "instanceHours": instance_hours,
            "byReason": reasons or {"reaped_no_heartbeat": su_hours},
        },
        upsert=True,
    )


def _counters(user):
    return (User().load(user["_id"], force=True) or {}).get(usage.USER_USAGE_FIELD) or {}


@pytest.mark.plugin("sivacor")
def test_the_credit_equals_the_debit_to_the_last_bit(tool, db, user):
    """The deployment's total must not move -- only who is carrying it.

    The su-hours come from the house's own ``byReason`` entry rather than from
    hours times a rate looked up today, which is what makes this exact: a
    catalogue edited since the accrual would otherwise substitute a rate that
    was never charged, and the two sides would part company by a few SU nobody
    would ever notice.
    """
    _house()
    before = usage.report()["total_su_hours"]

    move = tool.plan(db, user["login"], "reaped_no_heartbeat")
    tool.apply_plan(db, move, memory_gb=250)

    after = usage.report()
    assert after["total_su_hours"] == before
    assert after["users"][0]["login"] == user["login"]
    assert after["users"][0]["su_hours"] == 5246.08
    assert after["house"]["su_hours"] == 0.0


@pytest.mark.plugin("sivacor")
def test_a_drained_house_row_is_deleted_rather_than_left_at_zero(tool, db, user):
    """A reason at 0.0 is not a reason with no spend; it is one that no longer
    applies, and it would sit in the operator's breakdown meaning nothing."""
    _house()
    tool.apply_plan(db, tool.plan(db, user["login"], "reaped_no_heartbeat"))

    assert UsageTotals().collection.find_one({"_id": "house"}) is None
    report = usage.report()
    assert report["house"]["by_reason"] == {}
    assert report["house"]["since"] is None


@pytest.mark.plugin("sivacor")
def test_other_house_reasons_are_left_alone(tool, db, user):
    """Only the named charge moves. The rest of the house is somebody else's
    problem and must still be there afterwards."""
    _house(
        su_hours=100.0,
        instance_hours=10.0,
        instances=2,
        reasons={"reaped_no_heartbeat": 60.0, "unclaimed": 40.0},
    )
    move = tool.plan(db, user["login"], "reaped_no_heartbeat",
                     instance_hours=6.0, instances=1)
    tool.apply_plan(db, move)

    house = UsageTotals().collection.find_one({"_id": "house"})
    assert house["byReason"] == {"unclaimed": 40.0}
    assert house["suHours"] == pytest.approx(40.0)
    assert house["instanceHours"] == pytest.approx(4.0)
    assert house["instances"] == 1


@pytest.mark.plugin("sivacor")
def test_a_multi_reason_house_refuses_to_guess_the_hours(tool, db, user):
    """``instanceHours`` is not broken down by reason, so with more than one
    reason present there is nothing to default from -- and a wrong split here
    would move hours that belong to a different failure."""
    _house(reasons={"reaped_no_heartbeat": 60.0, "unclaimed": 40.0})
    with pytest.raises(SystemExit) as excinfo:
        tool.plan(db, user["login"], "reaped_no_heartbeat")
    assert "--instance-hours is required" in str(excinfo.value)


@pytest.mark.plugin("sivacor")
def test_a_charge_the_house_does_not_hold_is_refused(tool, db, user):
    _house()
    with pytest.raises(SystemExit) as excinfo:
        tool.plan(db, user["login"], "out_of_memory")
    assert "reaped_no_heartbeat" in str(excinfo.value)


@pytest.mark.plugin("sivacor")
def test_more_hours_than_the_house_holds_is_refused(tool, db, user):
    """Otherwise the house goes negative and the deployment's total grows."""
    _house()
    with pytest.raises(SystemExit) as excinfo:
        tool.plan(db, user["login"], "reaped_no_heartbeat", instance_hours=999.0)
    assert "fewer than" in str(excinfo.value)


@pytest.mark.plugin("sivacor")
def test_volume_gb_hours_are_derived_the_way_the_accrual_would_have(tool, db, user):
    """The one figure neither side records: the house never computes it, because
    ``accrue`` only does so on the path that charges a user."""
    _house(instance_hours=10.0)
    tool.apply_plan(db, tool.plan(db, user["login"], "reaped_no_heartbeat", volume_gb=470))
    assert _counters(user)["volumeGbHours"] == pytest.approx(4700.0)


@pytest.mark.plugin("sivacor")
def test_the_worker_rung_is_tallied_like_a_real_accrual(tool, db, user):
    _house()
    tool.apply_plan(db, tool.plan(db, user["login"], "reaped_no_heartbeat"), memory_gb=250)
    assert _counters(user)["bySize"] == {"250": 1}


@pytest.mark.plugin("sivacor")
def test_a_correction_lands_in_the_past_without_moving_an_earlier_since(tool, db, user):
    """09-U4's ``since`` is the earliest accrual and must never move forward.

    An account whose first real run came *after* the corrected one would
    otherwise claim its accounting began later than the spend it now carries --
    and ``since`` is the only thing a reader has to judge a small total by.
    """
    earlier = datetime.datetime(2026, 9, 1, 0, 0, tzinfo=UTC)
    later = datetime.datetime(2026, 9, 30, 0, 0, tzinfo=UTC)
    User().collection.update_one(
        {"_id": user["_id"]},
        {"$set": {usage.USER_USAGE_FIELD: {
            "since": earlier, "lastAt": later, "submissions": 1,
            "suHours": 1.0, "instanceHours": 1.0, "volumeGbHours": 0.0,
        }}},
    )
    _house()
    tool.apply_plan(db, tool.plan(db, user["login"], "reaped_no_heartbeat"))

    counters = _counters(user)
    assert counters["since"] == earlier, "an earlier since must not be pushed forward"
    assert counters["lastAt"] == later, "a later run must not be pushed backward"
    assert counters["suHours"] == pytest.approx(5247.08)


@pytest.mark.plugin("sivacor")
def test_a_correction_sets_both_stamps_on_an_account_that_had_none(tool, db, user):
    _house()
    tool.apply_plan(db, tool.plan(db, user["login"], "reaped_no_heartbeat"))
    counters = _counters(user)
    assert counters["since"] == AT
    assert counters["lastAt"] == AT
