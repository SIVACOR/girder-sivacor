"""Boundary tests for what survives deleting a submission -- girder-sivacor#129.

Read these the way :mod:`tests.test_execution_records` is read: most of them
assert what must *not* survive. The chain of celery jobs is the one part of a
submission that used to outlive both deletion paths, and it did so by accident
rather than by decision -- carrying a ``Girder-Token``, the secrets envelope,
workspace paths, the main file's name and the creator's id, forever.
"""

import datetime
import hashlib
import json

import pytest
from bson.objectid import ObjectId
from girder.models.folder import Folder
from girder.models.setting import Setting
from girder_jobs.constants import JobStatus
from girder_jobs.models.job import Job
from pytest_girder.assertions import assertStatus, assertStatusOk

from girder_sivacor import chain, narration, telemetry
from girder_sivacor.errors import FailureCode, SubmissionError, failure_record
from girder_sivacor.settings import PluginSettings
from girder_sivacor.telemetry import sanitize_failure

from .conftest import (
    get_submission_folder,
    submit_sivacor_job,
    upload_test_file,
)

R_STAGES = [{"image_name": "rocker/r-ver", "image_tag": "4.3.1", "main_file": "main.R"}]

#: Two frames of 2026-10-12's traceback, as ``traceback.format_tb`` emits them.
#: The run died 7.9 s into its 50th hour because the worker could not resolve
#: ``registry-1.docker.io``; these are the frames that *are* girder-sivacor#126.
DNS_FRAMES = [
    ('  File "/girder/plugins/04-girder-sivacor/girder_sivacor/worker_plugin/'
    'lib.py", line 2019, in pull_image\n    failure = _attempt_pull(cli, beat, '
    "image_reference, layers)\n"),
    ('  File "/usr/local/lib/python3.12/site-packages/docker/api/client.py", '
     "line 275, in _raise_for_status\n    raise create_api_error_from_http_"
     "exception(e) from e\n"),
]


def _run_submission(server, user, uploads_folder, submission_collection, name):
    """Run one real submission to completion and return (job_id, folder).

    The chain is then built by :func:`_build_chain`, because the suite runs
    celery eagerly and ``girder_before_task_publish`` -- the hook that creates
    a job document per step -- only fires on a real publish. Every other test
    that needs child jobs synthesizes them the same way; see
    ``test_job_routes.py``.
    """
    test_file = upload_test_file(uploads_folder, user, name)
    resp = submit_sivacor_job(server, user, test_file, R_STAGES)
    assertStatusOk(resp)
    job_id = resp.json["_id"]
    assert Job().load(job_id, force=True)["status"] == JobStatus.SUCCESS
    resp = get_submission_folder(server, user, job_id, submission_collection)
    assertStatusOk(resp)
    folder = Folder().load(resp.json[0]["_id"], force=True)
    _build_chain(user, job_id, folder)
    return job_id, folder


#: One job per step of the chain ``build_submission_chain`` publishes, with the
#: argument shapes that matter to the redaction: the head carries the
#: submission's job id positionally at ``args[3]``, everything after it gets the
#: previous step's return value at ``args[0]``, and ``execute_workflow`` also
#: carries its stage and the secrets envelope.
_CHAIN_STEPS = (
    # Verbatim from build_submission_chain: the head step's title is generated
    # from the uploaded file's name, which is the leak this fixture exists to
    # reproduce. 881 of the 5,175 chain jobs in a restored production database
    # are this shape.
    "Moving jane-thesis-2026-replication.zip to submission collection",
    "Create Workspace",
    "Fetch Analysis Images",
    "Record initial arrangement",
    "Execute SIVACOR Workflow",
    "Record final arrangement",
    "Record user workflow TRP",
    "Prune Workspace",
    "Sign TRO",
    "Upload Replicated Package",
    "Finalize Job Submission",
)


def _build_chain(user, job_id, folder):
    """Create the chain job documents a real publish would have created."""
    submission = {
        "job_id": str(job_id),
        "folder_id": str(folder["_id"]),
        "file_id": "f" * 24,
        "tmp_dir": f"/tmp/tmp-{folder['_id']}",
        "workspace_dir": f"/tmp/workspace-{folder['_id']}",
        "queue": "sivacor-worker-0e1f2a3b",
        "stages": R_STAGES,
    }
    parent = Job().load(job_id, force=True)
    for index, title in enumerate(_CHAIN_STEPS):
        if index == 0:
            args = [str(user["_id"]), "f" * 24, R_STAGES, str(job_id), None, None]
        elif title == "Execute SIVACOR Workflow":
            args = [submission, R_STAGES[0], {"envelope": "AQIDBA==", "key": "zzz"}]
        else:
            args = [submission]
        job = Job().createJob(
            title=title,
            type="celery",
            user=user,
            parentJob=parent,
            args=args,
            otherFields={"celeryTaskId": f"task-{index}"},
        )
        Job().collection.update_one(
            {"_id": job["_id"]},
            {"$set": {
                "status": JobStatus.SUCCESS,
                "celeryParentTaskId": f"task-{index - 1}" if index else None,
                "jobInfoSpec": {
                    "url": f"http://girder/api/v1/job/{job['_id']}",
                    "method": "PUT",
                    "headers": {"Girder-Token": "deadbeefdeadbeef"},
                },
                "log": [
                    (
                        "Starting log publisher for stata_1 on Redis channel "
                        f"docker:logs:{user['_id']}\n"
                    ),
                    "insheet using /home/jane/confidential.dta\n",
                ],
            }},
        )
        parent = job


