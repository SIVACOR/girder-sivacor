"""The folder-subtree manifest that sivacor-girderfs mounts from.

These are contract tests as much as unit tests. The daemon has no fallback: it
mounts from this response or it refuses to mount, it derives inode numbers from
array order, and it locates blobs on disk from ``sha512``. So the things
asserted here -- ``manifestVersion``, sort order, ``""`` rather than ``null``
for a missing hash, ``root.parent`` being null -- are promises to a client that
cannot negotiate, not internal details that may drift.

The other half is access control. The walk must use the requesting user's own
permissions, never ``force=True``: a manifest is a listing of a whole subtree,
so a leak here is a leak of everything below the mount point.
"""

import hashlib
import io
import types

import pytest
from bson import ObjectId
from girder.constants import AccessType
from girder.models.file import File
from girder.models.folder import Folder
from girder.models.item import Item
from girder.models.upload import Upload
from pytest_girder.assertions import assertStatus, assertStatusOk

ALPHA = b"alpha, and a little more to make the size distinctive"
BRAVO = b"bravo"
DEEP = b"deep"


def manifest(server, as_user, folder, expected=200, **params):
    resp = server.request(
        path="/sivacor/fs/manifest",
        method="GET",
        user=as_user,
        params={"folderId": str(folder["_id"]), **params},
    )
    if expected == 200:
        assertStatusOk(resp)
    else:
        assertStatus(resp, expected)
    return resp.json


def put_file(item, name, contents, user):
    return Upload().uploadFromFile(
        io.BytesIO(contents),
        size=len(contents),
        name=name,
        parentType="item",
        parent=item,
        user=user,
    )


def by_name(entries):
    return {entry["name"]: entry for entry in entries}


@pytest.fixture
def tree(admin, user, fsAssetstore):
    """A subtree covering every shape the manifest has to round-trip.

    Owned by ``admin``, readable by ``user``, so the same tree serves both the
    shape tests and the admin/non-admin split without being rebuilt.

    root/
      empty/                    -- a folder with no children at all
      nested/
        deep                    -- an item one level down, one file
      multi                     -- an item with two files
      empty-item                -- an item with no files
    """
    root = Folder().createFolder(
        admin, "mount-root", parentType="user", public=False, creator=admin
    )
    Folder().setUserAccess(root, user, level=AccessType.READ, save=True)

    empty = Folder().createFolder(root, "empty", creator=admin)
    nested = Folder().createFolder(root, "nested", creator=admin)

    multi = Item().createItem("multi", admin, root)
    alpha = put_file(multi, "a.bin", ALPHA, admin)
    bravo = put_file(multi, "b.bin", BRAVO, admin)

    empty_item = Item().createItem("empty-item", admin, root)

    deep_item = Item().createItem("deep", admin, nested)
    deep = put_file(deep_item, "deep.txt", DEEP, admin)

    return types.SimpleNamespace(
        root=root,
        empty=empty,
        nested=nested,
        multi=multi,
        empty_item=empty_item,
        deep_item=deep_item,
        alpha=alpha,
        bravo=bravo,
        deep=deep,
        # 3 folders + 3 items + 3 files
        nodes=9,
    )


@pytest.mark.plugin("sivacor")
def test_manifest_describes_the_whole_subtree(server, db, user, tree):
    body = manifest(server, user, tree.root)

    assert body["manifestVersion"] == 1
    assert body["root"] == {"type": "folder", "id": str(tree.root["_id"])}
    assert body["generatedAt"].endswith("Z")

    folders = by_name(body["folders"])
    assert set(folders) == {"mount-root", "empty", "nested"}
    assert folders["empty"]["parent"] == str(tree.root["_id"])
    assert folders["nested"]["parent"] == str(tree.root["_id"])

    items = by_name(body["items"])
    # An item with no files is legal and must survive the walk; so is a folder
    # with no children. The daemon has defined behaviour for both (an empty
    # directory, and a directory whose item contributes nothing).
    assert set(items) == {"multi", "empty-item", "deep"}
    assert items["multi"]["folder"] == str(tree.root["_id"])
    assert items["empty-item"]["folder"] == str(tree.root["_id"])
    assert items["deep"]["folder"] == str(tree.nested["_id"])

    files = by_name(body["files"])
    assert set(files) == {"a.bin", "b.bin", "deep.txt"}
    # Both files of the multi-file item point back at it.
    assert files["a.bin"]["item"] == str(tree.multi["_id"])
    assert files["b.bin"]["item"] == str(tree.multi["_id"])
    assert files["deep.txt"]["item"] == str(tree.deep_item["_id"])

    assert files["a.bin"]["size"] == len(ALPHA)
    assert files["a.bin"]["imported"] is False
    assert files["a.bin"]["linkUrl"] is None
    for stamp in ("created", "updated"):
        assert folders["nested"][stamp].endswith("Z")


