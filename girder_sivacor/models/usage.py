"""The three collections per-user resource accounting keeps.

Design: ``development_notes/09_user_usage_accounting_plan.md``. The arithmetic
that writes them is :mod:`girder_sivacor.usage`; this module is only where they
live and what is indexed.

**All three are Girder models, including the one the fleet controller writes.**
That is worth saying because it looks like it could not be: ``record_lifetime``
is called from ``sivacor-autoscaler``'s reap loop, which is not a Girder server
and has no plugin load. But the controller already reaches Girder's model layer
exactly this way -- ``dispatch.publish`` uses ``File()`` and ``Setting()``, and
``catalogue.load`` depends on ``Setting().get()`` applying the plugin's
``SettingDefault`` -- so a raw pymongo handle threaded through the call would be
a second way of doing something the process already does, and one that hardcodes
collection names Girder is entitled to own.

Two of the three are **transient**, and their TTL index is not hygiene: it is
what makes the sentence "this row is transient" true. ``sivacor_usage_attribution``
holds a Girder user id, and the privacy argument for keeping it (in the plan's
privacy section, and in the LIA) is that expiry is a property of the collection
rather than a sweep somebody has to remember to run. Declaring it here rather
than in a separate ``ensure_indices`` call wired into plugin load is the
difference between that being structurally true and being true by convention.
"""

from girder.models.model_base import Model

#: How long a transient row lives before Mongo expires it.
#:
#: The same fourteen days, and the same reasoning, as
#: ``run_submission.SPENT_MARKER_TTL_SECONDS``: it must comfortably exceed the
#: controller's ``max_lifetime``, which production sets to 180 h. An expired row
#: costs an under-count (09-U9); a row that expires *before* its partner arrives
#: would silently move a user's spend to the house.
TRANSIENT_TTL_SECONDS = 14 * 24 * 60 * 60


class _TransientJoin(Model):
    """Shared shape: one row per instance, expiring on ``at``."""

    def initialize(self):
        self.ensureIndices(
            [
                # Unique, because both writers are upserts keyed on it and a
                # duplicate would mean one instance's usage counted twice.
                ("instanceId", {"unique": True}),
                ("at", {"expireAfterSeconds": TRANSIENT_TTL_SECONDS}),
            ]
        )

    def validate(self, doc):
        return doc


class UsageAttribution(_TransientJoin):
    """Who a reaped instance's usage belongs to (09-U5).

    Written by Girder when a submission's job reaches a terminal status --
    the last moment the job document, and so the user id, is guaranteed to
    exist. Consumed and deleted at accrual.

    **The one genuinely new exposure in this feature**: it holds a Girder user
    id keyed by instance id, for at most the TTL and in practice for the ~18
    minutes between a job's terminal status and its instance's reap.
    """

    def initialize(self):
        self.name = "sivacor_usage_attribution"
        super().initialize()


class InstanceLifetime(_TransientJoin):
    """What the fleet controller observed about one instance (09-U16).

    Two timestamps and two tags, written at reap strictly before the instance is
    deleted. Holds no identifier at all -- an instance uuid, two datetimes and
    two integers -- and is in the same class as ``sivacor_execution_record``.
    """

    def initialize(self):
        self.name = "sivacor_instance_lifetime"
        super().initialize()


class UsageTotals(Model):
    """The house bucket: burn that belongs to nobody, or to us (09-U7, 09-U11).

    One singleton document. Permanent, and lawful to keep indefinitely for the
    same reason ``sivacor_execution_record`` is: **no user link, no job link, no
    instance id**. A real admin account was rejected for this because it would
    mix platform waste with that admin's genuine submissions, and a future quota
    would lock out the one account that must never be locked out.

    No indices: it has exactly one document, addressed by a literal ``_id``.
    """

    def initialize(self):
        self.name = "sivacor_usage_totals"

    def validate(self, doc):
        return doc