def _chain(job_id):
    return list(Job().collection.find(chain.chain_query(job_id)))


def _skeletons(job_id, chain_key):
    """Every redacted document of one chain, found the way an operator would.

    Deliberately not by ``chain_query``: ``args`` is gone, so that query can no
    longer see these. The pseudonym is what groups them now, and a test that
    cheated here would not notice if it stopped working.
    """
    return list(
        Job().collection.find({f"meta.{chain.REDACTED_KEY}.chain": chain_key})
    )


@pytest.mark.plugin("sivacor")
def test_deleting_a_submission_redacts_its_chain(
    server,
    db,
    user,
    eagerWorkerTasks,
    fsAssetstore,
    patched_gpg,
    uploads_folder,
    submission_collection,
):
    """The acceptance criterion, end to end on a real run.

    After DELETE no chain job retains a log, a token, a secrets envelope, a
    filename or a reversible user id -- and the chain still answers step order,
    per-step status and which image each stage used.
    """
    job_id, folder = _run_submission(
        server, user, uploads_folder, submission_collection, "with_space_R.zip"
    )
    before = _chain(job_id)
    # The thing being fixed: this is what Delete used to leave behind.
    assert before, "the submission produced no chain jobs"
    assert any(job.get("jobInfoSpec") for job in before)
    assert any(job.get("args") for job in before)

    resp = server.request(
        path=f"/sivacor/submission/{folder['_id']}", method="DELETE", user=user
    )
    assertStatusOk(resp)

    chain_key = chain.pseudonym(job_id, prefix="c-")
    after = _skeletons(job_id, chain_key)
    assert len(after) == len(before)

    user_key = chain.pseudonym(str(user["_id"]), prefix="u-")
    for job in after:
        # Nothing that identifies, authenticates or quotes the researcher.
        for field in ("log", "jobInfoSpec", "args", "kwargs"):
            assert field not in job, f"{field} survived on {job['title']}"
        assert job.get("userId") is None
        assert "jane-thesis" not in repr(job)
        skeleton = job["meta"][chain.REDACTED_KEY]
        assert skeleton["version"] == chain.REDACTION_VERSION
        assert skeleton["user"] == user_key
        assert str(user["_id"]) not in repr(job)
        assert str(folder["_id"]) not in repr(job)
        # ...and everything the incident appendix marks load-bearing.
        assert job["title"]
        assert "status" in job
        assert "timestamps" in job
        assert "created" in job and "updated" in job

    titles = [job["title"] for job in after]
    assert "Execute SIVACOR Workflow" in titles
    assert "Sign TRO" in titles
    # The one generated title, rewritten to a constant. The step is still
    # identifiable; the uploaded file's name is not in it.
    assert telemetry.HEAD_STEP_TITLE in titles
    assert not any("jane-thesis" in title for title in titles)
    stages = [
        job["meta"][chain.REDACTED_KEY]["stage"]
        for job in after
        if job["meta"][chain.REDACTED_KEY].get("stage")
    ]
    assert stages == [{
        "image_name": "rocker/r-ver",
        "image_tag": "4.3.1",
        "network_isolation": False,
    }]


@pytest.mark.plugin("sivacor")
def test_the_platforms_narration_survives_the_submission(
    server,
    db,
    user,
    eagerWorkerTasks,
    fsAssetstore,
    patched_gpg,
    uploads_folder,
    submission_collection,
):
    """Finding A: the most-quoted source used to be destroyed by both paths.

    The ``dropped`` assertion is the rot alarm for
    :mod:`girder_sivacor.narration`. A new line written to a submission job's
    log, with no template for it, fails **here** rather than quietly vanishing
    from every future post-mortem.
    """
    job_id, folder = _run_submission(
        server, user, uploads_folder, submission_collection, "with_space_R.zip"
    )
    log = Job().load(job_id, force=True, includeLog=True)["log"]
    assert log, "the submission job wrote no narration to copy"

    resp = server.request(
        path=f"/sivacor/submission/{folder['_id']}", method="DELETE", user=user
    )
    assertStatusOk(resp)

    skeletons = _skeletons(job_id, chain.pseudonym(job_id, prefix="c-"))
    narrated = [
        job for job in skeletons if "narration" in job["meta"][chain.REDACTED_KEY]
    ]
    # The head of the chain, and only the head.
    assert len(narrated) == 1
    skeleton = narrated[0]["meta"][chain.REDACTED_KEY]
    # Named rather than counted, so the failure says which line needs a
    # template rather than how many do.
    unmatched = [
        line
        for chunk in log
        for line in chunk.splitlines()
        if line.strip() and narration._keep(line) is None
    ]
    assert unmatched == [], (
        "the submission job logged lines girder_sivacor.narration has no "
        f"template for, so every future post-mortem loses them: {unmatched}. "
        "Add the template -- see that module's docstring."
    )
    assert skeleton["narration_dropped"] == 0
    text = "\n".join(skeleton["narration"])
    assert "Creating workspace from source folder." in text
    assert "Uploading executed replication package to Girder." in text
    # The uploaded package's name is in the submission job's log and must not
    # be in the copy.
    assert "with_space_R" not in text


