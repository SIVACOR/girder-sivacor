import json
import os
from collections.abc import Generator
from typing import Any
from unittest import mock

import pytest
from girder.models.api_key import ApiKey
from girder.models.collection import Collection
from girder.models.folder import Folder
from girder.models.setting import Setting
from girder.models.upload import Upload
from girder_plugin_worker.constants import PluginSettings as WorkerSettings

from girder_sivacor.settings import PluginSettings


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "starts_first: run before everything else; for the few tests that alone "
        "take longer than a worker's whole share of the suite",
    )


def pytest_collection_modifyitems(config, items):
    """Move ``starts_first`` tests to the front, otherwise keeping the order.

    Under ``-n 16`` every other worker was done by ~80s while one sat until
    ~110s, because the 74s Julia test had been handed out ~33s in. Starting it
    at t=0 hides it behind the rest of the suite instead.

    Keep the marker rare. xdist's load scheduler deals out the *front* of the
    collection as one consecutive chunk per worker -- two tests each, because
    tox.ini passes ``--maxschedchunk 2``; without it, len/workers/4 -- so marked
    tests pair up on the first workers and run back to back there. One or two
    is a head start; a dozen is a new tail.
    """
    items.sort(key=lambda item: item.get_closest_marker("starts_first") is None)


def _module_plugins(module_node):
    """The one ``plugin`` marker list every test in a module agrees on.

    A module shares one server, so it can load only one set of plugins. Mixed
    markers are a usage error rather than something to guess around: split the
    module instead.
    """
    sets = set()
    for item in module_node.session.items:
        markers = list(item.iter_markers("plugin"))
        if item.module is not module_node.obj or not markers:
            continue
        if any(len(m.args) > 1 for m in markers):
            raise pytest.UsageError(
                f"{item.nodeid}: test-registered plugins are not supported"
            )
        sets.add(tuple(m.args[0] for m in markers))
    if len(sets) > 1:
        raise pytest.UsageError(
            f"{module_node.nodeid}: tests ask for different plugin sets "
            f"{sorted(sets)}; one module shares one server, so split it"
        )
    return list(sets.pop()) if sets else []


@pytest.fixture(scope="module")
def _module_girder(request):
    """One database and one listening server per test module, per worker.

    Replaces pytest-girder's per-test ``db``/``boundServer``, which dropped the
    database, rebuilt every index and started and stopped CherryPy for every
    test: ~1s of fixture against a median test body of 0s, and 79% of the
    suite's worker-time. ``_fresh_db`` empties the collections between tests
    instead, which keeps their indexes. Measured 2026-11-10: test_usage.py's
    81 tests went from 54.8s to 1.3s serially.

    Two things pytest-girder did that this deliberately does not:

    * reset each model's ``_instance`` on teardown. Girder's own comment in
      ``_ModelSingleton`` says never to, and doing it without pruning
      ``_modelSingletons`` made every ``db`` setup reconnect -- rebuild every
      index on -- every dead instance any earlier test left, so a test's setup
      cost grew with how many had run before it in the worker. The live
      singletons are simply pointed at the next module's database below.
    * honour ``--keep-db`` / ``--mock-db``. Nothing here uses them.

    The database name carries the xdist worker id: two workers can each be
    handed part of the same module, and a name from the module alone made them
    truncate each other's data.
    """
    import hashlib

    from girder.models import _dbClients, getDbConnection, model_base
    from girder.models.user import User
    from pytest_girder.plugin_registry import PluginRegistry
    from pytest_girder.utils import serverContext

    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    digest = hashlib.md5(f"{worker}:{request.node.nodeid}".encode()).hexdigest()
    name = f"girder_test_{digest}"
    connection = getDbConnection(
        uri=f"{request.config.getoption('--mongo-uri')}/{name}", quiet=False
    )
    _dbClients[(None, None)] = connection
    connection.drop_database(name)
    for model in model_base._modelSingletons:
        try:
            model.reconnect()
        except OSError:
            pass  # lazy index creation, as pytest-girder ignores it too
    User()._cryptContext.scheme = "plaintext"  # skip bcrypt, as pytest-girder does

    try:
        with PluginRegistry()(), serverContext(
            _module_plugins(request.node), bindPort=True
        ) as server:
            yield connection, server
    finally:
        connection.drop_database(name)
        connection.close()
        _dbClients.clear()


