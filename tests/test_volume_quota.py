"""Per-user scratch-volume approval, and a submission recording what it asked for.

Nothing here creates a volume. C1 records the request and enforces who may make
one; C2 is what turns it into a Cinder volume. So every test below is about a
*refusal* or a *record*, and the most important ones assert that a submission
which does not ask for a volume is completely unaffected -- that is what makes
this feature inert by default.

The resource being guarded is not merely expensive, it is *shared*: the
gigabytes come out of the same 1000 GB of OpenStack Cinder quota that
production's 800 GB assetstore volume draws on (see
development_notes/cinder_volumes_plan.md section 2). So every uncertain input
here has to fail closed, and several tests exist only to pin that direction.
"""

import pytest
from girder.exceptions import ValidationException
from girder.models.setting import Setting
from girder.models.user import User
from girder_jobs.models.job import Job
from girder_sivacor.rest import (
    USER_VOLUME_QUOTA_FIELD,
    VOLUME_GRANULARITY_GB,
    resolve_volume_gb,
    stage_schema,
    user_volume_quota,
    volume_total_gb,
    volumes_enabled,
)
from girder_sivacor.settings import PluginSettings
from girder_sivacor.telemetry import sanitize_record
from pytest_girder.assertions import assertStatus, assertStatusOk

from .conftest import (
    get_submission_folder,
    submit_sivacor_job,
    upload_test_file,
)

DATE = "2026-08-21"

STAGES = [
    {
        "image_name": "dataeditors/stata18_5-mp",
        "image_tag": "2025-02-26",
        "main_file": "main.do",
    }
]


def _enable(total_gb=500):
    Setting().set(PluginSettings.VOLUMES_ENABLED, True)
    Setting().set(PluginSettings.VOLUME_TOTAL_GB, total_gb)


def _approve(user, max_gb):
    User().update({"_id": user["_id"]}, {"$set": {USER_VOLUME_QUOTA_FIELD: max_gb}})
    return User().load(user["_id"], force=True)


# --- off by default --------------------------------------------------------


@pytest.mark.plugin("sivacor")
def test_the_feature_is_off_and_unfunded_by_default(server):
    """Both switches, because they mean different things.

    "Not turned on" and "turned on and budgeted nothing" are distinguishable
    states, and only the first should read to a researcher as *not offered*.
    """
    assert volumes_enabled() is False
    assert volume_total_gb() == 0


@pytest.mark.plugin("sivacor")
def test_nobody_is_approved_by_default(server, user, admin):
    """The per-user field is absent, which is the same as zero."""
    assert USER_VOLUME_QUOTA_FIELD not in user
    assert user_volume_quota(user) == 0
    # Including administrators. Unlike the worker-size gate, admins are not
    # special here: a ceiling is a number, and there is no "they could grant it
    # to themselves anyway" argument that produces one. An admin who wants a
    # volume sets their own field -- the same operator action, with the same
    # record of what was granted.
    assert user_volume_quota(admin) == 0


@pytest.mark.plugin("sivacor")
def test_a_workflow_that_does_not_ask_gets_no_volume(server, user):
    """None, not zero, and on every deployment however it is configured.

    The absent case must take a path with no volume in it at all. This is the
    single most important assertion in the file: it is what makes the feature
    impossible to regress into for the ~90% of submissions that never want one.
    """
    assert resolve_volume_gb({"stages": STAGES}, user) is None
    assert resolve_volume_gb({"stages": STAGES, "resources": {}}, user) is None
    assert (
        resolve_volume_gb({"stages": STAGES, "resources": {"memory_gb": 60}}, user)
        is None
    )
    # ...and still None once the feature is on and the user is approved. Asking
    # for nothing is not the same as asking for the minimum.
    _enable()
    assert resolve_volume_gb({"stages": STAGES}, _approve(user, 200)) is None


# --- the four refusals (V8) ------------------------------------------------


