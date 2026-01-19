import pytest
from girder.models.setting import Setting
from girder.models.user import User
from girder.settings import SettingKey
from girder_oauth.settings import PluginSettings
from girder_sivacor.auth.orcid import ORCID, RestException


@pytest.fixture()
def close_registration_policy(db):
    old_policy = Setting().get(SettingKey.REGISTRATION_POLICY)
    old_ignore = Setting().get(PluginSettings.IGNORE_REGISTRATION_POLICY)

    Setting().set(SettingKey.REGISTRATION_POLICY, "closed")
    Setting().set(PluginSettings.IGNORE_REGISTRATION_POLICY, False)

    yield

    Setting().set(SettingKey.REGISTRATION_POLICY, old_policy)
    Setting().set(PluginSettings.IGNORE_REGISTRATION_POLICY, old_ignore)


@pytest.mark.plugin("sivacor")
def test_getUser_creates_user_and_sets_oauth(server, monkeypatch):
    # _getJson returns expected emails and names
    token = {"access_token": "token", "orcid": "0000-0001"}
    resp = {
        "emails": {"email": [{"email": "user@example.com"}]},
        "name": {"family-name": {"value": "Doe"}, "given-names": {"value": "John"}},
    }
    monkeypatch.setattr(ORCID, "_getJson", lambda self, **kwargs: resp)

    orcid = ORCID("https://nowhere.org")
    user = orcid.getUser(token)

    assert any(
        o.get("provider") == "orcid" and o.get("id") == "0000-0001"
        for o in user.get("oauth", [])
    )
    assert user["email"] == "user@example.com"


@pytest.mark.plugin("sivacor")
def test_getUser_missing_email_uses_orcid_fallback(server, monkeypatch):
    token = {"access_token": "t2", "orcid": "0000-0002", "orcid_path": "0000-0002"}
    resp = {
        # no emails key
        "name": {"family-name": {"value": "Doe"}, "given-names": {"value": "John"}},
    }
    monkeypatch.setattr(ORCID, "_getJson", lambda self, **kwargs: resp)

    orcid = ORCID("https://nowhere.org")
    user = orcid.getUser(token)

    assert user["email"] == "0000-0002@orcid.org"
    assert any(o["id"] == "0000-0002" for o in user.get("oauth", []))


@pytest.mark.plugin("sivacor")
def test_getUser_missing_orcid_raises(server, monkeypatch):
    token = {"access_token": "t3", "orcid": "", "orcid_path": ""}
    resp = {
        "emails": {"email": [{"email": "x@example.org"}]},
        "name": {"family-name": {"value": "X"}, "given-names": {"value": "Y"}},
    }
    monkeypatch.setattr(ORCID, "_getJson", lambda self, **kwargs: resp)
    orcid = ORCID("https://nowhere.org")
    with pytest.raises(RestException):
        orcid.getUser(token)


@pytest.mark.plugin("sivacor")
def test_getUser_empty_names_raises(server, monkeypatch):
    token = {"access_token": "t4", "orcid": "0000-0004", "orcid_path": "0000-0004"}
    resp = {
        "emails": {"email": [{"email": "n@example.org"}]},
        "name": {"family-name": {"value": ""}, "given-names": {"value": ""}},
    }
    monkeypatch.setattr(ORCID, "_getJson", lambda self, **kwargs: resp)
    orcid = ORCID("https://nowhere.org")
    with pytest.raises(RestException):
        orcid.getUser(token)


@pytest.mark.plugin("sivacor")
def test_getUser_registration_closed_blocks_creation(
    server, close_registration_policy, monkeypatch
):
    token = {"access_token": "t5", "orcid": "0000-0005", "orcid_path": "0000-0005"}
    resp = {
        "emails": {"email": [{"email": "blocked@example.org"}]},
        "name": {"family-name": {"value": "Blocked"}, "given-names": {"value": "User"}},
    }
    monkeypatch.setattr(ORCID, "_getJson", lambda self, **kwargs: resp)

    orcid = ORCID("https://nowhere.org")
    with pytest.raises(RestException):
        orcid.getUser(token)


@pytest.mark.plugin("sivacor")
def test_getUser_updates_existing_user(server, monkeypatch):
    token = {"access_token": "t6", "orcid": "0000-0006", "orcid_path": "0000-0006"}
    resp = {
        "emails": {"email": [{"email": "old@example.org"}]},
        "name": {
            "family-name": {"value": "OldLast"},
            "given-names": {"value": "OldFirst"},
        },
    }
    monkeypatch.setattr(ORCID, "_getJson", lambda self, **kwargs: resp)
    orcid = ORCID("https://nowhere.org")
    user = orcid.getUser(token)

    user = User().load(user["_id"], force=True)

    resp = {
        "emails": {"email": [{"email": "newemail@example.org"}]},
        "name": {
            "family-name": {"value": "NewLast"},
            "given-names": {"value": "NewFirst"},
        },
    }
    monkeypatch.setattr(ORCID, "_getJson", lambda self, **kwargs: resp)
    orcid = ORCID("https://nowhere.org")
    newuser = orcid.getUser(token)
    newuser = User().load(newuser["_id"], force=True)
    assert newuser["_id"] == user["_id"]
    assert newuser["firstName"] == "NewFirst"
    assert newuser["lastName"] == "NewLast"
    assert newuser["oauth"][0]["id"] == "0000-0006"
