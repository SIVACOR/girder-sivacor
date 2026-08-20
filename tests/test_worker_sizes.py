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
from girder.models.group import Group
from girder.models.setting import Setting
from girder.models.user import User
from girder_jobs.models.job import Job
from girder_sivacor.rest import (
    default_worker_size,
    may_select_gated_sizes,
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
    # Nobody is logged in, so the gated rung is visible but not choosable.
    assert resp.json["sizes"][2]["selectable"] is False
    assert resp.json["sizes"][0]["selectable"] is True


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
        assert set(entry) == {"memory_gb", "vcpus", "gated", "selectable"}


@pytest.mark.plugin("sivacor")
def test_the_endpoint_reports_selectability_per_caller(server, user, size_group):
    """'gated' describes the catalogue; 'selectable' answers this request. The
    picker needs both, or it cannot show a rung it may not choose."""
    Setting().set(PluginSettings.WORKER_SIZES, LADDER)

    def gated_entry(as_user):
        resp = server.request(path="/sivacor/worker_sizes", method="GET", user=as_user)
        assertStatusOk(resp)
        return next(e for e in resp.json["sizes"] if e["memory_gb"] == 250)

    assert gated_entry(user)["selectable"] is False
    Group().addUser(size_group, user)
    entry = gated_entry(User().load(user["_id"], force=True))
    assert entry["selectable"] is True
    # Still gated: membership changes who may choose it, not what it is.
    assert entry["gated"] is True


# --- resolution and validation --------------------------------------------


@pytest.mark.plugin("sivacor")
def test_resolution_of_an_absent_request(server, user):
    assert resolve_worker_size({"stages": []}, user) == 60
    assert resolve_worker_size({"stages": [], "resources": {}}, user) == 60


@pytest.mark.plugin("sivacor")
def test_resolution_of_an_explicit_request(server, user):
    Setting().set(PluginSettings.WORKER_SIZES, LADDER)
    assert resolve_worker_size({"resources": {"memory_gb": 60}}, user) == 60


@pytest.mark.plugin("sivacor")
def test_an_unknown_size_names_the_available_ones(server, user):
    """"Invalid" is not enough. The catalogue is a published contract -- an
    exported workflow carries a bare number -- so a submission rejected because
    a rung was withdrawn has to be told what it may ask for instead."""
    Setting().set(PluginSettings.WORKER_SIZES, LADDER)
    with pytest.raises(ValidationException) as excinfo:
        resolve_worker_size({"resources": {"memory_gb": 120}}, user)
    message = str(excinfo.value)
    assert "120" in message
    assert "30" in message and "60" in message
    # The gated rung is not something to advertise to someone who cannot have
    # it -- but a member is told about it, since for them it is available.
    assert "250" not in message


@pytest.mark.plugin("sivacor")
def test_a_gated_size_is_refused_and_points_at_support(server, user):
    """A non-member gets the FAQ's existing "please contact us" path, not a
    button. Failing closed matters here: the gated rungs are the expensive
    ones."""
    Setting().set(PluginSettings.WORKER_SIZES, LADDER)
    with pytest.raises(ValidationException) as excinfo:
        resolve_worker_size({"resources": {"memory_gb": 250}}, user)
    assert "support@sivacor.org" in str(excinfo.value)


# --- S5 guard 2: the group gate -------------------------------------------


@pytest.fixture
def size_group(server, admin):
    """The group named by the setting, as an operator would create it."""
    return Group().createGroup(
        Setting().get(PluginSettings.WORKER_SIZE_GROUP_NAME), admin, public=False
    )


@pytest.mark.plugin("sivacor")
def test_a_member_may_select_a_gated_size(server, user, size_group):
    Setting().set(PluginSettings.WORKER_SIZES, LADDER)
    Group().addUser(size_group, user)
    user = User().load(user["_id"], force=True)
    assert may_select_gated_sizes(user) is True
    assert resolve_worker_size({"resources": {"memory_gb": 250}}, user) == 250


@pytest.mark.plugin("sivacor")
def test_a_member_is_told_the_gated_size_is_available(server, user, size_group):
    """The "available sizes" list is per-caller, not per-catalogue: telling a
    member their own rung is unavailable is how a working gate reads as a broken
    one."""
    Setting().set(PluginSettings.WORKER_SIZES, LADDER)
    Group().addUser(size_group, user)
    user = User().load(user["_id"], force=True)
    with pytest.raises(ValidationException) as excinfo:
        resolve_worker_size({"resources": {"memory_gb": 120}}, user)
    assert "250" in str(excinfo.value)


@pytest.mark.plugin("sivacor")
def test_membership_of_another_group_does_not_open_the_gate(
    server, user, admin, size_group
):
    """The check is the named group, not "any group" -- the editors group in
    particular is created by every submission."""
    Setting().set(PluginSettings.WORKER_SIZES, LADDER)
    other = Group().createGroup("Editors", admin, public=False)
    Group().addUser(other, user)
    user = User().load(user["_id"], force=True)
    assert may_select_gated_sizes(user) is False


@pytest.mark.plugin("sivacor")
def test_a_missing_group_refuses_everyone(server, user):
    """Fails closed. An operator who mistyped the setting locks the gated rungs
    to nobody rather than opening them to everybody."""
    Setting().set(PluginSettings.WORKER_SIZES, LADDER)
    Setting().set(PluginSettings.WORKER_SIZE_GROUP_NAME, "Nonexistent")
    assert may_select_gated_sizes(user) is False
    with pytest.raises(ValidationException):
        resolve_worker_size({"resources": {"memory_gb": 250}}, user)


@pytest.mark.plugin("sivacor")
def test_an_admin_bypasses_the_gate(server, admin):
    """No group needed: an admin can add themselves to it anyway, so refusing
    them only takes away the ability to exercise a gated rung while testing."""
    Setting().set(PluginSettings.WORKER_SIZES, LADDER)
    assert may_select_gated_sizes(admin) is True
    assert resolve_worker_size({"resources": {"memory_gb": 250}}, admin) == 250


@pytest.mark.plugin("sivacor")
def test_an_ungated_catalogue_never_asks_about_the_group(server, user, caplog):
    """No gated rung means no group to look up -- and no warning about one.

    Every deployment is in this state until the upper rungs are added, so a
    missing-group warning here would land on every submission, naming a group
    nobody has any reason to have created."""
    assert resolve_worker_size({"resources": {"memory_gb": 60}}, user) == 60
    assert "worker_size_group_name" not in caplog.text


@pytest.mark.plugin("sivacor")
def test_an_anonymous_caller_has_no_gated_access(server, size_group):
    """submit_job is @access.user, but GET /sivacor/worker_sizes is public and
    asks the same question."""
    assert may_select_gated_sizes(None) is False


@pytest.mark.plugin("sivacor")
def test_a_member_can_submit_a_gated_size(
    server, db, user, fsAssetstore, uploads_folder, size_group
):
    """End to end through the endpoint: the gate is server-side, so the proof
    has to be that submit_job accepts it, not that the helper does."""
    Setting().set(PluginSettings.WORKER_SIZES, LADDER)
    Group().addUser(size_group, user)
    fobj = upload_test_file(uploads_folder, user, "test_stata.tar.gz")
    resp = submit_sivacor_job(server, user, fobj, STAGES, resources={"memory_gb": 250})
    assertStatusOk(resp)
    job = Job().load(resp.json["_id"], force=True)
    assert job["meta"]["requested_memory_gb"] == 250


@pytest.mark.plugin("sivacor")
def test_a_non_member_cannot_submit_a_gated_size(
    server, db, user, fsAssetstore, uploads_folder, size_group
):
    """400, and no job -- the same shape as an unknown size, because an
    imported workflow file can carry a rung the importer may not have."""
    Setting().set(PluginSettings.WORKER_SIZES, LADDER)
    fobj = upload_test_file(uploads_folder, user, "test_stata.tar.gz")
    resp = submit_sivacor_job(server, user, fobj, STAGES, resources={"memory_gb": 250})
    assertStatus(resp, 400)
    assert Job().collection.count_documents({"type": "sivacor_submission"}) == 0


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
