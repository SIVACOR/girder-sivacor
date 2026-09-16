"""The namespace key the image-size table is keyed by.

Registry-qualified references are new with the Julia images -- every other
family is a bare Docker Hub reference -- and the naive first-segment split
keys them on the registry host instead of the namespace.
"""

import pytest

from girder_sivacor.worker_plugin.lib import image_family, is_julia


@pytest.mark.parametrize(
    "reference,expected",
    [
        # Bare Docker Hub references: unchanged, and this is the half that must
        # not regress -- every existing entry in the size table is one of these.
        ("dataeditors/stata18-mp", "dataeditors"),
        ("rocker/r-ver:4.6.1", "rocker"),
        ("dynare/dynare:6.5-R2025b", "dynare"),
        # Registry-qualified: the host is stripped, so the key is the namespace.
        ("ghcr.io/sivacor/julia1.11", "sivacor"),
        ("ghcr.io/sivacor/julia1.11:1.11.9-20260916", "sivacor"),
        # A host with a port is still a host.
        ("registry.local:5000/team/image", "team"),
        # A single segment has no namespace to find.
        ("ubuntu", "ubuntu"),
        # "localhost" carries no dot, but neither does a namespace -- this is
        # the one case the dot/colon rule gets wrong, and it is recorded rather
        # than fixed because SIVACOR never pulls from an unqualified localhost.
        ("localhost/image", "localhost"),
    ],
)
def test_image_family(reference, expected):
    assert image_family(reference) == expected


@pytest.mark.parametrize(
    "reference,expected",
    [
        ("ghcr.io/sivacor/julia1.10:1.10.12-20260916", True),
        ("ghcr.io/sivacor/julia1.11:1.11.9-20260916", True),
        ("ghcr.io/sivacor/julia1.13:1.13.0-20260916", True),
        # Not ours: the official image is not allow-listed and must not be
        # mistaken for one of ours.
        ("julia:1.11.9-bookworm", False),
        ("ghcr.io/someoneelse/julia1.11", False),
        ("rocker/r-ver:4.6.1", False),
    ],
)
def test_is_julia(reference, expected):
    assert is_julia(reference) is expected


def test_the_julia_family_has_a_measured_entry():
    """10-P4: the size table must actually reach our images.

    The family key and the table entry were added in different phases, and the
    failure mode if they disagree is silent -- ``image_on_disk_estimate``
    returns ``None`` for an unknown family and the pull simply goes unchecked,
    which looks exactly like working.
    """
    from girder_sivacor.worker_plugin.lib import (
        IMAGE_ON_DISK_MULTIPLIER,
        _IMAGE_FAMILY_COMPRESSED_GB,
    )

    family = image_family("ghcr.io/sivacor/julia1.13:1.13.0-20260916")
    assert family in _IMAGE_FAMILY_COMPRESSED_GB

    # Bracketed rather than pinned: the point is that it is the smallest family
    # and stays that way. A Julia image big enough to break the upper bound
    # would mean packages had been baked back in, which is a decision to make
    # deliberately (10-D8), not to discover from a disk failure.
    compressed = _IMAGE_FAMILY_COMPRESSED_GB[family]
    assert 0.3 <= compressed <= 1.0
    assert compressed < min(
        v for k, v in _IMAGE_FAMILY_COMPRESSED_GB.items() if k != family
    )
    assert compressed * IMAGE_ON_DISK_MULTIPLIER < 2.0