@pytest.mark.plugin("sivacor")
def test_redaction_is_idempotent(
    server,
    db,
    user,
    eagerWorkerTasks,
    fsAssetstore,
    patched_gpg,
    uploads_folder,
    submission_collection,
):
    job_id, folder = _run_submission(
        server, user, uploads_folder, submission_collection, "with_space_R.zip"
    )
    assertStatusOk(
        server.request(
            path=f"/sivacor/submission/{folder['_id']}", method="DELETE", user=user
        )
    )
    chain_key = chain.pseudonym(job_id, prefix="c-")
    first = {job["_id"]: job["meta"][chain.REDACTED_KEY] for job in
             _skeletons(job_id, chain_key)}
    assert first
    # A second pass must change nothing -- not even the timestamp, which is
    # what a merely-repeated write would move.
    assert chain.redact_chain(job_id) == 0
    again = {job["_id"]: job["meta"][chain.REDACTED_KEY] for job in
             _skeletons(job_id, chain_key)}
    assert again == first


# --------------------------------------------------------------------------
# The two tracebacks the appendix says must survive, replayed.
# --------------------------------------------------------------------------


def _chain_job(user, title="Execute SIVACOR Workflow", submission_id="x" * 24):
    """A chain job shaped like the ones production holds."""
    job = Job().createJob(
        title=title,
        type="celery",
        user=user,
        args=[{"job_id": submission_id, "folder_id": "f" * 24}, {
            "image_name": "dynare/dynare",
            "image_tag": "6.5-R2025b",
            "main_file": "jane-thesis-replication.m",
        }, {"envelope": "AQIDBA=="}],
    )
    Job().collection.update_one(
        {"_id": job["_id"]},
        {"$set": {
            "jobInfoSpec": {"url": "http://girder/api/v1/job/x", "headers": {
                "Girder-Token": "deadbeef"}},
            "log": [
                (
                    "Starting log publisher for stata_1 on Redis channel "
                    f"docker:logs:{user['_id']}\n"
                ),
                "insheet using /home/jane/confidential.dta\n",
            ],
        }},
    )
    return Job().load(job["_id"], force=True)


@pytest.mark.plugin("sivacor")
def test_the_dns_incidents_traceback_survives_redaction(server, db, user):
    """2026-10-12: the ``APIError`` frames are girder-sivacor#126 itself."""
    import docker.errors

    job = _chain_job(user)
    exc = docker.errors.APIError(
        "Get \"https://registry-1.docker.io/v2/\": dial tcp: lookup "
        "registry-1.docker.io: no such host"
    )
    assertStatusOk(
        server.request(
            path=f"/sivacor/failure/{job['_id']}",
            method="PUT",
            user=user,
            body=json.dumps(failure_record(exc, DNS_FRAMES)),
            type="application/json",
        )
    )
    chain.redact_job(
        Job().load(job["_id"], force=True, includeLog=True), chain_key="c-1"
    )

    redacted = Job().load(job["_id"], force=True, includeLog=True)
    failure = redacted["meta"][chain.FAILURE_KEY]
    assert failure["type"] == "docker.errors.APIError"
    assert "registry-1.docker.io" in failure["message"]
    assert len(failure["frames"]) == 2
    assert "pull_image" in failure["frames"][0]
    assert "_raise_for_status" in failure["frames"][1]
    # ...and none of the things that used to ride in the same field.
    assert "log" not in redacted
    assert "args" not in redacted
    assert "jobInfoSpec" not in redacted
    assert "confidential.dta" not in repr(redacted)
    assert str(user["_id"]) not in repr(redacted)


@pytest.mark.plugin("sivacor")
def test_a_lost_state_transition_survives_redaction(server, db, user):
    """2026-09-25: the line that proved the worker was alive and disagreeing."""
    from girder_worker.utils import StateTransitionException

    job = _chain_job(user, title="Upload Replicated Package")
    exc = StateTransitionException("Invalid state transition to '3', Current state is '4'")
    assertStatusOk(
        server.request(
            path=f"/sivacor/failure/{job['_id']}",
            method="PUT",
            user=user,
            body=json.dumps(failure_record(exc, DNS_FRAMES[:1])),
            type="application/json",
        )
    )
    chain.redact_job(
        Job().load(job["_id"], force=True, includeLog=True), chain_key="c-1"
    )

    failure = Job().load(job["_id"], force=True)["meta"][chain.FAILURE_KEY]
    assert failure["type"] == "girder_worker.utils.StateTransitionException"
    assert "Invalid state transition to '3'" in failure["message"]


