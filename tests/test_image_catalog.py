"""GET /sivacor/image_catalog: what software each allowed image runs.

The labels come from the same predicates that pick the entrypoint, so these
tests pin that agreement rather than a list of names -- an image the picker
labels as Stata must be one the worker runs as Stata.
"""

from unittest import mock

import pytest
from pytest_girder.assertions import assertStatusOk

from girder_sivacor.rest import SIVACOR
from girder_sivacor.worker_plugin.lib import image_software

TAGS = {
    "sscng/stata19-mp": ["2026-08-12"],
    "sscng/stata19_5-mp-i-python": ["2026-08-12"],
    "rocker/r-ver": ["4.6.1"],
    "dynare/dynare": ["6.5-R2025b"],
    "ghcr.io/sivacor/julia1.11": ["1.11.9-20261106"],
    "someone/else": ["latest"],
}


@pytest.mark.parametrize(
    "image, expected",
    [
        ("sscng/stata19-mp", ["stata"]),
        ("sscng/stata19_5-mp-i-python", ["stata", "python"]),
        ("sscng/stata18-mp-i-python", ["stata", "python"]),
        ("rocker/tidyverse", ["r"]),
        ("dynare/dynare", ["matlab"]),
        ("ghcr.io/sivacor/julia1.13", ["julia"]),
        # An -i-python suffix alone does not make an image Stata.
        ("someone/thing-i-python", []),
        # Not under a Stata namespace, so not run as Stata either.
        ("someone/stata18-mp", []),
    ],
)
def test_image_software(image, expected):
    assert image_software(image) == expected


@pytest.mark.plugin("sivacor")
def test_image_catalog_covers_every_allowed_image(server):
    """Keyed exactly as image_tags is, and public like it."""
    with mock.patch.object(SIVACOR, "_get_tags", staticmethod(lambda: TAGS)):
        resp = server.request(path="/sivacor/image_catalog", method="GET")
    assertStatusOk(resp)
    assert set(resp.json) == set(TAGS)
    assert resp.json["sscng/stata19_5-mp-i-python"] == {
        "software": ["stata", "python"]
    }
    assert resp.json["someone/else"] == {"software": []}
