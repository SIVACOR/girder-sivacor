"""The worker-size catalogue, and a submission recording the size it asked for.

Nothing here changes how a submission runs -- the catalogue holds one entry, so
every submission resolves to the same size it would have got anyway. What is
under test is that the size is *recorded*, at every hop it has to survive, and
that a value the catalogue does not contain cannot get past the server.

That last part is not hygiene. An unknown size becomes an unknown flavour name
at the controller, whose create_instance raises, and three of those trip the
circuit breaker and stop the entire fleet. One user's typo must not be able to
do that.
"""

import pytest
from girder.exceptions import ValidationException
from girder.models.setting import Setting
from girder_jobs.models.job import Job
from girder_sivacor.rest import (
    default_worker_size,
    resolve_worker_size,
    stage_schema,
    worker_sizes,
)
from girder_sivacor.settings import PluginSettings
from girder_sivacor.telemetry import sanitize_record
from pytest_girder.assertions import assertStatus, assertStatusOk

from .conftest import submit_sivacor_job, upload_test_file

DATE = "2026-08-19"

#: A catalogue with more than one rung, for the tests that need a choice. The
#: figures are the real Jetstream2 ladder, verified against `openstack flavor
#: list` on 2026-08-13.
LADDER = [
    {"memory_gb": 30, "flavor": "m3.medium", "vcpus": 8, "gated": False},
    {"memory_gb": 60, "flavor": "m3.large", "vcpus": 16, "gated": False},
    {"memory_gb": 250, "flavor": "m3.2xl", "vcpus": 64, "gated": True},
]

STAGES = [
    {
        "image_name": "dataeditors/stata18_5-mp",
        "image_tag": "2025-02-26",
        "main_file": "main.do",
    }
]


# --- the catalogue setting -------------------------------------------------


@pytest.mark.plugin("sivacor")
def test_default_catalogue_matches_production(server):
    """One entry, and it is the flavour production runs today.

    P1 records the size without letting anyone choose one. If this ever ships
    with two ungated rungs before the controller can boot a heterogeneous
    fleet, a submission can ask for a shape that will never be created.
    """
    assert worker_sizes() == [
        {"memory_gb": 60, "flavor": "m3.large", "vcpus": 16, "gated": False}
    ]
    assert default_worker_size() == 60


@pytest.mark.plugin("sivacor")
def test_catalogue_is_sorted_by_size(server):
    """"Smallest" has to be well defined however the setting was written."""
    Setting().set(PluginSettings.WORKER_SIZES, list(reversed(LADDER)))
    assert [entry["memory_gb"] for entry in worker_sizes()] == [30, 60, 250]


@pytest.mark.plugin("sivacor")
def test_default_is_the_smallest_ungated_entry(server):
    """Derived, not configured -- so no field can contradict the catalogue.

    The smallest entry being gated is the case that distinguishes this from
    "the smallest entry".
    """
    Setting().set(
        PluginSettings.WORKER_SIZES,
        [
            {"memory_gb": 30, "flavor": "m3.medium", "vcpus": 8, "gated": True},
            {"memory_gb": 60, "flavor": "m3.large", "vcpus": 16, "gated": False},
        ],
    )
    assert default_worker_size() == 60


@pytest.mark.plugin("sivacor")
@pytest.mark.parametrize(
    "value",
    [
        "m3.large",
        [],
        {},
        [{"memory_gb": 60, "flavor": "m3.large", "vcpus": 16}],  # no gated
        [{"memory_gb": 60, "flavor": "m3.large", "gated": False}],  # no vcpus
        [{"memory_gb": 60, "vcpus": 16, "gated": False}],  # no flavor
        [{"flavor": "m3.large", "vcpus": 16, "gated": False}],  # no memory_gb
        [{"memory_gb": 0, "flavor": "m3.large", "vcpus": 16, "gated": False}],
        [{"memory_gb": -60, "flavor": "m3.large", "vcpus": 16, "gated": False}],
        [{"memory_gb": 60.5, "flavor": "m3.large", "vcpus": 16, "gated": False}],
        # True is an int in python and would format as 1.
        [{"memory_gb": True, "flavor": "m3.large", "vcpus": 16, "gated": False}],
        [{"memory_gb": 60, "flavor": "", "vcpus": 16, "gated": False}],
        [{"memory_gb": 60, "flavor": "m3.large", "vcpus": 0, "gated": False}],
        [{"memory_gb": 60, "flavor": "m3.large", "vcpus": 16, "gated": "no"}],
        # Two rungs claiming the same wire value: the enum would be ambiguous
        # and the flavour actually booted would depend on ordering.
        [
            {"memory_gb": 60, "flavor": "m3.large", "vcpus": 16, "gated": False},
            {"memory_gb": 60, "flavor": "m3.xl", "vcpus": 32, "gated": False},
        ],
        # Everything gated locks every non-member out of submitting at all, and
        # the failure would look like a broken picker rather than a bad setting.
        [{"memory_gb": 60, "flavor": "m3.large", "vcpus": 16, "gated": True}],
    ],
)
def test_validator_rejects_a_bad_catalogue(server, value):
    with pytest.raises(ValidationException):
        Setting().set(PluginSettings.WORKER_SIZES, value)