@pytest.mark.plugin("sivacor")
def test_a_disabled_deployment_refuses_without_mentioning_approval(server, user):
    """Refusal 1. The researcher must not be sent to ask for access to a
    feature the deployment does not have -- they would be asking the operator
    for something the operator cannot grant them by approving anyone.
    """
    _approve(user, 500)   # approved, and it must not matter
    with pytest.raises(ValidationException) as exc:
        resolve_volume_gb({"resources": {"disk_gb": 100}}, User().load(user["_id"], force=True))
    assert "not available on this deployment" in str(exc.value)
    assert "support@sivacor.org" not in str(exc.value)


@pytest.mark.plugin("sivacor")
def test_an_unapproved_user_is_sent_to_the_request_route(server, user):
    """Refusal 2, and it is the FAQ's existing "please contact us" path."""
    _enable()
    with pytest.raises(ValidationException) as exc:
        resolve_volume_gb({"resources": {"disk_gb": 100}}, user)
    assert "needs approval" in str(exc.value)
    assert "support@sivacor.org" in str(exc.value)


@pytest.mark.plugin("sivacor")
def test_over_the_users_own_ceiling_names_the_ceiling(server, user):
    """Refusal 3, and the only one the researcher can act on alone.

    So it names the number. "Too large" without it means a round trip to
    support for something they could have fixed themselves -- which is exactly
    the distinction from the worker-size gate, where both answers were "ask us"
    and the two refusals are deliberately identical.
    """
    _enable()
    with pytest.raises(ValidationException) as exc:
        resolve_volume_gb({"resources": {"disk_gb": 300}}, _approve(user, 200))
    message = str(exc.value)
    assert "300" in message
    assert "200 GB limit" in message
    assert "Ask for 200 GB or less" in message


@pytest.mark.plugin("sivacor")
def test_over_the_deployment_reservation_is_a_capacity_message(server, user):
    """Refusal 4. This user *is* approved for what they asked for.

    Telling them to request access would send them to ask for something they
    already have, so the message is about the deployment, not about them.
    """
    _enable(total_gb=100)
    with pytest.raises(ValidationException) as exc:
        resolve_volume_gb({"resources": {"disk_gb": 200}}, _approve(user, 500))
    message = str(exc.value)
    assert "this deployment currently has available (100 GB)" in message
    assert "needs approval" not in message


@pytest.mark.plugin("sivacor")
@pytest.mark.parametrize("value", [0, -10, True, 1.5, "100", [100], {}])
def test_a_nonsense_size_is_a_shape_error_not_a_permissions_one(server, user, value):
    """Shape is checked first, so a typo is never reported as "ask for access".

    ``True`` is in here because it is an ``int`` in Python and would otherwise
    validate and round to one granularity unit.
    """
    _enable()
    _approve(user, 500)
    with pytest.raises(ValidationException) as exc:
        resolve_volume_gb(
            {"resources": {"disk_gb": value}}, User().load(user["_id"], force=True)
        )
    assert "positive whole number of gigabytes" in str(exc.value)


# --- rounding --------------------------------------------------------------


@pytest.mark.plugin("sivacor")
@pytest.mark.parametrize(
    "asked,granted",
    [(1, 10), (10, 10), (11, 20), (37, 40), (100, 100), (101, 110)],
)
def test_a_size_is_rounded_up_to_the_granularity(server, user, asked, granted):
    """Up, so a researcher never silently gets less than they asked for."""
    _enable(total_gb=1000)
    assert resolve_volume_gb({"resources": {"disk_gb": asked}}, _approve(user, 1000)) == (
        granted
    )


@pytest.mark.plugin("sivacor")
def test_rounding_cannot_push_a_request_past_the_ceiling_it_was_checked_against(
    server, user
):
    """The ordering bug this exists to prevent.

    Check-then-round accepts 195 against a 200 GB ceiling and then grants 200 --
    which happens to be fine -- but accepts 199 against 195 and grants 200,
    handing out more than the operator allowed. Rounding first is what makes the
    ceiling mean what it says.
    """
    _enable(total_gb=1000)
    approved = _approve(user, 195)
    with pytest.raises(ValidationException):
        resolve_volume_gb({"resources": {"disk_gb": 191}}, approved)
    # And the largest thing that does fit under a non-round ceiling is the
    # granularity step below it, not the ceiling itself.
    assert resolve_volume_gb({"resources": {"disk_gb": 190}}, approved) == 190


