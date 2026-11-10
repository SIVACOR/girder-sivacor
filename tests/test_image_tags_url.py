"""Where the image allow-list comes from is a setting (issue #151).

Every test points the cache into ``tmp_path`` and stubs the fetch: the real
cache is shared with anything else on the host's ``/tmp``, and the real URL is
on the network (see ``julia_is_allow_listed`` in ``test_julia.py``).
"""

import json
import os
from unittest import mock

import pytest
from girder.exceptions import ValidationException
from girder.models.setting import Setting

from girder_sivacor import rest
from girder_sivacor.rest import SIVACOR
from girder_sivacor.settings import PluginSettings

FORK = "https://example.org/our-fork/allowed_repos.yaml"
UPSTREAM_TAGS = {"rocker/r-ver": ["4.6.1"]}
FORK_TAGS = {"ourorg/analysis": ["1.0"]}


@pytest.fixture
def cache(tmp_path, monkeypatch):
    path = tmp_path / "image_tags.json"
    monkeypatch.setattr(rest, "IMAGE_TAGS_CACHE", str(path))
    return path


def _serve(url_to_tags):
    def get(url, timeout):
        response = mock.MagicMock()
        response.text = json.dumps(url_to_tags[url])  # JSON is valid YAML
        return response

    return mock.patch.object(rest.requests, "get", side_effect=get)


UPSTREAM = (
    "https://raw.githubusercontent.com/SIVACOR/sivacor-repo-choice"
    "/refs/heads/main/allowed_repos.yaml"
)


@pytest.mark.plugin("sivacor")
def test_by_default_nothing_is_fetched(server, cache):
    """A deployment's allow-list is its own until its operator says otherwise."""
    assert Setting().get(PluginSettings.IMAGE_TAGS_URL) == ""
    with mock.patch.object(rest.requests, "get") as get:
        assert SIVACOR._get_tags() == Setting().get(PluginSettings.IMAGE_TAGS)
    get.assert_not_called()


@pytest.mark.plugin("sivacor")
def test_the_list_is_fetched_from_the_configured_url(server, cache):
    Setting().set(PluginSettings.IMAGE_TAGS_URL, FORK)
    with _serve({FORK: FORK_TAGS}) as get:
        assert SIVACOR._get_tags() == FORK_TAGS
        # ...and the second call is served from the cache.
        assert SIVACOR._get_tags() == FORK_TAGS
    assert get.call_count == 1


@pytest.mark.plugin("sivacor")
def test_changing_the_url_invalidates_the_cache_at_once(server, cache):
    """Not up to four hours later: the cache records where it came from."""
    Setting().set(PluginSettings.IMAGE_TAGS_URL, UPSTREAM)
    with _serve({UPSTREAM: UPSTREAM_TAGS, FORK: FORK_TAGS}):
        assert SIVACOR._get_tags() == UPSTREAM_TAGS
        Setting().set(PluginSettings.IMAGE_TAGS_URL, FORK)
        assert SIVACOR._get_tags() == FORK_TAGS


@pytest.mark.plugin("sivacor")
def test_a_cache_from_before_the_setting_is_refetched(server, cache):
    """The old format was a bare tag dict, which names no source."""
    cache.write_text(json.dumps(UPSTREAM_TAGS))
    Setting().set(PluginSettings.IMAGE_TAGS_URL, FORK)
    with _serve({FORK: FORK_TAGS}):
        assert SIVACOR._get_tags() == FORK_TAGS


@pytest.mark.plugin("sivacor")
def test_a_stale_cache_is_refetched(server, cache):
    Setting().set(PluginSettings.IMAGE_TAGS_URL, FORK)
    cache.write_text(json.dumps({"source_url": FORK, "tags": UPSTREAM_TAGS}))
    five_hours_ago = cache.stat().st_mtime - 5 * 3600
    os.utime(cache, (five_hours_ago, five_hours_ago))
    with _serve({FORK: FORK_TAGS}):
        assert SIVACOR._get_tags() == FORK_TAGS


@pytest.mark.plugin("sivacor")
def test_a_failed_fetch_falls_back_to_the_setting(server, cache):
    Setting().set(PluginSettings.IMAGE_TAGS_URL, FORK)
    with mock.patch.object(rest.requests, "get", side_effect=OSError("offline")):
        assert SIVACOR._get_tags() == Setting().get(PluginSettings.IMAGE_TAGS)


@pytest.mark.plugin("sivacor")
def test_an_empty_url_never_fetches_and_uses_the_setting_as_is(server, cache):
    """For an air-gapped deployment, or one that curates its list by hand."""
    Setting().set(PluginSettings.IMAGE_TAGS_URL, "")
    Setting().set(PluginSettings.IMAGE_TAGS, FORK_TAGS)
    with mock.patch.object(rest.requests, "get") as get:
        assert SIVACOR._get_tags() == FORK_TAGS
        # No cache either, so an edit to the list applies immediately.
        Setting().set(PluginSettings.IMAGE_TAGS, UPSTREAM_TAGS)
        assert SIVACOR._get_tags() == UPSTREAM_TAGS
    get.assert_not_called()
    assert not cache.exists()


@pytest.mark.plugin("sivacor")
@pytest.mark.parametrize(
    "value", [None, 123, "ftp://example.org/x.yaml", "file:///etc/passwd"]
)
def test_a_url_that_is_not_http_is_refused(server, value):
    with pytest.raises(ValidationException):
        Setting().set(PluginSettings.IMAGE_TAGS_URL, value)


@pytest.mark.plugin("sivacor")
@pytest.mark.parametrize(
    "value",
    [
        [],
        {"rocker/r-ver": "4.6.1"},
        {"rocker/r-ver": [4.6]},
        {"": ["4.6.1"]},
        {"rocker/r-ver": [""]},
    ],
)
def test_a_malformed_image_list_is_refused(server, value):
    """The config page writes this by hand, so typos must not reach it."""
    with pytest.raises(ValidationException):
        Setting().set(PluginSettings.IMAGE_TAGS, value)
