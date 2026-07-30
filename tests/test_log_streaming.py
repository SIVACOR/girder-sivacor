"""The Redis-to-WebSocket log relay.

The bug these cover was invisible to a mocked test: with a ``MagicMock`` in place
of the websocket, ``send_text(b"...")`` is perfectly happy. It only fails once a
real ASGI server is on the other end, because Starlette accepts bytes into the
ASGI message's ``"text"`` field and *uvicorn* is what calls ``.encode()`` on it.
So the relay test below runs against an actual uvicorn instance.
"""

import asyncio
import threading

import mock
import pytest


def test_pubsub_client_decodes_responses(monkeypatch):
    """The pubsub client must hand back str, not bytes.

    ``listen_to_redis`` forwards ``message["data"]`` straight to
    ``websocket.send_text``, which is only valid for str.
    """
    from girder_sivacor import logs

    logs._redis_client_async.cache_clear()
    monkeypatch.setenv("GIRDER_NOTIFICATION_REDIS_URL", "redis://localhost:6379")
    try:
        client = logs._redis_client_async()
        kwargs = client.connection_pool.connection_kwargs
        assert kwargs.get("decode_responses") is True, (
            "pubsub payloads would arrive as bytes and break send_text"
        )
    finally:
        logs._redis_client_async.cache_clear()


def test_relay_forwards_str_over_a_real_asgi_server():
    """A bytes payload must never reach send_text.

    Drives the real thing: uvicorn serving a Starlette websocket route, with a
    websockets client on the far end. Sending a str works; sending bytes raises
    inside uvicorn, which is exactly how the live log stream died in production
    while every mocked test passed.
    """
    uvicorn = pytest.importorskip("uvicorn")
    websockets = pytest.importorskip("websockets")
    from starlette.applications import Starlette
    from starlette.routing import WebSocketRoute

    outcome = {}

    async def endpoint(websocket):
        await websocket.accept()
        await websocket.send_text("greeting")
        try:
            await websocket.send_text(b"bytes payload")
            outcome["bytes_raised"] = None
        except Exception as exc:  # noqa: BLE001 - recording it is the point
            outcome["bytes_raised"] = type(exc).__name__
        await asyncio.sleep(0.3)

    app = Starlette(routes=[WebSocketRoute("/ws", endpoint)])
    config = uvicorn.Config(app, host="127.0.0.1", port=8769, log_level="error")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()

    async def drive():
        for _ in range(40):
            try:
                async with websockets.connect("ws://127.0.0.1:8769/ws") as conn:
                    first = await asyncio.wait_for(conn.recv(), timeout=2)
                    outcome["first_frame"] = first
                    try:
                        await asyncio.wait_for(conn.recv(), timeout=1)
                        outcome["second_frame"] = "delivered"
                    except Exception as exc:  # noqa: BLE001
                        outcome["second_frame"] = type(exc).__name__
                    return
            except (OSError, asyncio.TimeoutError):
                await asyncio.sleep(0.25)
        pytest.skip("could not reach the local uvicorn instance")

    try:
        asyncio.run(drive())
    finally:
        server.should_exit = True

    assert outcome.get("first_frame") == "greeting", "a str payload must arrive intact"
    # This is the trap: bytes look fine to Starlette and blow up in uvicorn.
    assert outcome["bytes_raised"] == "AttributeError", (
        "expected uvicorn to reject bytes in the ASGI 'text' field; if this "
        "changes, revisit the decode_responses comment in logs.py"
    )


@pytest.mark.plugin("sivacor")
def test_listener_only_forwards_data_messages(server, db):
    """Subscription confirmations must not be relayed as log lines."""
    from girder_sivacor.logs import DockerLogStreamer

    streamer = DockerLogStreamer(scope={"type": "websocket"}, receive=None, send=None)

    async def fake_listen():
        yield {"type": "subscribe", "channel": "docker:logs:x", "data": 1}
        yield {"type": "message", "channel": "docker:logs:x", "data": "a log line"}

    streamer.pubsub = mock.MagicMock()
    streamer.pubsub.listen = fake_listen
    streamer.pubsub.unsubscribe = mock.AsyncMock()
    streamer.pubsub.close = mock.AsyncMock()

    ws = mock.MagicMock()
    ws.send_text = mock.AsyncMock()
    ws.close = mock.AsyncMock()

    asyncio.run(streamer.listen_to_redis(ws))

    ws.send_text.assert_awaited_once_with("a log line")
    for call in ws.send_text.await_args_list:
        assert isinstance(call.args[0], str), "send_text requires str, not bytes"
