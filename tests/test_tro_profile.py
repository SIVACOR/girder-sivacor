"""A deployment declares its own TRS, and is told when it has not (issue #153).

A deployment left on the built-in profile signs TROs, with its own key, that
name SIVACOR as the Trusted Research System. Nothing about such a declaration
looks wrong, so the only defence is saying so where an operator will see it.
"""

import json
import logging

import pytest
from girder.exceptions import ValidationException
from girder.models.setting import Setting
from girder.settings import SettingKey
from pytest_girder.assertions import assertStatus, assertStatusOk

from girder_sivacor import tro_profile
from girder_sivacor.settings import (
    ALWAYS_CLAIMED,
    CLAIMED_ON_REQUEST,
    REQUIRED_CAPABILITIES,
    PluginSettings,
)

OURS = {
    "@id": "https://trs.example.org/",
    "schema:name": "Example Verification Service",
    "schema:url": "https://example.org/",
    "trov:hasCapability": [
        {"@id": cap.value, "@type": cap.value} for cap in REQUIRED_CAPABILITIES
    ],
}


def _put(server, user, profile):
    return server.request(
        path="/sivacor/tro_profile",
        method="PUT",
        user=user,
        type="application/json",
        body=json.dumps(profile),
    )


# --- telling a deployment it is still on SIVACOR's profile -------------------


@pytest.mark.plugin("sivacor")
def test_an_unset_profile_is_the_builtin_one(server):
    assert tro_profile.is_builtin()


@pytest.mark.plugin("sivacor")
def test_a_stored_copy_of_the_default_is_still_the_builtin_one(server):
    """A seed that copied the default has not declared anything."""
    Setting().set(PluginSettings.TRO_PROFILE, tro_profile.builtin())
    assert tro_profile.is_builtin()


@pytest.mark.plugin("sivacor")
def test_a_profile_of_our_own_is_not_warned_about(server):
    Setting().set(SettingKey.SERVER_ROOT, "https://girder.example.org")
    Setting().set(PluginSettings.TRO_PROFILE, OURS)
    assert not tro_profile.is_builtin()
    assert tro_profile.warning() is None


@pytest.mark.plugin("sivacor")
@pytest.mark.parametrize("root", ["https://sivacor.org", "https://girder.sivacor.org/"])
def test_sivacor_itself_is_not_warned_about(server, root):
    Setting().set(SettingKey.SERVER_ROOT, root)
    assert tro_profile.warning() is None


@pytest.mark.plugin("sivacor")
@pytest.mark.parametrize(
    "root", ["https://girder.example.org", "https://notsivacor.org", "https://sivacor.org.evil"]
)
def test_another_domain_on_the_builtin_profile_is_warned_about(server, root):
    Setting().set(SettingKey.SERVER_ROOT, root)
    message = tro_profile.warning()
    assert "names SIVACOR" in message
    assert root.split("//")[1] in message


@pytest.mark.plugin("sivacor")
def test_an_unknown_domain_is_warned_about_rather_than_guessed(server):
    assert Setting().get(SettingKey.SERVER_ROOT) == ""
    assert "if this deployment is not sivacor.org" in tro_profile.warning()


@pytest.mark.plugin("sivacor")
def test_the_warning_is_logged(server, caplog):
    Setting().set(SettingKey.SERVER_ROOT, "https://girder.example.org")
    with caplog.at_level(logging.WARNING, logger="girder_sivacor.tro_profile"):
        tro_profile.warn_if_builtin()
    assert "names SIVACOR" in caplog.text


# --- what a profile has to declare -------------------------------------------


def test_every_claimed_attribute_needs_a_required_capability():
    """tro-utils refuses a performance whose capability is undeclared."""
    assert {cap.name for cap in REQUIRED_CAPABILITIES} == {
        attr.name for attr in ALWAYS_CLAIMED + CLAIMED_ON_REQUEST
    }


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
def test_extra_capabilities_are_allowed(server):
    profile = dict(OURS)
    profile["trov:hasCapability"] = OURS["trov:hasCapability"] + [
        {"@id": "trov:CanRecordInternetAccess", "@type": "trov:CanRecordInternetAccess"}
    ]
    assert Setting().set(PluginSettings.TRO_PROFILE, profile)


# --- the endpoints the config page uses -------------------------------------


@pytest.mark.plugin("sivacor")
def test_status_names_the_required_capabilities(server, admin):
    resp = server.request(path="/sivacor/tro_profile", user=admin)
    assertStatusOk(resp)
    assert resp.json["builtin"] is True
    assert resp.json["warning"]
    assert resp.json["profile"]["@id"] == "https://sivacor.org/"
    assert resp.json["required_capabilities"] == [
        cap.value for cap in REQUIRED_CAPABILITIES
    ]


@pytest.mark.plugin("sivacor")
def test_saving_a_profile_returns_the_resulting_status(server, admin):
    resp = _put(server, admin, OURS)
    assertStatusOk(resp)
    assert resp.json["builtin"] is False
    assert resp.json["warning"] is None
    assert resp.json["profile"] == OURS
    assert Setting().get(PluginSettings.TRO_PROFILE) == OURS


@pytest.mark.plugin("sivacor")
def test_a_refused_profile_is_not_saved(server, admin):
    resp = _put(server, admin, dict(OURS, **{"@id": "trs"}))
    assertStatus(resp, 400)
    assert "absolute IRI" in resp.json["message"]
    assert tro_profile.is_builtin()


@pytest.mark.plugin("sivacor")
def test_the_profile_endpoints_are_admin_only(server, user):
    assertStatus(server.request(path="/sivacor/tro_profile", user=user), 403)
    assertStatus(_put(server, user, OURS), 403)
