"""Julia submissions, end to end.

The one thing here that no other test can reach: a Julia stage runs as **two**
containers, and the signed declaration has to say which of them was isolated.
Everything else in this file exists to make that assertion trustworthy.

These build their packages inline rather than shipping a fixture archive,
because what is under test *is the contents* -- whether a ``Project.toml`` is
present, and what it declares. A zip would hide exactly the variable.
"""

import json
import os
import tarfile
import tempfile

import mock
import pytest
from girder.models.file import File
from girder_jobs.constants import JobStatus
from girder_jobs.models.job import Job
from pytest_girder.assertions import assertStatusOk

from girder_sivacor.errors import FailureCode
from girder_sivacor.rest import SIVACOR
from girder_sivacor.models.execution_record import ExecutionRecord

from .conftest import (
    assert_submission_metadata,
    get_submission_folder,
    submit_sivacor_job,
    upload_test_file,
)

IMAGE = "ghcr.io/sivacor/julia1.11"
TAG = "1.11.9-20260916"


@pytest.fixture(autouse=True)
def julia_is_allow_listed():
    """Put the Julia image on the allow-list for the duration of these tests.

    ``submit_job`` validates against ``allowed_repos.yaml``, fetched from the
    ``sivacor-repo-choice`` repo's **main branch**, and the Julia images are
    deliberately not there yet: merging that entry arms Julia in *production*
    within a four-hour cache window, with no deploy and no review gate, so it is
    the last thing to land. Until then a real submission here is refused with
    ``Invalid image``.

    Stubbing rather than seeding ``/tmp/sivacor_image_tags.json`` on purpose --
    that file is ``_get_tags``' cache, it is shared with anything else using the
    host's ``/tmp``, and a test that depends on it passes or fails according to
    what happened to be lying around. These tests failed in CI for exactly that
    reason while passing on a workstation that had the file.

    It also takes the network out of these tests: without it every submission
    test reaches raw.githubusercontent before it can start.
    """
    with mock.patch.object(
        SIVACOR, "_get_tags", staticmethod(lambda: {IMAGE: [TAG]})
    ):
        yield

#: A real, tiny, pure-Julia dependency. Small enough that the resolve phase is
#: seconds rather than minutes, and registered, so resolution genuinely succeeds
#: rather than succeeding vacuously against an empty environment.
JSON_UUID = "682c06a0-de6a-54ab-a142-c8b1cf79cde6"


def julia_package(
    uploads_folder, user, main_file="main.jl", project=None, script=None,
    manifest=None,
):
    """Build and upload a Julia replication package.

    ``project=None`` ships no ``Project.toml`` at all, which is the case
    PROJECT_FILE_MISSING exists for.
    """
    if script is None:
        script = 'using JSON\nprintln(JSON.json(Dict("ok" => true)))\n'
    with (
        tempfile.NamedTemporaryFile(suffix=".tar.gz") as archive,
        tempfile.TemporaryDirectory() as directory,
    ):
        with open(os.path.join(directory, main_file), "w") as handle:
            handle.write(script)
        if project is not None:
            with open(os.path.join(directory, "Project.toml"), "w") as handle:
                handle.write(project)
        if manifest is not None:
            with open(os.path.join(directory, "Manifest.toml"), "w") as handle:
                handle.write(manifest)
        with tarfile.open(archive.name, "w:gz") as tar:
            tar.add(directory, arcname=".")
        return upload_test_file(uploads_folder, user, archive.name)


def read(fobj):
    with File().open(fobj) as handle:
        return handle.read().decode("utf-8", errors="ignore")


def listify(value):
    return value if isinstance(value, list) else ([] if value is None else [value])


