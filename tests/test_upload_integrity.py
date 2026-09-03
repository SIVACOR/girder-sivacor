"""A submission is refused when the stored bytes disagree with the document.

Girder's chunked upload can finalise a file whose blob is *shorter* than the
``size`` on its document: two concurrent ``POST /file/chunk`` requests for one
upload let the aborted one's rollback delete bytes the other had already
written, while the ``received`` counter it finalises on keeps the advanced
value. It happened twice in production on 2026-09-02, to the same 1.66 GB
archive, losing 10 485 760 and 134 938 624 bytes. Mechanism, evidence and the
upstream fix: ``development_notes/girder_upload_race_plan.md``.

Nothing downstream notices until something reads the whole file, and by then a
fleet instance has been created and an image pulled. The failure surfaced on the
worker as ``ChunkedEncodingError`` in ``create_workspace``, a name that says
"network" and means "the file was never fully stored".

**The mismatch is simulated on the document, never on the blob.** The
filesystem assetstore is content-addressed, so truncating a real blob would
poison that content's path for every other test that uploads the same bytes --
and the suite runs at ``-n 4`` or wider, against a shared assetstore. Raising
``file['size']`` produces exactly the state under test (a document claiming more
bytes than the blob holds) and touches only this test's database.
"""

import mock
import pytest
from girder.exceptions import ValidationException
from girder.models.file import File
from girder.models.user import User
from girder_jobs.models.job import Job
from girder_sivacor.rest import stored_blob_size, verify_upload_complete
from pytest_girder.assertions import assertStatus, assertStatusOk

from .conftest import submit_sivacor_job, upload_test_file

STAGES = [
    {"image_name": "rocker/r-ver", "image_tag": "4.3.1", "main_file": "main.R"}
]

#: Any real upload will do -- nothing here runs the package, and the checks are
#: about byte counts. Matches what test_targeted_assignment submits.
FIXTURE = "with_space_R.zip"

#: What the first production occurrence lost: two of the frontend's 5 MiB
#: upload chunks. Used as the shortfall so the assertions read against a figure
#: that actually happened rather than an invented one.
SHORTFALL = 2 * 5 * 1024 * 1024


def _claim_extra_bytes(file, extra=SHORTFALL):
    """Make ``file``'s document claim ``extra`` bytes more than its blob has."""
    file["size"] += extra
    return File().save(file)


# --- stored_blob_size ------------------------------------------------------


@pytest.mark.plugin("sivacor")
def test_stored_blob_size_reads_the_real_blob(server, db, user, fsAssetstore, uploads_folder):
    file = upload_test_file(uploads_folder, user, FIXTURE)
    assert stored_blob_size(file) == file["size"]


@pytest.mark.plugin("sivacor")
def test_stored_blob_size_reports_a_missing_blob_as_zero(
    server, db, user, fsAssetstore, uploads_folder
):
    """Zero, not None: the absence of a blob is a definite answer.

    ``None`` means "cannot tell, do not act"; a file document pointing at
    nothing is the one case where the missing file *is* the finding.
    """
    file = upload_test_file(uploads_folder, user, FIXTURE)
    file["path"] = "de/ad/beef"
    file = File().save(file)
    assert stored_blob_size(file) == 0


@pytest.mark.plugin("sivacor")
def test_stored_blob_size_declines_to_answer_for_a_link_file(
    server, db, user, fsAssetstore, uploads_folder
):
    file = upload_test_file(uploads_folder, user, FIXTURE)
    file["linkUrl"] = "https://example.org/thing.tar.gz"
    del file["assetstoreId"]
    file = File().save(file)
    assert stored_blob_size(file) is None


@pytest.mark.plugin("sivacor")
def test_stored_blob_size_declines_to_answer_without_a_path(
    server, db, user, fsAssetstore, uploads_folder
):
    file = upload_test_file(uploads_folder, user, FIXTURE)
    del file["path"]
    file = File().save(file)
    assert stored_blob_size(file) is None


# --- verify_upload_complete ------------------------------------------------


@pytest.mark.plugin("sivacor")
def test_a_complete_upload_passes(server, db, user, fsAssetstore, uploads_folder):
    file = upload_test_file(uploads_folder, user, FIXTURE)
    verify_upload_complete(file)  # must not raise


@pytest.mark.plugin("sivacor")
def test_a_short_blob_is_refused(server, db, user, fsAssetstore, uploads_folder):
    file = _claim_extra_bytes(upload_test_file(uploads_folder, user, FIXTURE))
    with pytest.raises(ValidationException) as excinfo:
        verify_upload_complete(file)
    message = str(excinfo.value)
    # Both figures, because the remedy is only obvious when the reader can see
    # how far short the upload fell.
    assert f"{file['size']:,}" in message
    assert f"{file['size'] - SHORTFALL:,}" in message
    assert "upload it" in message