@pytest.mark.plugin("sivacor")
def test_a_job_token_may_write_its_own_jobs_failure(server, db, user):
    """The authorization that makes the endpoint usable from the worker.

    girder_worker's failure hook runs with the per-job token out of
    ``jobInfoSpec`` -- scope ``jobs.job_<id>`` and nothing else -- so this
    cannot be ``@access.admin`` like its neighbours. The same token must not
    reach a different job.
    """
    job = _chain_job(user)
    other = _chain_job(user, title="Sign TRO")
    token = Job().createJobToken(job)
    body = json.dumps(failure_record(RuntimeError("boom"), DNS_FRAMES[:1]))

    assertStatusOk(
        server.request(
            path=f"/sivacor/failure/{job['_id']}",
            method="PUT",
            token=token,
            body=body,
            type="application/json",
        )
    )
    assertStatus(
        server.request(
            path=f"/sivacor/failure/{other['_id']}",
            method="PUT",
            token=token,
            body=body,
            type="application/json",
        ),
        401,
    )


@pytest.mark.plugin("sivacor")
def test_a_researcher_facing_message_is_not_kept(db):
    """``SubmissionError``'s text is the researcher's by contract.

    The operator-facing half of it -- the code and the allow-listed detail --
    is, which is the whole reason the class carries both.
    """
    exc = SubmissionError(
        FailureCode.MAIN_FILE_MISSING,
        "Could not find 'jane-thesis-2026/analysis/master.do' in the package.",
    )
    record = sanitize_failure(failure_record(exc, DNS_FRAMES))
    assert record["message"] is None
    assert record["code"] == "main_file_missing"
    assert record["type"] == "girder_sivacor.errors.SubmissionError"
    assert len(record["frames"]) == 2


@pytest.mark.plugin("sivacor")
def test_sanitize_failure_rebuilds_rather_than_trusts(db):
    """The boundary, probed the way ``sanitize_record`` is probed."""
    # An unknown type keeps nothing of its message...
    assert sanitize_failure(
        {"type": "mypkg.Boom", "message": "/home/jane/thesis.dta", "frames": []}
    )["message"] is None
    # ...a frame that is not format_tb output is not stored at all...
    assert sanitize_failure(
        {"type": "docker.errors.APIError", "frames": ["r(601) in master.do"]}
    )["frames"] == []
    # ...nor is one naming the workspace, which is where their files live.
    workspace_frame = (
        '  File "/tmp/workspace-abc/project/run.py", line 1, in <module>\n'
    )
    assert sanitize_failure(
        {"type": "docker.errors.APIError", "frames": [workspace_frame]}
    )["frames"] == []
    # A field the caller invented does not survive.
    assert "folder_id" not in sanitize_failure(
        {"type": "docker.errors.APIError", "folder_id": "abc", "frames": []}
    )
    # Nothing recognisable at all is not worth a field.
    assert sanitize_failure({"type": "/home/jane", "code": "nope"}) is None
    assert sanitize_failure("not a dict") is None


# --------------------------------------------------------------------------
# The narration filter on its own.
# --------------------------------------------------------------------------


@pytest.mark.plugin("sivacor")
def test_a_step_title_is_allow_listed_not_trusted(db):
    """Measured against a restored production database, not invented.

    Of its 5,175 chain jobs, exactly 14 distinct titles pass this rule and
    every one is a step name we wrote -- including three the current builder no
    longer produces. 1,324 fail: 883 are the head step carrying an uploaded
    filename, 441 are Girder's own ``deleteFolderTask``, whose title is the
    literal string ``<unnamed job>``.
    """
    # Retired step names still pass, which is the point of a charset rule over
    # a list of today's strings: a job document outlives the release that
    # titled it.
    for title in (
        "Execute SIVACOR Workflow",
        "Sign TRO",
        "Record Performance Metrics",
        "Resolve declared dependencies",
        "Cleanup submission folder",
    ):
        assert telemetry.safe_title(title) == title
    # The generated one loses its filename and keeps its meaning.
    assert (
        telemetry.safe_title("Moving final_project(3).zip to submission collection")
        == telemetry.HEAD_STEP_TITLE
    )
    # Everything else becomes a constant, carrying nothing out.
    for title in ("<unnamed job>", "", None, "/home/jane/thesis.dta", "a" * 200):
        assert telemetry.safe_title(title) == telemetry.REDACTED_TITLE


@pytest.mark.plugin("sivacor")
def test_the_pseudonym_prefers_the_documents_own_user_id(server, db, user):
    """``userId`` is set on every chain job a real deployment holds.

    The issue reported it as ``None`` -- "they look anonymous and are not" --
    and a restored production database disagrees on all 5,175. Scraping the log
    only ever worked for a job that started a container; this works for the
    whole chain.
    """
    job = _chain_job(user, submission_id="0" * 24)
    # No docker:logs line at all, so the fallback has nothing to find.
    Job().collection.update_one({"_id": job["_id"]}, {"$set": {"log": ["quiet\n"]}})

    chain.redact_job(
        Job().load(job["_id"], force=True, includeLog=True), chain_key="c-1"
    )
    redacted = Job().load(job["_id"], force=True)
    assert redacted["meta"][chain.REDACTED_KEY]["user"] == chain.pseudonym(
        str(user["_id"]), prefix="u-"
    )
    assert redacted["userId"] is None
    assert str(user["_id"]) not in repr(redacted)


