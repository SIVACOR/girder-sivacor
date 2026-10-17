"""The platform's own account of a run, kept after the submission is gone.

A submission job's log is the single most quoted source in the incident
write-ups -- five of nine lean on it at length -- and **both** deletion paths
destroy it. The chain jobs that survive a user's Delete preserve strictly less
than those write-ups were written from, which is finding A of the appendix in
``../development_notes/incidents/2026-10-12-dns-pull-kills-run.md``.

So the narration is copied onto the redacted chain (see
:mod:`girder_sivacor.chain`) before the submission job goes. That makes it a
permanent store, and it starts life as free text with the researcher's
filenames interpolated into it, so it needs a boundary -- the same kind
:func:`girder_sivacor.telemetry.sanitize_record` is, and for the same reason.

**Why this is a line-template allow-list and not a scrubber.** Every line in a
submission job's log was written by us: ``report``, ``_note`` and
:class:`~girder_sivacor.worker_plugin.lib.ProgressBeat` are the only writers,
and the researcher's container output goes to the *chain* job, never here. What
is theirs is not whole lines but the values interpolated into ours -- a
filename, a folder name, the daemon's error text. "Which parts of our sentence
are theirs" is not expressible as a set of characters, exactly as
``telemetry.safe_detail`` argues for ``detail``; it is only expressible as
"which of the few sentences we wrote is this". Hence a template per sentence,
with every variable part constrained to a shape that cannot be a path.

**It will rot, and it is built to rot loudly.** A line nobody listed is dropped
and *counted*: :func:`scrub` reports ``dropped`` alongside the kept lines, and
``tests/test_chain_redaction.py`` runs a real submission end to end and asserts
that number is zero. A new log line therefore fails CI rather than quietly
disappearing from every future post-mortem.

Fail-closed in the other direction too: a line that matches no template is
dropped whole. Never trimmed, never repaired -- see ``_matching`` in
:mod:`girder_sivacor.telemetry` for why keeping the remainder is worse than
keeping nothing.
"""

import re

#: The timestamp ``report`` and the server's own writers prefix every line
#: with, in ``America/Chicago``. Optional: ``_note`` and the recorded-run
#: opener write bare lines.
_STAMP = r"(?:\[\d{4}-\d{2}-\d{2}T[\d:]{8}(?:[+-]\d{2}:\d{2})?\] )?"

#: A docker image reference. Deliberately the same shape
#: ``telemetry._IMAGE_NAME``/``_TAG`` allow, spelled inline because it appears
#: mid-sentence here rather than as a whole field.
_REF = r"[a-z0-9]+(?:[._-][a-z0-9]+)*(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*(?::[A-Za-z0-9][A-Za-z0-9._-]{0,63})?"

#: A ``randomname`` submission folder name -- ``ferocious-fader``. Machine
#: generated from a fixed word list, so it names a submission and nothing else,
#: and it is how every incident write-up refers to the run.
_NAME = r"[a-z0-9]+(?:-[a-z0-9]+)*"

_INT = r"\d{1,12}"
_DEC = r"\d{1,9}\.\d{1,3}"

#: The TRO actions ``build_submission_chain`` can ask for. A closed set it
#: chooses from, never anything a researcher can name.
_TRO_ACTION = r"add_arrangement|add_performance|prune_performance|sign"

#: The prose half of ``report_failure``'s line, which is the ``failure``
#: argument of the :func:`submission_task` decorator -- one of seven fixed
#: strings, plus ``prepare_submission``'s own. The *exception* it interpolates
#: lands on the next line, tab-indented, and that line is deliberately matched
#: by nothing: it is where ``r(601)``, a ``renv`` library path under
#: ``/tmp/workspace-...`` and a ``UnicodeDecodeError``'s bytes all showed up in
#: the audit. Keeping the headline and dropping the detail is the whole point
#: -- and since girder-sivacor#129 the detail has a structured home of its own
#: (``meta.sivacorFailure``), so nothing is lost by dropping it here.
_FAILURE_HEADLINE = r"Failed to [A-Za-z][A-Za-z ]{0,60}:"


def _whole(pattern):
    return re.compile(_STAMP + pattern + r"\s*$")


def _prefix(pattern):
    """A line whose tail is free text: keep what matched, drop the rest."""
    return re.compile(_STAMP + pattern)


