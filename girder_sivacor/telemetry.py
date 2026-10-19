"""The shape of a permanent execution record, and the filter that enforces it.

Submissions are deleted -- by the user, or by the retention sweep -- and take
every trace of the run with them. These records are what survives, so they are
kept indefinitely for reporting and post-mortem debugging. That is only
defensible if they are not personal data, which means the filter below is a
security boundary, not a convenience.

Design rules, in the order they matter:

1. **Allow-list, never deny-list.** :func:`sanitize_record` rebuilds the
   document field by field. A key the worker invents does not survive, so a
   future change on the worker cannot widen what is stored without a matching
   change here.
2. **The server decides the timestamp.** The date is stamped here and
   coarsened to the day. A wall-clock time, at pilot scale, is close to a
   submission identifier -- it would let a record be joined back to a job that
   still exists.
3. **No identifiers at all.** No user id, no job id, no folder id, no worker
   queue name (which identifies an instance), no filename, no path. There is
   deliberately no key to join a record back to a person, which is what keeps
   these out of scope for erasure requests.
4. **Free text is never stored.** Every string is either drawn from a fixed
   set (status, failure code, size bucket) or passed through
   :func:`_safe_text`, which caps length and drops anything outside a
   conservative charset.

The cost of rule 3 is real: you cannot ask "what did *this* submission do"
after it is gone. That is the trade. While the submission still exists its job
log holds the full story, including the parts stripped here.
"""

import math
import re

from .errors import (
    MAX_FRAME,
    MAX_FRAMES,
    MAX_MESSAGE,
    PLATFORM_EXCEPTIONS,
    FailureCode,
)

#: Hard cap on any single recorded string.
_MAX_TEXT = 64

#: A python identifier: pipeline step names and exception class names.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

#: A dotted python path to an exception class, as ``errors.exception_key``
#: spells it. The longest member of ``PLATFORM_EXCEPTIONS`` is well inside
#: ``_MAX_TEXT``, so ``_matching``'s cap applies unchanged.
_EXCEPTION_TYPE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*){0,8}$")

#: A docker image reference without its tag (``dataeditors/stata18_5-mp``).
#: Anchored and slash-bounded, so an absolute path or one containing ``..``
#: cannot pass as one. A *relative* all-lowercase path would still match the
#: shape -- this pattern is not what rules that out. What does is that image
#: names never come from the researcher: submit_job validates every stage
#: against the allow-list before the submission is created, so by the time a
#: name reaches here it is one the operator published.
_IMAGE_NAME = re.compile(r"^[a-z0-9]+([._-][a-z0-9]+)*(/[a-z0-9]+([._-][a-z0-9]+)*)*$")

#: An image tag, or a version string.
_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: A Stata return code as :func:`~girder_sivacor.worker_plugin.lib.stata_error_code`
#: emits it.
_STATA_CODE = re.compile(r"^r\(\d{1,6}\)$")

#: The non-numeric Stata diagnoses, which are our own fixed strings.
_STATA_FIXED = frozenset(
    {"License is invalid", "Cannot find license file", "License has expired"}
)

#: Fixed qualifiers for archives rejected before extraction.
_ARCHIVE_REASONS = frozenset({"path_traversal", "unsafe_link"})

#: Upper bounds, in bytes, of the buckets uploaded package sizes are reported
#: in. An exact byte count is unusually identifying for a file someone chose to
#: upload; the bucket answers "are people submitting big packages?" without it.
_SIZE_BUCKETS = (
    (10 * 1024**2, "<10MB"),
    (100 * 1024**2, "10-100MB"),
    (1024**3, "100MB-1GB"),
    (5 * 1024**3, "1-5GB"),
)
_SIZE_BUCKET_OVERFLOW = ">5GB"

#: The terminal states a record can describe.
STATUSES = ("completed", "failed", "reaped")

#: Every value :func:`size_bucket` can return, for validation and for reports
#: that want to show empty buckets.
SIZE_BUCKETS = tuple(label for _, label in _SIZE_BUCKETS) + (_SIZE_BUCKET_OVERFLOW,)


def size_bucket(size_bytes):
    """Bucket a byte count. ``None`` for a size that could not be determined."""
    if size_bytes is None:
        return None
    for upper, label in _SIZE_BUCKETS:
        if size_bytes < upper:
            return label
    return _SIZE_BUCKET_OVERFLOW