# --- the per-user field, read defensively ----------------------------------


@pytest.mark.plugin("sivacor")
@pytest.mark.parametrize("stored", [None, -1, 0, True, 1.5, "200", [], {}])
def test_an_unusable_stored_ceiling_reads_as_not_approved(server, user, stored):
    """Fails closed on anything written straight into Mongo.

    The field is only meant to be set through the admin endpoint, but nothing
    can guarantee that, and the resource is a quota production's assetstore
    shares. "We could not establish a ceiling" has to mean zero.
    """
    assert user_volume_quota(dict(user, **{USER_VOLUME_QUOTA_FIELD: stored})) == 0


@pytest.mark.plugin("sivacor")
def test_an_anonymous_caller_is_never_approved(server):
    assert user_volume_quota(None) == 0
    assert user_volume_quota({}) == 0


# --- the endpoints ---------------------------------------------------------


@pytest.mark.plugin("sivacor")
def test_the_quota_endpoint_answers_without_a_login(server):
    """Public, like /sivacor/worker_sizes, so a client can render the control's
    state before the user has signed in. Anonymous is never approved.
    """
    response = server.request(path="/sivacor/volume_quota", method="GET")
    assertStatusOk(response)
    assert response.json == {
        "enabled": False,
        "max_gb": 0,
        "granularity_gb": VOLUME_GRANULARITY_GB,
        "deployment_gb": 0,
    }


@pytest.mark.plugin("sivacor")
def test_the_quota_endpoint_reports_the_callers_own_allowance(server, user):
    _enable(total_gb=800)
    _approve(user, 250)
    response = server.request(path="/sivacor/volume_quota", method="GET", user=user)
    assertStatusOk(response)
    assert response.json["enabled"] is True
    assert response.json["max_gb"] == 250
    assert response.json["deployment_gb"] == 800


@pytest.mark.plugin("sivacor")
def test_only_an_administrator_may_grant_an_allowance(server, user, admin):
    """The endpoint is the only way to approve anyone, so it is also the only
    thing standing between an ordinary user and a shared OpenStack quota.
    """
    path = f"/sivacor/user/{user['_id']}/volume_quota"
    assertStatus(server.request(path=path, method="PUT", params={"maxGb": 100}), 401)
    assertStatus(
        server.request(path=path, method="PUT", params={"maxGb": 100}, user=user), 403
    )
    assertStatusOk(
        server.request(path=path, method="PUT", params={"maxGb": 100}, user=admin)
    )
    assert user_volume_quota(User().load(user["_id"], force=True)) == 100


@pytest.mark.plugin("sivacor")
def test_an_allowance_can_be_revoked_with_zero(server, user, admin):
    path = f"/sivacor/user/{user['_id']}/volume_quota"
    server.request(path=path, method="PUT", params={"maxGb": 100}, user=admin)
    assertStatusOk(
        server.request(path=path, method="PUT", params={"maxGb": 0}, user=admin)
    )
    assert user_volume_quota(User().load(user["_id"], force=True)) == 0


@pytest.mark.plugin("sivacor")
def test_a_negative_allowance_is_refused(server, user, admin):
    path = f"/sivacor/user/{user['_id']}/volume_quota"
    assertStatus(
        server.request(path=path, method="PUT", params={"maxGb": -1}, user=admin), 400
    )


# --- settings validators ---------------------------------------------------


@pytest.mark.plugin("sivacor")
@pytest.mark.parametrize("value", ["true", "false", 1, 0, None, []])
def test_the_enable_switch_must_be_a_real_boolean(server, value):
    """A truthy string turns on a feature that spends the assetstore's quota."""
    with pytest.raises(ValidationException):
        Setting().set(PluginSettings.VOLUMES_ENABLED, value)


@pytest.mark.plugin("sivacor")
@pytest.mark.parametrize("value", [-1, 1.5, "500", None, [], True])
def test_the_reservation_must_be_a_non_negative_whole_number(server, value):
    """``True`` is an ``int`` in Python and would reserve one gigabyte."""
    with pytest.raises(ValidationException):
        Setting().set(PluginSettings.VOLUME_TOTAL_GB, value)


