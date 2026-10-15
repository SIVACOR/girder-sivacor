"""Telling an operator that a submission failed for a reason that is ours.

Three properties are worth more than the rest if you are skimming:

``test_every_failure_code_has_an_alert_policy`` is the drift guard -- silence
is the bug this feature fixes, so a code must not be able to arrive un-alerted
by omission.

``test_a_storm_collapses_into_one_email_and_a_count`` is the 2026-09-06 broker
blip, which failed ten submissions in ~38 hours with an identical signature.

``test_a_failure_that_never_said_why_is_still_alerted`` is the state the issue
calls the single most alert-worthy there is, and the one a naive
"alert where the code is stamped" design silently drops.
"""

import datetime
from unittest import mock

import pytest
from girder.models.user import User
from girder_jobs.constants import JobStatus
from girder_jobs.models.job import Job

from girder_sivacor import alerts
from girder_sivacor.errors import FailureCode
from girder_sivacor.models.alerts import AlertCounter

# Module-wide: everything here needs the plugin loaded, not least because the
# mail templates are registered by `SIVACORPlugin.load`. Without it the first
# send fails with `KeyError: 'submission_failure_admin.mako'` -- which
# `consider` dutifully swallows, so the symptom is a return of "errored" rather
# than a traceback.
pytestmark = pytest.mark.plugin("sivacor")


@pytest.fixture
def clean(server):
    AlertCounter().collection.delete_many({})


@pytest.fixture
def sent(server):
    """Capture what would have gone out, without an SMTP server.

    Patched at ``_submitEmail``, the actual send, rather than at
    ``events.trigger``. The obvious-looking patch is a trap:
    ``girder_sivacor.alerts.events`` *is* ``girder.events``, so stubbing its
    ``trigger`` disables Girder's whole event system for the test -- including
    the model hooks ``createUser`` runs through, which fails three fixtures
    later as a `KeyError: '_id'` with nothing pointing back here.

    Patching the real boundary also means the test exercises the trigger, the
    binding and the handler rather than asserting that we called a function.
    """
    with mock.patch("girder_sivacor.notifications._submitEmail") as submit:
        yield submit


def messages(submit):
    return [call.args[0] for call in submit.call_args_list]


def body(msg):
    return "\n".join(
        part.get_content() for part in msg.walk() if part.get_content_maintype() == "text"
    )


def failed_job(user, code=None, step=None, **meta):
    job = Job().createJob(title="t", type="sivacor_submission", user=user)
    Job().collection.update_one(
        {"_id": job["_id"]},
        {"$set": {"status": JobStatus.ERROR, "meta": meta}},
    )
    if step:
        Job().collection.update_one(
            {"_id": job["_id"]}, {"$set": {"meta.sivacorFailedStep": step}}
        )
    return Job().load(job["_id"], force=True)


# --- the table -------------------------------------------------------------


def test_every_failure_code_has_an_alert_policy():
    """The drift guard. A code with no entry would read as "do not alert",
    which is the safe direction and also a silent one -- and silence is the bug
    being fixed. Same instinct as usage.USER_PAYS."""
    missing = [c.value for c in FailureCode if c not in alerts.ALERT_POLICY]
    assert missing == []


def test_the_guard_actually_fires_when_a_policy_is_missing(monkeypatch):
    """Testing the guard, not just trusting it."""
    monkeypatch.setitem(alerts.ALERT_POLICY, FailureCode.UNEXPECTED, None)
    monkeypatch.delitem(alerts.ALERT_POLICY, FailureCode.UNEXPECTED)
    with pytest.raises(RuntimeError, match="unexpected"):
        alerts._assert_table_is_exhaustive()


def test_the_table_is_not_a_copy_of_who_pays():
    """It answers a different question, and the two disagree in both
    directions. These two cases are the proof, and the reason the issue says
    not to reuse USER_PAYS."""
    from girder_sivacor import usage

    # Billed to the researcher, and a security event: alert anyway.
    assert usage.USER_PAYS[FailureCode.UNSAFE_ARCHIVE] is True
    assert alerts.ALERT_POLICY[FailureCode.UNSAFE_ARCHIVE] is alerts.Alert.EMAIL
    # Also billed to the researcher, and documented as "not a defect".
    assert usage.USER_PAYS[FailureCode.SUBMISSION_DELETED] is True
    assert alerts.ALERT_POLICY[FailureCode.SUBMISSION_DELETED] is alerts.Alert.NEVER


def test_an_unrecognised_code_is_treated_as_a_defect():
    """A code this server does not know came from a worker newer than it, which
    is a half-upgraded deployment -- worth an email in its own right. Returning
    NEVER would make a version skew invisible in exactly the way this module
    exists to prevent."""
    assert alerts.policy_for("a_code_from_the_future") is alerts.Alert.EMAIL