@pytest.mark.plugin("sivacor")
def test_an_unverifiable_file_is_allowed(server, db, user, fsAssetstore, uploads_folder):
    """Fails open. This is a detector for one corruption, not an access check.

    A deployment whose assetstore cannot be statted -- S3, or a path this
    process cannot read -- must keep accepting submissions. The download is
    still there to fail if something really is wrong.
    """
    file = upload_test_file(uploads_folder, user, FIXTURE)
    file["size"] += SHORTFALL
    del file["path"]
    file = File().save(file)
    verify_upload_complete(file)  # unknowable, so no opinion


@pytest.mark.plugin("sivacor")
def test_a_file_with_no_size_is_allowed(server, db, user, fsAssetstore, uploads_folder):
    file = upload_test_file(uploads_folder, user, FIXTURE)
    del file["size"]
    file = File().save(file)
    verify_upload_complete(file)


# --- the submit_job boundary -----------------------------------------------


@pytest.mark.plugin("sivacor")
def test_submit_job_refuses_a_short_upload(server, db, user, fsAssetstore, uploads_folder):
    """400 rather than a doomed run, and *nothing* created.

    The whole point of checking here is that it happens before a job document
    exists -- so no submission folder, no fleet instance, no image pull, and no
    30-minute wait for an error that names the wrong subsystem.
    """
    file = _claim_extra_bytes(upload_test_file(uploads_folder, user, FIXTURE))
    resp = submit_sivacor_job(server, user, file, STAGES, exception=True)
    assertStatus(resp, 400)
    assert "did not upload completely" in resp.json["message"]
    assert Job().collection.count_documents({"type": "sivacor_submission"}) == 0


@pytest.mark.plugin("sivacor")
def test_submit_job_still_accepts_a_healthy_upload(
    server, db, user, fsAssetstore, uploads_folder, submission_collection
):
    """The guard must not cost anything for the normal case.

    Every other submission test would catch a false positive here, but they all
    also run a container; this one asserts the accept path on its own so a
    regression in the check is not mistaken for a docker problem.
    """
    file = upload_test_file(uploads_folder, user, FIXTURE)
    with mock.patch("celery.canvas._chain.apply_async"):
        resp = submit_sivacor_job(server, user, file, STAGES)
    assertStatusOk(resp)


# --- GET /sivacor/upload_integrity -----------------------------------------


@pytest.mark.plugin("sivacor")
def test_integrity_endpoint_confirms_a_healthy_upload(
    server, db, user, fsAssetstore, uploads_folder
):
    file = upload_test_file(uploads_folder, user, FIXTURE)
    resp = server.request(
        path="/sivacor/upload_integrity",
        user=user,
        params={"id": str(file["_id"])},
    )
    assertStatusOk(resp)
    assert resp.json == {
        "declared": file["size"],
        "stored": file["size"],
        "complete": True,
    }


@pytest.mark.plugin("sivacor")
def test_integrity_endpoint_reports_a_short_blob(
    server, db, user, fsAssetstore, uploads_folder
):
    """The endpoint exists because the client cannot work this out itself.

    A file document's ``size`` is what the upload *declared*, which is the very
    value the race corrupts -- so the uploader comparing ``GET /file/:id``
    against its local ``File.size`` would agree with itself no matter how few
    bytes were stored. Only the server can see both numbers.
    """
    stored = upload_test_file(uploads_folder, user, FIXTURE)["size"]
    file = _claim_extra_bytes(upload_test_file(uploads_folder, user, FIXTURE))
    resp = server.request(
        path="/sivacor/upload_integrity",
        user=user,
        params={"id": str(file["_id"])},
    )
    assertStatusOk(resp)
    assert resp.json == {
        "declared": stored + SHORTFALL,
        "stored": stored,
        "complete": False,
    }


@pytest.mark.plugin("sivacor")
def test_integrity_endpoint_says_complete_when_it_cannot_tell(
    server, db, user, fsAssetstore, uploads_folder
):
    """'Cannot tell' must not read as 'broken' -- it would block every
    submission on an assetstore this process cannot stat."""
    file = upload_test_file(uploads_folder, user, FIXTURE)
    del file["path"]
    file = File().save(file)
    resp = server.request(
        path="/sivacor/upload_integrity",
        user=user,
        params={"id": str(file["_id"])},
    )
    assertStatusOk(resp)
    assert resp.json["stored"] is None
    assert resp.json["complete"] is True


@pytest.mark.plugin("sivacor")
def test_integrity_endpoint_needs_read_access(
    server, db, user, admin, fsAssetstore, uploads_folder
):
    """Another user's upload is not theirs to interrogate."""
    file = upload_test_file(uploads_folder, user, FIXTURE)
    other = User().createUser(
        login="nosy", password="arglebargle123", firstName="No", lastName="Sy",
        email="nosy@example.org",
    )
    resp = server.request(
        path="/sivacor/upload_integrity",
        user=other,
        params={"id": str(file["_id"])},
    )
    assertStatus(resp, 403)