@pytest.mark.plugin("sivacor")
def test_zero_is_a_valid_reservation(server):
    """"Enabled but unfunded" is a state an operator is allowed to be in."""
    Setting().set(PluginSettings.VOLUME_TOTAL_GB, 0)
    assert volume_total_gb() == 0


# --- the schema ------------------------------------------------------------


@pytest.mark.plugin("sivacor")
def test_the_schema_accepts_disk_gb_beside_memory_gb(server):
    resources = stage_schema["properties"]["resources"]
    assert resources["properties"]["disk_gb"] == {"type": "integer", "minimum": 1}
    # Still closed, so a misspelled sibling is rejected rather than ignored.
    assert resources["additionalProperties"] is False


@pytest.mark.plugin("sivacor")
def test_the_schema_does_not_bound_disk_gb(server):
    """The ceiling is per user, so the schema cannot express it.

    A bound here would either duplicate the per-user ceiling or contradict it,
    and the schema is served to clients as a published contract.
    """
    assert "maximum" not in stage_schema["properties"]["resources"]["properties"][
        "disk_gb"
    ]


# --- the submit path ------------------------------------------------------


@pytest.mark.plugin("sivacor")
def test_a_submission_records_the_size_it_asked_for(
    server, user, uploads_folder, fsAssetstore
):
    """On the job, where the fleet will read it in C2."""
    _enable(total_gb=500)
    approved = _approve(user, 300)
    file_obj = upload_test_file(uploads_folder, approved, "test_stata.tar.gz")
    response = submit_sivacor_job(
        server, approved, file_obj, STAGES, resources={"disk_gb": 95}
    )
    assertStatusOk(response)
    job = Job().load(response.json["_id"], force=True)
    # Rounded up, and it is the granted figure that is recorded -- not the
    # figure asked for. C2 creates a volume from this number, so it has to be
    # the one the quota was checked against.
    assert job["meta"]["requested_disk_gb"] == 100


@pytest.mark.plugin("sivacor")
def test_a_submission_that_asks_for_nothing_records_nothing(
    server, user, uploads_folder, fsAssetstore
):
    """The field is present and None, rather than absent.

    So "did not ask" and "asked and got it" are distinguishable in the job
    document without inferring from a missing key.
    """
    file_obj = upload_test_file(uploads_folder, user, "test_stata.tar.gz")
    response = submit_sivacor_job(server, user, file_obj, STAGES)
    assertStatusOk(response)
    job = Job().load(response.json["_id"], force=True)
    assert job["meta"]["requested_disk_gb"] is None


@pytest.mark.plugin("sivacor")
def test_the_submit_endpoint_refuses_an_unapproved_request(
    server, user, uploads_folder, fsAssetstore
):
    """The gate is server-side or it is not a gate.

    The value can arrive from an imported workflow file exported by somebody who
    *is* approved, so the picker offering only permitted values is not enough.
    """
    _enable()
    file_obj = upload_test_file(uploads_folder, user, "test_stata.tar.gz")
    response = submit_sivacor_job(
        server, user, file_obj, STAGES, resources={"disk_gb": 100}, exception=True
    )
    assertStatus(response, 400)
    assert "needs approval" in response.json["message"]


# --- telemetry ------------------------------------------------------------


@pytest.mark.plugin("sivacor")
def test_the_requested_size_survives_the_sanitizer(server):
    record = sanitize_record({"status": "completed", "requested_disk_gb": 100}, DATE)
    assert record["requested_disk_gb"] == 100


@pytest.mark.plugin("sivacor")
def test_no_volume_is_recorded_as_none_rather_than_dropped(server):
    """"Nobody asked" is a fact worth keeping when the question is how often
    anyone asks."""
    record = sanitize_record({"status": "completed", "requested_disk_gb": None}, DATE)
    assert "requested_disk_gb" in record
    assert record["requested_disk_gb"] is None


