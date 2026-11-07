"""Stata under the namespace upstream moved it to (sivacor-repo-choice#7).

The Stata images moved from ``dataeditors`` to ``sscng`` on Docker Hub. The
allow-list switches the moment that PR merges -- ``_get_tags`` reads it from
the repo's main branch, so production follows within its four-hour cache, with
no deploy -- and every Stata-specific step in the worker used to key on the
literal ``dataeditors/stata`` prefix. An ``sscng`` image then failed at the
entrypoint, and with that fixed alone, it would have run unlicensed and had an
``r(NNN)`` that exits 0 certified as a success.

Kept apart from ``test_stata.py`` so its check line keeps meaning "5 passed".
"""

from unittest import mock

import pytest
from girder.models.file import File
from girder_jobs.constants import JobStatus
from girder_jobs.models.job import Job
from pytest_girder.assertions import assertStatusOk

from girder_sivacor.rest import SIVACOR

from .conftest import get_submission_folder, submit_sivacor_job, upload_test_file

IMAGE = "sscng/stata18_5-mp"
TAG = "2025-02-26"


@pytest.fixture(autouse=True)
def sscng_is_allow_listed():
    """Allow-list the new namespace whatever ``main`` currently says.

    Stubbed rather than seeded for the reasons ``test_julia.py``'s fixture
    gives: the cache file is shared host state, and this keeps the network out.
    """
    with mock.patch.object(
        SIVACOR, "_get_tags", staticmethod(lambda: {IMAGE: [TAG]})
    ):
        yield


def _log(job):
    # Job().load omits the log unless asked.
    logged = Job().load(job["_id"], force=True, includeLog=True)
    return "".join(logged.get("log") or [])


def _stdout(server, user, job, submission_collection):
    resp = get_submission_folder(server, user, job["_id"], submission_collection)
    assertStatusOk(resp)
    stdout = File().load(resp.json[0]["meta"]["stdout_file_id"], force=True)
    with File().open(stdout) as f:
        return f.read().decode("utf-8")


@pytest.mark.plugin("sivacor")
def test_an_sscng_stata_image_runs_licensed(
    server,
    db,
    user,
    eagerWorkerTasks,
    fsAssetstore,
    patched_gpg,
    uploads_folder,
    submission_collection,
):
    """Entrypoint and license mount both recognise the new namespace."""
    stages = [{"image_name": IMAGE, "image_tag": TAG, "main_file": "main.do"}]
    fobj = upload_test_file(uploads_folder, user, "test_stata.tar.gz")

    resp = submit_sivacor_job(server, user, fobj, stages)
    assertStatusOk(resp)
    job = Job().load(resp.json["_id"], force=True)

    assert job["status"] == JobStatus.SUCCESS
    assert f"{IMAGE}:{TAG}" in _log(job)


@pytest.mark.plugin("sivacor")
def test_an_sscng_stata_error_is_still_a_failure(
    server,
    db,
    user,
    eagerWorkerTasks,
    fsAssetstore,
    patched_gpg,
    uploads_folder,
    submission_collection,
):
    """Stata exits 0 on ``r(199)``; only ``is_stata`` turns that into a failure."""
    stages = [{"image_name": IMAGE, "image_tag": TAG, "main_file": "fail.do"}]
    fobj = upload_test_file(uploads_folder, user, "test_stata.tar.gz")

    resp = submit_sivacor_job(server, user, fobj, stages, exception=True)
    assertStatusOk(resp)
    job = Job().load(resp.json["_id"], force=True)

    assert job["status"] == JobStatus.ERROR
    assert "Stata returned an error" in _log(job)
    assert "command command_does_not_exist is unrecognized" in _stdout(
        server, user, job, submission_collection
    )
