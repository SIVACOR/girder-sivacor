"""A minimal reproduction of the upstream Girder chunked-upload data loss.

**Nothing here tests SIVACOR code.** Every call is Girder's own upload model.
This file exists so the bug we work around has an executable statement, and so
that we find out the moment upstream fixes it: both tests are
``xfail(strict=True)``, which means **a PASS here is a failure of this file** —
it says the bug is gone and the mitigation in ``rest.py`` can be reconsidered.

The bug, in one line: ``FilesystemAssetstoreAdapter.uploadChunk`` rolls a failed
chunk back with ``tempFile.truncate(upload['received'])``, using the ``received``
value from *its own* in-memory copy of the upload document — which by then can be
many chunks behind, so the rollback deletes bytes that other requests wrote and
already counted.

Production consequence, twice on 2026-09-02: an upload finalises with a blob
short of its document's ``size``, and nothing notices until something reads the
whole file. Full write-up, log evidence and the upstream plan:
``development_notes/girder_upload_race_plan.md``.

**No threads are needed to demonstrate it.** Concurrency is only how two
requests come to hold the same ``received``; the defect is what the rollback
then does. Holding a stale copy of the document reproduces it deterministically,
which is the form upstream can act on.
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
@pytest.mark.xfail(
    strict=True,
    reason="Upstream girder bug: the rollback truncates to a stale 'received'. "
    "A PASS means upstream fixed it -- see girder_upload_race_plan.md",
)
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

    # Its rollback truncated to ITS received (CHUNK), deleting the second
    # chunk's 5 MiB -- bytes another request wrote and counted.
    assert os.path.getsize(upload["tempFile"]) == 2 * CHUNK


@pytest.mark.plugin("sivacor")
@pytest.mark.xfail(
    strict=True,
    reason="Upstream girder bug: an upload finalises with a blob shorter than "
    "its document's size -- see girder_upload_race_plan.md",
)
def test_an_upload_cannot_finalise_shorter_than_its_document(
    server, db, user, fsAssetstore, uploads_folder
):
    """The whole point: this is not a lost chunk, it is a *finalised lie*.

    ``handleChunk`` finalises on ``received == size`` alone, so the counter
    carries the upload to completion while the blob is short. The resulting file
    document reports a size its blob cannot deliver, and its ``sha512`` matches
    neither the stored bytes nor the intended content -- so nothing downstream,
    including a hash check, can detect it.
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