@pytest.mark.plugin("sivacor")
def test_an_empty_catalogue_names_the_setting(server):
    """The validator refuses one, so reaching here means the setting was written
    before the validator existed. That must not surface as an IndexError from
    inside the submission path."""
    Setting().collection.update_one(
        {"key": PluginSettings.WORKER_SIZES},
        {"$set": {"key": PluginSettings.WORKER_SIZES, "value": []}},
        upsert=True,
    )
    with pytest.raises(ValidationException) as excinfo:
        default_worker_size()
    assert PluginSettings.WORKER_SIZES in str(excinfo.value)


@pytest.mark.plugin("sivacor")
def test_validator_accepts_the_real_ladder(server):
    Setting().set(PluginSettings.WORKER_SIZES, LADDER)
    assert len(worker_sizes()) == 3


# --- the endpoint ---------------------------------------------------------


@pytest.mark.plugin("sivacor")
def test_worker_sizes_endpoint_is_public(server):
    """No auth: the form is rendered before a login in some flows, as with
    image_tags and workflow_schema."""
    Setting().set(PluginSettings.WORKER_SIZES, LADDER)
    resp = server.request(path="/sivacor/worker_sizes", method="GET")
    assertStatusOk(resp)
    assert resp.json["default"] == 30
    assert [entry["memory_gb"] for entry in resp.json["sizes"]] == [30, 60, 250]
    assert resp.json["sizes"][0]["vcpus"] == 8
    assert resp.json["sizes"][2]["gated"] is True


@pytest.mark.plugin("sivacor")
def test_endpoint_never_exposes_the_flavour_name(server):
    """The cloud's name for a machine shape stays server-side.

    S1/D7: nothing provider-specific may become load-bearing where a
    researcher or an exported workflow can see it. A flavour name leaking into
    the picker is how it would end up in every exported YAML.
    """
    Setting().set(PluginSettings.WORKER_SIZES, LADDER)
    resp = server.request(path="/sivacor/worker_sizes", method="GET")
    assertStatusOk(resp)
    for entry in resp.json["sizes"]:
        assert "flavor" not in entry
        assert set(entry) == {"memory_gb", "vcpus", "gated"}


# --- resolution and validation --------------------------------------------


@pytest.mark.plugin("sivacor")
def test_resolution_of_an_absent_request(server):
    assert resolve_worker_size({"stages": []}) == 60
    assert resolve_worker_size({"stages": [], "resources": {}}) == 60


@pytest.mark.plugin("sivacor")
def test_resolution_of_an_explicit_request(server):
    Setting().set(PluginSettings.WORKER_SIZES, LADDER)
    assert resolve_worker_size({"resources": {"memory_gb": 60}}) == 60


@pytest.mark.plugin("sivacor")
def test_an_unknown_size_names_the_available_ones(server):
    """"Invalid" is not enough. The catalogue is a published contract -- an
    exported workflow carries a bare number -- so a submission rejected because
    a rung was withdrawn has to be told what it may ask for instead."""
    Setting().set(PluginSettings.WORKER_SIZES, LADDER)
    with pytest.raises(ValidationException) as excinfo:
        resolve_worker_size({"resources": {"memory_gb": 120}})
    message = str(excinfo.value)
    assert "120" in message
    assert "30" in message and "60" in message
    # The gated rung is not something to advertise as available.
    assert "250" not in message