@pytest.mark.plugin("sivacor")
def test_root_parent_is_null_even_though_it_has_one(server, db, user, tree):
    """A subtree mount must not be told anything about what is above it."""
    assert tree.root["parentId"] is not None

    body = manifest(server, user, tree.root)
    root_entry = by_name(body["folders"])["mount-root"]
    assert root_entry["parent"] is None

    # ...and rooting the mount lower down says the same thing about that folder.
    body = manifest(server, user, tree.nested)
    assert by_name(body["folders"])["nested"]["parent"] is None
    assert {f["name"] for f in body["files"]} == {"deep.txt"}


@pytest.mark.plugin("sivacor")
def test_sha512_is_the_real_content_hash(server, db, user, tree):
    """The reason the endpoint exists: the hash arrives without reading bytes."""
    files = by_name(manifest(server, user, tree.root)["files"])

    assert files["a.bin"]["sha512"] == hashlib.sha512(ALPHA).hexdigest()
    assert files["b.bin"]["sha512"] == hashlib.sha512(BRAVO).hexdigest()
    assert files["deep.txt"]["sha512"] == hashlib.sha512(DEEP).hexdigest()

    # And it is Girder's own recorded value, not something recomputed here.
    assert files["a.bin"]["sha512"] == File().load(
        tree.alpha["_id"], force=True
    )["sha512"]


@pytest.mark.plugin("sivacor")
def test_folder_the_user_cannot_read_is_403(server, db, admin, user, fsAssetstore):
    private = Folder().createFolder(
        admin, "not-yours", parentType="user", public=False, creator=admin
    )
    manifest(server, user, private, expected=403)


@pytest.mark.plugin("sivacor")
def test_unreadable_subfolder_is_absent_but_the_rest_is_returned(
    server, db, admin, user, tree
):
    """The one that matters.

    A manifest lists a whole subtree in one response, so a walk that ignored
    per-folder permissions would hand the caller everything below the mount
    point. The requested folder being readable must not imply its children are.
    """
    secret = Folder().createFolder(tree.root, "secret", creator=admin)
    # createFolder copies the parent's access policies, so the grant on root
    # has to be revoked explicitly -- which is also the realistic shape: a
    # shared folder with one branch locked down after the fact.
    Folder().setUserAccess(secret, user, level=None, save=True)
    secret_item = Item().createItem("classified", admin, secret)
    put_file(secret_item, "classified.dta", b"restricted microdata", admin)

    body = manifest(server, user, tree.root)

    assert "secret" not in by_name(body["folders"])
    assert "classified" not in by_name(body["items"])
    assert "classified.dta" not in by_name(body["files"])
    # The readable part is untouched -- an unreadable branch is skipped, not
    # a reason to fail the whole request.
    assert set(by_name(body["folders"])) == {"mount-root", "empty", "nested"}
    assert set(by_name(body["files"])) == {"a.bin", "b.bin", "deep.txt"}

    # The admin, who can read it, sees it.
    admin_body = manifest(server, admin, tree.root)
    assert "secret" in by_name(admin_body["folders"])
    assert "classified.dta" in by_name(admin_body["files"])


@pytest.mark.plugin("sivacor")
def test_path_is_admin_only(server, db, admin, user, tree):
    """``path`` is an absolute host path, i.e. infrastructure layout."""
    # Only imported files carry an absolute path. Nothing in SIVACOR produces
    # one, so stand in for Assetstore().importData by setting the two fields it
    # sets (filesystem_assetstore_adapter.py:377 and the import walk).
    imported = File().load(tree.deep["_id"], force=True)
    imported["imported"] = True
    imported["path"] = "/srv/restricted/census/deep.txt"
    File().save(imported)

    as_admin = by_name(manifest(server, admin, tree.root)["files"])["deep.txt"]
    assert as_admin["imported"] is True
    assert as_admin["path"] == "/srv/restricted/census/deep.txt"

    as_user = by_name(manifest(server, user, tree.root)["files"])["deep.txt"]
    assert as_user["imported"] is True
    assert as_user["path"] is None


@pytest.mark.plugin("sivacor")
def test_ordinary_uploads_have_no_path_even_for_an_admin(server, db, admin, tree):
    """A normal assetstore file has a ``path``, but a relative one.

    ``filesystem_assetstore_adapter.py:234`` stores ``ab/12/ab12...`` --
    relative to the assetstore root and derivable from ``sha512``. Emitting it
    would put a value in the field that looks like a host path and is not.
    """
    assert "path" in File().load(tree.alpha["_id"], force=True)

    files = by_name(manifest(server, admin, tree.root)["files"])
    assert files["a.bin"]["path"] is None