def _matching(value, pattern):
    """Return ``value`` as a string only if it matches ``pattern`` entirely.

    Match-or-drop, never repair. Stripping the offending characters out of a
    string and keeping the remainder is how a path like
    ``/home/jane/thesis.dta`` survives a filter as ``homejanethesis.dta`` --
    still identifying, and now also unreadable. If a value is not recognisably
    one of the things we meant to store, it is not stored.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text if len(text) <= _MAX_TEXT and pattern.match(text) else None


def _one_of(value, allowed):
    """Return ``value`` only if it is one of a fixed set of known strings."""
    if value is None:
        return None
    text = str(value).strip()
    return text if text in allowed else None


def _safe_number(value, minimum=0):
    """Coerce to a non-negative number, or ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    # `isinstance(value, float)` first, deliberately: `math.isfinite` raises
    # OverflowError on an int too large to convert to float, and this function
    # is the boundary that sanitizes whatever a worker posted -- JSON carries
    # arbitrarily large integers, and a sanitizer that can be made to raise is
    # not one. Only a float can be NaN or infinite, so the guard costs nothing.
    if isinstance(value, float) and not math.isfinite(value):  # NaN / inf
        return None
    return value if value >= minimum else None


def _safe_int(value, minimum=None):
    """Coerce to an int, or ``None``. Used for exit codes, which go negative."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if minimum is not None and value < minimum:
        return None
    return value


#: Largest scratch volume that may be recorded, in GB. A shape bound, not a
#: policy one: the policy ceiling is per user and lives in the settings, and this
#: module stays free of I/O so it cannot read either. Anything above this is a
#: value no operator could have configured against the 1000 GB the whole feature
#: has, so it is dropped rather than stored.
_MAX_VOLUME_GB = 2000


def _volume_gb(value):
    """Validate a recorded scratch-volume size.

    ``None`` -- no volume -- is the overwhelmingly common answer and is stored as
    ``None``, not dropped: "did not ask" is a fact worth having when the question
    is how often anyone asks.

    A whole number of gigabytes, rounded to
    :data:`~girder_sivacor.rest.VOLUME_GRANULARITY_GB` by the server before it
    ever reaches a worker, so the stored value comes from a set of ~100 coarse
    figures. That is the same class as ``mem_limit_bytes`` and
    ``requested_memory_gb``: a machine capability shared by every submission at
    that size, and strictly less revealing than the ``max_disk_bytes`` this
    module already stores per stage, which is an exact byte count.

    Deliberately **not** checked against the granularity. A worker on an older
    build could legitimately report an unrounded figure, and dropping it would
    lose the fact that a volume was requested at all in order to enforce a
    cosmetic property.
    """
    if value is None:
        return None
    size = _safe_int(value, minimum=0)
    return size if size is not None and size <= _MAX_VOLUME_GB else None


def _stata_detail(value):
    return _matching(value, _STATA_CODE) or _one_of(value, _STATA_FIXED)


#: What ``detail`` is allowed to be, per failure code.
#:
#: Deliberately one validator per code rather than a single charset filter. A
#: charset permissive enough for ``rocker/r-ver:4.6.1`` is also permissive
#: enough for ``/home/jane/confidential.dta``, and the difference between those
#: two is not expressible as a set of characters -- only as "which of the few
#: things we meant to store is this". A code with no entry stores no detail.
_DETAIL_VALIDATORS = {
    FailureCode.STATA_ERROR: _stata_detail,
    FailureCode.NONZERO_EXIT: lambda v: _safe_int(v),
    # Retired with the code (see errors.py), and kept for the same reason: a
    # validator this table has no entry for stores detail=None, which would
    # silently rewrite what a replayed historical payload means.
    FailureCode.DEPENDENCY_RESOLUTION_FAILED: lambda v: _safe_int(v),
    # The memory cap the run exceeded. A machine fact -- it is derived from the
    # worker's flavor, identically for every submission that lands on one -- so
    # it says nothing about the researcher, and it is the number that makes the
    # record actionable: "failed at 59 GiB" and "failed at 28 GiB" are different
    # problems with different fixes.
    FailureCode.OUT_OF_MEMORY: lambda v: _safe_int(v, minimum=0),
    # A catalogue rung, not a measurement: non-identifying by construction.
    FailureCode.SIZE_UNAVAILABLE: lambda v: _safe_int(v, minimum=0),
    FailureCode.MAIN_FILE_AMBIGUOUS: lambda v: _safe_int(v, minimum=0),
    FailureCode.IMAGE_PULL_FAILED: lambda v: _image_reference(v),
    FailureCode.UNSAFE_ARCHIVE: lambda v: _one_of(v, _ARCHIVE_REASONS),
    FailureCode.UNEXPECTED: lambda v: _matching(v, _IDENTIFIER),
}


def _image_reference(value):
    """Validate ``name:tag``, each half against its own pattern."""
    if value is None:
        return None
    text = str(value).strip()
    name, separator, tag = text.partition(":")
    if not _matching(name, _IMAGE_NAME):
        return None
    if separator and not _matching(tag, _TAG):
        return None
    return text if len(text) <= _MAX_TEXT else None


def _catalogue_size(value, allowed):
    """Return ``value`` only if it is one of the configured worker sizes.

    ``_one_of`` in integer form, and for the same reason: the stored figure is
    drawn from a fixed set the operator published, never from anything the
    worker or the researcher can invent.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value in allowed else None


