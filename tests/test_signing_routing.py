"""Signing runs on the manager, not on the worker (plan D2 / P4).

The TRS private key is the trust anchor of the whole system: anything holding it
can mint TROs indistinguishable from real ones. Workers are ephemeral, fully
automated VMs, so signing is routed to ``LOCAL_QUEUE``, which only the manager's
co-located worker consumes.

Every test here covers a failure that is **silent** in production -- a mis-routed
sign step simply keeps running on the worker, and while workers still carry key
material during the transition, nothing looks wrong.
"""

import mock
import pytest
from girder_sivacor.settings import PluginSettings
from girder_sivacor.worker_plugin.routing import (
    DISPATCH_QUEUE,
    LOCAL_QUEUE,
    UNPINNED_TASKS,
    pin_chain,
)
from girder_sivacor.worker_plugin.run_submission import _run_tro, run_tro, sign_tro

FINGERPRINT = "A" * 40
PASSPHRASE = "the TRS signing passphrase"


def test_sign_step_is_declared_on_the_manager_queue():
    """The three facts that have to agree for signing to leave the worker."""
    assert run_tro.queue == DISPATCH_QUEUE
    assert sign_tro.queue == LOCAL_QUEUE
    # ...and pin_chain has to recognise it, or the queue above is overwritten
    # the moment prepare_submission pins the chain.
    assert sign_tro.name.rsplit(".", 1)[-1] in UNPINNED_TASKS


def test_pin_chain_skips_the_signing_step_and_pins_everything_else():
    """
    The regression test for the tempting-but-broken version of this fix.

    Skipping links that "already carry a queue" looks equivalent to skipping the
    sign step and is catastrophically different: links arrive stamped with
    DISPATCH_QUEUE, so such a check would pin *nothing* and scatter one
    submission's chain across workers that do not hold its workspace. The first
    link below is exactly that case and must still be overwritten.
    """
    task = mock.MagicMock()
    task.request.chain = [
        # Arrives carrying the dispatch queue -- must still be pinned.
        {"task": "...run_submission.upload_workspace", "options": {"queue": "sivacor"}},
        # No options at all -- must be pinned.
        {"task": "...run_submission.run_tro"},
        # Signing -- must be left on whatever it was built with.
        {"task": "...run_submission.sign_tro", "options": {"queue": LOCAL_QUEUE}},
    ]

    pin_chain(task, "sivacor.worker-3")

    assert [link["options"]["queue"] for link in task.request.chain] == [
        "sivacor.worker-3",
        "sivacor.worker-3",
        LOCAL_QUEUE,
    ]


def _settings_stub(keys):
    """Return only the settings actually asked for.

    Honouring the requested keys is the point: it lets the tests below assert
    that a passphrase which was never requested cannot reach ``TRO``.
    """
    available = {
        PluginSettings.TRO_PROFILE: {"trov:name": "test-profile"},
        PluginSettings.TRO_GPG_FINGERPRINT: FINGERPRINT,
        PluginSettings.TRO_GPG_PASSPHRASE: PASSPHRASE,
    }
    return {key: available[key] for key in keys}


def _run(action, inumber=0, condition=None):
    """Drive _run_tro with everything external stubbed out.

    Returns the api stub and the patched TRO class so callers can assert on the
    REST calls made and the credentials handed to tro-utils.
    """
    api = mock.MagicMock()
    api.settings.side_effect = _settings_stub
    # add_performance reads back the stage's performance_data JSON; give it real
    # bytes, since a MagicMock would json.loads() as an empty string.
    api.file_chunks.return_value = [b"{}"]
    submission = {
        "job_id": "job-1",
        "folder_id": "folder-1",
        "workspace_dir": "/nonexistent/workspace",
        "runs": [
            {
                "run_start_time": "2026-07-31T10:00:00+00:00",
                "run_end_time": "2026-07-31T10:05:00+00:00",
                "run_attrs": [],
            }
        ],
        "stages": [{"main_file": "main.do"}],
    }
    with (
        mock.patch(
            "girder_sivacor.worker_plugin.run_submission.TRO"
        ) as tro_cls,
        mock.patch("girder_sivacor.worker_plugin.run_submission.os.remove"),
    ):
        _run_tro(mock.MagicMock(), api, submission, action, inumber, condition)
    return api, tro_cls


@pytest.mark.parametrize(
    "action,inumber",
    [("add_arrangement", 0), ("add_performance", 0), ("prune_performance", 0)],
)
def test_non_signing_steps_never_receive_gpg_credentials(action, inumber):
    """
    Neither GPG setting may be shipped to a remote worker.

    The passphrase unlocks the TRS private key; the fingerprint is what makes
    tro-utils go looking in a keyring at all. Since tro-utils 0.4.6 only
    attach_public_key() touches the keyring, and that runs from
    request_timestamp() -- so a non-signing step needs neither, and asking for
    them would hand the signing credential to an ephemeral VM 4 + 2N times per
    submission for nothing.
    """
    api, tro_cls = _run(action, inumber)

    requested = api.settings.call_args[0][0]
    assert PluginSettings.TRO_GPG_PASSPHRASE not in requested
    assert PluginSettings.TRO_GPG_FINGERPRINT not in requested

    kwargs = tro_cls.call_args.kwargs
    assert kwargs["gpg_fingerprint"] is None
    assert kwargs["gpg_passphrase"] is None


def test_signing_step_does_receive_gpg_credentials():
    """Signing is the one action that needs them, so it must still get both."""
    api, tro_cls = _run("sign")

    requested = api.settings.call_args[0][0]
    assert PluginSettings.TRO_GPG_PASSPHRASE in requested
    assert PluginSettings.TRO_GPG_FINGERPRINT in requested
    assert tro_cls.call_args.kwargs["gpg_fingerprint"] == FINGERPRINT
    assert tro_cls.call_args.kwargs["gpg_passphrase"] == PASSPHRASE
    # And it signs + timestamps in one call; see TRO.request_timestamp, which
    # calls trs_signature() internally to produce the .sig before the .tsr.
    tro_cls.return_value.request_timestamp.assert_called_once()


def test_signing_step_never_touches_the_workspace():
    """
    Why signing can run on a host that has never seen the submission's files.

    ``workspace_dir`` above points at a path that does not exist. Signing works
    anyway because it downloads the declaration over REST and, unlike
    add_arrangement, never walks the workspace -- so if this test ever starts
    failing with a filesystem error, signing has acquired a local dependency and
    can no longer run on the manager.
    """
    _, tro_cls = _run("sign")

    tro = tro_cls.return_value
    tro.add_arrangement.assert_not_called()
    tro.add_performance.assert_not_called()
