"""The MATLAB license server is a setting, and only MATLAB stages get it (#154).

Every analysis container used to get a hardwired
``MLM_LICENSE_FILE=27007@rtlicense1.uits.indiana.edu``, so a deployment with
its own MATLAB license had no way to point containers at it.
"""

import json

import pytest
from girder.exceptions import ValidationException
from girder.models.setting import Setting
from pytest_girder.assertions import assertStatusOk

from girder_sivacor.settings import DEFAULT_MLM_LICENSE_FILE, PluginSettings
from girder_sivacor.worker_plugin.lib import (
    MASK,
    container_environment,
    recorded_environment,
)

MATLAB = "dynare/dynare:6.4"
OTHER = "27000@license.example.org"


class FakeApi:
    def __init__(self, settings=None):
        self._settings = settings or {}
        self.calls = 0

    def settings(self, keys):
        self.calls += 1
        return {key: self._settings.get(key) for key in keys}


def _env(api, image, user_env=None):
    return container_environment(api, image, "/workspace/home", user_env or {})


@pytest.mark.plugin("sivacor")
def test_the_default_is_the_server_every_deployment_used_before(server):
    assert Setting().get(PluginSettings.MLM_LICENSE_FILE) == DEFAULT_MLM_LICENSE_FILE
    assert DEFAULT_MLM_LICENSE_FILE == "27007@rtlicense1.uits.indiana.edu"


@pytest.mark.plugin("sivacor")
@pytest.mark.parametrize("value", ["", "   ", 123, None])
def test_an_invalid_value_is_refused(server, value):
    with pytest.raises(ValidationException):
        Setting().set(PluginSettings.MLM_LICENSE_FILE, value)


@pytest.mark.plugin("sivacor")
def test_surrounding_whitespace_is_stripped_on_save(server):
    Setting().set(PluginSettings.MLM_LICENSE_FILE, f"  {OTHER}\n")
    assert Setting().get(PluginSettings.MLM_LICENSE_FILE) == OTHER


@pytest.mark.plugin("sivacor")
def test_the_worker_can_read_it_with_an_admin_token(server, admin):
    resp = server.request(
        path="/system/setting",
        method="PUT",
        user=admin,
        params={"key": PluginSettings.MLM_LICENSE_FILE, "value": json.dumps(OTHER)},
    )
    assertStatusOk(resp)
    resp = server.request(
        path="/system/setting",
        method="GET",
        user=admin,
        params={"list": json.dumps([PluginSettings.MLM_LICENSE_FILE])},
    )
    assertStatusOk(resp)
    assert resp.json[PluginSettings.MLM_LICENSE_FILE] == OTHER


def test_a_matlab_stage_gets_the_configured_server():
    api = FakeApi({PluginSettings.MLM_LICENSE_FILE: OTHER})
    assert _env(api, MATLAB)["MLM_LICENSE_FILE"] == OTHER


@pytest.mark.parametrize(
    "image",
    ["rocker/r-ver:4.6.1", "sscng/stata18_5-mp:2025-01", "ghcr.io/sivacor/julia1.11:1"],
)
def test_other_stages_do_not_get_it_and_do_not_ask(image):
    api = FakeApi({PluginSettings.MLM_LICENSE_FILE: OTHER})
    assert "MLM_LICENSE_FILE" not in _env(api, image)
    assert api.calls == 0


@pytest.mark.parametrize("returned", [None, "", "   "])
def test_an_empty_value_falls_back_rather_than_dropping_matlab(returned):
    """A server predating the setting returns nothing for it; that must not
    leave dynare without a license server."""
    api = FakeApi({PluginSettings.MLM_LICENSE_FILE: returned})
    assert _env(api, MATLAB)["MLM_LICENSE_FILE"] == DEFAULT_MLM_LICENSE_FILE


def test_a_stage_can_still_override_it():
    api = FakeApi({PluginSettings.MLM_LICENSE_FILE: OTHER})
    env = _env(api, MATLAB, {"MLM_LICENSE_FILE": "1234@mine.example.org"})
    assert env["MLM_LICENSE_FILE"] == "1234@mine.example.org"


def test_the_tro_does_not_publish_the_license_server():
    """DockerRunArgs lands in the signed declaration; a deployment's own server
    may be an internal hostname or a path."""
    api = FakeApi({PluginSettings.MLM_LICENSE_FILE: OTHER})
    recorded = recorded_environment(_env(api, MATLAB), {})
    assert recorded["MLM_LICENSE_FILE"] == MASK
    assert OTHER not in recorded.values()
    assert recorded["HOME"] == "/workspace/home"


def test_secrets_are_still_masked_and_nothing_else_is():
    env = _env(FakeApi(), "rocker/r-ver:4.6.1", {"TOKEN": "s3cret"})
    recorded = recorded_environment(env, {"TOKEN": "s3cret"})
    assert recorded["TOKEN"] == MASK
    assert {k for k, v in recorded.items() if v == MASK} == {"TOKEN"}
