"""Getting a :class:`SubmissionError` out of a celery task intact.

Every failure the pipeline classifies is raised inside a celery task and
re-raised by :func:`submission_task`'s wrapper. Celery pickles whatever leaves
a task; when the round-trip fails it swaps in
``celery.utils.serialization.UnpickleableExceptionWrapper`` and logs the task as
having "raised unexpected". On 2026-08-13 an OOM-killed Stata run in production
did exactly that::

    UnpickleableExceptionWrapper('girder_sivacor.errors', 'SubmissionError',
        ('The analysis used more memory than this worker allows ...',), ...)

because ``Exception.__reduce__`` rebuilds from ``self.args`` -- ``(message,)``
-- and ``SubmissionError(message)`` is one argument short of the signature. The
researcher's text survived inside the wrapper; ``code`` and ``detail`` did not.

**Why the end-to-end tests cannot catch this, and why these exist instead.**
Every pipeline test takes ``eagerWorkerTasks``, which sets
``task_always_eager`` and ``task_eager_propagates``. Eager mode never publishes
to a broker or a result backend, and ``task_eager_propagates`` re-raises the
*original* object in-process, so the exception reaches the assertions without
ever being serialized. Driving a task both ways confirms it::

    # unfixed, eager (what the suite sees)
    caller: SubmissionError    code=OUT_OF_MEMORY  detail=61
    # unfixed, traced as a real worker does (what production got)
    stored: UnpickleableExceptionWrapper  code=<LOST>  detail=<LOST>

So ``test_memory_limit.py`` can drive a container to a genuine OOM kill and
still pass with the bug present: it is handed the real ``SubmissionError``,
which is exactly what production does not get. This is a known blind spot, not
a new one -- ``CI_SETUP.md`` notes that CI's ``GIRDER_WORKER_BROKER`` has said
``locahost`` (missing ``l``) all along and nothing has ever noticed, "because
the suite runs celery tasks eagerly and never reaches the broker". No amount of
end-to-end coverage closes that gap while the boundary is stubbed out, so these
tests call the serialization boundary directly instead.
"""

import errno
import pickle

import pytest
from celery.utils.serialization import (
    UnpickleableExceptionWrapper,
    get_pickleable_exception,
)
from girder_sivacor.errors import FailureCode, SubmissionError, classify

GIB = 1024**3

