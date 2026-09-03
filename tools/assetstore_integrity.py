#!/usr/bin/env python3
"""Find files whose Girder document disagrees with the blob it points at.

Why this exists
---------------
A submission failed in ``create_workspace`` with::

    ChunkedEncodingError: Connection broken:
    IncompleteRead(1777014101 bytes read, 10485760 more expected)

That was not a network fault. The declared length came from the *file
document* (``girder_async_routes/file.py`` builds ``Content-Length`` from
``file['size']``) while the bytes came from the *blob on disk*, and the two
disagreed by 10 485 760 bytes -- exactly two 5 MiB upload chunks, the chunk
size in ``aea-sivacor``'s ``FileUploader``. So the archive had been short in
the assetstore since the day it was uploaded, and the download was the first
thing that ever read it end to end.

Nothing in the stack detects that state:

* the server declares a length it does not verify against the file;
* ``finalizeUpload`` stores ``sha512`` from the streaming checksum -- the hash
  of what the request bodies delivered, never of the file that landed on disk;
* ``girder_client.downloadFile``'s own size check is unreachable, because
  urllib3 >= 2 enforces ``Content-Length`` and raises first.

This script is that missing detector. It compares every file document's
``size`` to ``os.path.getsize`` of the blob, and separately looks for upload
records whose ``received`` counter outran their temp file -- the same skew,
caught before finalisation.

**Read-only.** It issues ``find`` queries and ``stat``/``read`` calls and
nothing else: no Mongo writes, no file writes, no REST calls, no repair. What
to do about a finding is a decision for a human, partly because of the
content-addressed store described under "Re-uploading does not fix it" below.

Where to run it
---------------
Inside the ``girder`` container, which already has pymongo, the mongo network
and the assetstore mounted::

    docker exec -i $(docker ps -qf name=wt_girder) \
        python3 - < tools/assetstore_integrity.py

Or with the tree available::

    docker exec -i $(docker ps -qf name=wt_girder) \
        python3 /path/to/assetstore_integrity.py --jsonl /tmp/integrity.jsonl

Re-uploading does not fix it
---------------------------
``FilesystemAssetstoreAdapter.finalizeUpload`` is content-addressed: the blob
lives at ``<root>/<sha512[0:2]>/<sha512[2:4]>/<sha512>``, and if that path
already exists the freshly uploaded temp file is *discarded* in favour of it.
The stored hash is the hash of the intended content, so a short blob sits at
the path a correct upload of the same archive would also claim. Every later
upload of that file therefore inherits the corruption. Any repair has to deal
with the blob, not just the document -- which is why this script only reports.

What this cannot see
--------------------
A blob of the right *length* but the wrong *content*. Catching that means
hashing everything, which is not a thing to do by accident on an 800 GB
assetstore. ``--verify-hash`` hashes the files this script has already
flagged, which is enough to prove a finding; it is not a full audit.

Output note
-----------
Findings carry researcher filenames, so the report and any ``--jsonl`` file
are as sensitive as the submissions themselves. Unlike
``girder_sivacor.telemetry``, this is deliberately *not* an anonymous record:
it exists to identify specific files so they can be dealt with. Treat it as
transient operator output, not as something to keep.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys

#: Girder's ``AssetstoreType.FILESYSTEM``. Hardcoded rather than imported so
#: this runs anywhere pymongo does, including a plain python:3.12 container
#: pointed at the volume -- there is no reason a diagnostic should need the
#: whole girder import to stat a file.
FILESYSTEM = 0

#: ``aea-sivacor``'s ``FileUploader.UPLOAD_CHUNK_SIZE``. Only used to label a
#: shortfall as chunk-aligned, which is the fingerprint of the upload-side
#: accounting bug rather than of, say, a half-written file or a truncating
#: filesystem. Overridable because it is the *client's* constant, and a
#: different client (girder_client's own uploader, the web client) uses its own.
UPLOAD_CHUNK_SIZE = 1024 * 1024 * 5

#: Read granularity for --verify-hash. Matches the assetstore adapter's own.
BUF_SIZE = 65536


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Compare Girder file documents against their blobs.",
        epilog="Read-only. Exits 1 if anything was found, 0 if clean.",
    )
    parser.add_argument(
        "--mongo-uri",
        default=os.environ.get("GIRDER_MONGO_URI", "mongodb://mongo:27017/girder"),
        help="Mongo connection string (default: $GIRDER_MONGO_URI).",
    )
    parser.add_argument(
        "--assetstore",
        action="append",
        metavar="ID",
        help="Limit to this assetstore id. Repeatable. Default: all filesystem ones.",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=UPLOAD_CHUNK_SIZE,
        metavar="BYTES",
        help=f"Upload chunk size used to label aligned shortfalls (default {UPLOAD_CHUNK_SIZE}).",
    )
    parser.add_argument(
        "--verify-hash",
        action="store_true",
        help="sha512 the flagged blobs and compare to the document. Reads every "
        "flagged byte, so it costs one full pass over those files only.",
    )
    parser.add_argument(
        "--jsonl",
        metavar="PATH",
        help="Also write one JSON object per finding here. Contains filenames.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        metavar="N",
        help="Stop after examining N file documents (0 = no limit). For a smoke test.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the progress counter on stderr.",
    )
    return parser.parse_args(argv)


def load_assetstores(db, wanted):
    """Return ``{_id: assetstore}`` for the filesystem assetstores in scope.

    Non-filesystem stores (S3) are returned too, so the caller can count their
    files as unexaminable rather than silently omitting them -- a sweep that
    quietly skips half the deployment is worse than one that says it did.
    """
    from bson.objectid import ObjectId

    query = {}
    if wanted:
        query["_id"] = {"$in": [ObjectId(value) for value in wanted]}
    return {store["_id"]: store for store in db.assetstore.find(query)}


def resolve_path(file_doc, assetstore):
    """The absolute path of ``file_doc``'s blob, or None if it has none.

    Mirrors ``FilesystemAssetstoreAdapter.fullPath``: an imported file's
    ``path`` is already absolute and points outside the assetstore, everything
    else is relative to the store's root.
    """
    path = file_doc.get("path")
    if not path:
        return None
    if file_doc.get("imported"):
        return path
    root = assetstore.get("root")
    if not root:
        return None
    return os.path.join(root, path)


def classify(doc_size, disk_size, chunk_size):
    """Name the disagreement between the document and the blob.

    ``chunk_aligned_short`` is the interesting one: a shortfall that is a whole
    number of upload chunks is the signature of the upload having counted bytes
    it never wrote, as opposed to a truncated write or a partial copy, which
    land on no particular boundary.
    """
    if doc_size == disk_size:
        return None, 0
    delta = doc_size - disk_size
    if delta > 0 and chunk_size and delta % chunk_size == 0:
        return "chunk_aligned_short", delta
    if delta > 0:
        return "short", delta
    return "long", delta


def sha512_of(path):
    digest = hashlib.sha512()
    with open(path, "rb") as handle:
        while True:
            data = handle.read(BUF_SIZE)
            if not data:
                break
            digest.update(data)
    return digest.hexdigest()


def sweep_files(db, assetstores, args, report):
    """Compare every in-scope file document to its blob.

    ``stat`` results are cached by path because the store is content-addressed:
    one corrupt blob can be referenced by any number of file documents, and
    re-statting it per document would multiply the work by the dedup factor
    while telling us nothing new. The finding is still reported per document --
    each one is a submission someone will try to download -- with the distinct
    blob count reported separately at the end.
    """
    query = {"assetstoreId": {"$in": list(assetstores)}} if assetstores else {}
    total = db.file.count_documents(query)
    report["files_total"] = total

    stat_cache: dict[str, int | None] = {}
    findings = []
    examined = 0

    for file_doc in db.file.find(query, no_cursor_timeout=False):
        if args.limit and examined >= args.limit:
            report["stopped_early"] = True
            break
        examined += 1
        if not args.quiet and examined % 5000 == 0:
            print(f"  ... {examined}/{total} documents", file=sys.stderr, flush=True)

        # A link file has no blob at all, so there is nothing to disagree with.
        if file_doc.get("linkUrl") and not file_doc.get("assetstoreId"):
            report["skipped_link"] += 1
            continue

        assetstore = assetstores.get(file_doc.get("assetstoreId"))
        if assetstore is None:
            report["skipped_no_assetstore"] += 1
            continue
        if assetstore.get("type") != FILESYSTEM:
            # S3 sizes are only checkable over the network, one HEAD per
            # object. Out of scope here, but counted so the total adds up.
            report["skipped_non_filesystem"] += 1
            continue

        doc_size = file_doc.get("size")
        if not isinstance(doc_size, int):
            report["skipped_no_size"] += 1
            continue

        path = resolve_path(file_doc, assetstore)
        if path is None:
            report["skipped_no_path"] += 1
            continue

        if path in stat_cache:
            disk_size = stat_cache[path]
        else:
            try:
                disk_size = os.path.getsize(path)
            except OSError:
                disk_size = None
            stat_cache[path] = disk_size

        if disk_size is None:
            report["missing_blob"] += 1
            findings.append(
                {
                    "kind": "missing_blob",
                    "file_id": str(file_doc["_id"]),
                    "item_id": str(file_doc.get("itemId")),
                    "name": file_doc.get("name"),
                    "doc_size": doc_size,
                    "disk_size": None,
                    "delta": doc_size,
                    "path": path,
                    "imported": bool(file_doc.get("imported")),
                    "sha512": file_doc.get("sha512"),
                    "created": str(file_doc.get("created")),
                }
            )
            continue

        report["checked"] += 1
        kind, delta = classify(doc_size, disk_size, args.chunk_size)
        if kind is None:
            continue

        finding = {
            "kind": kind,
            "file_id": str(file_doc["_id"]),
            "item_id": str(file_doc.get("itemId")),
            "name": file_doc.get("name"),
            "doc_size": doc_size,
            "disk_size": disk_size,
            "delta": delta,
            "path": path,
            "imported": bool(file_doc.get("imported")),
            "sha512": file_doc.get("sha512"),
            "created": str(file_doc.get("created")),
        }
        if kind == "chunk_aligned_short":
            finding["missing_chunks"] = delta // args.chunk_size
        findings.append(finding)

    report["blobs_distinct"] = len(stat_cache)
    report["examined"] = examined
    return findings


def verify_hashes(findings, report):
    """sha512 each flagged blob and compare to the document's recorded hash.

    This is the proof, not the detection: a mismatch here says the bytes on
    disk are not the bytes whose hash was recorded at finalisation, which is
    what distinguishes "the document is wrong" from "the file was corrupted
    after the fact". Only flagged files are hashed -- see the module docstring.
    """
    hashed: dict[str, str] = {}
    for finding in findings:
        if finding["disk_size"] is None or not finding.get("sha512"):
            continue
        if finding["path"] in hashed:
            actual = hashed[finding["path"]]
        else:
            try:
                actual = sha512_of(finding["path"])
            except OSError as exc:
                finding["hash_error"] = str(exc)
                continue
            hashed[finding["path"]] = actual
        finding["sha512_actual"] = actual
        finding["sha512_matches"] = actual == finding["sha512"]
        if not finding["sha512_matches"]:
            report["hash_mismatch"] += 1


def sweep_uploads(db, report):
    """Find in-flight uploads whose counter has outrun their temp file.

    Same skew as the finalised case, caught one step earlier: ``received`` is
    what ``handleChunk`` finalises on, and the filesystem adapter only handles
    the *opposite* direction (temp file longer than ``received``, from a server
    that died mid-write). An upload sitting here with a temp file shorter than
    ``received`` is a file that would finalise short -- and, because the store
    is content-addressed, would poison that content's path for every later
    upload of it.
    """
    findings = []
    for upload in db.upload.find({}):
        temp_path = upload.get("tempFile")
        received = upload.get("received")
        if not temp_path or not isinstance(received, int):
            continue
        try:
            temp_size = os.path.getsize(temp_path)
        except OSError:
            # No temp file at all. That is ordinary debris, not a skew: an
            # abandoned upload keeps its document long after its temp file is
            # swept, so every stale upload in the collection would otherwise be
            # reported as corruption. Counted, not listed -- the first run of
            # this script listed 95 of these and buried the fact that it had
            # found no real skew at all.
            report["uploads_temp_missing"] += 1
            continue
        report["uploads_checked"] += 1
        if temp_size == received:
            continue
        findings.append(
            {
                "kind": "upload_skew",
                "upload_id": str(upload["_id"]),
                "name": upload.get("name"),
                "declared_size": upload.get("size"),
                "received": received,
                "temp_size": temp_size,
                "delta": None if temp_size is None else received - temp_size,
                "temp_file": temp_path,
                "created": str(upload.get("created")),
            }
        )
    return findings


def _format_bytes(count):
    """``1787499861`` -> ``'1,787,499,861 B (1.665 GiB)'``.

    The parenthetical is only added above 1 MiB: "7 B (0.000 GiB)" reads as
    though the tool has lost track of the scale it is reporting.
    """
    if count is None:
        return "-"
    if count < 1024 ** 2:
        return f"{count:,} B"
    if count < 1024 ** 3:
        return f"{count:,} B ({count / 1024 ** 2:.2f} MiB)"
    return f"{count:,} B ({count / 1024 ** 3:.3f} GiB)"


def print_report(file_findings, upload_findings, report, args):
    print()
    print("=" * 72)
    print("Assetstore integrity sweep")
    print("=" * 72)
    print(f"  file documents in scope   : {report['files_total']:,}")
    print(f"  examined                  : {report['examined']:,}")
    print(f"  size-checked              : {report['checked']:,}")
    print(f"  distinct blobs statted    : {report['blobs_distinct']:,}")
    print(f"  skipped, link file        : {report['skipped_link']:,}")
    print(f"  skipped, unknown store    : {report['skipped_no_assetstore']:,}")
    print(f"  skipped, non-filesystem   : {report['skipped_non_filesystem']:,}")
    print(f"  skipped, no size on doc   : {report['skipped_no_size']:,}")
    print(f"  skipped, no path on doc   : {report['skipped_no_path']:,}")
    print(f"  uploads in flight checked : {report['uploads_checked']:,}")
    print(f"  abandoned, temp file gone : {report['uploads_temp_missing']:,}")
    if report.get("stopped_early"):
        print("  NOTE: stopped early by --limit; this is not a full sweep.")

    # Content-addressed dedup means a single short blob can be the target of
    # any number of file documents, and every one of them is a submission that
    # will fail the same way. Counting them separately is what tells an
    # operator whether this is one bad upload or one bad blob N users inherited.
    shared: dict[str, int] = {}
    for finding in file_findings:
        shared[finding["path"]] = shared.get(finding["path"], 0) + 1

    aligned = [f for f in file_findings if f["kind"] == "chunk_aligned_short"]
    short = [f for f in file_findings if f["kind"] == "short"]
    long_ = [f for f in file_findings if f["kind"] == "long"]
    missing = [f for f in file_findings if f["kind"] == "missing_blob"]

    print()
    print(f"  blob shorter than doc, chunk-aligned : {len(aligned):,}   <-- the bug")
    print(f"  blob shorter than doc, unaligned     : {len(short):,}")
    print(f"  blob longer than doc                 : {len(long_):,}")
    print(f"  blob missing entirely                : {len(missing):,}")
    print(f"  upload counter ahead of temp file    : {len(upload_findings):,}")
    print(f"  distinct bad blobs behind the above  : {len(shared):,}")
    if args.verify_hash:
        print(f"  sha512 mismatches among the above    : {report['hash_mismatch']:,}")

    for title, group in (
        ("CHUNK-ALIGNED SHORT BLOBS", aligned),
        ("SHORT BLOBS (unaligned)", short),
        ("LONG BLOBS", long_),
        ("MISSING BLOBS", missing),
    ):
        if not group:
            continue
        print()
        print(f"-- {title} " + "-" * max(0, 68 - len(title)))
        for finding in sorted(group, key=lambda f: -abs(f["delta"] or 0)):
            print(f"  file {finding['file_id']}  {finding['name']!r}")
            print(f"    document : {_format_bytes(finding['doc_size'])}")
            print(f"    on disk  : {_format_bytes(finding['disk_size'])}")
            delta = finding["delta"]
            suffix = ""
            if finding.get("missing_chunks"):
                suffix = f"  = {finding['missing_chunks']} x {args.chunk_size:,} B chunks"
            print(f"    delta    : {delta:+,} B{suffix}")
            print(f"    created  : {finding['created']}")
            print(f"    path     : {finding['path']}")
            if shared.get(finding["path"], 1) > 1:
                others = shared[finding["path"]] - 1
                print(f"    shared   : {others} other file document(s) point at this")
                print("               same blob and will fail identically")
            if finding.get("imported"):
                print("    imported : yes -- path is a host path, so a mismatch here")
                print("               means the source changed, not this bug")
            if "sha512_matches" in finding:
                verdict = "matches" if finding["sha512_matches"] else "MISMATCH"
                print(f"    sha512   : {verdict}")
            if finding.get("hash_error"):
                print(f"    sha512   : could not read ({finding['hash_error']})")

    if upload_findings:
        print()
        print("-- UPLOADS WHOSE COUNTER IS AHEAD OF THEIR TEMP FILE " + "-" * 19)
        for finding in upload_findings:
            print(f"  upload {finding['upload_id']}  {finding['name']!r}")
            print(f"    declared : {_format_bytes(finding['declared_size'])}")
            print(f"    received : {_format_bytes(finding['received'])}")
            print(f"    temp file: {_format_bytes(finding['temp_size'])}")
            print(f"    temp path: {finding['temp_file']}")
            print(f"    created  : {finding['created']}")

    print()
    if not file_findings and not upload_findings:
        print("  Clean: every checked document agrees with its blob.")
    else:
        print("  Findings above. This script changes nothing -- see the module")
        print("  docstring on why a short blob cannot be fixed by re-uploading.")
    print()


def main(argv=None):
    args = parse_args(argv)

    try:
        from pymongo import MongoClient
        from pymongo.errors import PyMongoError
    except ImportError:
        print(
            "pymongo is required. Run this inside the girder container, which "
            "already has it.",
            file=sys.stderr,
        )
        return 2

    client = MongoClient(args.mongo_uri)
    db = client.get_default_database()
    if db is None:
        print(
            "The Mongo URI names no database; append one, e.g. "
            "mongodb://mongo:27017/girder",
            file=sys.stderr,
        )
        return 2

    report = {
        "files_total": 0,
        "examined": 0,
        "checked": 0,
        "blobs_distinct": 0,
        "skipped_link": 0,
        "skipped_no_assetstore": 0,
        "skipped_non_filesystem": 0,
        "skipped_no_size": 0,
        "skipped_no_path": 0,
        "missing_blob": 0,
        "hash_mismatch": 0,
        "uploads_checked": 0,
        "uploads_temp_missing": 0,
    }

    # The first query is also the connection test. MongoClient constructs
    # lazily, so a wrong URI or an unreachable mongo only shows up here -- and
    # a diagnostic tool should say that in one line rather than in a driver
    # traceback the operator has to read past.
    try:
        assetstores = load_assetstores(db, args.assetstore)
    except PyMongoError as exc:
        print(f"Could not query {args.mongo_uri}: {exc}", file=sys.stderr)
        return 2
    if not assetstores:
        print("No assetstores matched.", file=sys.stderr)
        return 2
    if not args.quiet:
        for store_id, store in assetstores.items():
            kind = "filesystem" if store.get("type") == FILESYSTEM else f"type {store.get('type')}"
            root = store.get("root", "-")
            print(f"assetstore {store_id} ({kind}) root={root}", file=sys.stderr)

    file_findings = sweep_files(db, assetstores, args, report)
    if args.verify_hash:
        verify_hashes(file_findings, report)
    upload_findings = sweep_uploads(db, report)

    print_report(file_findings, upload_findings, report, args)

    if args.jsonl:
        with open(args.jsonl, "w", encoding="utf-8") as handle:
            for finding in file_findings + upload_findings:
                handle.write(json.dumps(finding, sort_keys=True) + "\n")
        print(f"  Wrote {len(file_findings) + len(upload_findings)} findings to {args.jsonl}")
        print("  It names researcher files; treat it as transient.")
        print()

    return 1 if (file_findings or upload_findings) else 0


if __name__ == "__main__":
    sys.exit(main())