@pytest.fixture
def _fresh_db(_module_girder):
    """Empty every collection, keeping it and its indexes, before each test.

    Named apart from ``db`` because test_reattribute_usage.py overrides ``db``
    with a fixture that itself needs ``server``.
    """
    connection, _ = _module_girder
    database = connection.get_database()
    for collection in database.list_collection_names():
        if not collection.startswith("system."):
            database[collection].delete_many({})
    return connection


@pytest.fixture
def db(_fresh_db):
    return _fresh_db


@pytest.fixture
def boundServer(_module_girder, _fresh_db):
    return _module_girder[1]


@pytest.fixture
def server(boundServer, admin, monkeypatch):
    """A listening server, overriding pytest-girder's in-process ``server``.

    Submission tasks reach Girder over HTTP now, so a test that runs the
    pipeline needs a real socket to connect back to. Eager tasks also skip
    celery's publish path entirely -- that is where ``submit_job``'s
    ``girder_api_url``/``girder_client_token`` headers would be attached -- so
    stand in for them with the environment fallback that ``GirderApi.for_task``
    also uses for the periodic retention sweep.

    ``boundServer`` exposes the same ``request()`` helper, so tests use it the
    same way they used ``server``.
    """
    api_url = f"http://127.0.0.1:{boundServer.boundPort}/api/v1"
    Setting().set(WorkerSettings.API_URL, api_url)
    api_key = ApiKey().createApiKey(admin, name="sivacor-test-worker")
    monkeypatch.setenv("GIRDER_API_URL", api_url)
    monkeypatch.setenv("GIRDER_API_KEY", api_key["key"])
    yield boundServer


@pytest.fixture
def gpg_mock() -> mock.MagicMock:
    """
    Mock GPG object for testing TRO utils GPG functionality.

    Returns a properly configured MagicMock that simulates GPG operations
    needed for SIVACOR submission workflows.

    Example usage:
        def test_custom_gpg_behavior(gpg_mock):
            # Customize mock for specific test needs
            gpg_mock.sign.return_value = "custom_signature"

            with mock.patch("gnupg.GPG", return_value=gpg_mock):
                # Your test code here
                pass
    """
    gpg_mock = mock.MagicMock()

    # Mock list_keys response with key_map
    keys_mock = mock.MagicMock()
    keys_mock.key_map = {
        "fingerprint": {"keyid": "dummykeyid"},
    }
    gpg_mock.list_keys.return_value = keys_mock

    # Mock export and signing operations
    gpg_mock.export_keys.return_value = "dummypublickey"
    gpg_mock.sign.return_value = "dummysignature"

    return gpg_mock


@pytest.fixture
def patched_gpg(
    gpg_mock: mock.MagicMock,
) -> Generator[mock.MagicMock | mock.AsyncMock, Any, None]:
    """
    Context manager fixture that patches tro_utils GPG with the mock.

    This fixture automatically handles the patching and cleanup,
    making it easy to use in tests that need GPG functionality.

    Example usage:
        def test_with_mocked_gpg(patched_gpg):
            # GPG is automatically mocked, no manual patching needed
            # Your test code here
            pass

    The gpg_mock is available as patched_gpg.return_value if you need
    to access or customize it during the test.
    """
    # Patch GPG on the `gnupg` module itself, NOT as
    # "tro_utils.tro_utils.gnupg.GPG". From tro-utils 0.4.6 the `import gnupg`
    # lives inside TRO._gpg() rather than at module scope, so that attribute no
    # longer exists on tro_utils.tro_utils and mock.patch falls back to importing
    # it as a package: "No module named 'tro_utils.tro_utils.gnupg';
    # 'tro_utils.tro_utils' is not a package" -- 22 fixture errors, none of which
    # name the real cause. Patching the attribute on `gnupg` is also
    # import-style-agnostic: it works whether tro-utils imports eagerly or lazily.
    with mock.patch("gnupg.GPG", return_value=gpg_mock) as patch:
        yield patch