@pytest.mark.plugin("sivacor")
def test_a_julia_run_isolates_the_analysis_but_not_the_resolve(
    server, db, user, eagerWorkerTasks, fsAssetstore, patched_gpg,
    uploads_folder, submission_collection,
):
    """The claim the two-phase design exists to keep honest.

    ``Pkg.instantiate()`` needs the network and ``InternetIsolation`` is a claim
    about one performance, so the phases cannot share a container without one of
    them lying. Asserted on the declaration itself rather than on the container
    arguments, because the declaration is what is signed and what a reader
    later trusts.
    """
    fobj = julia_package(
        uploads_folder, user, project=f'[deps]\nJSON = "{JSON_UUID}"\n'
    )
    stages = [
        {
            "image_name": IMAGE,
            "image_tag": TAG,
            "main_file": "main.jl",
            "network_isolation": True,
        }
    ]
    resp = submit_sivacor_job(server, user, fobj, stages)
    assertStatusOk(resp)
    job = Job().load(resp.json["_id"], force=True)
    assert job["status"] == JobStatus.SUCCESS

    resp = get_submission_folder(server, user, job["_id"], submission_collection)
    assertStatusOk(resp)
    metadata = resp.json[0]["meta"]
    assert_submission_metadata(
        metadata, user, job["_id"], stages, "completed",
        ["tro_file_id", "stdout_file_id", "stderr_file_id", "tsr_file_id",
         "replpack_file_id"],
    )

    tro = json.loads(read(File().load(metadata["tro_file_id"], force=True)))
    root = tro["@graph"][0]
    performances = listify(root.get("trov:hasPerformance"))

    # Two performances for ONE stage. Every other stack produces one.
    assert len(performances) == 2
    resolve, analysis = performances
    assert "dependency resolution" in resolve["rdfs:comment"]
    assert "workflow execution" in analysis["rdfs:comment"]

    def isolated(performance):
        key = next(k for k in performance if "ttribute" in k)
        types = {a.get("@type") for a in listify(performance.get(key))}
        return "trov:InternetIsolation" in types

    assert not isolated(resolve), "the resolve phase had the network; it must not claim otherwise"
    assert isolated(analysis), "the analysis was isolated and the TRO must say so"

    # ...and the claim has to match what the container actually got, or the two
    # halves could drift apart without either looking wrong on its own.
    assert json.loads(resolve["sivacor:DockerRunArgs"])["network_disabled"] is False
    assert json.loads(analysis["sivacor:DockerRunArgs"])["network_disabled"] is True


@pytest.mark.plugin("sivacor")
def test_the_resolve_phase_is_what_produces_the_manifest(
    server, db, user, eagerWorkerTasks, fsAssetstore, patched_gpg,
    uploads_folder, submission_collection,
):
    """Manifest.toml must appear in the resolve's arrangement, not the run's.

    A package that ships only ``Project.toml`` has its manifest written by
    ``Pkg.instantiate()``. If the resolve had no arrangement of its own, that
    file would first appear in the after-analysis snapshot, reading as though
    the researcher's code produced it.
    """
    fobj = julia_package(
        uploads_folder, user, project=f'[deps]\nJSON = "{JSON_UUID}"\n'
    )
    stages = [{"image_name": IMAGE, "image_tag": TAG, "main_file": "main.jl"}]
    resp = submit_sivacor_job(server, user, fobj, stages)
    assertStatusOk(resp)
    job = Job().load(resp.json["_id"], force=True)
    assert job["status"] == JobStatus.SUCCESS

    resp = get_submission_folder(server, user, job["_id"], submission_collection)
    metadata = resp.json[0]["meta"]
    tro = json.loads(read(File().load(metadata["tro_file_id"], force=True)))
    arrangements = listify(tro["@graph"][0].get("trov:hasArrangement"))

    def paths(arrangement):
        return {
            location["trov:path"]
            for location in listify(arrangement.get("trov:hasArtifactLocation"))
        }

    before, after_resolve = paths(arrangements[0]), paths(arrangements[1])
    assert "Manifest.toml" not in before
    assert "Manifest.toml" in after_resolve
    # And the snapshot says what made it, in words a reader will see.
    assert "resolving dependencies" in arrangements[1]["rdfs:comment"]

    # Both phases keep their own metrics; one filename would lose the resolve's.
    names = {
        item["name"]
        for item in server.request(
            path="/item", params={"folderId": resp.json[0]["_id"], "limit": 100},
            user=user,
        ).json
    }
    assert "performance_data_stage_1.json" in names
    assert "performance_data_stage_1_resolve.json" in names

    # 10-D2: a generated manifest is a weaker guarantee than a supplied one, and
    # the researcher has to be told which they got, in the place they are
    # already reading.
    fetched = server.request(path=f"/job/{job['_id']}", method="GET", user=user).json
    log = "".join(fetched["log"])
    assert "No Manifest.toml was supplied" in log
    assert "does not pin them in advance" in log


