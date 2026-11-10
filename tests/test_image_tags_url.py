"""Where the image allow-list comes from is a setting (issue #151).

Every test points the cache into ``tmp_path`` and stubs the fetch: the real
cache is shared with anything else on the host's ``/tmp``, and the real URL is
on the network (see ``julia_is_allow_listed`` in ``test_julia.py``).
"""

import datetime
import json
import threading
from unittest import mock

import pytest
from girder.exceptions import ValidationException
from girder.models.setting import Setting
from pytest_girder.assertions import assertStatus, assertStatusOk

from girder_sivacor import image_tags
from girder_sivacor.rest import SIVACOR
from girder_sivacor.settings import PluginSettings

FORK = "https://example.org/our-fork/allowed_repos.yaml"
UPSTREAM = (
    "https://raw.githubusercontent.com/SIVACOR/sivacor-repo-choice"
    "/refs/heads/main/allowed_repos.yaml"
)
UPSTREAM_TAGS = {"rocker/r-ver": ["4.6.1"]}
FORK_TAGS = {"ourorg/analysis": ["1.0"]}
FALLBACK = {"rocker/r-ver": ["4.5.3"]}


@pytest.fixture
def cache(tmp_path, monkeypatch):
    path = tmp_path / "image_tags.json"
    monkeypatch.setattr(image_tags, "CACHE", str(path))
    return path


def _serve(url_to_body):
    """Serve each URL's body: a dict as JSON (which is YAML), a str verbatim."""

    def get(url, timeout):
        body = url_to_body[url]
        response = mock.MagicMock()
        response.text = body if isinstance(body, str) else json.dumps(body)
        return response

    return mock.patch.object(image_tags.requests, "get", side_effect=get)


def _age_cache(cache, by):
    record = json.loads(cache.read_text())
    checked = datetime.datetime.fromisoformat(record["checked_at"]) - by
    record["checked_at"] = checked.isoformat()
    cache.write_text(json.dumps(record))


# --- where the list comes from ---------------------------------------------


@pytest.mark.plugin("sivacor")
def test_by_default_nothing_is_fetched(server, cache):
    """A deployment's allow-list is its own until its operator says otherwise."""
    assert Setting().get(PluginSettings.IMAGE_TAGS_URL) == ""
    with mock.patch.object(image_tags.requests, "get") as get:
        assert SIVACOR._get_tags() == Setting().get(PluginSettings.IMAGE_TAGS)
    get.assert_not_called()
    assert image_tags.status()["source"] == "setting"


@pytest.mark.plugin("sivacor")
def test_the_list_is_fetched_from_the_configured_url(server, cache):
    Setting().set(PluginSettings.IMAGE_TAGS_URL, FORK)
    with _serve({FORK: FORK_TAGS}) as get:
        assert SIVACOR._get_tags() == FORK_TAGS
        # ...and the second call is served from the cache.
        assert SIVACOR._get_tags() == FORK_TAGS
    assert get.call_count == 1
    assert image_tags.status()["source"] == "url"


@pytest.mark.plugin("sivacor")
def test_changing_the_url_invalidates_the_cache_at_once(server, cache):
    """Not up to four hours later: the cache records where it came from."""
    Setting().set(PluginSettings.IMAGE_TAGS_URL, UPSTREAM)
    with _serve({UPSTREAM: UPSTREAM_TAGS, FORK: FORK_TAGS}):
        assert SIVACOR._get_tags() == UPSTREAM_TAGS
        Setting().set(PluginSettings.IMAGE_TAGS_URL, FORK)
        assert SIVACOR._get_tags() == FORK_TAGS


@pytest.mark.plugin("sivacor")
@pytest.mark.parametrize(
    "old",
    [
        UPSTREAM_TAGS,  # the original bare dict
        {"source_url": FORK, "tags": UPSTREAM_TAGS},  # this branch's first cut
    ],
)
def test_a_cache_from_an_older_release_is_refetched(server, cache, old):
    cache.write_text(json.dumps(old))
    Setting().set(PluginSettings.IMAGE_TAGS_URL, FORK)
    with _serve({FORK: FORK_TAGS}):
        assert SIVACOR._get_tags() == FORK_TAGS


@pytest.mark.plugin("sivacor")
def test_a_stale_cache_is_refetched(server, cache):
    Setting().set(PluginSettings.IMAGE_TAGS_URL, FORK)
    with _serve({FORK: UPSTREAM_TAGS}):
        SIVACOR._get_tags()
    _age_cache(cache, image_tags.REFRESH + datetime.timedelta(minutes=1))
    with _serve({FORK: FORK_TAGS}):
        assert SIVACOR._get_tags() == FORK_TAGS


