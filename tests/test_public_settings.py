import json

import pytest
from girder.exceptions import ValidationException
from girder.models.setting import Setting
from girder_sivacor.settings import PluginSettings
from pytest_girder.assertions import assertStatus, assertStatusOk


@pytest.mark.plugin("sivacor")
def test_public_settings_include_banner_defaults(server):
    """The banner settings are exposed via public_settings with their defaults."""
    resp = server.request(path="/system/public_settings", method="GET")
    assertStatusOk(resp)
    assert resp.json[PluginSettings.BANNER_ENABLED] is False
    assert resp.json[PluginSettings.BANNER_MESSAGE] == ""


@pytest.mark.plugin("sivacor")
def test_public_settings_available_without_auth(server):
    """Anonymous clients (no auth token) must be able to read the banner."""
    Setting().set(PluginSettings.BANNER_ENABLED, True)
    Setting().set(PluginSettings.BANNER_MESSAGE, "Scheduled maintenance tonight.")

    # No `user=` -> unauthenticated request.
    resp = server.request(path="/system/public_settings", method="GET")
    assertStatusOk(resp)
    assert resp.json[PluginSettings.BANNER_ENABLED] is True
    assert resp.json[PluginSettings.BANNER_MESSAGE] == "Scheduled maintenance tonight."


@pytest.mark.plugin("sivacor")
def test_banner_enabled_validator_rejects_non_bool(server):
    """A non-boolean banner_enabled value is rejected."""
    with pytest.raises(ValidationException):
        Setting().set(PluginSettings.BANNER_ENABLED, "yes")


@pytest.mark.plugin("sivacor")
def test_banner_message_validator_rejects_non_string(server):
    """A non-string banner_message value is rejected."""
    with pytest.raises(ValidationException):
        Setting().set(PluginSettings.BANNER_MESSAGE, 123)


@pytest.mark.plugin("sivacor")
def test_banner_message_allows_empty_string(server):
    """An empty message is valid and means 'no banner'."""
    Setting().set(PluginSettings.BANNER_MESSAGE, "")
    assert Setting().get(PluginSettings.BANNER_MESSAGE) == ""


@pytest.mark.plugin("sivacor")
def test_set_banner_via_rest_roundtrips_to_public_settings(server, admin):
    """Setting the banner through the REST API surfaces it on public_settings."""
    for key, value in (
        (PluginSettings.BANNER_ENABLED, True),
        (PluginSettings.BANNER_MESSAGE, "Down for upgrades."),
    ):
        resp = server.request(
            path="/system/setting",
            method="PUT",
            user=admin,
            params={"key": key, "value": json.dumps(value)},
        )
        assertStatusOk(resp)

    resp = server.request(path="/system/public_settings", method="GET")
    assertStatusOk(resp)
    assert resp.json[PluginSettings.BANNER_ENABLED] is True
    assert resp.json[PluginSettings.BANNER_MESSAGE] == "Down for upgrades."


@pytest.mark.plugin("sivacor")
def test_set_invalid_banner_enabled_via_rest_rejected(server, admin):
    """An invalid banner_enabled value via REST returns a 400."""
    resp = server.request(
        path="/system/setting",
        method="PUT",
        user=admin,
        params={
            "key": PluginSettings.BANNER_ENABLED,
            "value": json.dumps("not-a-bool"),
        },
    )
    assertStatus(resp, 400)