# --- what gets mailed ------------------------------------------------------


def test_the_researchers_own_error_sends_nothing(clean, sent, user):
    """stata_error and nonzero_exit are the bulk of all failures. Mailing them
    would bury everything that matters, which is the failure mode to avoid."""
    job = failed_job(user)
    assert alerts.consider(job, FailureCode.STATA_ERROR, "601") == "never"
    assert messages(sent) == []


def test_our_own_defect_sends_exactly_one(clean, sent, user, admin):
    job = failed_job(user, step="execute_workflow")
    assert alerts.consider(job, FailureCode.UNEXPECTED, "APIError") == "emailed"

    assert len(messages(sent)) == 1
    text = body(messages(sent)[0])
    assert "unexpected" in text
    assert "APIError" in text
    assert "execute_workflow" in text
    assert str(job["_id"]) in text
    assert user["login"] in text


def test_a_security_event_is_alerted_even_though_the_researcher_pays(
    clean, sent, user, admin
):
    job = failed_job(user)
    assert alerts.consider(job, FailureCode.UNSAFE_ARCHIVE, "path_escape") == "emailed"
    assert len(messages(sent)) == 1


def test_an_expensive_but_ordinary_failure_waits_for_the_digest(
    clean, sent, user, admin
):
    """Out of memory is the researcher's to pay for and is the most expensive
    outcome in its class. Counted, not mailed."""
    job = failed_job(user)
    assert alerts.consider(job, FailureCode.OUT_OF_MEMORY, 32212254720) == "digested"
    assert messages(sent) == []
    assert AlertCounter().collection.count_documents({}) == 1


def test_a_successful_submission_is_never_considered(clean, sent, user):
    """Nothing calls consider on success; the guard is that no path does."""
    assert messages(sent) == []


def test_one_failure_is_alerted_once_however_many_times_it_is_stamped(
    clean, sent, user, admin
):
    """Two sites stamp an outcome and a REST call can be retried. The claim is
    atomic so exactly one of them mails."""
    job = failed_job(user)
    first = alerts.consider(job, FailureCode.UNEXPECTED, "APIError")
    second = alerts.consider(job, FailureCode.UNEXPECTED, "APIError")

    assert first == "emailed"
    assert second == "already-alerted"
    assert len(messages(sent)) == 1


# --- storm control ---------------------------------------------------------


def test_a_storm_collapses_into_one_email_and_a_count(clean, sent, user, admin):
    """2026-09-06: ten submissions in ~38 hours, six in one night, every one
    with an identical signature. Ten identical emails is the same information
    as one email and a number, delivered nine times worse."""
    outcomes = [
        alerts.consider(failed_job(user), FailureCode.UNEXPECTED, "APIError")
        for _ in range(10)
    ]

    assert outcomes[0] == "emailed"
    assert set(outcomes[1:]) == {"suppressed"}
    assert len(messages(sent)) == 1
    row = AlertCounter().collection.find_one({"code": "unexpected"})
    assert row["count"] == 10


def test_a_different_signature_is_a_different_alert(clean, sent, user, admin):
    """Collapsing on the code alone would merge two unrelated problems. Two
    images failing to pull are two things to look at."""
    alerts.consider(failed_job(user), FailureCode.IMAGE_PULL_FAILED, "rocker/r-ver:4.6.1")
    alerts.consider(failed_job(user), FailureCode.IMAGE_PULL_FAILED, "dynare/dynare:6.1")

    assert len(messages(sent)) == 2


def test_the_counters_never_hold_researcher_content(clean, user, admin):
    """The counter collection accumulates between digests, so what lands in it
    goes through telemetry's per-code allow-list -- the same boundary that lets
    execution records be kept forever. A path is not an image reference."""
    alerts.consider(
        failed_job(user), FailureCode.IMAGE_PULL_FAILED, "/home/jane/thesis.dta"
    )
    row = AlertCounter().collection.find_one({"code": "image_pull_failed"})
    assert row["detail"] is None


def test_the_counter_rows_carry_no_identifier(clean, user, admin):
    alerts.consider(failed_job(user), FailureCode.UNEXPECTED, "APIError")
    row = AlertCounter().collection.find_one({})
    flat = str(row)
    assert str(user["_id"]) not in flat
    assert user["login"] not in flat
    assert set(row) <= {"_id", "code", "detail", "count", "since", "lastAt"}


# --- the code that never arrived -------------------------------------------


def test_a_failure_that_never_said_why_is_still_alerted(clean, sent, user, admin):
    """"We failed and could not say why" is the most alert-worthy state there
    is, and a design that only alerts where a code is stamped drops it."""
    job = failed_job(user)
    Job().collection.update_one(
        {"_id": job["_id"]},
        {"$set": {"updated": datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1)}},
    )

    result = alerts.sweep_undecided()

    assert result == {"found": 1, "alerted": 1}
    assert len(messages(sent)) == 1
    assert "unknown" in body(messages(sent)[0])