#: The phases one stage could run through. A closed set of two machine-chosen
#: words, not researcher content -- the same class as ``network_isolation``.
#: A row with no phase is an analysis by definition, which covers both ends of
#: the feature's life: records written before SIVACOR resolved Julia
#: environments, and records written now that it does not. ``resolve`` stays in
#: the set for the rows in between, and because a worker still running the old
#: code may be mid-submission during a rollout.
_PHASES = frozenset({"analysis", "resolve"})

#: Stage rows kept per record, and so also the largest ``error.stage`` that can
#: name one of them. A shape bound on a payload the worker sends, not a limit on
#: workflows.
MAX_STAGES = 32


def _sanitize_stage(stage, allowed_sizes=()):
    if not isinstance(stage, dict):
        return None
    return {
        "phase": _one_of(stage.get("phase", "analysis"), _PHASES) or "analysis",
        # What the submission *asked* for, beside mem_limit_bytes -- what it got.
        # Same class as mem_limit_bytes: a machine capability shared by every
        # submission at that size, not an identifier. Validated against the
        # catalogue rather than a charset, per rule 4.
        "requested_memory_gb": _catalogue_size(
            stage.get("requested_memory_gb"), allowed_sizes
        ),
        "image_name": _matching(stage.get("image_name"), _IMAGE_NAME),
        "image_tag": _matching(stage.get("image_tag"), _TAG),
        "network_isolation": bool(stage.get("network_isolation", False)),
        "duration_seconds": _safe_number(stage.get("duration_seconds")),
        "exit_code": _safe_int(stage.get("exit_code")),
        "max_cpu_percent": _safe_number(stage.get("max_cpu_percent")),
        # Cumulative CPU time. Not an identifier: a duration, in the same family as
        # duration_seconds, and it is what makes max_cpu_percent interpretable.
        "cpu_seconds_total": _safe_number(stage.get("cpu_seconds_total"), minimum=0),
        "max_memory_bytes": _safe_number(stage.get("max_memory_bytes")),
        # The cap the run was given. Same class as image_size_bytes: a property
        # of the worker, identical for every submission on that flavor.
        "mem_limit_bytes": _safe_number(stage.get("mem_limit_bytes"), minimum=0),
        # Peak bytes the run's own workspace occupied, and the size of the image
        # it needed on the machine. Both are machine facts, not researcher
        # content -- an exact byte count is fine here, unlike the uploaded
        # package size, which is bucketed because the researcher chose it.
        "max_disk_bytes": _safe_number(stage.get("max_disk_bytes")),
        "image_size_bytes": _safe_number(stage.get("image_size_bytes")),
    }


def _sanitize_error(error):
    if not isinstance(error, dict):
        return None
    try:
        code = FailureCode(error.get("code"))
    except ValueError:
        # An unknown code means the worker is ahead of the server, or someone
        # is making things up. Either way it is a failure we cannot name -- and
        # its detail is validated by no one, so it is dropped with the code.
        return {
            "step": _matching(error.get("step"), _IDENTIFIER),
            "code": FailureCode.UNEXPECTED.value,
            "detail": None,
            "stage": _stage_number(error.get("stage")),
        }
    return {
        "step": _matching(error.get("step"), _IDENTIFIER),
        "code": code.value,
        "detail": safe_detail(code, error.get("detail")),
        "stage": _stage_number(error.get("stage")),
    }


def _stage_number(value):
    """A 1-based stage number within the bound :func:`sanitize_record` keeps."""
    value = _safe_int(value, minimum=1)
    return value if value is not None and value <= MAX_STAGES else None


