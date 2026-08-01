import socket

from girder_worker import GirderWorkerPluginABC


def _keepalive_transport_options() -> dict:
    """Broker/backend socket settings that let a dead peer actually be noticed.

    **The problem this solves cost a VM 30 h of allocation on 2026-08-01.** A
    worker finished its submissions, sat idle ~13 min, and its Redis connection
    went half-open -- the flow was dropped somewhere in the path (NAT, security
    group, or Redis's own ``timeout``) with no FIN/RST. Nothing noticed:

    * A *fresh* connection worked fine (``redis-cli ping`` -> ``NOAUTH``), so the
      network was healthy; only celery's established socket was a zombie.
    * The worker never logged again, never retried, and could not answer control
      broadcasts -- so ``celery inspect`` timed out forever, the self-shutdown
      supervisor read that as "busy", and the instance became immortal.
    * Even ``kill -QUIT`` hung: the process handles signals fine, it is blocked
      in ``recv()`` on a socket the kernel still believes is ESTABLISHED.

    kombu already sets ``health_check_interval`` (default 25 s), and that is not
    enough on its own: redis-py health-checks a connection when it is *taken from
    the pool*, but the consumer connection is parked in a blocking read and is
    never taken from anywhere. Only TCP keepalive probes an idle-but-open socket,
    and kombu leaves ``socket_keepalive`` at ``None`` -- off.

    With these settings the kernel starts probing after 60 s idle and gives up
    after 3 x 10 s, so a dead peer surfaces as a normal connection error in ~90 s
    and kombu reconnects, instead of hanging indefinitely.

    Worth being clear about the stakes: both wedges observed so far happened
    *after* the work was done, so they only cost SUs. Nothing prevents a worker
    going deaf mid-chain, and that would strand a live submission until the
    reaper -- which is why this is a correctness fix, not a tidiness one.
    """
    options: dict = {
        "socket_keepalive": True,
        # Only bites if a socket timeout is ever configured -- redis-py raises
        # TimeoutError, and this retries instead of failing the operation. Inert
        # today (no socket_timeout is set) and kept as a deliberate default for
        # whoever adds one. `socket_timeout` is *not* set here on purpose: the
        # consumer parks in a blocking read, and a timeout there causes spurious
        # errors. Keepalive is the correct mechanism for a dead peer.
        "retry_on_timeout": True,
    }
    # Linux names; guarded because these constants are absent or spelled
    # differently on other platforms and the test suite is not Linux-only.
    keepalive_options = {
        getattr(socket, name): value
        for name, value in (
            ("TCP_KEEPIDLE", 60),
            ("TCP_KEEPINTVL", 10),
            ("TCP_KEEPCNT", 3),
        )
        if hasattr(socket, name)
    }
    if keepalive_options:
        options["socket_keepalive_options"] = keepalive_options
    return options


def _apply_transport_options(sender, **kwargs):
    """Merge the keepalive settings into an app whose config is already loaded.

    Merged rather than assigned: girder_worker or another plugin may have put
    something here, and silently dropping broker settings is exactly the class of
    bug this exists to fix.

    Both connections get it. The result backend is the same Redis and parks
    connections just as long, so a half-open backend socket would hang a chain
    *mid-flight* rather than at rest -- the worse of the two failures.
    """
    keepalive = _keepalive_transport_options()
    for setting in ("broker_transport_options", "result_backend_transport_options"):
        options = dict(getattr(sender.conf, setting, None) or {})
        options.update(keepalive)
        setattr(sender.conf, setting, options)


class SIVACORWorkerPlugin(GirderWorkerPluginABC):
    def __init__(self, app, *args, **kwargs):
        self.app = app

        # MUST be deferred to the signal; setting app.conf here does not stick.
        # girder_worker's app.py instantiates plugins via discover_tasks(app) and
        # only *then* calls app.config_from_object(..., force=True), which discards
        # anything already on app.conf. Verified against the deployed image: the
        # options apply correctly to a bare Celery() instance, yet the imported
        # girder_worker.app shows broker_transport_options == {}.
        #
        # on_after_configure fires once the config source has been applied, so our
        # merge lands on top of it rather than under it -- and it fires for clients
        # as well as workers, so the Girder server's publishing connection is
        # covered too. weak=False because the only reference to this receiver is
        # the connection itself.
        app.on_after_configure.connect(_apply_transport_options, weak=False)

    def task_imports(self):
        return [
            "girder_sivacor.worker_plugin.run_submission",
        ]
