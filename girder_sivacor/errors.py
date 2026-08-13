"""Structured failure codes for the submission pipeline.

A submission that fails leaves two very different audiences behind:

* the **researcher**, who needs the full message -- the Stata command that
  errored, the file that could not be found -- to fix their package; and
* the **operator**, who needs to know, months later and in aggregate, that
  Stata runs fail on ``r(601)`` more often than anything else.

The first lives in the job log, inside the retention window, and is deleted
with the submission. The second is kept indefinitely (see
:mod:`girder_sivacor.telemetry`), so it must contain no personal data at all.

:class:`SubmissionError` carries both. ``str(exc)`` is the researcher's message
and is free to interpolate their filenames and log lines, exactly as the plain
``ValueError``\\ s it replaced did. ``code`` and ``detail`` are the operator's
view: an enum member and a short, whitelisted scalar, never free text taken
from the package under test.

Keeping them on one exception is deliberate -- the raise site is the only place
that knows both, and splitting them would mean classifying error strings after
the fact, which is exactly the thing that leaks.

This module imports nothing from girder, so both the server and a remote
worker (which has no Mongo, only HTTP) can use it.
"""

from enum import Enum


class FailureCode(str, Enum):
    """Why a submission failed, in terms that carry no researcher content.

    Values are stable identifiers: they are written into permanent execution
    records, so renaming one silently reinterprets historical data. Add new
    members rather than repurposing old ones.
    """

    #: The workspace filesystem fell below the free-space floor mid-run.
    OUT_OF_DISK = "out_of_disk"
    #: A Stata image was requested but the deployment has no license.
    NO_STATA_LICENSE = "no_stata_license"
    #: Stata reported an error. ``detail`` is the ``r(NNN)`` return code.
    STATA_ERROR = "stata_error"
    #: The analysis container exited non-zero. ``detail`` is the exit code.
    NONZERO_EXIT = "nonzero_exit"
    #: The kernel OOM-killed the analysis container's cgroup. ``detail`` is the
    #: limit in bytes it exceeded -- a property of the worker's flavor, not of
    #: the researcher. Distinct from NONZERO_EXIT because the exit code is an
    #: indistinguishable 137 and the container's own logs say nothing: the
    #: process is killed with no chance to report.
    OUT_OF_MEMORY = "out_of_memory"
    #: The image could not be pulled. ``detail`` is the (allow-listed) image ref.
    IMAGE_PULL_FAILED = "image_pull_failed"
    #: No run command could be inferred for the image family.
    NO_ENTRYPOINT = "no_entrypoint"
    #: The declared main file is not in the package.
    MAIN_FILE_MISSING = "main_file_missing"
    #: The declared main file appears more than once. ``detail`` is the count.
    MAIN_FILE_AMBIGUOUS = "main_file_ambiguous"
    #: The upload is neither a readable zip nor a readable tar.
    UNSUPPORTED_ARCHIVE = "unsupported_archive"
    #: The archive tried to escape the extraction root.
    UNSAFE_ARCHIVE = "unsafe_archive"
    #: The worker lost the submission; the reaper failed it. No heartbeat.
    REAPED_NO_HEARTBEAT = "reaped_no_heartbeat"
    #: The submission ran past the configured maximum runtime.
    REAPED_MAX_RUNTIME = "reaped_max_runtime"
    #: Anything not classified above. ``detail`` is the exception class name.
    UNEXPECTED = "unexpected"


class SubmissionError(Exception):
    """A pipeline failure with an operator-facing classification attached.

    Args:
        code: a :class:`FailureCode`.
        message: the researcher-facing text. May contain their filenames and
            log content -- it goes to the job log, not to the permanent record.
        detail: an optional short scalar qualifying ``code``. Must be safe to
            keep forever: an exit code, an ``r(NNN)``, an allow-listed image
            reference. Never a path, filename, or line from the package.
    """

    def __init__(self, code, message, detail=None):
        self.code = FailureCode(code)
        self.detail = detail
        super().__init__(message)

    def __reduce__(self):
        """Rebuild through the real signature, so this survives pickling.

        ``Exception.__reduce__`` reconstructs by calling ``cls(*self.args)``,
        and ``self.args`` here is ``(message,)`` -- that is all
        ``super().__init__`` was handed. Unpickling therefore calls
        ``SubmissionError(message)`` and dies on the missing ``message``
        argument, one short of the signature.

        That matters because celery pickles every exception that leaves a task.
        When the round-trip raises, it substitutes
        ``UnpickleableExceptionWrapper``: the researcher's text survives, but
        ``code`` and ``detail`` -- the entire operator-facing half of this class
        -- do not, and a classified failure is logged as "raised unexpected".
        Observed in production on 2026-08-13 on an OOM-killed Stata run.

        Widening ``self.args`` to all three would also make the round-trip work,
        but ``str()`` on a multi-argument ``Exception`` is the repr of the
        tuple, and ``str(exc)`` is the researcher's message (see the module
        docstring). Keeping ``args`` one long and overriding here preserves
        both halves.
        """
        return (self.__class__, (self.code, str(self), self.detail))


def classify(exc):
    """Return the ``(code, detail)`` pair to record for ``exc``.

    Unclassified exceptions -- bugs, ``OSError``, a docker-py surprise -- are
    recorded as :attr:`FailureCode.UNEXPECTED` with the *class name* only.
    Their ``str()`` is discarded on purpose: it is arbitrary text from an
    unknown source, which is the one thing that must not reach the permanent
    record. The class name plus the pipeline step is still enough to notice
    "every OSError is in create_workspace" months later.
    """
    if isinstance(exc, SubmissionError):
        return exc.code, exc.detail
    return FailureCode.UNEXPECTED, type(exc).__name__
