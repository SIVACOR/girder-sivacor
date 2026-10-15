"""Regression tests for the upstream Girder chunked-upload data loss.

**Nothing here tests SIVACOR code.** Every call is Girder's own upload model.
This file exists because we hit the bug in production, and it now pins the
upstream fix so a dependency floor that slips back below it fails here rather
than in an assetstore.

The bug, in one line: ``FilesystemAssetstoreAdapter.uploadChunk`` rolled a
failed chunk back with ``tempFile.truncate(upload['received'])``, using the
``received`` value from *its own* in-memory copy of the upload document — which
by then could be many chunks behind, so the rollback deleted bytes that other
requests wrote and already counted.

Production consequence, twice on 2026-09-02: an upload finalises with a blob
short of its document's ``size``, and nothing notices until something reads the
whole file. Full write-up and log evidence:
``development_notes/08_girder_upload_race_plan.md``.

**Fixed upstream in girder/girder#3934** ("Prevent concurrent chunk
collisions", fixing girder/girder#3925), first released in **girder 5.0.19**,
which ``setup.py`` now requires as a floor. The fix advances ``received`` with
a database update rather than a save of a stale document, and truncates to the
temp file's actual size. Both tests below were ``xfail(strict=True)`` until
2026-10-15; they are plain assertions now. A failure here means the floor was
lowered or upstream regressed — it does **not** mean SIVACOR broke.

The SIVACOR-side mitigation (``verify_upload_complete`` in ``rest.py`` and
``GET /sivacor/upload_integrity``) is deliberately kept: it is cheap, it covers
deployments upgrading across the floor, and it is the only thing that detects a
short blob already on disk.

**Only the half reproduced here is fixed upstream.** A rejected chunk that
interleaves with a legitimate one still finalises a file at *exactly* its
document's length with the wrong bytes inside it — measured against released
5.0.19 on 2026-10-15. These tests do not cover that; the minimal reproduction is
``wholetale-ng/girder/test/test_upload_chunk_interleave.py``. No length check
can see it, and only a full re-hash of the blob can. Open item 0 of the plan.

**No threads are needed to demonstrate it.** Concurrency is only how two
requests come to hold the same ``received``; the defect was what the rollback
then did. Holding a stale copy of the document reproduced it deterministically,
which is the form upstream acted on.
"""

import copy
import io
import os

import pytest
from girder.exceptions import ValidationException
from girder.models.upload import Upload
from girder.utility import RequestBodyStream

#: Girder's own default for ``core.upload_minimum_chunk_size``. A non-final
#: chunk smaller than this is what fails validation and takes the rollback path.
CHUNK = 1024 * 1024 * 5

#: Deliberately smaller than CHUNK, and not the last chunk of the upload, so it
#: fails ``checkUploadSize``. This is what an abandoned request delivers once its
#: socket is gone and the WSGI side reads EOF early.
SHORT = b"x" * 1024


def _start_upload(user, folder, chunks=3):
    return Upload().createUpload(
        user=user,
        name="race.bin",
        parentType="folder",
        parent=folder,
        size=chunks * CHUNK,
    )


def _send(upload, payload):
    return Upload().handleChunk(upload, RequestBodyStream(io.BytesIO(payload)))


@pytest.mark.plugin("sivacor")
def test_a_failed_chunk_does_not_delete_another_chunks_bytes(
    server, db, user, fsAssetstore, uploads_folder
):
    """The rollback must only undo the chunk that failed."""
    upload = _start_upload(user, uploads_folder)
    upload = _send(upload, b"a" * CHUNK)

    # What a second request for this same offset holds: the document as it was
    # before the chunk above was saved. Two requests reach this state whenever
    # one is retried at the HTTP layer -- both pass the REST offset check at
    # api/v1/file.py:238, because neither has seen the other's save.
    stale = copy.deepcopy(upload)

    upload = _send(upload, b"b" * CHUNK)
    assert upload["received"] == 2 * CHUNK
    assert os.path.getsize(upload["tempFile"]) == 2 * CHUNK

    # The stale request now finishes reading a short body and fails validation.
    with pytest.raises(ValidationException):
        _send(stale, SHORT)

    # Before the fix its rollback truncated to ITS received (CHUNK), deleting
    # the second chunk's 5 MiB -- bytes another request wrote and counted. The
    # rollback now subtracts only what this request itself wrote.
    assert os.path.getsize(upload["tempFile"]) == 2 * CHUNK


@pytest.mark.plugin("sivacor")
def test_an_upload_cannot_finalise_shorter_than_its_document(
    server, db, user, fsAssetstore, uploads_folder
):
    """The whole point: the failure mode was not a lost chunk but a *finalised
    lie*.

    ``handleChunk`` finalises on ``received == size`` alone, so a counter that
    had outrun the blob carried the upload to completion anyway. The resulting
    file document reported a size its blob could not deliver, and its ``sha512``
    matched neither the stored bytes nor the intended content -- so nothing
    downstream, including a hash check, could detect it. A finalised upload must
    therefore have a blob exactly as long as its document says.
    """
    upload = _start_upload(user, uploads_folder, chunks=3)
    upload = _send(upload, b"a" * CHUNK)
    stale = copy.deepcopy(upload)
    upload = _send(upload, b"b" * CHUNK)

    with pytest.raises(ValidationException):
        _send(stale, SHORT)

    # Carry on exactly as an unaware client does: the counter says 2 chunks are
    # in, so the client sends the third and last one, and the upload completes.
    file = _send(upload, b"c" * CHUNK)

    assert "size" in file, "expected a finalised file document"
    stored = os.path.getsize(
        os.path.join(fsAssetstore["root"], file["path"])
    )
    assert stored == file["size"], (
        f"file document claims {file['size']:,} bytes, blob holds {stored:,} "
        f"(short by {file['size'] - stored:,})"
    )