#: Every ``raise SubmissionError`` in the plugin, one entry per site, because
#: the constructor call is what broke -- so the table that matters is the set of
#: ``(code, detail)`` shapes the code actually constructs, not a sample of them.
#: Two sites appear twice: ``detail`` there is an expression that is ``None`` on
#: a real branch, and ``None`` is the value the buggy ``__reduce__`` was least
#: likely to be noticed on.
#:
#: Kept deliberately parallel to the source. To re-derive it::
#:
#:     grep -rn "SubmissionError(" girder_sivacor/worker_plugin/
#:
#: ``REAPED_NO_HEARTBEAT``, ``REAPED_MAX_RUNTIME`` and ``UNEXPECTED`` are absent
#: on purpose: they are never raised as an exception, only written into a record
#: (by the reaper and by :func:`classify` respectively), so they never cross a
#: celery boundary. ``test_every_failure_code_survives`` still covers them.
RAISE_SITES = [
    # -- lib.py -----------------------------------------------------------
    pytest.param(
        FailureCode.NO_STATA_LICENSE,
        "A Stata image was requested but this deployment has no Stata license: "
        "set the 'sivacor.stata_license' Girder setting, or "
        "STATA_LICENSE_HOSTPATH on the worker.",
        None,
        id="no_stata_license",  # lib.py:405, detail omitted
    ),
    pytest.param(
        FailureCode.NO_ENTRYPOINT,
        "Cannot infer the entrypoint for submission",
        None,
        id="no_entrypoint",  # lib.py:507, detail omitted
    ),
    pytest.param(
        FailureCode.MAIN_FILE_MISSING,
        "Cannot infer run command for submission. No main.do found.",
        None,
        id="main_file_missing",  # lib.py:528, detail omitted
    ),
    pytest.param(
        FailureCode.MAIN_FILE_AMBIGUOUS,
        "Cannot infer run command for submission. Multiple main.do files "
        "found: a/main.do, b/main.do",
        2,
        id="main_file_ambiguous",  # lib.py:537, detail=len(relative_paths)
    ),
    pytest.param(
        FailureCode.IMAGE_PULL_FAILED,
        "Failed to pull image rocker/r-ver:4.3.1: rate limit exceeded",
        "rocker/r-ver:4.3.1",
        id="image_pull_failed",  # lib.py:794, detail=image_reference
    ),
    pytest.param(
        FailureCode.OUT_OF_DISK,
        "Ran out of disk space: 0.4 GiB free on the workspace filesystem, "
        "below the 2.0 GiB floor.",
        None,
        id="out_of_disk",  # lib.py:996, detail omitted (message is the shortfall)
    ),
    pytest.param(
        FailureCode.OUT_OF_MEMORY,
        "The analysis used more memory than this worker allows (56.8 GiB) and "
        "was stopped.",
        61 * GIB,
        id="out_of_memory",  # lib.py:1217, detail=mem_limit
    ),
    pytest.param(
        FailureCode.OUT_OF_MEMORY,
        "The analysis was stopped by the kernel for using too much memory.",
        None,
        # Same site, the branch where the host was too small to cap at all, so
        # container_memory_limit() returned None and there is no limit to quote.
        id="out_of_memory-uncapped",  # lib.py:1217, detail=mem_limit is None
    ),
    pytest.param(
        FailureCode.NONZERO_EXIT,
        "Error executing recorded run. Check stdout/stderr for details.",
        137,
        id="nonzero_exit",  # lib.py:1231, detail=ret["StatusCode"]
    ),
    pytest.param(
        FailureCode.STATA_ERROR,
        "Stata returned an error (use /data/private.dta\nr(601);). Check "
        "stdout/stderr for details.",
        "r(601)",
        id="stata_error",  # lib.py:1240, detail=stata_error_code(...)
    ),
    pytest.param(
        FailureCode.STATA_ERROR,
        "Stata returned an error (end of do-file). Check stdout/stderr for "
        "details.",
        None,
        # Same site: stata_error_code() returns None when the log carries no
        # r(NNN) and no fixed diagnosis, which is the case that most needs the
        # code to survive -- there is no detail left to identify it by.
        id="stata_error-unrecognised",  # lib.py:1240, stata_error_code() -> None
    ),
    # -- run_submission.py -------------------------------------------------
    pytest.param(
        FailureCode.UNSAFE_ARCHIVE,
        "Attempted Path Traversal in Tar File: ../../etc/passwd",
        "path_traversal",
        id="unsafe_archive-traversal",  # run_submission.py:215
    ),
    pytest.param(
        FailureCode.UNSAFE_ARCHIVE,
        "Tar File contains unsafe links: evil -> /etc/shadow",
        "unsafe_link",
        id="unsafe_archive-link",  # run_submission.py:222
    ),
    pytest.param(
        FailureCode.UNSUPPORTED_ARCHIVE,
        "The uploaded file is neither a readable zip nor a readable tar.",
        None,
        id="unsupported_archive",  # run_submission.py:421, detail omitted
    ),
    pytest.param(
        FailureCode.NONZERO_EXIT,
        "Error executing recorded run. Check stdout/stderr for details.",
        1,
        id="nonzero_exit-stage",  # run_submission.py:630, detail=ret["StatusCode"]
    ),
]


@pytest.mark.parametrize("code,message,detail", RAISE_SITES)
def test_celery_does_not_wrap_a_submission_error(code, message, detail):
    """The production regression, asserted against the function that caused it.

    ``get_pickleable_exception`` is what celery calls on every exception leaving
    a task: it returns the exception untouched if it survives a pickle
    round-trip, and an ``UnpickleableExceptionWrapper`` if it does not. Asking it
    directly tests the real mechanism rather than a stand-in for it.
    """
    exc = SubmissionError(code, message, detail=detail)

    pickleable = get_pickleable_exception(exc)

    assert not isinstance(pickleable, UnpickleableExceptionWrapper)
    assert pickleable is exc


