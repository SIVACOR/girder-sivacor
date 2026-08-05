import pytest
from girder_sivacor.rest import stage_schema
from pytest_girder.assertions import assertStatusOk


@pytest.mark.plugin("sivacor")
def test_workflow_schema_is_served_verbatim(server):
    """The endpoint hands out exactly the schema submit_job validates against.

    The submission UI validates an imported YAML/JSON workflow with it, so any
    drift between the two would let the UI accept a definition the server then
    rejects (or the other way round).
    """
    resp = server.request(path="/sivacor/workflow_schema", method="GET")
    assertStatusOk(resp)
    assert resp.json == stage_schema


@pytest.mark.plugin("sivacor")
def test_workflow_schema_available_without_auth(server):
    """No auth token: the form is rendered before/without a login in some flows."""
    resp = server.request(path="/sivacor/workflow_schema", method="GET")
    assertStatusOk(resp)
    assert resp.json["required"] == ["stages"]
    stage = resp.json["properties"]["stages"]["items"]
    assert set(stage["properties"]) == {
        "image_name",
        "main_file",
        "image_tag",
        "network_isolation",
    }