@pytest.mark.plugin("sivacor")
def test_an_empty_url_never_fetches_and_uses_the_setting_as_is(server, cache):
    """For an air-gapped deployment, or one that curates its list by hand."""
    Setting().set(PluginSettings.IMAGE_TAGS_URL, "")
    Setting().set(PluginSettings.IMAGE_TAGS, FORK_TAGS)
    with mock.patch.object(image_tags.requests, "get") as get:
        assert SIVACOR._get_tags() == FORK_TAGS
        # No cache either, so an edit to the list applies immediately.
        Setting().set(PluginSettings.IMAGE_TAGS, UPSTREAM_TAGS)
        assert SIVACOR._get_tags() == UPSTREAM_TAGS
    get.assert_not_called()
    assert not cache.exists()


# --- a failed fetch is a failure, not a list --------------------------------


@pytest.mark.plugin("sivacor")
def test_a_failed_fetch_serves_the_live_fallback_and_says_so(server, cache):
    """The fallback is read on every call, so an edit to it applies at once --
    it used to be frozen into the cache under the URL for four hours."""
    Setting().set(PluginSettings.IMAGE_TAGS_URL, FORK)
    Setting().set(PluginSettings.IMAGE_TAGS, FALLBACK)
    with mock.patch.object(
        image_tags.requests, "get", side_effect=OSError("offline")
    ) as get:
        state = image_tags.status()
        assert state["source"] == "fallback"
        assert "offline" in state["error"]
        assert state["tags"] == FALLBACK
        Setting().set(PluginSettings.IMAGE_TAGS, UPSTREAM_TAGS)
        assert SIVACOR._get_tags() == UPSTREAM_TAGS
    # ...without retrying the URL on every call, which would put a timeout in
    # front of every submit_job during an outage.
    assert get.call_count == 1


@pytest.mark.plugin("sivacor")
def test_a_failed_fetch_is_retried_in_minutes_not_hours(server, cache):
    Setting().set(PluginSettings.IMAGE_TAGS_URL, FORK)
    with mock.patch.object(image_tags.requests, "get", side_effect=OSError("x")):
        image_tags.status()
    _age_cache(cache, image_tags.RETRY + datetime.timedelta(seconds=1))
    assert image_tags.RETRY < image_tags.REFRESH
    with _serve({FORK: FORK_TAGS}):
        assert image_tags.status()["source"] == "url"
        assert SIVACOR._get_tags() == FORK_TAGS


# --- the fetched file is held to the setting's shape -------------------------


@pytest.mark.plugin("sivacor")
@pytest.mark.parametrize(
    "body",
    [
        "",  # an empty file: safe_load gives None, and submit_job 500'd
        "rocker/r-ver: 4.6.1\n",  # a string, allow-listing every substring
        "- rocker/r-ver\n",  # a list, not a mapping
        "rocker/r-ver: []\n",  # allows nothing
        "rocker/r-ver: [4.6.1\n",  # not YAML at all
    ],
)
def test_a_malformed_fetched_file_is_a_failed_fetch(server, cache, body):
    Setting().set(PluginSettings.IMAGE_TAGS_URL, FORK)
    Setting().set(PluginSettings.IMAGE_TAGS, FALLBACK)
    with _serve({FORK: body}):
        state = image_tags.status()
    assert state["source"] == "fallback"
    assert state["tags"] == FALLBACK
    assert state["error"]


@pytest.mark.plugin("sivacor")
def test_unquoted_scalars_are_read_as_the_text_written(server, cache):
    """safe_load reads 2026-08-12 as a date (which broke the JSON cache) and
    4.10 as the float 4.1 -- allow-listing a tag the file never named."""
    Setting().set(PluginSettings.IMAGE_TAGS_URL, FORK)
    with _serve({FORK: "dataeditors/stata19-mp:\n- 2026-08-12\nrocker/r-ver:\n- 4.10\n"}):
        tags = SIVACOR._get_tags()
    assert tags == {"dataeditors/stata19-mp": ["2026-08-12"], "rocker/r-ver": ["4.10"]}
    assert json.loads(cache.read_text())["tags"] == tags


# --- the cache file ---------------------------------------------------------


