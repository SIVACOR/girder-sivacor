"""The support address is a setting, not a literal (issue #152).

A third party standing up the stack from deploy-sivacor found every "contact
support" in it pointing at SIVACOR's own mailbox. These tests set a different
address and check that each kind of consumer -- REST refusals, worker failure
messages, mail, and the public settings the web UI reads -- follows it.
"""

import json
from unittest import mock

import pytest
from girder.exceptions import ValidationException
from girder.models.setting import Setting
from pytest_girder.assertions import assertStatus, assertStatusOk

from girder_sivacor.rest import resolve_volume_gb
from girder_sivacor.settings import DEFAULT_SUPPORT_EMAIL, PluginSettings
from girder_sivacor.worker_plugin.lib import DISK_FLOOR_BYTES, disk_shortfall

OTHER = "help@example.org"


@pytest.mark.plugin("sivacor")
def test_the_default_is_the_address_every_deployment_used_before(server):
    """No deployment change: an unset setting is today's address."""
    assert Setting().get(PluginSettings.SUPPORT_EMAIL) == "support@sivacor.org"
    assert DEFAULT_SUPPORT_EMAIL == "support@sivacor.org"


@pytest.mark.plugin("sivacor")
@pytest.mark.parametrize("value", ["", "not an address", 123, None])
def test_an_invalid_address_is_refused(server, value):
    with pytest.raises(ValidationException):
        Setting().set(PluginSettings.SUPPORT_EMAIL, value)


@pytest.mark.plugin("sivacor")
def test_it_is_public_so_the_web_ui_can_read_it(server, admin):
    resp = server.request(path="/system/public_settings", method="GET")
    assertStatusOk(resp)
    assert resp.json[PluginSettings.SUPPORT_EMAIL] == DEFAULT_SUPPORT_EMAIL

    resp = server.request(
        path="/system/setting",
        method="PUT",
        user=admin,
        params={"key": PluginSettings.SUPPORT_EMAIL, "value": json.dumps(OTHER)},
    )
    assertStatusOk(resp)
    # Anonymous, like the banner: the login page has a Contact Support link.
    resp = server.request(path="/system/public_settings", method="GET")
    assertStatusOk(resp)
    assert resp.json[PluginSettings.SUPPORT_EMAIL] == OTHER


@pytest.mark.plugin("sivacor")
def test_rest_refusals_quote_the_configured_address(server, user):
    Setting().set(PluginSettings.SUPPORT_EMAIL, OTHER)
    Setting().set(PluginSettings.VOLUMES_ENABLED, True)
    Setting().set(PluginSettings.VOLUME_TOTAL_GB, 500)
    Setting().set(PluginSettings.TARGETED_ASSIGNMENT, True)
    with pytest.raises(ValidationException) as exc:
        resolve_volume_gb({"resources": {"disk_gb": 100}}, user)
    assert OTHER in str(exc.value)
    assert DEFAULT_SUPPORT_EMAIL not in str(exc.value)


@pytest.mark.plugin("sivacor")
def test_approval_mails_link_the_configured_address(server, user):
    from girder_sivacor import send_approval_email, send_approved_email

    Setting().set(PluginSettings.SUPPORT_EMAIL, OTHER)
    for send in (send_approval_email, send_approved_email):
        event = mock.MagicMock(info={"user": user})
        with mock.patch("girder_sivacor.events.trigger") as trigger:
            send(event)
        body = trigger.call_args.kwargs["info"]["message"].as_string()
        assert f"mailto:{OTHER}" in body
        assert DEFAULT_SUPPORT_EMAIL not in body


def test_worker_messages_quote_the_address_carried_on_the_submission(tmp_path):
    """prepare_submission puts the address on the submission dict."""
    usage = mock.MagicMock(free=DISK_FLOOR_BYTES // 2)
    with mock.patch(
        "girder_sivacor.worker_plugin.lib.shutil.disk_usage", return_value=usage
    ):
        carried = disk_shortfall(
            {"workspace_dir": str(tmp_path), "support_email": OTHER}
        )
        # Started before this change, or the public_settings read failed.
        legacy = disk_shortfall({"workspace_dir": str(tmp_path)})
    assert OTHER in carried
    assert DEFAULT_SUPPORT_EMAIL not in carried
    assert DEFAULT_SUPPORT_EMAIL in legacy


@pytest.mark.plugin("sivacor")
def test_setting_it_via_rest_rejects_garbage(server, admin):
    resp = server.request(
        path="/system/setting",
        method="PUT",
        user=admin,
        params={"key": PluginSettings.SUPPORT_EMAIL, "value": json.dumps("nope")},
    )
    assertStatus(resp, 400)


def test_the_worker_reads_it_from_public_settings_and_never_fails_on_it():
    """A remote worker has no Mongo, but public_settings needs no admin token."""
    from girder_sivacor.worker_plugin.run_submission import _support_email

    api = mock.MagicMock()
    api.public_settings.return_value = {PluginSettings.SUPPORT_EMAIL: OTHER}
    assert _support_email(api) == OTHER

    api.public_settings.side_effect = RuntimeError("girder is down")
    assert _support_email(api) is None
