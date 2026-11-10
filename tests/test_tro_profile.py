"""A deployment declares its own TRS, and is told when it has not (issue #153).

A deployment whose profile names SIVACOR signs TROs, with its own key, that
name SIVACOR as the Trusted Research System. Nothing about such a declaration
looks wrong, so the only defence is saying so where an operator will see it.
"""

import json
import logging
import tarfile
import tempfile
from unittest import mock

import pytest
from girder.exceptions import ValidationException
from girder.models.setting import Setting
from girder.settings import SettingDefault, SettingKey
from pytest_girder.assertions import assertStatus, assertStatusOk
from tro_utils import TROVCapability

from girder_sivacor import tro_profile
from girder_sivacor.rest import SIVACOR
from girder_sivacor.settings import (
    ALWAYS_CLAIMED,
    CLAIMED_ON_REQUEST,
    REQUIRED_CAPABILITIES,
    PluginSettings,
)

from .conftest import submit_sivacor_job, upload_test_file

NET = TROVCapability.NET_ISOLATION.value

OURS = {
    "@id": "https://trs.example.org/",
    "schema:name": "Example Verification Service",
    "schema:url": "https://example.org/",
    "trov:hasCapability": [
        {"@id": cap.value, "@type": cap.value} for cap in REQUIRED_CAPABILITIES
    ],
}

#: SIVACOR's profile as an older seed stored it: a bare @id, the pre-schema.org
#: url key and relative capability ids. Today's validator refuses it, so a
#: deployment can only have it from before, which is what makes it the case
#: most likely to be signing as SIVACOR unnoticed.
LEGACY = {
    "@id": "trs",
    "trov:name": "sivacor",
    "trov:url": "https://sivacor.org/",
    "trov:hasCapability": [
        {"@id": f"trs/capability/{i}", "@type": cap.value}
        for i, cap in enumerate(REQUIRED_CAPABILITIES)
    ],
}


def _store_unvalidated(profile):
    """Store a profile the way an older release could have, past the validator."""
    Setting().save({"key": PluginSettings.TRO_PROFILE, "value": profile}, validate=False)
    Setting()._get.invalidate(Setting(), PluginSettings.TRO_PROFILE)


def _put(server, user, profile):
    return server.request(
        path="/sivacor/tro_profile",
        method="PUT",
        user=user,
        type="application/json",
        body=json.dumps(profile),
    )


def _identity_warnings():
    return [message for message in tro_profile.warnings() if "names SIVACOR" in message]


# --- telling a deployment its TROs name SIVACOR ------------------------------


@pytest.mark.plugin("sivacor")
def test_a_profile_of_our_own_is_not_warned_about(server):
    Setting().set(SettingKey.SERVER_ROOT, "https://girder.example.org")
    Setting().set(PluginSettings.TRO_PROFILE, OURS)
    assert tro_profile.warnings() == []


@pytest.mark.plugin("sivacor")
@pytest.mark.parametrize("root", ["https://sivacor.org", "https://girder.sivacor.org/"])
def test_sivacor_itself_is_not_warned_about(server, root):
    Setting().set(SettingKey.SERVER_ROOT, root)
    assert tro_profile.warnings() == []


@pytest.mark.plugin("sivacor")
@pytest.mark.parametrize(
    "root", ["https://girder.example.org", "https://notsivacor.org", "https://sivacor.org.evil"]
)
def test_another_domain_on_the_default_profile_is_warned_about(server, root):
    Setting().set(SettingKey.SERVER_ROOT, root)
    (message,) = _identity_warnings()
    assert root.split("//")[1] in message


@pytest.mark.plugin("sivacor")
def test_an_unknown_domain_is_warned_about_rather_than_guessed(server):
    assert Setting().get(SettingKey.SERVER_ROOT) == ""
    (message,) = _identity_warnings()
    assert "if this deployment is not sivacor.org" in message


@pytest.mark.plugin("sivacor")
def test_an_older_copy_of_sivacors_profile_is_warned_about(server):
    """Not an exact copy of today's default, and still SIVACOR's identity."""
    Setting().set(SettingKey.SERVER_ROOT, "https://girder.example.org")
    _store_unvalidated(LEGACY)
    assert _identity_warnings()


@pytest.mark.plugin("sivacor")
def test_an_edited_copy_of_the_default_is_warned_about(server):
    """Changing the name or contact does not change who the TRS is."""
    Setting().set(SettingKey.SERVER_ROOT, "https://girder.example.org")
    profile = dict(
        SettingDefault.defaults[PluginSettings.TRO_PROFILE],
        **{"schema:name": "Example Verification Service"},
    )
    Setting().set(PluginSettings.TRO_PROFILE, profile)
    assert _identity_warnings()


@pytest.mark.parametrize(
    "profile, host",
    [
        ({"@id": "https://trs.example.org/", "schema:url": "https://sivacor.org/"}, "trs.example.org"),
        ({"@id": "ex:trs", "schema:url": "https://sivacor.org/"}, "sivacor.org"),
        ({"@id": "trs", "trov:url": "https://example.org/"}, "example.org"),
        ({"schema:name": "nothing to go on"}, None),
    ],
)
def test_the_identifier_decides_who_the_profile_names(profile, host):
    """@id first, as in the declaration; a url only when the @id has no host."""
    assert tro_profile.identified_host(profile) == host