@pytest.fixture
def uploads_folder(user):
    """
    Get the user's uploads folder.

    Returns the uploads folder for the given user, which is automatically
    created when the user is created by the SIVACOR plugin.
    """
    return Folder().findOne(
        {
            "name": Setting().get(PluginSettings.UPLOADS_FOLDER_NAME),
            "parentCollection": "user",
            "parentId": user["_id"],
        }
    )


@pytest.fixture
def submission_collection():
    """
    Get or create the submissions collection.

    Returns the collection where submission folders are stored.
    Creates the collection if it doesn't exist.
    """
    collection_name = Setting().get(PluginSettings.SUBMISSION_COLLECTION_NAME)
    collection = Collection().findOne({"name": collection_name})
    if not collection:
        # Create the collection if it doesn't exist (needed for tests)
        collection = Collection().createCollection(
            name=collection_name,
            description="Test submissions collection",
            public=True,
            creator=None,  # System collection
        )
    return collection


def upload_test_file(uploads_folder, user, filename):
    """
    Helper function to upload a test file to the uploads folder.

    Args:
        uploads_folder: The folder to upload to
        user: The user uploading the file
        filename: Name of the file in the test data directory

    Returns:
        The uploaded file object
    """
    # Get the correct path to test data directory
    test_dir = os.path.dirname(os.path.abspath(__file__))
    filepath = os.path.join(test_dir, "data", filename)
    with open(filepath, "rb") as f:
        return Upload().uploadFromFile(
            f,
            size=os.path.getsize(filepath),
            name=filename,
            parentType="folder",
            parent=uploads_folder,
            user=user,
        )


def submit_sivacor_job(
    server, user, file_obj, stages, secrets=None, exception=False, resources=None
):
    """
    Helper function to submit a SIVACOR job.

    Args:
        server: The test server
        user: The user submitting the job
        file_obj: The uploaded file object
        image_tag: Docker image tag to use
        main_file: Main file to execute

    Returns:
        The server response
    """
    return server.request(
        path="/sivacor/submit_job",
        method="POST",
        user=user,
        params={
            "id": str(file_obj["_id"]),
        },
        body=json.dumps(
            {
                "stages": stages,
                "env_secrets": secrets or [],
                # Omitted entirely when not asked for, so the default-resolution
                # path is what every other test exercises.
                **({"resources": resources} if resources is not None else {}),
            }
        ),
        type="application/json",
        exception=exception,
    )


def get_submission_folder(server, user, job_id, submission_collection):
    """
    Helper function to get the submission folder for a job.

    Args:
        server: The test server
        user: The user
        job_id: The job ID to search for
        submission_collection: The submissions collection

    Returns:
        The submission folder response
    """
    return server.request(
        path="/folder",
        method="GET",
        user=user,
        params={
            "jobId": str(job_id),
            "parentType": "collection",
            "parentId": str(submission_collection["_id"]),
        },
    )


def assert_submission_metadata(metadata, user, job_id, stages, status, expected_files):
    """
    Helper function to assert submission folder metadata.

    Args:
        metadata: The folder metadata to check
        user: The expected user
        job_id: The expected job ID
        image_tag: The expected image tag
        main_file: The expected main file
        status: The expected status
        expected_files: List of file keys that should be present in metadata
    """
    assert metadata["creator_id"] == str(user["_id"])
    assert metadata["job_id"] == str(job_id)
    assert metadata["stages"] == stages

    for key in expected_files:
        assert key in metadata
