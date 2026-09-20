"""Julia submissions, end to end.

The one thing here that no other test can reach: whether a researcher can set
up a Julia environment **themselves**, in a stage of their own, and have the
next stage use it while staying network-isolated. SIVACOR used to do that
setup for them in an injected container; the depot lives in the workspace, so
an ordinary stage can do it instead, and that is the property under test.

These build their packages inline rather than shipping a fixture archive,
because what is under test *is the contents* -- which scripts are present and
what they declare. A zip would hide exactly the variable.
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


#: A real, tiny, pure-Julia dependency. Small enough that installing it is
#: seconds rather than minutes, and registered, so a setup stage genuinely
#: resolves something rather than succeeding vacuously against an empty
#: environment.
JSON_UUID = "682c06a0-de6a-54ab-a142-c8b1cf79cde6"

#: What a researcher writes when their package declares dependencies. There is
#: nothing SIVACOR-specific about it, which is the point of the reversal: it is
#: a Julia script, run by a Julia stage, and the platform has no opinion about
#: what is in it.
SETUP_SCRIPT = "using Pkg\nPkg.instantiate()\n"

USES_JSON = 'using JSON\nprintln(JSON.json(Dict("ok" => true)))\n'


def julia_package(uploads_folder, user, scripts, project=None, manifest=None):
    """Build and upload a Julia replication package.

    ``scripts`` maps filename to contents, so a package can carry a setup script
    beside its main file. ``project=None`` ships no ``Project.toml`` at all,
    which is an ordinary package now rather than a refusal.
    """
    with (
        tempfile.NamedTemporaryFile(suffix=".tar.gz") as archive,
        tempfile.TemporaryDirectory() as directory,
    ):
        for name, contents in scripts.items():
            with open(os.path.join(directory, name), "w") as handle:
                handle.write(contents)
        if project is not None:
            with open(os.path.join(directory, "Project.toml"), "w") as handle:
                handle.write(project)
        if manifest is not None:
            with open(os.path.join(directory, "Manifest.toml"), "w") as handle:
                handle.write(manifest)
        with tarfile.open(archive.name, "w:gz") as tar:
            tar.add(directory, arcname=".")
        return upload_test_file(uploads_folder, user, archive.name)


def stage(main_file, isolated=False):
    return {
        "image_name": IMAGE,
        "image_tag": TAG,
        "main_file": main_file,
        "network_isolation": isolated,
    }


def read(fobj):
    with File().open(fobj) as handle:
        return handle.read().decode("utf-8", errors="ignore")


def listify(value):
    return value if isinstance(value, list) else ([] if value is None else [value])


def paths(arrangement):
    return {
        location["trov:path"]
        for location in listify(arrangement.get("trov:hasArtifactLocation"))
    }


def isolated(performance):
    key = next(k for k in performance if "ttribute" in k)
    types = {a.get("@type") for a in listify(performance.get(key))}
    return "trov:InternetIsolation" in types


@pytest.mark.plugin("sivacor")
def test_a_setup_stage_installs_what_the_isolated_analysis_then_uses(
    server, db, user, eagerWorkerTasks, fsAssetstore, patched_gpg,
    uploads_folder, submission_collection,
):
    """The whole design, in one submission.

    Two stages the researcher wrote: one that installs, unisolated, and one that
    runs their analysis with the network off. The depot is a sibling of
    ``project/`` inside the workspace, so what the first stage downloaded is
    still there for the second -- which is what makes a setup stage a real
    answer rather than a suggestion.

    The isolation claims are asserted on the declaration, because that is what
    is signed and what a reader later trusts, and against the container
    arguments beside them, because the two could otherwise drift apart without
    either looking wrong on its own.
    """
    fobj = julia_package(
        uploads_folder,
        user,
        {"setup.jl": SETUP_SCRIPT, "main.jl": USES_JSON},
        project=f'[deps]\nJSON = "{JSON_UUID}"\n',
    )
    stages = [stage("setup.jl"), stage("main.jl", isolated=True)]
    resp = submit_sivacor_job(server, user, fobj, stages)
    assertStatusOk(resp)
    job = Job().load(resp.json["_id"], force=True)
    assert job["status"] == JobStatus.SUCCESS

    resp = get_submission_folder(server, user, job["_id"], submission_collection)
    assertStatusOk(resp)
    folder = resp.json[0]
    metadata = folder["meta"]
    assert_submission_metadata(
        metadata, user, job["_id"], stages, "completed",
        ["tro_file_id", "stdout_file_id", "stderr_file_id", "tsr_file_id",
         "replpack_file_id"],
    )

    # The analysis ran with no network and still loaded JSON, so the setup
    # stage's depot survived into it.
    assert '{"ok":true}' in read(File().load(metadata["stdout_file_id"], force=True))

    tro = json.loads(read(File().load(metadata["tro_file_id"], force=True)))
    root = tro["@graph"][0]
    performances = listify(root.get("trov:hasPerformance"))

    # Two stages, two performances -- and both are workflow executions. Neither
    # is a phase of the other.
    assert len(performances) == 2
    setup, analysis = performances
    assert "workflow execution (setup.jl)" in setup["rdfs:comment"]
    assert "workflow execution (main.jl)" in analysis["rdfs:comment"]

    assert not isolated(setup), "the setup stage had the network; it must not claim otherwise"
    assert isolated(analysis), "the analysis was isolated and the TRO must say so"
    assert json.loads(setup["sivacor:DockerRunArgs"])["network_disabled"] is False
    assert json.loads(analysis["sivacor:DockerRunArgs"])["network_disabled"] is True

    # The manifest Pkg wrote appears in the setup stage's own snapshot, so it
    # reads as that stage's output rather than the analysis's.
    arrangements = listify(root.get("trov:hasArrangement"))
    assert "Manifest.toml" not in paths(arrangements[0])
    assert "Manifest.toml" in paths(arrangements[1])
    assert "After executing workflow step 1" in arrangements[1]["rdfs:comment"]

    # One metrics file per stage, named by stage alone.
    names = {
        item["name"]
        for item in server.request(
            path="/item", params={"folderId": folder["_id"], "limit": 100}, user=user,
        ).json
    }
    assert {"performance_data_stage_1.json", "performance_data_stage_2.json"} <= names
    assert not any(name.endswith("_resolve.json") for name in names)


@pytest.mark.plugin("sivacor")
def test_a_lone_julia_stage_runs_exactly_once(
    server, db, user, eagerWorkerTasks, fsAssetstore, patched_gpg,
    uploads_folder, submission_collection,
):
    """Nothing is inserted, and nothing is required.

    A package with no ``Project.toml`` and no setup script is a legitimate Julia
    submission -- the standard library is a great deal of Julia -- and it used
    to be refused outright because SIVACOR insisted on having an environment to
    resolve.
    """
    fobj = julia_package(
        uploads_folder, user, {"main.jl": 'println("no dependencies here")\n'}
    )
    stages = [stage("main.jl", isolated=True)]
    resp = submit_sivacor_job(server, user, fobj, stages)
    assertStatusOk(resp)
    job = Job().load(resp.json["_id"], force=True)
    assert job["status"] == JobStatus.SUCCESS

    resp = get_submission_folder(server, user, job["_id"], submission_collection)
    metadata = resp.json[0]["meta"]
    tro = json.loads(read(File().load(metadata["tro_file_id"], force=True)))
    assert len(listify(tro["@graph"][0].get("trov:hasPerformance"))) == 1

    stdout = read(File().load(metadata["stdout_file_id"], force=True))
    assert "no dependencies here" in stdout
    # The log is stamped once, as one stage's output.
    assert stdout.count("===== Stage 1 Output =====") == 1
    assert "Dependency Resolution" not in stdout

    # And the permanent record counts one stage, which is what was submitted.
    records = list(ExecutionRecord().find({}))
    assert len(records) == 1
    assert records[0]["n_stages"] == 1


@pytest.mark.plugin("sivacor")
def test_a_missing_dependency_fails_in_the_stage_that_needed_it(
    server, db, user, eagerWorkerTasks, fsAssetstore, patched_gpg,
    uploads_folder, submission_collection,
):
    """The cost of the reversal, stated plainly.

    A researcher who declares dependencies and writes no setup stage gets a
    Julia error from their own script rather than a platform message about
    dependency resolution. That is an ordinary non-zero exit, classified like
    any other, and the remedy lives in the docs rather than in a failure code.
    """
    fobj = julia_package(
        uploads_folder,
        user,
        {"main.jl": USES_JSON},
        project=f'[deps]\nJSON = "{JSON_UUID}"\n',
    )
    stages = [stage("main.jl", isolated=True)]
    resp = submit_sivacor_job(server, user, fobj, stages, exception=True)
    assertStatusOk(resp)

    job = Job().load(resp.json["_id"], force=True)
    assert job["status"] == JobStatus.ERROR

    records = list(ExecutionRecord().find({}))
    assert len(records) == 1
    assert records[0]["error"]["code"] == FailureCode.NONZERO_EXIT.value
    assert records[0]["error"]["step"] == "execute_workflow"
    # The exit code is safe to keep forever; nothing about the package is.
    assert isinstance(records[0]["error"]["detail"], int)
    assert "JSON" not in json.dumps(records[0], default=str)

    # Julia's own diagnosis reaches the researcher.
    resp = get_submission_folder(server, user, job["_id"], submission_collection)
    stderr = read(File().load(resp.json[0]["meta"]["stderr_file_id"], force=True))
    assert "JSON" in stderr