@pytest.mark.plugin("sivacor")
def test_maxnodes_refuses_a_tree_that_exceeds_it(server, db, user, tree):
    assertStatus(
        server.request(
            path="/sivacor/fs/manifest",
            method="GET",
            user=user,
            params={"folderId": str(tree.root["_id"]), "maxNodes": tree.nodes - 1},
        ),
        413,
    )

    body = manifest(server, user, tree.root, maxNodes=tree.nodes)
    assert len(body["folders"]) + len(body["items"]) + len(body["files"]) == tree.nodes


@pytest.mark.plugin("sivacor")
def test_every_array_is_sorted_by_id(server, db, user, tree):
    """Inode numbers are derived from manifest order (PLAN.md §6.2).

    Asserted against sorted() rather than by comparing two calls, which passes
    whenever Mongo happens to be consistent with itself.
    """
    body = manifest(server, user, tree.root)
    for key in ("folders", "items", "files"):
        ids = [entry["id"] for entry in body[key]]
        assert ids == sorted(ids), key
        assert len(set(ids)) == len(ids), key


@pytest.mark.plugin("sivacor")
def test_bad_folder_id_is_a_client_error(server, db, user, tree):
    def status(**params):
        return server.request(
            path="/sivacor/fs/manifest", method="GET", user=user, params=params
        )

    assertStatus(status(), 400)
    assertStatus(status(folderId="not-an-object-id"), 400)
    # Well-formed but nothing there.
    assertStatus(status(folderId=str(ObjectId())), 400)
    # An id that resolves to something that is not a folder.
    assertStatus(status(folderId=str(tree.multi["_id"])), 400)


@pytest.mark.plugin("sivacor")
def test_file_without_a_hash_reports_an_empty_string(server, db, admin, user, tree):
    """A link file has no blob, so Girder never computed a sha512 for it.

    Empty means "cannot be located by hash, fetch over HTTP" -- a supported
    path in the daemon. ``null`` or a missing key would not be.
    """
    link = File().createLinkFile(
        name="external.csv",
        parent=tree.multi,
        parentType="item",
        url="https://example.org/external.csv",
        creator=admin,
    )
    assert "sha512" not in link

    entry = by_name(manifest(server, user, tree.root)["files"])["external.csv"]
    assert entry["sha512"] == ""
    assert entry["linkUrl"] == "https://example.org/external.csv"
    assert entry["path"] is None
    assert entry["size"] == 0


@pytest.mark.plugin("sivacor")
def test_plugin_widens_the_file_schema_site_wide(server, db, admin, user, tree):
    """``exposeFields`` is global, so this is not scoped to the manifest.

    Asserted here because it is a change to an endpoint the plugin does not
    own: every ``File`` response in this Girder grows these fields, including
    the ones aea-sivacor consumes.
    """
    path = "/file/%s" % tree.alpha["_id"]

    resp = server.request(path=path, method="GET", user=user)
    assertStatusOk(resp)
    assert resp.json["sha512"] == hashlib.sha512(ALPHA).hexdigest()
    assert "path" not in resp.json

    resp = server.request(path=path, method="GET", user=admin)
    assertStatusOk(resp)
    assert "path" in resp.json


@pytest.mark.plugin("sivacor")
def test_empty_folder_mounts_as_an_empty_manifest(server, db, user, tree):
    body = manifest(server, user, tree.empty)
    assert len(body["folders"]) == 1
    assert body["items"] == []
    assert body["files"] == []



@pytest.mark.plugin("sivacor")
def test_files_are_fetched_in_batches_not_one_query_per_item(
    server, db, user, tree, monkeypatch
):
    """The walk must not issue one Mongo query per item.

    ``Item().childFiles(item)`` is exactly ``File().find({"itemId": id})``, so
    calling it in a loop costs a round trip per item: at 50 000 items that
    measured 18.7s of a 26s response, against 0.55s for the same rows fetched
    with ``$in``. Nothing about the response reveals the difference, which is
    precisely why it needs a test -- a future refactor back to the obvious
    per-item loop would be invisible until someone mounts a big folder.
    """
    from girder_sivacor import rest as rest_module

    queries = []
    real_find = File().find

    def counting_find(query=None, **kwargs):
        queries.append(query)
        return real_find(query, **kwargs)

    monkeypatch.setattr(File(), "find", counting_find)

    body = manifest(server, user, tree.root)
    assert len(body["files"]) == 3

    itemid_queries = [q for q in queries if q and "itemId" in q]
    assert itemid_queries, "the walk should look files up by itemId"
    # Every lookup is a batch, never a bare equality on a single item id.
    for query in itemid_queries:
        assert "$in" in query["itemId"], (
            f"file lookup {query!r} is per-item; it should batch with $in"
        )
    # The fixture has 3 items across 2 folders, so at most one query per folder.
    assert len(itemid_queries) <= 2, (
        f"{len(itemid_queries)} file queries for 2 folders of items"
    )
    assert rest_module._MANIFEST_FILE_BATCH >= 1