@pytest.mark.parametrize("code,message,detail", RAISE_SITES)
def test_both_halves_survive_the_round_trip(code, message, detail):
    """``str()`` for the researcher, ``code``/``detail`` for the operator.

    Carrying both on one exception is the point of the class, so a round-trip
    that kept only the message would satisfy celery and still lose the half the
    permanent record is built from.
    """
    restored = pickle.loads(pickle.dumps(SubmissionError(code, message, detail=detail)))

    assert type(restored) is SubmissionError
    assert str(restored) == message
    assert restored.code == code
    assert restored.detail == detail


def test_the_message_is_not_reshaped_into_a_tuple():
    """Guards the tempting one-line alternative fix.

    Passing all three arguments to ``super().__init__`` would also make the
    default ``__reduce__`` work, but ``str()`` on a multi-argument ``Exception``
    is the repr of ``args``. ``report_failure`` interpolates ``str(exc)``
    straight into the job log, so that would turn every researcher-facing
    message into ``(<FailureCode.STATA_ERROR: 'stata_error'>, 'Stata ...')``.
    """
    exc = SubmissionError(FailureCode.STATA_ERROR, "Stata returned an error", "r(601)")

    assert str(exc) == "Stata returned an error"
    assert exc.args == ("Stata returned an error",)


@pytest.mark.parametrize("code", list(FailureCode))
def test_every_failure_code_survives(code):
    """No member is special, and a new one should not have to be remembered.

    ``FailureCode`` values are written into permanent execution records, so a
    code that cannot cross the task boundary is recorded as ``UNEXPECTED``
    forever after.
    """
    restored = pickle.loads(pickle.dumps(SubmissionError(code, "message", detail=1)))

    assert restored.code is code


@pytest.mark.parametrize("code,message,detail", RAISE_SITES)
def test_classification_is_unchanged_by_the_boundary(code, message, detail):
    """Why the round-trip has to be lossless, stated in terms of the outcome.

    ``classify`` is what turns an exception into the ``(code, detail)`` pair
    kept forever. Anything celery has replaced with a wrapper is no longer a
    ``SubmissionError``, so it falls through to ``UNEXPECTED`` and the run is
    filed under "unclassified bug" rather than "out of memory".
    """
    exc = SubmissionError(code, message, detail=detail)

    assert classify(pickle.loads(pickle.dumps(exc))) == classify(exc) == (code, detail)


def test_the_table_covers_every_raise_site_in_the_plugin():
    """Keeps ``RAISE_SITES`` honest as the pipeline grows.

    The tests above are only as good as the table they are parametrized over,
    and a table of literals hand-copied from the source is exactly the kind that
    stops matching it. Rather than trust that, read the ``(code, detail)`` pairs
    back out of the plugin and require each one to be represented.

    Compares the *shape* -- code plus the type ``detail`` takes -- not the
    literal values, because the values are runtime expressions. A new site
    reusing an existing shape needs nothing; a new shape fails here.
    """
    import ast
    import pathlib

    import girder_sivacor.worker_plugin as wp

    def detail_type(node):
        """The type of the ``detail`` argument at a call site, or None."""
        detail = node.args[2] if len(node.args) > 2 else None
        for keyword in node.keywords:
            if keyword.arg == "detail":
                detail = keyword.value
        if detail is None or isinstance(detail, ast.Constant) and detail.value is None:
            return type(None)
        if isinstance(detail, ast.Constant):
            return type(detail.value)
        # A call or subscript: len() and ret["StatusCode"] are ints, the rest
        # are the str-or-None helpers. Both variants are in the table already.
        return "dynamic"

    found = set()
    for path in pathlib.Path(wp.__file__).parent.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "SubmissionError"):
                continue
            code = node.args[0]
            assert isinstance(code, ast.Attribute), (
                f"{path.name}:{node.lineno} passes a non-literal FailureCode; "
                "this audit can no longer see what it raises"
            )
            found.add((code.attr, detail_type(node)))

    assert found, "found no raise sites at all -- the audit is broken, not the code"

    covered = set()
    for param in RAISE_SITES:
        code, _, detail = param.values
        covered.add((code.name, type(detail)))
        covered.add((code.name, "dynamic"))

    missing = {shape for shape in found if shape not in covered}
    assert not missing, (
        f"raise sites not represented in RAISE_SITES: {sorted(map(str, missing))}. "
        "Add an entry so the new shape is known to survive a task boundary."
    )