@pytest.mark.plugin("sivacor")
def test_narration_keeps_the_templates_and_drops_the_rest(db):
    kept, dropped = narration.scrub(
        [
            (
                "[2026-10-12T03:14:15-05:00] Preparing SIVACOR submission\n"
                "[2026-10-12T03:14:16-05:00] New submission: 'ferocious-fader'"
                " created.\n"
            ),
            "Still pulling dynare/dynare:6.5-R2025b (39 layers seen)\n",
            "All analysis images are ready. 23.7 GiB free on the worker's own disk.\n",
            (
                "[2026-10-12T03:15:00-05:00] Failed to execute workflow: \n"
                "\tr(601) file /home/jane/confidential.dta not found\n"
            ),
        ]
    )
    assert "Preparing SIVACOR submission" in kept[0]
    assert "ferocious-fader" in kept[1]
    assert kept[2] == "Still pulling dynare/dynare:6.5-R2025b (39 layers seen)"
    assert kept[3].endswith("23.7 GiB free on the worker's own disk.")
    # The failure *headline* is kept -- which step failed is the most useful
    # line in a failed submission's log -- and the Stata output it interpolated
    # on the line below is dropped, and counted.
    assert kept[4] == "[2026-10-12T03:15:00-05:00] Failed to execute workflow:"
    assert dropped == 1
    assert "confidential.dta" not in "\n".join(kept)
    assert "r(601)" not in "\n".join(kept)


#: Every line *shape* a real deployment's submission logs were found to
#: contain, when all 228 of them were audited line by line on 2026-10-17.
#:
#: **Shapes, with invented values.** The platform half below is our own source
#: prose and is reproduced as-is; the researcher half further down is
#: deliberately *not* -- those are made-up filenames and paths, because
#: pasting the real ones into a committed fixture would be the exact leak this
#: module exists to prevent.
#:
#: The audit is what this fixture preserves. It found the templates had already
#: rotted before shipping: 14% of real narration lines matched nothing, and
#: most of the non-detail half were **retired wordings** -- the line now
#: reading "Running TRO utilities *in the* workspace" used to read "*on*
#: workspace", and three "...successfully." lines no release still writes. A
#: template set written by reading today's source cannot see any of that.
_PLATFORM_LINES = [
    "[2026-10-02T09:42:36-05:00] Preparing SIVACOR submission",
    "[2026-10-02T09:44:43-05:00] New submission: 'exothermic-belt' created.",
    "[2026-10-02T09:44:44-05:00] Creating workspace from source folder.",
    ("[2026-10-02T09:44:45-05:00] Running TRO utilities in the workspace."
     " (add_arrangement)"),
    "[2026-10-02T09:44:46-05:00] Executing workflow on workspace.",
    "Starting recorded run",
    "Still pulling dynare/dynare:6.5-R2025b (39 layers seen)",
    "Pulled dynare/dynare:6.5-R2025b (39 layers)",
    ("[2026-10-02T09:55:01-05:00] Running TRO utilities in the workspace."
     " (add_performance)"),
    ("[2026-10-02T09:55:02-05:00] Running TRO utilities in the workspace."
     " (prune_performance)"),
    "[2026-10-02T09:55:02-05:00] Running TRO utilities in the workspace. (sign)",
    "[2026-10-02T09:55:03-05:00] Uploading executed replication package to Girder.",
    "[2026-10-02T09:55:09-05:00] Submission job finalized successfully.",
    # Retired wordings. Deleting their templates would not delete these lines,
    # it would only make them unreadable.
    "Running TRO utilities on workspace. (add_arrangement)",
    "TRO utilities completed successfully. (sign)",
    "Submission prepared successfully.",
    "Workspace created successfully.",
    "Workflow executed successfully.",
    # girder_jobs' own, and the only explanation a reader gets for that abort.
    ("Job aborted due to system restart. We apologize for any inconvenience."
     " Please try again."),
    # The headline of a failure; its detail is the next list down.
    "[2026-10-02T09:55:09-05:00] Failed to execute workflow: ",
    "Failed to run TRO utilities: ",
]

#: The other half of the same logs: the tab-indented exception text
#: ``report_failure`` interpolates. 127 of the 129 lines still dropped after
#: the audit are this shape. **Invented values, real shapes** -- see above.
_DETAIL_LINES = [
    "\tStata returned an error (r(601);). Check stdout/stderr for details.",
    "\tError executing recorded run. Check stdout/stderr for details.",
    "\tCannot infer run command for submission. No ThesisMaster.m found.",
    "\tCannot infer run command for submission. No Programs/R/make_figures.R found.",
    ("\t[Errno 13] Permission denied: '/tmp/workspace-6b1a3efcc1eaed9b2f/renv/"
     "library/R-4.3/x86_64-pc-linux-gnu/generics'"),
    ("\t[Errno 13] Permission denied: '/tmp/workspace-9f1ae3ed7afe4cb2/project/"
     "SomeJournalReplicationFile/?/.java/fonts/1.8.0_292'"),
    "\t[Errno 20] Not a directory: '/tmp/workspace-7a4f2d9e1b3/renv.lock/main.do'",
    "\t[Errno 2] No such file or directory: '/tmp/tmp7hpqnwjw/stdout'",
    "\t'utf-8' codec can't decode byte 0xfc in position 21: invalid start byte",
    "\tsequence item 0: expected str instance, PosixPath found",
    "\t'F3DB4F1AD0DC9DBB1AFF2A'",
]


