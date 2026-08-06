"""The read side of the execution records: listing, filtering, summarising.

These back the admin browse view. The aggregation is the part worth testing --
it is easy to write one that double-counts multi-stage runs, and the resulting
numbers look plausible enough that nobody notices.
"""

import json

import pytest
from girder_sivacor.models.execution_record import ExecutionRecord
from pytest_girder.assertions import assertStatus, assertStatusOk


def seed(server, admin, **payload):
    resp = server.request(
        path="/sivacor/execution_record",
        method="POST",
        user=admin,
        type="application/json",
        body=json.dumps(payload),
    )
    assertStatusOk(resp)
    return resp.json


def stage(image="rocker/r-ver", tag="4.6.1", **extra):
    return {"image_name": image, "image_tag": tag, **extra}


def backdate(date):
    """Records are stamped with today; the tests need a spread of dates."""
    ExecutionRecord().collection.update_one(
        {"date": {"$exists": True}, "_backdated": {"$exists": False}},
        {"$set": {"date": date, "_backdated": True}},
    )


def browse(server, admin, path="/sivacor/execution_record", **params):
    resp = server.request(path=path, method="GET", user=admin, params=params)
    assertStatusOk(resp)
    return resp.json


@pytest.mark.plugin("sivacor")
def test_list_requires_admin(server, db, user):
    assertStatus(
        server.request(path="/sivacor/execution_record", method="GET", user=user), 403
    )
    assertStatus(
        server.request(
            path="/sivacor/execution_record/summary", method="GET", user=user
        ),
        403,
    )


@pytest.mark.plugin("sivacor")
def test_list_returns_records_with_a_total(server, db, admin):
    for _ in range(3):
        seed(server, admin, status="completed", stages=[stage()])

    listing = browse(server, admin, limit=2)
    assert listing["count"] == 3
    assert len(listing["records"]) == 2


@pytest.mark.plugin("sivacor")
def test_count_reflects_the_filter_not_the_page(server, db, admin):
    """A page of results says nothing about how much matched -- the count has
    to be of the filtered set, or the view's paging lies."""
    for _ in range(4):
        seed(server, admin, status="completed", stages=[stage()])
    seed(
        server,
        admin,
        status="failed",
        stages=[stage()],
        error={"step": "execute_workflow", "code": "out_of_disk"},
    )

    assert browse(server, admin, limit=1)["count"] == 5
    assert browse(server, admin, status="failed", limit=1)["count"] == 1


@pytest.mark.plugin("sivacor")
def test_filters(server, db, admin):
    seed(
        server,
        admin,
        status="failed",
        stages=[stage(image="dataeditors/stata18_5-mp", tag="2024")],
        error={"step": "execute_workflow", "code": "stata_error", "detail": "r(601)"},
    )
    seed(server, admin, status="completed", stages=[stage()])

    assert browse(server, admin, errorCode="stata_error")["count"] == 1
    assert browse(server, admin, errorCode="out_of_disk")["count"] == 0
    assert browse(server, admin, imageName="rocker/r-ver")["count"] == 1
    assert browse(server, admin, imageName="dataeditors/stata18_5-mp")["count"] == 1


@pytest.mark.plugin("sivacor")
def test_date_range_filter(server, db, admin):
    seed(server, admin, status="completed", stages=[stage()])
    backdate("2020-01-01")
    seed(server, admin, status="completed", stages=[stage()])

    assert browse(server, admin, until="2020-06-01")["count"] == 1
    assert browse(server, admin, since="2020-06-01")["count"] == 1
    assert browse(server, admin, since="2019-01-01", until="2020-06-01")["count"] == 1


SUMMARY = "/sivacor/execution_record/summary"


def as_map(rows):
    return {row["_id"]: row["count"] for row in rows}


@pytest.mark.plugin("sivacor")
def test_summary_of_an_empty_collection_does_not_explode(server, db, admin):
    summary = browse(server, admin, path=SUMMARY)
    assert summary["total"] == 0
    assert summary["byStatus"] == []
    assert summary["duration"]["avg"] is None


@pytest.mark.plugin("sivacor")
def test_summary_counts_by_status_and_error(server, db, admin):
    seed(server, admin, status="completed", stages=[stage()])
    seed(server, admin, status="completed", stages=[stage()])
    seed(
        server,
        admin,
        status="failed",
        stages=[stage()],
        error={"step": "execute_workflow", "code": "out_of_disk"},
    )
    seed(
        server,
        admin,
        status="reaped",
        stages=[stage()],
        error={"step": "reaper", "code": "reaped_max_runtime"},
    )

    summary = browse(server, admin, path=SUMMARY)
    assert summary["total"] == 4
    assert as_map(summary["byStatus"]) == {"completed": 2, "failed": 1, "reaped": 1}
    assert as_map(summary["byErrorCode"]) == {
        "out_of_disk": 1,
        "reaped_max_runtime": 1,
    }


@pytest.mark.plugin("sivacor")
def test_summary_counts_every_stage_of_a_multi_stage_run(server, db, admin):
    """The trap: grouping on stages without unwinding counts a two-stage run
    once, against whichever image happens to be first."""
    seed(
        server,
        admin,
        status="completed",
        stages=[stage(), stage(image="dataeditors/stata18_5-mp", tag="2024")],
    )

    by_image = as_map(browse(server, admin, path=SUMMARY)["byImage"])
    assert by_image == {"rocker/r-ver:4.6.1": 1, "dataeditors/stata18_5-mp:2024": 1}


@pytest.mark.plugin("sivacor")
def test_summary_failed_column_counts_reaped_as_failed(server, db, admin):
    """'failed' in the per-image breakdown means 'did not complete' -- a reaped
    run is a failure of that image's run as much as an errored one."""
    seed(server, admin, status="completed", stages=[stage()])
    seed(server, admin, status="reaped", stages=[stage()])

    row = browse(server, admin, path=SUMMARY)["byImage"][0]
    assert row["count"] == 2
    assert row["failed"] == 1


@pytest.mark.plugin("sivacor")
def test_summary_by_date_is_chronological(server, db, admin):
    seed(server, admin, status="completed", stages=[stage()])
    backdate("2020-01-01")
    seed(server, admin, status="failed", stages=[stage()])

    by_date = browse(server, admin, path=SUMMARY)["byDate"]
    assert [row["_id"] for row in by_date] == sorted(row["_id"] for row in by_date)
    assert by_date[0]["_id"] == "2020-01-01"


@pytest.mark.plugin("sivacor")
def test_summary_honours_the_same_filters_as_the_listing(server, db, admin):
    seed(server, admin, status="completed", stages=[stage()])
    seed(
        server,
        admin,
        status="failed",
        stages=[stage()],
        error={"step": "execute_workflow", "code": "out_of_disk"},
    )

    assert browse(server, admin, path=SUMMARY, status="failed")["total"] == 1
    assert as_map(browse(server, admin, path=SUMMARY, status="failed")["byStatus"]) == {
        "failed": 1
    }


@pytest.mark.plugin("sivacor")
def test_summary_durations(server, db, admin):
    seed(server, admin, status="completed", stages=[stage()], total_duration_seconds=10.0)
    seed(server, admin, status="completed", stages=[stage()], total_duration_seconds=30.0)

    duration = browse(server, admin, path=SUMMARY)["duration"]
    assert duration["avg"] == 20.0
    assert duration["max"] == 30.0