@pytest.mark.plugin("sivacor")
def test_the_warnings_are_logged(server, caplog):
    Setting().set(SettingKey.SERVER_ROOT, "https://girder.example.org")
    with caplog.at_level(logging.WARNING, logger="girder_sivacor.tro_profile"):
        tro_profile.warn_if_misconfigured()
    assert "names SIVACOR" in caplog.text


# --- what a profile has to declare -------------------------------------------


def test_only_what_every_run_claims_is_required():
    """An opt-in claim's capability is not forced on a deployment that lacks it."""
    assert {cap.name for cap in REQUIRED_CAPABILITIES} == {
        attr.name for attr in ALWAYS_CLAIMED
    }
    assert NET not in {cap.value for cap in REQUIRED_CAPABILITIES}
    assert set(CLAIMED_ON_REQUEST) == {"network_isolation"}


@pytest.mark.plugin("sivacor")
@pytest.mark.parametrize("dropped", [cap.value for cap in REQUIRED_CAPABILITIES])
def test_a_profile_missing_a_claimed_capability_is_refused(server, dropped):
    """Otherwise every submission fails after its analysis has already run."""
    profile = dict(OURS)
    profile["trov:hasCapability"] = [
        cap for cap in OURS["trov:hasCapability"] if cap["@type"] != dropped
    ]
    with pytest.raises(ValidationException, match=dropped):
        Setting().set(PluginSettings.TRO_PROFILE, profile)


@pytest.mark.plugin("sivacor")
def test_a_profile_without_network_isolation_is_accepted(server):
    assert NET not in json.dumps(OURS)
    assert Setting().set(PluginSettings.TRO_PROFILE, OURS)


@pytest.mark.plugin("sivacor")
def test_a_stored_profile_missing_a_claimed_capability_is_warned_about(server):
    """The validator only runs on write; an older stored profile skipped it."""
    Setting().set(SettingKey.SERVER_ROOT, "https://girder.example.org")
    dropped = REQUIRED_CAPABILITIES[0].value
    profile = dict(OURS)
    profile["trov:hasCapability"] = [
        cap for cap in OURS["trov:hasCapability"] if cap["@type"] != dropped
    ]
    _store_unvalidated(profile)
    (message,) = tro_profile.warnings()
    assert dropped in message


@pytest.mark.plugin("sivacor")
def test_network_isolation_is_refused_when_the_profile_cannot_back_it(
    server, user, fsAssetstore, uploads_folder
):
    """Refused at submit time, not when the run is recorded after the analysis."""
    Setting().set(PluginSettings.TRO_PROFILE, OURS)
    with tempfile.NamedTemporaryFile(suffix=".tar.gz") as archive:
        with tarfile.open(archive.name, "w:gz"):
            pass
        fobj = upload_test_file(uploads_folder, user, archive.name)
    stage = {
        "image_name": "rocker/r-ver",
        "image_tag": "4.6.1",
        "main_file": "main.R",
        "network_isolation": True,
    }
    with mock.patch.object(
        SIVACOR, "_get_tags", staticmethod(lambda: {"rocker/r-ver": ["4.6.1"]})
    ):
        resp = submit_sivacor_job(server, user, fobj, [stage])
    assertStatus(resp, 400)
    assert NET in resp.json["message"]


def test_the_default_profile_backs_network_isolation():
    assert tro_profile.undeclared_requests(
        SettingDefault.defaults[PluginSettings.TRO_PROFILE]
    ) == {}
    assert tro_profile.undeclared_requests(OURS) == {"network_isolation": NET}


# --- the endpoints the config page uses -------------------------------------


@pytest.mark.plugin("sivacor")
def test_status_carries_the_warnings_and_the_required_capabilities(server, admin):
    resp = server.request(path="/sivacor/tro_profile", user=admin)
    assertStatusOk(resp)
    assert resp.json["warnings"]
    assert resp.json["profile"]["@id"] == "https://sivacor.org/"
    assert resp.json["required_capabilities"] == [
        cap.value for cap in REQUIRED_CAPABILITIES
    ]


@pytest.mark.plugin("sivacor")
def test_saving_a_profile_returns_the_resulting_status(server, admin):
    resp = _put(server, admin, OURS)
    assertStatusOk(resp)
    assert resp.json["warnings"] == []
    assert resp.json["profile"] == OURS
    assert Setting().get(PluginSettings.TRO_PROFILE) == OURS


@pytest.mark.plugin("sivacor")
def test_a_refused_profile_is_not_saved(server, admin):
    resp = _put(server, admin, dict(OURS, **{"@id": "trs"}))
    assertStatus(resp, 400)
    assert "absolute IRI" in resp.json["message"]
    assert Setting().findOne({"key": PluginSettings.TRO_PROFILE}) is None


@pytest.mark.plugin("sivacor")
def test_the_profile_endpoints_are_admin_only(server, user):
    assertStatus(server.request(path="/sivacor/tro_profile", user=user), 403)
    assertStatus(_put(server, user, OURS), 403)