@pytest.mark.plugin("sivacor")
def test_a_gated_size_is_refused_and_points_at_support(server):
    """The group check does not exist yet, so a gated rung is selectable by
    nobody. Failing closed matters here: the gated rungs are the expensive
    ones."""
    Setting().set(PluginSettings.WORKER_SIZES, LADDER)
    with pytest.raises(ValidationException) as excinfo:
        resolve_worker_size({"resources": {"memory_gb": 250}})
    assert "support@sivacor.org" in str(excinfo.value)


# --- the wire and the job document ----------------------------------------


@pytest.mark.plugin("sivacor")
def test_schema_carries_a_workflow_level_resources_object(server):
    """Workflow-level, not per-stage: pin_chain binds every stage to one
    machine, so a per-stage size would be a lie the schema endorsed."""
    resources = stage_schema["properties"]["resources"]
    assert set(resources["properties"]) == {"memory_gb"}
    assert resources["additionalProperties"] is False
    assert "resources" not in stage_schema["properties"]["stages"]["items"]["properties"]
    # Served verbatim, so the UI validates an imported workflow with the same
    # object the server does.
    resp = server.request(path="/sivacor/workflow_schema", method="GET")
    assertStatusOk(resp)
    assert resp.json["properties"]["resources"] == resources


@pytest.mark.plugin("sivacor")
def test_a_submission_records_the_default_size(
    server, db, user, fsAssetstore, uploads_folder
):
    fobj = upload_test_file(uploads_folder, user, "test_stata.tar.gz")
    resp = submit_sivacor_job(server, user, fobj, STAGES)
    assertStatusOk(resp)
    job = Job().load(resp.json["_id"], force=True)
    assert job["meta"]["requested_memory_gb"] == 60


@pytest.mark.plugin("sivacor")
def test_a_submission_records_an_explicitly_requested_size(
    server, db, user, fsAssetstore, uploads_folder
):
    Setting().set(PluginSettings.WORKER_SIZES, LADDER)
    fobj = upload_test_file(uploads_folder, user, "test_stata.tar.gz")
    resp = submit_sivacor_job(server, user, fobj, STAGES, resources={"memory_gb": 30})
    assertStatusOk(resp)
    job = Job().load(resp.json["_id"], force=True)
    assert job["meta"]["requested_memory_gb"] == 30


@pytest.mark.plugin("sivacor")
def test_an_unknown_size_is_rejected_before_a_job_exists(
    server, db, user, fsAssetstore, uploads_folder
):
    """400, and no job -- not a job that fails later. A submission that reached
    the controller with a nonexistent flavour is the fleet-stopping case."""
    fobj = upload_test_file(uploads_folder, user, "test_stata.tar.gz")
    resp = submit_sivacor_job(server, user, fobj, STAGES, resources={"memory_gb": 999})
    assertStatus(resp, 400)
    assert Job().collection.count_documents({"type": "sivacor_submission"}) == 0


# --- the execution record -------------------------------------------------


@pytest.mark.plugin("sivacor")
def test_the_record_keeps_a_catalogue_size(server):
    stored = sanitize_record(
        {"status": "completed", "stages": [{"requested_memory_gb": 60}]},
        DATE,
        allowed_sizes=[60],
    )
    assert stored["stages"][0]["requested_memory_gb"] == 60


@pytest.mark.plugin("sivacor")
@pytest.mark.parametrize(
    "value", [30, "60", 60.0, True, None, "sixty", {"memory_gb": 60}]
)
def test_the_record_drops_anything_not_in_the_catalogue(server, value):
    """Validated against the catalogue rather than a charset, per rule 4 of
    telemetry's docstring: the figure is drawn from a set the operator
    published, never from anything the worker can invent."""
    stored = sanitize_record(
        {"status": "completed", "stages": [{"requested_memory_gb": value}]},
        DATE,
        allowed_sizes=[60],
    )
    assert stored["stages"][0]["requested_memory_gb"] is None


def test_the_record_fails_closed_without_a_catalogue():
    """No allowed_sizes passed -> the field is dropped, not trusted."""
    stored = sanitize_record(
        {"status": "completed", "stages": [{"requested_memory_gb": 60}]}, DATE
    )
    assert stored["stages"][0]["requested_memory_gb"] is None
