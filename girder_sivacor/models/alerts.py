"""The one collection admin alerting keeps.

A counter per ``(code, detail)``, which is what makes storm control possible:
the 2026-09-06 broker blip failed ten submissions in ~38 hours, six of them in
one night, every one with an identical signature. Ten identical emails is the
same information as one email and a number, delivered nine times worse.

**It holds no identifier, and that is worth stating rather than discovering.**
``code`` is the closed operator vocabulary from :mod:`girder_sivacor.errors`
and ``detail`` is validated through :func:`girder_sivacor.telemetry.safe_detail`
-- the same per-code allow-list that lets execution records be kept forever. So
this collection is anonymous by construction, which is why it can accumulate
between digests without a privacy argument attached. The emails it drives *do*
name a job and therefore a person; those are sent and not stored.

Rows are consumed and deleted by each digest, so a quiet deployment's
collection is empty, not growing. The TTL index is the backstop for a digest
that never runs: a counter nobody collected should expire rather than make an
eventual digest claim to cover a month.
"""

import datetime

from girder.models.model_base import Model
from pymongo import ReturnDocument

#: How long an uncollected counter lives. Comfortably longer than the daily
#: digest, so expiry only ever happens when the digest is not running at all --
#: in which case a stale count is worse than no count.
COUNTER_TTL_SECONDS = 7 * 24 * 60 * 60


class AlertCounter(Model):
    """How many times one ``(code, detail)`` has failed since the last digest."""

    def initialize(self):
        self.name = "sivacor_alert_counter"
        self.ensureIndices(
            [
                # Unique, because `note` is an upsert keyed on it: two servers
                # failing the same way at the same moment must land on one row,
                # or the storm control they drive counts each separately and
                # mails twice.
                ([("code", 1), ("detail", 1)], {"unique": True}),
                ("since", {"expireAfterSeconds": COUNTER_TTL_SECONDS}),
            ]
        )

    def validate(self, doc):
        return doc

    def note(self, code, detail) -> tuple[int, bool]:
        """Count one failure. Returns ``(count_in_window, is_first)``.

        Atomic, and the atomicity is the whole point: ``is_first`` decides
        whether an email goes out, so two servers answering it independently
        would send two. ``ReturnDocument.BEFORE`` makes "did this row exist"
        and "increment it" the same operation.
        """
        now = datetime.datetime.now(datetime.UTC)
        previous = self.collection.find_one_and_update(
            {"code": code, "detail": detail},
            {
                "$inc": {"count": 1},
                "$set": {"lastAt": now},
                "$setOnInsert": {"since": now},
            },
            upsert=True,
            return_document=ReturnDocument.BEFORE,
        )
        if previous is None:
            return 1, True
        return previous.get("count", 0) + 1, False

    def drain(self) -> list[dict]:
        """Take every counter and empty the collection.

        Read-then-delete rather than delete-then-read, and claimed row by row,
        so a digest that dies half way through has reported what it removed
        rather than removed what it never reported. Losing a count to a crash
        under-reports a number; losing it to an unsent email loses the only
        record that it happened at all.
        """
        taken = []
        while True:
            row = self.collection.find_one_and_delete({})
            if row is None:
                return taken
            taken.append(row)