def safe_step(step):
    """Validate a pipeline step name, or ``None``.

    The same identifier allow-list execution records use. A step name is one we
    chose -- ``create_workspace``, ``fetch_images`` -- never anything read out
    of the researcher's package, and validating it here keeps that true for the
    callers that only *look* like they cannot be given something else.
    """
    return _matching(step, _IDENTIFIER)


def safe_detail(code, detail):
    """Validate one ``detail`` against the code it qualifies, or ``None``.

    Split out of :func:`_sanitize_error` so that anything else keeping a
    ``(code, detail)`` pair reuses *this* boundary instead of inventing a
    second one -- :mod:`girder_sivacor.alerts` counts failures by that pair, and
    a counter is a store like any other. A charset loose enough for
    ``rocker/r-ver:4.6.1`` is also loose enough for ``/home/jane/thesis.dta``,
    which is why the validation is per code rather than by charset, and why
    a code with no validator stores nothing rather than storing the input.
    """
    try:
        code = FailureCode(code)
    except ValueError:
        return None
    validator = _DETAIL_VALIDATORS.get(code)
    return validator(detail) if validator else None


def _sanitize_worker(worker):
    if not isinstance(worker, dict):
        return {}
    return {
        "arch": _matching(worker.get("arch"), _IDENTIFIER),
        "ncpu": _safe_int(worker.get("ncpu"), minimum=0),
        "mem_total_bytes": _safe_number(worker.get("mem_total_bytes")),
    }


def sanitize_record(payload, date, allowed_sizes=()):
    """Rebuild an untrusted record from the worker as a storable document.

    ``date`` is supplied by the caller (the server) rather than read from
    ``payload`` -- see rule 2 in the module docstring.

    ``allowed_sizes`` is the worker-size catalogue's ``memory_gb`` figures, also
    supplied by the caller: this module stays free of I/O, and reading the
    setting here would make the allow-list depend on a Girder connection. It
    defaults to empty, which drops the field rather than trusting it -- the
    fail-closed direction, as everywhere else here.
    """
    payload = payload if isinstance(payload, dict) else {}
    allowed_sizes = frozenset(allowed_sizes)

    status = payload.get("status")
    if status not in STATUSES:
        status = "failed"

    stages = payload.get("stages")
    stages = stages if isinstance(stages, list) else []
    # Bound the fan-out: a malformed or hostile payload must not be able to
    # write an unbounded document.
    stages = [
        s
        for s in (_sanitize_stage(x, allowed_sizes) for x in stages[:MAX_STAGES])
        if s
    ]

    bucket = payload.get("package_size_bucket")
    if bucket not in SIZE_BUCKETS:
        bucket = None

    record = {
        "date": date,
        "status": status,
        "stack_version": _matching(payload.get("stack_version"), _TAG),
        # Analysis phases only. Julia submissions briefly recorded two rows per
        # stage -- a dependency resolve and the run -- and counting both would
        # report those as twice the size of the identical thing run in R,
        # silently changing what n_stages means from one record to the next.
        "n_stages": sum(1 for s in stages if s["phase"] == "analysis"),
        "total_duration_seconds": _safe_number(payload.get("total_duration_seconds")),
        "package_size_bucket": bucket,
        "requested_disk_gb": _volume_gb(payload.get("requested_disk_gb")),
        "worker": _sanitize_worker(payload.get("worker")),
        "stages": stages,
    }
    if status != "completed":
        record["error"] = _sanitize_error(payload.get("error"))
    return record


#: A pipeline step's title: our own words, letters and digits only.
#:
#: Checked against every distinct title in a restored production database
#: (5,175 chain jobs, 2025-11-14 onward): **14 titles pass and every one of
#: them is a step name we wrote**, including three retired ones -- "Cleanup
#: submission folder", "Record Performance Metrics" and "Resolve declared
#: dependencies", the Julia resolver that was reversed on 2026-09-19. That is
#: the argument for a charset rule over a list of the current builder's
#: strings: a list would have redacted all three as unrecognised, and a job
#: document outlives the release that titled it.
_STEP_TITLE = re.compile(r"^[A-Za-z][A-Za-z0-9 ]{0,62}$")