@pytest.mark.plugin("sivacor")
def test_every_platform_line_a_real_deployment_wrote_has_a_template(db):
    """The audit, pinned. See ``_PLATFORM_LINES``."""
    unmatched = [line for line in _PLATFORM_LINES if narration._keep(line) is None]
    assert unmatched == [], (
        "these lines are in a real deployment's logs and would be dropped from "
        f"every future post-mortem: {unmatched}"
    )


@pytest.mark.plugin("sivacor")
def test_the_exception_detail_a_failure_interpolates_is_never_kept(db):
    """The other half of the same audit, and why the headline is split from it.

    Every one of these carries the researcher's: a Stata return code, their
    main file's name, a path inside their package. Since girder-sivacor#129 the
    operator-facing version of the same failure lives in
    ``meta.sivacorFailure``, so dropping these costs a post-mortem nothing.
    """
    kept, dropped = narration.scrub(_DETAIL_LINES)
    assert kept == []
    assert dropped == len(_DETAIL_LINES)
    text = "\n".join(narration.scrub(_PLATFORM_LINES + _DETAIL_LINES)[0])
    for leak in (
        "r(601)",
        "ThesisMaster.m",
        "make_figures.R",
        "/tmp/workspace-",
        "renv.lock",
        "F3DB4F1AD0DC9DBB1AFF2A",
    ):
        assert leak not in text
    # ...while the headline that says *which step* failed is kept.
    assert any("Failed to execute workflow" in line for line in
               narration.scrub(_PLATFORM_LINES + _DETAIL_LINES)[0])


@pytest.mark.plugin("sivacor")
def test_narration_truncates_a_line_with_a_free_text_tail(db):
    kept, dropped = narration.scrub(
        [
            (
                "Could not pull rocker/r-ver:4.3.1 (attempt 1 of 4); retrying "
                "in 5s. Docker reported: Get https://registry: i/o timeout\n"
            )
        ]
    )
    assert dropped == 0
    assert kept == [
        "Could not pull rocker/r-ver:4.3.1 (attempt 1 of 4); retrying in 5s."
        + narration.ELLIPSIS
    ]


# --------------------------------------------------------------------------
# One definition of "this submission's chain", and the backfill.
# --------------------------------------------------------------------------


@pytest.mark.plugin("sivacor")
def test_the_retention_sweep_removes_the_head_of_the_chain(
    server,
    db,
    admin,
    user,
    eagerWorkerTasks,
    fsAssetstore,
    patched_gpg,
    uploads_folder,
    submission_collection,
):
    """The sweep used to match only ``args.0.job_id``.

    ``prepare_submission`` is published positionally and carries the job id at
    ``args[3]``, so the head of every expired chain was left behind -- which is
    how production came to hold chain jobs older than any submission.
    """
    job_id, folder = _run_submission(
        server, user, uploads_folder, submission_collection, "with_space_R.zip"
    )
    head = Job().findOne({"type": "celery", "args.3": str(job_id)})
    assert head is not None, "the head of the chain is matched by args.3 alone"
    assert head["_id"] not in {
        job["_id"]
        for job in Job().collection.find({"args.0.job_id": str(job_id)})
    }

    Folder().update(
        {"_id": folder["_id"]},
        {"$set": {
            "created": datetime.datetime.now(datetime.UTC)
            - datetime.timedelta(days=365)
        }},
    )
    from girder_sivacor.worker_plugin.run_submission import cleanup_submissions

    cleanup_submissions()
    assert _chain(job_id) == []
    assert Job().load(head["_id"], force=True) is None


@pytest.mark.plugin("sivacor")
def test_backfill_redacts_orphans_and_leaves_live_chains_alone(server, db, user, admin):
    """Step 4: the 1,450 documents that predate the forward fix."""
    live_submission = Job().createJob(title="live", type="sivacor_submission", user=user)
    live = _chain_job(user, submission_id=str(live_submission["_id"]))
    orphan = _chain_job(user, submission_id="0" * 24)

    resp = server.request(
        path="/sivacor/redact_orphans", method="POST", user=admin, params={"limit": 50}
    )
    assertStatusOk(resp)
    assert resp.json["redacted"] == 1
    assert resp.json["live"] == 1
    assert resp.json["next"] is None

    # includeLog throughout: Job().load projects the field away by default, so
    # a check written without it passes whether or not the log is still there.
    redacted = Job().load(orphan["_id"], force=True, includeLog=True)
    assert "log" not in redacted
    # Finding C: the creator is recovered from the log's docker:logs line --
    # for these documents it is the only place the account survives at all --
    # and kept only as a pseudonym.
    assert redacted["meta"][chain.REDACTED_KEY]["user"] == chain.pseudonym(
        str(user["_id"]), prefix="u-"
    )
    assert str(user["_id"]) not in repr(redacted)

    # Untouched, because its submission is still running.
    assert Job().load(live["_id"], force=True, includeLog=True).get("log")

    # Idempotent: a second pass finds nothing left to do.
    resp = server.request(
        path="/sivacor/redact_orphans", method="POST", user=admin, params={"limit": 50}
    )
    assertStatusOk(resp)
    assert resp.json["redacted"] == 0