@pytest.mark.plugin("sivacor")
@pytest.mark.parametrize(
    "value",
    [
        -10,
        True,
        1.5,
        "100",
        [],
        {},
        999999,  # above any reservation the 1000 GB of quota could fund
    ],
)
def test_an_unusable_size_is_dropped_by_the_sanitizer(server, value):
    """The boundary fails closed, like every other field here."""
    record = sanitize_record({"status": "completed", "requested_disk_gb": value}, DATE)
    assert record["requested_disk_gb"] is None


@pytest.mark.plugin("sivacor")
def test_the_record_still_carries_no_identifier(server):
    """The rule this feature must not become the exception to.

    A per-user ceiling is the first thing in this plugin that is *about* a
    specific user, so it is the first plausible reason anyone has had to put a
    user id in a record that is kept forever. It stays out.
    """
    record = sanitize_record(
        {
            "status": "completed",
            "requested_disk_gb": 100,
            "userId": "6a8766218620bb99e31529f2",
            "volume_owner": "jane@example.org",
        },
        DATE,
    )
    assert "userId" not in record
    assert "volume_owner" not in record
    assert "jane@example.org" not in str(record)


# --- end to end -----------------------------------------------------------


@pytest.mark.plugin("sivacor")
def test_the_size_reaches_the_submission_folder(
    server,
    db,
    user,
    eagerWorkerTasks,
    fsAssetstore,
    patched_gpg,
    uploads_folder,
    submission_collection,
):
    """The third recording place, and the only one a unit test cannot reach.

    ``prepare_submission`` writes it, so this needs a real run. It matters
    because the folder is what the *UI* reads: the workflow exporter builds its
    YAML from the submission folder's metadata, so a size that stops here never
    round-trips through an export and re-import.

    Also the only test that proves the new task argument is wired end to end.
    ``prepare_submission`` takes the size as an argument rather than reading it
    off the job -- ``build_execution_record``'s docstring forbids reading the
    job at all, which is what keeps the records anonymous -- so a chain builder
    that forgot to pass it would fail silently here and nowhere else.
    """
    _enable(total_gb=500)
    approved = _approve(user, 300)
    fobj = upload_test_file(uploads_folder, approved, "with_space_R.zip")
    stages = [
        {"image_name": "rocker/r-ver", "image_tag": "4.3.1", "main_file": "main.R"}
    ]

    resp = submit_sivacor_job(
        server, approved, fobj, stages, resources={"disk_gb": 55}
    )
    assertStatusOk(resp)
    job = resp.json
    assert job["status"] == 2  # completed: asking for a volume changed nothing

    resp = get_submission_folder(server, approved, job["_id"], submission_collection)
    assertStatusOk(resp)
    assert resp.json[0]["meta"]["requested_disk_gb"] == 60  # 55 rounded up


@pytest.mark.plugin("sivacor")
def test_a_run_without_a_volume_records_none_on_the_folder(
    server,
    db,
    user,
    eagerWorkerTasks,
    fsAssetstore,
    patched_gpg,
    uploads_folder,
    submission_collection,
):
    """The default path, end to end, on a deployment with the feature off.

    This is the regression test for the whole feature: an ordinary submission on
    an ordinary deployment must run exactly as it did before C1 existed.

    **The folder carries no key at all**, which is not what the first draft of
    this test asserted. Girder's metadata PUT treats a null value as a *delete*,
    so writing None removes the field rather than storing it -- unlike the job
    document, which stores None quite happily (see the test above). The two are
    written by different mechanisms.

    Asserted rather than worked around, because the resulting behaviour is the
    one we want: the workflow exporter builds its YAML from this folder, and a
    submission that asked for no volume should export no ``disk_gb`` rather than
    an explicit null that a re-import would have to interpret.
    """
    fobj = upload_test_file(uploads_folder, user, "with_space_R.zip")
    stages = [
        {"image_name": "rocker/r-ver", "image_tag": "4.3.1", "main_file": "main.R"}
    ]

    resp = submit_sivacor_job(server, user, fobj, stages)
    assertStatusOk(resp)
    assert resp.json["status"] == 2

    resp = get_submission_folder(
        server, user, resp.json["_id"], submission_collection
    )
    assertStatusOk(resp)
    assert "requested_disk_gb" not in resp.json[0]["meta"]