#: The one chain step whose title is **generated**, and therefore the one that
#: carries researcher content: ``build_submission_chain`` names the head step
#: ``f"Moving {file['name']} to submission collection"``. 881 of those 5,175
#: documents are this shape, across 264 distinct filenames -- ``Code.zip``,
#: ``final_project_replication_package(3).zip``, and so on.
_PACKAGE_TITLE = re.compile(r"^Moving .{1,255} to submission collection$", re.DOTALL)

#: What that head step is called once its filename is gone. A constant, not
#: anything derived from the title, so it carries nothing out.
HEAD_STEP_TITLE = "Moving the replication package to submission collection"

#: What anything else unrecognised becomes.
REDACTED_TITLE = "(title redacted)"


def safe_title(value):
    """Validate a chain step's title, replacing what is not ours to keep.

    Unlike every other validator here this **substitutes** rather than
    returning ``None``, because a step with no title at all is harder to read
    than one labelled as redacted -- and the substitutes are constants, so the
    match-or-drop rule is intact: nothing of the input survives a failed match.
    """
    text = "" if value is None else str(value).strip()
    if _STEP_TITLE.match(text):
        return text
    if _PACKAGE_TITLE.match(text):
        return HEAD_STEP_TITLE
    return REDACTED_TITLE


def safe_image_name(value):
    """Validate a docker image name (no tag), or ``None``."""
    return _matching(value, _IMAGE_NAME)


def safe_image_tag(value):
    """Validate a docker image tag, or ``None``."""
    return _matching(value, _TAG)


#: One formatted traceback frame, as ``traceback.format_tb`` emits it: a
#: ``File "...", line N, in name`` header and, usually, the source line under
#: it.
_FRAME = re.compile(
    r'^\s*File "(?P<path>[^"\n]{1,512})", line \d{1,9}, in \S{1,128}'
    r"(?:\n.{0,400})?\n?$"
)

#: Paths a frame may not come from. Nothing researcher-written ever appears in
#: a python traceback -- their code runs in a container, in another process, in
#: another language -- but a frame raised *while handling* their package can
#: still name the file it was handling in its source line, and the workspace is
#: where every such path lives.
_WORKSPACE_PATH = re.compile(r"/tmp/(?:workspace|tmp)-")


def _safe_frame(frame):
    """Keep one traceback frame, or drop it.

    Match-or-drop, exactly as :func:`_matching`: a frame that is not
    recognisably ``format_tb`` output is not one we wrote, so it is not stored.
    """
    if not isinstance(frame, str):
        return None
    if len(frame) > MAX_FRAME:
        return None
    if _WORKSPACE_PATH.search(frame):
        return None
    return frame if _FRAME.match(frame) else None


def sanitize_failure(payload):
    """Rebuild an untrusted failure record as a storable one, or ``None``.

    The boundary for :data:`girder_sivacor.chain.FAILURE_KEY` -- the field that
    gives a traceback somewhere to live other than the job log. It is kept
    after the submission is deleted, so it is a permanent store and gets the
    same treatment as everything else in that class: an allow-list rebuild,
    with the message validated **by exception type** rather than by charset.

    ``message`` survives only for the types
    :data:`girder_sivacor.errors.PLATFORM_EXCEPTIONS` names. That distinction
    is the whole point and is not expressible any other way: 2026-09-25's
    ``Invalid state transition to '3'`` and 2026-10-12's ``APIError`` are
    platform prose that a post-mortem is built from, while a
    ``SubmissionError``'s message is the researcher's by contract and a
    ``docker.errors.ContainerError``'s is their container's stderr.

    ``code``/``detail`` go through :func:`safe_detail`, which is the same
    boundary execution records and the alert counters already use.
    """
    if not isinstance(payload, dict):
        return None
    exc_type = _matching(payload.get("type"), _EXCEPTION_TYPE)
    frames = payload.get("frames")
    frames = frames if isinstance(frames, list) else []
    frames = [f for f in (_safe_frame(x) for x in frames[:MAX_FRAMES]) if f]
    message = payload.get("message")
    if exc_type not in PLATFORM_EXCEPTIONS or not isinstance(message, str):
        message = None
    try:
        code = FailureCode(payload.get("code")).value
    except ValueError:
        code = None
    record = {
        "type": exc_type,
        "message": message[:MAX_MESSAGE] if message else None,
        "code": code,
        "detail": safe_detail(code, payload.get("detail")) if code else None,
        "step": safe_step(payload.get("step")),
        "frames": frames,
    }
    # Nothing recognisable survived, so there is nothing worth a field.
    if not any((record["type"], record["code"], record["frames"])):
        return None
    return record