@pytest.mark.plugin("sivacor")
def test_backfill_is_bounded_and_pages(server, db, user, admin):
    for _ in range(3):
        _chain_job(user, submission_id="0" * 24)

    resp = server.request(
        path="/sivacor/redact_orphans", method="POST", user=admin, params={"limit": 2}
    )
    assertStatusOk(resp)
    assert resp.json["examined"] == 2
    assert resp.json["redacted"] == 2
    assert resp.json["next"]

    resp = server.request(
        path="/sivacor/redact_orphans",
        method="POST",
        user=admin,
        params={"limit": 2, "after": resp.json["next"]},
    )
    assertStatusOk(resp)
    assert resp.json["redacted"] == 1


@pytest.mark.plugin("sivacor")
def test_the_head_step_is_found_by_its_positional_job_id(db, user):
    """``args[3]``, which is the half of ``chain_query`` that was missing.

    ``prepare_submission`` is published positionally, so it is the one step
    whose submission is not at ``args[0]["job_id"]``. The sweep matched only
    the latter, which is how a restored production database comes to hold chain
    jobs older than any submission.
    """
    head = Job().createJob(
        title="Moving thesis.zip to submission collection",
        type="celery",
        user=user,
        args=[str(user["_id"]), "f" * 24, R_STAGES, "a" * 24, 60, None],
    )
    rest = Job().createJob(
        title="Create Workspace",
        type="celery",
        user=user,
        args=[{"job_id": "a" * 24}],
    )
    assert chain.submission_id_of(Job().load(head["_id"], force=True)) == "a" * 24
    assert chain.submission_id_of(Job().load(rest["_id"], force=True)) == "a" * 24
    found = {j["_id"] for j in Job().collection.find(chain.chain_query("a" * 24))}
    assert found == {head["_id"], rest["_id"]}
    # And a job that names no submission at all is not attributed to one.
    assert chain.submission_id_of({"args": ["just-a-folder-id"]}) is None
    assert chain.submission_id_of({}) is None


@pytest.mark.plugin("sivacor")
def test_redact_job_itself_refuses_to_run_twice(db, user):
    """Not the same assertion as the backfill's.

    ``redact_orphans`` skips a redacted document in its *query*, so a second
    pass there proves nothing about this guard -- and neither does
    ``redact_chain``, which after a redaction finds nothing at all because
    ``args`` is what ``chain_query`` matches on. This is the only thing
    standing between a re-run and a skeleton whose ``at`` has been moved and
    whose narration has been replaced by a later caller's.
    """
    job = _chain_job(user, submission_id="0" * 24)
    assert chain.redact_job(
        Job().load(job["_id"], force=True, includeLog=True), chain_key="c-1"
    )
    first = Job().load(job["_id"], force=True)["meta"][chain.REDACTED_KEY]

    assert not chain.redact_job(Job().load(job["_id"], force=True), chain_key="c-2")
    again = Job().load(job["_id"], force=True)["meta"][chain.REDACTED_KEY]
    assert again == first
    assert again["chain"] == "c-1"


@pytest.mark.plugin("sivacor")
def test_the_creator_is_recovered_from_the_log_when_there_is_no_user_id(db, user):
    """The fallback, for the older documents that are why it exists."""
    job = _chain_job(user, submission_id="0" * 24)
    Job().collection.update_one({"_id": job["_id"]}, {"$set": {"userId": None}})

    # Deliberately loaded *without* includeLog, which is how Job().load behaves
    # by default: the fallback has to go back to the collection for the log
    # rather than conclude there is none.
    chain.redact_job(Job().load(job["_id"], force=True), chain_key="c-1")
    skeleton = Job().load(job["_id"], force=True)["meta"][chain.REDACTED_KEY]
    assert skeleton["user"] == chain.pseudonym(str(user["_id"]), prefix="u-")

    # Nothing to find is not an error; it is a chain with no pseudonym.
    other = _chain_job(user, submission_id="0" * 24)
    Job().collection.update_one(
        {"_id": other["_id"]}, {"$set": {"userId": None, "log": ["quiet\n"]}}
    )
    chain.redact_job(Job().load(other["_id"], force=True), chain_key="c-1")
    assert (
        Job().load(other["_id"], force=True)["meta"][chain.REDACTED_KEY]["user"] is None
    )
    assert chain._creator_from_log(None) is None
    assert chain._creator_from_log([b"not a string"]) is None
    assert chain._creator_from_log("docker:logs:" + "a" * 24) == "a" * 24