def test_an_unclassified_exception_still_reads_as_unexpected():
    """The other half of ``classify``, on the same boundary.

    A bug in the pipeline arrives as an arbitrary exception, and some of those
    genuinely are unpickleable. That must keep degrading to ``UNEXPECTED``
    rather than becoming an error in the failure handler itself.
    """
    wrapper = get_pickleable_exception(ValueError("something surprising"))

    assert classify(wrapper) == (FailureCode.UNEXPECTED, type(wrapper).__name__)


# --- C0.1: ENOSPC is classified from the errno --------------------------------


def test_a_full_disk_is_recorded_as_a_full_disk_not_as_a_bug():
    """``upload_workspace`` and ``create_workspace`` write with no guard at all.

    Both run as container-less pool tasks, so ``recorded_run``'s free-space poll
    does not exist for them -- and both write the researcher's data twice over on
    one filesystem: an archive beside its extracted tree, and a zip of the whole
    project *inside* that project. A disk that filled there raised ``OSError`` and
    was recorded as ``UNEXPECTED``/``OSError``, indistinguishable from a bug in
    our own code. See workspace_disk_waste.md.
    """
    exc = OSError(errno.ENOSPC, "No space left on device")

    assert classify(exc) == (FailureCode.OUT_OF_DISK, None)


def test_the_researchers_path_never_reaches_the_record():
    """``OSError.filename`` is the one field here that is their data.

    A zip write that fails names the file it was writing, which is a path out of
    the replication package. The detail must stay ``None``: ``OUT_OF_DISK`` has no
    telemetry validator, so anything set here would be dropped server-side, and
    relying on that is not the same as not setting it.
    """
    exc = OSError(errno.ENOSPC, "No space left on device")
    exc.filename = "/tmp/workspace-abc/project/confidential_wages_2019.dta"

    code, detail = classify(exc)

    assert code is FailureCode.OUT_OF_DISK
    assert detail is None
    assert "confidential" not in str(detail)


@pytest.mark.parametrize(
    "number",
    [errno.EACCES, errno.ENOENT, errno.EIO, errno.EDQUOT],
)
def test_other_os_errors_are_still_unexpected(number):
    """Only ENOSPC.

    ``EDQUOT`` in particular is a quota, not a full disk, and it has never been
    observed here -- classifying it speculatively would make a future real
    occurrence harder to notice.

    Note what the recorded name is: Python maps most errnos to an ``OSError``
    subclass, so these already store ``PermissionError`` or ``FileNotFoundError``
    rather than a flat ``OSError``, which is strictly more useful in aggregate.
    ``ENOSPC`` has no such subclass, which is part of why it needed classifying
    by hand.
    """
    exc = OSError(number, "something else")

    assert classify(exc) == (FailureCode.UNEXPECTED, type(exc).__name__)
    assert classify(exc)[0] is not FailureCode.OUT_OF_DISK


def test_an_oserror_with_no_errno_is_still_unexpected():
    """``OSError("text")`` sets no errno. It must not fall into the disk branch."""
    assert classify(OSError("stale NFS handle")) == (
        FailureCode.UNEXPECTED,
        "OSError",
    )


def test_a_submission_error_still_wins_over_the_errno_branch():
    """Ordering: an explicit classification is always more specific.

    ``recorded_run`` raises OUT_OF_DISK deliberately with a researcher-facing
    message; that must not be re-derived from an errno that is not there.
    """
    explicit = SubmissionError(FailureCode.OUT_OF_MEMORY, "capped", detail=61)

    assert classify(explicit) == (FailureCode.OUT_OF_MEMORY, 61)


def test_the_enospc_classification_survives_a_task_boundary():
    """Same boundary the rest of this module exists for.

    ``OSError`` round-trips through pickle where ``SubmissionError`` did not, but
    asserting it costs one line and this is the file that would have caught the
    2026-08-13 regression.
    """
    exc = OSError(errno.ENOSPC, "No space left on device")

    restored = get_pickleable_exception(exc)

    assert classify(restored) == (FailureCode.OUT_OF_DISK, None)