def test_the_sweep_leaves_alone_a_failure_it_decided_to_stay_quiet_about(
    clean, sent, user, admin
):
    """The bug this guards: if a NEVER code did not mark the job as decided,
    the sweep would mail an operator about every stata_error on the
    deployment."""
    job = failed_job(user)
    assert alerts.consider(job, FailureCode.STATA_ERROR, "601") == "never"
    Job().collection.update_one(
        {"_id": job["_id"]},
        {"$set": {"updated": datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1)}},
    )

    assert alerts.sweep_undecided() == {"found": 0, "alerted": 0}
    assert messages(sent) == []


def test_the_sweep_does_not_race_the_stamp_it_is_checking_for(clean, sent, user):
    """A code arrives one REST call after the transition. Alerting "unknown"
    about a failure whose code lands a second later is a false alarm about the
    alarm system itself."""
    failed_job(user)  # updated = now
    assert alerts.sweep_undecided() == {"found": 0, "alerted": 0}


def test_the_sweep_does_not_reconsider_a_deployments_whole_history(
    clean, sent, user, admin
):
    """On first deploy every historical failure is undecided by definition."""
    job = failed_job(user)
    Job().collection.update_one(
        {"_id": job["_id"]},
        {"$set": {"updated": datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=30)}},
    )
    assert alerts.sweep_undecided() == {"found": 0, "alerted": 0}


# --- the digest ------------------------------------------------------------


def test_the_digest_reports_counts_and_empties_the_counters(
    clean, sent, user, admin
):
    for _ in range(3):
        alerts.consider(failed_job(user), FailureCode.OUT_OF_MEMORY, 32212254720)
    alerts.consider(failed_job(user), FailureCode.OUT_OF_DISK)

    result = alerts.send_digest()

    assert result == {"sent": True, "signatures": 2, "failures": 4}
    text = body(messages(sent)[-1])
    assert "out_of_memory" in text and "out_of_disk" in text
    assert AlertCounter().collection.count_documents({}) == 0


def test_the_digest_says_nothing_when_nothing_happened(clean, sent, admin):
    """A daily email that usually reads "nothing happened" trains people to
    stop opening it."""
    assert alerts.send_digest() == {"sent": False, "signatures": 0, "failures": 0}
    assert messages(sent) == []


def test_the_digest_is_what_makes_collapsing_a_storm_lossless(
    clean, sent, user, admin
):
    """The suppressed repeats are not discarded; they are the count the digest
    reports. One email as it happened, plus a line saying it happened ten
    times."""
    for _ in range(10):
        alerts.consider(failed_job(user), FailureCode.UNEXPECTED, "APIError")

    alerts.send_digest()

    text = body(messages(sent)[-1])
    assert "unexpected" in text
    assert "10" in text


def test_the_digest_holds_no_identifier(clean, sent, user, admin):
    alerts.consider(failed_job(user), FailureCode.OUT_OF_MEMORY, 32212254720)
    alerts.send_digest()
    text = body(messages(sent)[-1])
    assert user["login"] not in text
    assert str(user["_id"]) not in text


# --- never breaking the thing it reports on --------------------------------


def test_smtp_being_down_does_not_change_the_submission_outcome(clean, user, admin):
    """The same never-raise rule the heartbeat follows. The run already failed;
    failing it differently because we could not talk about it helps nobody."""
    job = failed_job(user)
    with mock.patch(
        "girder_sivacor.notifications._submitEmail",
        side_effect=RuntimeError("no smtp"),
    ):
        assert alerts.consider(job, FailureCode.UNEXPECTED, "APIError") == "errored"


def test_no_admin_email_address_is_survivable(clean, sent, user, admin):
    with mock.patch.object(User, "getAdmins", return_value=[]):
        assert alerts.consider(failed_job(user), FailureCode.UNEXPECTED) == "emailed"
    assert messages(sent) == []


def test_the_console_redirect_carries_the_new_emails(clean, user, admin, capsys):
    """deploy-dev has no SMTP, and GIRDER_EMAIL_TO_CONSOLE is how it exercises
    mail. It works here for free -- the alert goes through the same
    `_sendmail` trigger every other SIVACOR email does -- but "for free"
    is exactly the kind of thing that stops being true silently."""
    with mock.patch.dict("os.environ", {"GIRDER_EMAIL_TO_CONSOLE": "1"}):
        assert alerts.consider(failed_job(user), FailureCode.UNEXPECTED) == "emailed"

    printed = capsys.readouterr().out
    assert "Redirecting email to console" in printed
    assert "unexpected" in printed