@pytest.mark.plugin("sivacor")
def test_a_step_with_no_stage_records_none(db, user):
    """Only ``execute_workflow`` carries an image; the rest are bookkeeping."""
    assert chain._stage_of({"args": [{"job_id": "x"}]}) is None
    assert chain._stage_of({"args": [{}, "add_arrangement", 0]}) is None
    assert chain._stage_of({}) is None
    # ...and an image name that is not one is not stored.
    assert chain._stage_of({"args": [{}, {"image_name": "/home/jane/x"}]}) is None


@pytest.mark.plugin("sivacor")
def test_the_backfill_leaves_alone_what_it_cannot_attribute(server, db, user, admin):
    """611 documents in a restored production database take this path.

    Girder's own ``deleteFolderTask`` (``[folderId, progress, userId]``) and the
    retired ``Cleanup submission folder`` (``[folderId]``) are ``type:
    "celery"`` and name no submission. Not touching them is the fail-closed
    answer -- it is also the case where we cannot tell whether a live run is
    reading them.
    """
    stray = Job().createJob(
        title="<unnamed job>",
        type="celery",
        user=user,
        args=[str(ObjectId()), False, str(user["_id"])],
    )
    # An args[3] that is not a valid ObjectId, which 25 real documents have. A
    # malformed id cannot name a live submission, so this one IS redacted.
    malformed = Job().createJob(
        title="Moving old.zip to submission collection",
        type="celery",
        user=user,
        args=["u", "f", [], "not-an-object-id", 60],
    )

    resp = server.request(
        path="/sivacor/redact_orphans", method="POST", user=admin, params={"limit": 50}
    )
    assertStatusOk(resp)
    assert resp.json["unknown"] == 1
    assert resp.json["redacted"] == 1

    assert chain.REDACTED_KEY not in (
        Job().load(stray["_id"], force=True).get("meta") or {}
    )
    assert Job().load(stray["_id"], force=True, includeLog=True).get("args")
    assert chain.REDACTED_KEY in Job().load(malformed["_id"], force=True)["meta"]


@pytest.mark.plugin("sivacor")
def test_a_very_long_narration_is_kept_from_both_ends(db):
    """A ProgressBeat ticks for the length of the run.

    A 50-hour submission is thousands of "still pulling" lines, and a
    post-mortem reads the start and the end of a run, not its middle.
    """
    line = "Still pulling dynare/dynare:6.5-R2025b (39 layers seen)"
    kept, dropped = narration.scrub([f"{line}\n" * (narration.MAX_LINES + 50)])
    assert dropped == 0
    assert len(kept) == narration.MAX_LINES + 1
    assert kept[0] == line
    assert kept[-1] == line
    assert "50 lines" in kept[narration.MAX_LINES // 2]


@pytest.mark.plugin("sivacor")
def test_narration_cannot_be_fed_something_that_is_not_a_log(db):
    """``scrub`` is a boundary, so it takes whatever the document holds."""
    assert narration.scrub(None) == ([], 0)
    assert narration.scrub([]) == ([], 0)
    assert narration.scrub([None, 42, {"not": "a line"}]) == ([], 3)
    # A bare string, which is Girder's legacy log shape.
    assert narration.scrub("Starting recorded run\n") == (["Starting recorded run"], 0)
    # Blank lines are neither kept nor counted as lost.
    assert narration.scrub(["\n   \n"]) == ([], 0)


@pytest.mark.plugin("sivacor")
def test_redact_orphans_refuses_nonsense_rather_than_500ing(server, db, admin):
    for params in ({"limit": 0}, {"limit": 100000}, {"after": "not-an-id"}):
        assertStatus(
            server.request(
                path="/sivacor/redact_orphans",
                method="POST",
                user=admin,
                params=params,
            ),
            400,
        )


@pytest.mark.plugin("sivacor")
def test_redact_orphans_is_admin_only(server, db, user):
    assertStatus(
        server.request(path="/sivacor/redact_orphans", method="POST", user=user), 403
    )


@pytest.mark.plugin("sivacor")
def test_pseudonyms_are_stable_salted_and_revocable(db):
    """The salt is the kill switch; see girder_sivacor.chain."""
    first = chain.pseudonym("abc", prefix="u-")
    assert first.startswith("u-")
    assert chain.pseudonym("abc", prefix="u-") == first
    assert chain.pseudonym("abd", prefix="u-") != first
    assert chain.pseudonym(None) is None
    # Keyed, not a bare digest: a Girder object id is 24 hex characters from a
    # space small enough to enumerate, so an unsalted hash would be reversible
    # by anyone who could list the users.
    assert first != "u-" + hashlib.sha256(b"abc").hexdigest()[:16]

    salt = Setting().get(PluginSettings.PSEUDONYM_SALT)
    assert salt and len(salt) >= 32
    Setting().unset(PluginSettings.PSEUDONYM_SALT)
    assert chain.pseudonym("abc", prefix="u-") != first