@pytest.mark.plugin("sivacor")
def test_concurrent_refreshes_leave_one_valid_cache_and_no_debris(server, cache):
    """A pid-derived temp name is shared by every thread in one worker."""
    Setting().set(PluginSettings.IMAGE_TAGS_URL, FORK)
    errors = []
    start = threading.Barrier(16)

    def refresh():
        try:
            start.wait()
            image_tags._write_cache(
                {
                    "version": image_tags.CACHE_VERSION,
                    "source_url": FORK,
                    "checked_at": datetime.datetime.now(datetime.UTC).isoformat(),
                    "tags": FORK_TAGS,
                    "error": None,
                }
            )
        except Exception as exc:  # pragma: no cover - the failure being tested
            errors.append(exc)

    threads = [threading.Thread(target=refresh) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert json.loads(cache.read_text())["tags"] == FORK_TAGS
    assert [p.name for p in cache.parent.iterdir()] == [cache.name]


@pytest.mark.plugin("sivacor")
def test_an_unwritable_cache_does_not_fail_the_request(server, cache, monkeypatch):
    monkeypatch.setattr(image_tags, "CACHE", str(cache.parent / "missing" / "c.json"))
    Setting().set(PluginSettings.IMAGE_TAGS_URL, FORK)
    with _serve({FORK: FORK_TAGS}):
        assert SIVACOR._get_tags() == FORK_TAGS


# --- validation -------------------------------------------------------------


@pytest.mark.plugin("sivacor")
def test_the_url_is_stored_stripped(server):
    """Setting.validate ignores a validator's return value, so the stripped
    value has to be written back into the document."""
    Setting().set(PluginSettings.IMAGE_TAGS_URL, f"  {FORK}  ")
    assert Setting().get(PluginSettings.IMAGE_TAGS_URL) == FORK
    Setting().set(PluginSettings.IMAGE_TAGS_URL, "   ")
    assert Setting().get(PluginSettings.IMAGE_TAGS_URL) == ""


@pytest.mark.plugin("sivacor")
@pytest.mark.parametrize(
    "value",
    [None, 123, "ftp://example.org/x.yaml", "file:///etc/passwd", "https://", "http:///x"],
)
def test_a_url_that_is_not_http_with_a_host_is_refused(server, value):
    with pytest.raises(ValidationException):
        Setting().set(PluginSettings.IMAGE_TAGS_URL, value)


@pytest.mark.plugin("sivacor")
@pytest.mark.parametrize(
    "value",
    [
        [],
        {},
        {"rocker/r-ver": "4.6.1"},
        {"rocker/r-ver": [4.6]},
        {"rocker/r-ver": []},
        {"": ["4.6.1"]},
        {"rocker/r-ver": [""]},
    ],
)
def test_a_malformed_image_list_is_refused(server, value):
    """The config page writes this by hand, so typos must not reach it."""
    with pytest.raises(ValidationException):
        Setting().set(PluginSettings.IMAGE_TAGS, value)


# --- the endpoints the config page uses -------------------------------------


@pytest.mark.plugin("sivacor")
def test_saving_both_is_all_or_nothing(server, admin):
    """PUT /system/setting saves a list one entry at a time, so a refused list
    used to leave the new URL saved and live."""
    resp = server.request(
        path="/sivacor/image_tags/config",
        method="PUT",
        user=admin,
        type="application/json",
        body=json.dumps({"url": FORK, "tags": {"rocker/r-ver": "4.6.1"}}),
    )
    assertStatus(resp, 400)
    assert Setting().get(PluginSettings.IMAGE_TAGS_URL) == ""


@pytest.mark.plugin("sivacor")
def test_saving_both_returns_the_resulting_status(server, admin, cache):
    with _serve({FORK: FORK_TAGS}):
        resp = server.request(
            path="/sivacor/image_tags/config",
            method="PUT",
            user=admin,
            type="application/json",
            body=json.dumps({"url": f" {FORK} ", "tags": FALLBACK}),
        )
    assertStatusOk(resp)
    assert resp.json["source"] == "url"
    assert resp.json["url"] == FORK
    assert Setting().get(PluginSettings.IMAGE_TAGS) == FALLBACK


@pytest.mark.plugin("sivacor")
def test_the_config_endpoints_are_admin_only(server, user):
    resp = server.request(path="/sivacor/image_tags/status", user=user)
    assertStatus(resp, 403)
    resp = server.request(
        path="/sivacor/image_tags/config",
        method="PUT",
        user=user,
        type="application/json",
        body=json.dumps({"url": "", "tags": FALLBACK}),
    )
    assertStatus(resp, 403)