@pytest.mark.plugin("sivacor")
def test_a_supplied_manifest_is_reported_as_such(
    server, db, user, eagerWorkerTasks, fsAssetstore, patched_gpg,
    uploads_folder, submission_collection,
):
    """The other half of 10-D2 -- and a resolve with nothing to do still runs.

    An empty environment on purpose: it exercises the "supplied" branch without
    a download, and it checks the claim that the resolve phase is unconditional.
    Making it conditional on having something to fetch would make the TRO's
    shape depend on cache contents.
    """
    fobj = julia_package(
        uploads_folder,
        user,
        project="[deps]\n",
        manifest='julia_version = "1.11.9"\nmanifest_format = "2.0"\n\n[deps]\n',
        script='println("no dependencies here")\n',
    )
    stages = [{"image_name": IMAGE, "image_tag": TAG, "main_file": "main.jl"}]
    resp = submit_sivacor_job(server, user, fobj, stages)
    assertStatusOk(resp)
    job = Job().load(resp.json["_id"], force=True)
    assert job["status"] == JobStatus.SUCCESS

    fetched = server.request(path=f"/job/{job['_id']}", method="GET", user=user).json
    log = "".join(fetched["log"])
    assert "Manifest.toml was supplied" in log
    assert "No Manifest.toml was supplied" not in log

    # The resolve still ran, and still has a performance of its own.
    resp = get_submission_folder(server, user, job["_id"], submission_collection)
    metadata = resp.json[0]["meta"]
    tro = json.loads(read(File().load(metadata["tro_file_id"], force=True)))
    performances = listify(tro["@graph"][0].get("trov:hasPerformance"))
    assert len(performances) == 2


@pytest.mark.plugin("sivacor")
def test_a_package_with_no_project_file_is_refused_before_anything_is_pulled(
    server, db, user, eagerWorkerTasks, fsAssetstore, patched_gpg,
    uploads_folder, submission_collection,
):
    """SIVACOR resolves what the researcher declares, so there must be a declaration.

    Cheap by design: this fails in ``_infer_run_command``, before an image is
    pulled or a container created.
    """
    fobj = julia_package(uploads_folder, user, project=None)
    stages = [{"image_name": IMAGE, "image_tag": TAG, "main_file": "main.jl"}]
    resp = submit_sivacor_job(server, user, fobj, stages, exception=True)
    assertStatusOk(resp)

    job = Job().load(resp.json["_id"], force=True)
    assert job["status"] == JobStatus.ERROR

    # Through REST, not Job().load(): the loaded document carries no `log`.
    fetched = server.request(path=f"/job/{job['_id']}", method="GET", user=user).json
    message = "".join(fetched["log"])
    # The researcher has to be told what to add, not merely that something is
    # missing -- this message is the whole remedy for this failure.
    assert "Project.toml" in message
    assert "Manifest.toml" in message

    records = list(ExecutionRecord().find({}))
    assert len(records) == 1
    assert records[0]["error"]["code"] == FailureCode.PROJECT_FILE_MISSING.value
    # It failed in the resolve phase, which is the step that first needs to know
    # which environment it is assembling.
    assert records[0]["error"]["step"] == "resolve_dependencies"
    # A path out of the researcher's package is never kept.
    assert records[0]["error"]["detail"] is None


@pytest.mark.plugin("sivacor")
def test_an_unsatisfiable_environment_fails_in_the_resolve_not_the_analysis(
    server, db, user, eagerWorkerTasks, fsAssetstore, patched_gpg,
    uploads_folder, submission_collection,
):
    """The distinction the phase exists to draw.

    Folded into NONZERO_EXIT, "your declared environment could not be
    assembled" would be indistinguishable from "your code raised" -- and the
    researcher would go looking in the wrong file.
    """
    fobj = julia_package(
        uploads_folder,
        user,
        project='[deps]\nSivacorNoSuchPackage = "d7a1b2c3-0000-4000-8000-000000000001"\n',
        script='println("never reached")\n',
    )
    stages = [{"image_name": IMAGE, "image_tag": TAG, "main_file": "main.jl"}]
    resp = submit_sivacor_job(server, user, fobj, stages, exception=True)
    assertStatusOk(resp)

    job = Job().load(resp.json["_id"], force=True)
    assert job["status"] == JobStatus.ERROR

    records = list(ExecutionRecord().find({}))
    assert len(records) == 1
    assert records[0]["error"]["code"] == FailureCode.DEPENDENCY_RESOLUTION_FAILED.value
    assert records[0]["error"]["step"] == "resolve_dependencies"
    # The exit code is safe to keep forever; the package name is not.
    assert isinstance(records[0]["error"]["detail"], int)
    assert "SivacorNoSuchPackage" not in json.dumps(records[0], default=str)

    # And the message points at stderr, because that is where Pkg writes.
    fetched = server.request(path=f"/job/{job['_id']}", method="GET", user=user).json
    assert "stderr" in "".join(fetched["log"])
