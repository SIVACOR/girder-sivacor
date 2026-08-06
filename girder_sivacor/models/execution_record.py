"""Permanent, non-personal records of what the pipeline actually ran.

Everything else about a submission is transient: the user can delete it, and
the retention sweep deletes whatever they do not. That is intentional, but it
means the deployment currently cannot answer "how many Stata runs failed last
quarter, and why" -- by the time the question is asked the evidence is gone.

This collection is the exception. It is deliberately *not* linked to the job,
folder, or user it came from, so nothing in it has to be found and deleted when
a submission goes away. :func:`girder_sivacor.telemetry.sanitize_record` is
what guarantees that; this model only stores what that filter returns.

Nothing here is user-accessible: there is no REST route that reads it. Reports
are run against Mongo directly by an operator.
"""

from girder.models.model_base import Model


class ExecutionRecord(Model):
    def initialize(self):
        self.name = "sivacor_execution_record"
        # 'date' for time-series reporting, the other two for the two questions
        # actually asked of this collection: what fails, and what fails on
        # which image.
        self.ensureIndices(["date", "status", "error.code", "stages.image_name"])

    def validate(self, doc):
        # Documents only ever arrive from sanitize_record(), which constructs
        # them field by field from an allow-list. Re-validating the shape here
        # would duplicate that without adding a guarantee -- the filter is the
        # boundary, and it is unit-tested as one.
        return doc