#: Every line the platform writes to a submission job's log, in the order the
#: pipeline writes them. ``(pattern, truncating)``: a truncating entry keeps
#: only the part that matched and discards the tail, which is how the retry
#: lines keep "could not pull X, attempt 2 of 4" without keeping the daemon's
#: free-form explanation of why.
_TEMPLATES = (
    # rest.py, submit_job -- the first line of every submission.
    (_whole(r"Preparing SIVACOR submission"), False),
    # run_submission.prepare_submission
    (_whole(rf"New submission: '{_NAME}' created\."), False),
    # run_submission.create_workspace, and its ProgressBeat
    (_whole(r"Creating workspace from source folder\."), False),
    (
        _whole(
            rf"Still preparing the workspace \({_DEC} GiB downloaded, "
            rf"{_INT} files unpacked\)"
        ),
        False,
    ),
    # run_submission.fetch_images and lib.prefetch_images
    (_whole(r"Fetching analysis images\."), False),
    (_whole(rf"Fetching analysis image {_INT} of {_INT}: {_REF}"), False),
    (
        _whole(rf"Still fetching analysis images \({_INT} of {_INT}: {_REF}\)"),
        False,
    ),
    (_whole(r"Cancelled while fetching analysis images\."), False),
    # lib.pull_image
    (_whole(rf"Still pulling {_REF} \({_INT} layers seen\)"), False),
    (
        _whole(
            rf"Pulled {_REF} \({_INT} layers\)"
            rf"(?:, on attempt {_INT} of {_INT})?"
        ),
        False,
    ),
    # The daemon's own text follows the semicolon and is dropped with it.
    (
        _prefix(
            rf"Could not pull {_REF} \(attempt {_INT} of {_INT}\); "
            rf"retrying in {_INT}s\."
        ),
        True,
    ),
    (_whole(rf"Giving up on {_REF} after {_INT} attempts\."), False),
    # lib._free_space_note
    (
        _whole(
            r"All analysis images are ready\."
            rf"(?: {_DEC} GiB free on (?:the workspace filesystem|the worker's own disk)\.)?"
        ),
        False,
    ),
    # run_submission.execute_workflow and lib.recorded_run
    (_whole(r"Executing workflow on workspace\."), False),
    (_whole(r"Starting recorded run"), False),
    # run_submission.run_tro. The action is one of a closed set the chain
    # builder chooses from, never anything a researcher can name.
    #
    # "on workspace" is an older wording of the same line, and
    # ``TRO utilities completed successfully`` is a retired companion to it.
    # Both are here because an audit of 228 real submission logs found them --
    # see RETIRED below.
    (
        _whole(
            r"(?:Running TRO utilities (?:in the|on) workspace|"
            r"TRO utilities completed successfully)\. "
            rf"\((?:{_TRO_ACTION})\)"
        ),
        False,
    ),
    # run_submission.upload_workspace, and its ProgressBeat
    (_whole(r"Uploading executed replication package to Girder\."), False),
    (
        _whole(rf"Still packaging the replication package \({_INT} files archived\)"),
        False,
    ),
    # run_submission.finalize_job -- the last line of a run that worked.
    (_whole(r"Submission job finalized successfully\."), False),
    # run_submission.report_failure. Which step failed is the single most
    # useful line in a failed submission's log, and it was being dropped with
    # the exception text on the line below it.
    (_whole(_FAILURE_HEADLINE), False),
    # rest.py, the reaper and the status reconciler. Both write prose we chose,
    # but `reason` is assembled from figures and is left to the tail rule.
    (_prefix(r"Submission abandoned:"), True),
    (_prefix(r"Status reconciled to SUCCESS by /sivacor/reap:"), True),
    # --- RETIRED ------------------------------------------------------------
    #
    # Lines no release still writes. They are here because the store is older
    # than the code: an audit of all 228 submission logs in a restored
    # production database found 14% of narration lines unmatched, and these
    # were most of the half that was not a failure's interpolated tail.
    #
    # That is this module's own rot, caught before it shipped rather than
    # after -- the templates were written by reading today's source, and the
    # store contains history. Deleting one of these would not delete the lines
    # it matches, it would only make them unreadable. Same reasoning as a
    # retired FailureCode in errors.py.
    (_whole(r"Submission prepared successfully\."), False),
    (_whole(r"Workspace created successfully\."), False),
    (_whole(r"Workflow executed successfully\."), False),
    # Not ours at all -- girder_jobs writes this when the server restarts under
    # a running job. Kept because it is the only explanation a reader will ever
    # get for that abort, and it is a fixed string in Girder's own source.
    (
        _whole(
            r"Job aborted due to system restart\. We apologize for any "
            r"inconvenience\. Please try again\."
        ),
        False,
    ),
)

#: Appended where a truncating template dropped a tail, so a reader can tell
#: "the line said no more" from "the rest of the line was not ours to keep".
ELLIPSIS = " [...]"

#: Most lines a submission ever writes. A ProgressBeat ticks every few minutes
#: for the length of the run, so a 50-hour submission is thousands of "still
#: pulling" lines; past this the narration is kept from both ends, which is
#: where a post-mortem looks anyway.
MAX_LINES = 400


def scrub(log):
    """Return ``(lines, dropped)`` for a submission job's ``log``.

    ``log`` is Girder's list of appended chunks, each possibly several lines.
    ``dropped`` counts lines that matched no template -- the rot signal; see
    the module docstring.
    """
    if isinstance(log, str):
        log = [log]
    kept = []
    dropped = 0
    for chunk in log or []:
        if not isinstance(chunk, str):
            dropped += 1
            continue
        for line in chunk.splitlines():
            if not line.strip():
                continue
            text = _keep(line)
            if text is None:
                dropped += 1
            else:
                kept.append(text)
    if len(kept) > MAX_LINES:
        half = MAX_LINES // 2
        kept = kept[:half] + [f"[... {len(kept) - MAX_LINES} lines ...]"] + kept[-half:]
    return kept, dropped


def _keep(line):
    """The storable form of one line, or ``None`` to drop it."""
    for pattern, truncating in _TEMPLATES:
        match = pattern.match(line)
        if not match:
            continue
        if not truncating:
            return line.rstrip()
        head = match.group(0).rstrip()
        return head if head == line.rstrip() else head + ELLIPSIS
    return None
