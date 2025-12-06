import asyncio
import datetime
import functools
import os

import redis.asyncio as aioredis
from girder.constants import TokenScope
from girder.models.token import Token
from starlette.endpoints import WebSocketEndpoint


@functools.lru_cache
def _redis_client_async() -> aioredis.Redis:
    url = os.environ.get("GIRDER_NOTIFICATION_REDIS_URL", "redis://localhost:6379")
    return aioredis.Redis.from_url(url)


class DockerLogStreamer(WebSocketEndpoint):
    # This task holds the reference to the running Redis listener loop
    encoding = "text"
    redis_listener_task = None

    async def on_connect(self, websocket):
        """Called when a client connects to the WebSocket."""
        token_id = websocket.query_params.get("token")
        if not token_id:
            await websocket.close(
                code=3000, reason="Token is required"
            )  # Policy Violation
            return

        token = Token().load(token_id, force=True, objectId=False)
        if (
            token is None
            or token["expires"] < datetime.datetime.now(datetime.timezone.utc)
            or "userId" not in token
            or not Token().hasScope(token, TokenScope.USER_AUTH)
        ):
            await websocket.close(code=3000, reason="Invalid or expired token")
            return

        await websocket.accept()
        self.user_id = token["userId"]
        self.channel = f"docker:logs:{self.user_id}"
        self.pubsub = _redis_client_async().pubsub()
        await self.pubsub.subscribe(self.channel)

        self.redis_listener_task = asyncio.create_task(self.listen_to_redis(websocket))
        await websocket.send_text("Connected to Docker log stream.")

    async def listen_to_redis(self, websocket):
        """
        An infinite loop that blocks on the Pub/Sub channel and sends data
        to the client when a message arrives.
        """
        try:
            # The async iterator waits for messages
            async for message in self.pubsub.listen():
                if message["type"] == "message":
                    log_line = message["data"]
                    # Send the log line directly over the WebSocket
                    await websocket.send_text(log_line)

        except asyncio.CancelledError:
            # Expected error when the task is cancelled (e.g., on_disconnect)
            pass
        except Exception as e:
            print(f"Redis listener error: {e}")
            await websocket.close(code=1011)  # Server error
        finally:
            # Cleanup on exit
            await self.pubsub.unsubscribe()
            await self.pubsub.close()

    async def on_receive(self, websocket, data):
        """
        Logs are read-only; we can optionally handle commands here.
        e.g., await websocket.send_text(f"Received command: {data}")
        """
        pass  # Not needed for simple log streaming

    async def on_disconnect(self, websocket, close_code):
        """Called when the client closes the connection."""
        print(f"WebSocket disconnected: {websocket.client} with code {close_code}")
        if self.redis_listener_task:
            self.redis_listener_task.cancel()
            await asyncio.gather(self.redis_listener_task, return_exceptions=True)
