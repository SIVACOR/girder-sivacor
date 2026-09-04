"""The lifecycle values a submission folder's ``meta.status`` can hold.

A submission has *two* statuses, and the difference between them is the whole
reason this module exists. The Girder **job** status is the server's view --
what it has been told about the run. The folder's ``meta.status`` is the view
that gates destructive action: :meth:`~girder_sivacor.rest.SIVACOR.delete_submission`
removes a submission only if its status is one of :data:`DELETABLE`.

Those two were treated as one fact, and on 2026-09-03 that cost a researcher
six hours of a run. Cancelling put the job straight into ``CANCELED``, so the
folder went straight to :data:`FAILED` and became deletable -- while the worker
still had about twelve seconds of results to write back. The user deleted at
second eight, and the worker's first upload was answered ``No such folder``.
:data:`CANCELING` is what the folder says for those seconds: the server has
accepted the cancel, the worker has not finished with the folder yet. See
``development_notes/incidents/2026-09-03-cancel-delete-race.md``.

This module imports nothing -- not from girder, not from anywhere -- so the
server, the job-update handler and a remote celery worker with no database can
all agree on the same strings.
"""

#: The run is in progress and has not reached any terminal state.
PROCESSING = "processing"

#: The cancel has been accepted; the worker is stopping the container and
#: writing back whatever the run produced. **Transitional, not terminal**:
#: ``run_submission.settle_canceling_folder`` replaces it with :data:`FAILED`
#: once the worker is genuinely done, and ``/sivacor/reap`` settles it if the
#: worker never gets that far.
CANCELING = "canceling"

#: The run finished and its artifacts are in the folder.
COMPLETED = "completed"

#: Nothing more will be written: the run errored, or it was cancelled and has
#: since stopped, or the reaper gave up on the worker.
FAILED = "failed"

#: The statuses a submission may be deleted from. Anything else means some
#: other party may still be writing to the folder.
#:
#: **Do not add** :data:`CANCELING` here. Its absence *is* the fix for the
#: 2026-09-03 delete race -- the guard in ``delete_submission`` needed no
#: change at all once the status it consults stopped claiming a cancelled run
#: was finished.
DELETABLE = (COMPLETED, FAILED)

#: Statuses that mean the submission is still in flight, for callers that want
#: to display or count "not done yet" without enumerating the terminal set.
IN_FLIGHT = (PROCESSING, CANCELING)
