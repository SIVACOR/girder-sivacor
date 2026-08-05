"""Tests for the REST layer the remote workers reach Girder through."""

import mock
import pytest
from girder_client import HttpError
from girder_sivacor.worker_plugin.girder_api import GirderApi


def _api():
    """A GirderApi whose underlying girder-client is a stub."""
    api = GirderApi.__new__(GirderApi)
    api.client = mock.MagicMock()
    return api


def _conflict(url):
    return HttpError(
        status=400,
        text='{"field": "name", "message": "A collection with that name already exists."}',
        url=url,
        method="POST",
    )


def test_find_or_create_collection_returns_existing():
    api = _api()
    api.client.listCollection.return_value = [
        {"_id": "1", "name": "Other"},
        {"_id": "2", "name": "Submissions"},
    ]

    assert api.find_or_create_collection("Submissions")["_id"] == "2"
    api.client.createCollection.assert_not_called()


def test_find_or_create_collection_survives_a_concurrent_creator():
    """
    Two submissions starting at once both look, find nothing, and both create.

    Girder rejects the loser's duplicate name; it has to settle for whichever
    collection landed first rather than failing the submission.
    """
    api = _api()
    api.client.listCollection.side_effect = [
        [],  # nobody has created it yet...
        [{"_id": "2", "name": "Submissions"}],  # ...but by now the other task has
    ]
    api.client.createCollection.side_effect = _conflict("collection")

    assert api.find_or_create_collection("Submissions")["_id"] == "2"


def test_find_or_create_collection_reraises_when_nothing_appeared():
    """A validation error that is not a lost race still has to surface."""
    api = _api()
    api.client.listCollection.side_effect = [[], []]
    api.client.createCollection.side_effect = _conflict("collection")

    with pytest.raises(HttpError):
        api.find_or_create_collection("Submissions")


def test_find_or_create_group_survives_a_concurrent_creator():
    api = _api()
    api.client.listResource.side_effect = [
        iter([]),
        iter([{"_id": "7", "name": "Editors"}]),
    ]
    api.client.post.side_effect = _conflict("group")

    assert api.find_or_create_group("Editors")["_id"] == "7"


def test_find_or_create_collection_does_not_swallow_other_errors():
    api = _api()
    api.client.listCollection.return_value = []
    api.client.createCollection.side_effect = HttpError(
        status=403, text="denied", url="collection", method="POST"
    )

    with pytest.raises(HttpError) as exc_info:
        api.find_or_create_collection("Submissions")
    assert exc_info.value.status == 403
