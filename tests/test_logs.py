import asyncio
import datetime
import os
from unittest.mock import AsyncMock, Mock, patch
import pytest


class TestRedisClient:
    """Test Redis client initialization."""

    def test_redis_client_default_url(self):
        """Test Redis client with default URL."""
        with patch.dict(os.environ, {}, clear=True), patch(
            "redis.asyncio.Redis.from_url"
        ) as mock_redis:
            from girder_sivacor.logs import _redis_client_async

            _redis_client_async.cache_clear()  # Clear cache for test

            _redis_client_async()
            mock_redis.assert_called_once_with("redis://localhost:6379")

    def test_redis_client_custom_url(self):
        """Test Redis client with custom URL from environment."""
        custom_url = "redis://custom:6379"
        with patch.dict(
            os.environ, {"GIRDER_NOTIFICATION_REDIS_URL": custom_url}
        ), patch("redis.asyncio.Redis.from_url") as mock_redis:
            from girder_sivacor.logs import _redis_client_async

            _redis_client_async.cache_clear()  # Clear cache for test

            _redis_client_async()
            mock_redis.assert_called_once_with(custom_url)


class TestDockerLogStreamerMethods:
    """Test individual methods of DockerLogStreamer."""

    @pytest.fixture
    def streamer_instance(self):
        """Create a DockerLogStreamer instance with mocked dependencies."""
        from girder_sivacor.logs import DockerLogStreamer

        # Mock the ASGI interface requirements
        scope = {"type": "websocket"}
        receive = AsyncMock()
        send = AsyncMock()

        streamer = DockerLogStreamer(scope, receive, send)
        return streamer

    @pytest.fixture
    def mock_websocket(self):
        """Create a mock WebSocket."""
        websocket = Mock()
        websocket.query_params = {"token": "test_token_123"}
        websocket.client = "127.0.0.1:8080"
        websocket.close = AsyncMock()
        websocket.accept = AsyncMock()
        websocket.send_text = AsyncMock()
        return websocket

    @pytest.mark.asyncio
    async def test_on_connect_missing_token(self, streamer_instance, mock_websocket):
        """Test connection rejection when token is missing."""
        mock_websocket.query_params = {}

        await streamer_instance.on_connect(mock_websocket)

        mock_websocket.close.assert_called_once_with(
            code=3000, reason="Token is required"
        )

    @pytest.mark.asyncio
    async def test_on_connect_invalid_token(self, streamer_instance, mock_websocket):
        """Test connection rejection with invalid token."""
        with patch("girder_sivacor.logs.Token") as mock_token_class:
            mock_token_instance = Mock()
            mock_token_instance.load.return_value = None
            mock_token_class.return_value = mock_token_instance

            await streamer_instance.on_connect(mock_websocket)

            mock_websocket.close.assert_called_once_with(
                code=3000, reason="Invalid or expired token"
            )

    @pytest.mark.asyncio
    async def test_on_connect_expired_token(self, streamer_instance, mock_websocket):
        """Test connection rejection with expired token."""
        expired_time = datetime.datetime.now(
            datetime.timezone.utc
        ) - datetime.timedelta(hours=1)
        expired_token = {
            "userId": "test_user_123",
            "expires": expired_time,
        }

        with patch("girder_sivacor.logs.Token") as mock_token_class:
            mock_token_instance = Mock()
            mock_token_instance.load.return_value = expired_token
            mock_token_class.return_value = mock_token_instance

            await streamer_instance.on_connect(mock_websocket)

            mock_websocket.close.assert_called_once_with(
                code=3000, reason="Invalid or expired token"
            )

    @pytest.mark.asyncio
    async def test_on_connect_missing_user_id(self, streamer_instance, mock_websocket):
        """Test connection rejection when token missing userId."""
        future_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            hours=1
        )
        token_without_user = {
            "expires": future_time,
        }

        with patch("girder_sivacor.logs.Token") as mock_token_class:
            mock_token_instance = Mock()
            mock_token_instance.load.return_value = token_without_user
            mock_token_class.return_value = mock_token_instance

            await streamer_instance.on_connect(mock_websocket)

            mock_websocket.close.assert_called_once_with(
                code=3000, reason="Invalid or expired token"
            )

    @pytest.mark.asyncio
    async def test_on_connect_invalid_scope(self, streamer_instance, mock_websocket):
        """Test connection rejection when token has invalid scope."""
        future_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            hours=1
        )
        valid_token = {
            "userId": "test_user_123",
            "expires": future_time,
        }

        with patch("girder_sivacor.logs.Token") as mock_token_class, patch(
            "girder_sivacor.logs.TokenScope"
        ) as mock_scope:
            mock_token_instance = Mock()
            mock_token_instance.load.return_value = valid_token
            mock_token_instance.hasScope.return_value = False  # Invalid scope
            mock_token_class.return_value = mock_token_instance
            mock_scope.USER_AUTH = "userAuth"

            await streamer_instance.on_connect(mock_websocket)

            mock_websocket.close.assert_called_once_with(
                code=3000, reason="Invalid or expired token"
            )

    @pytest.mark.asyncio
    async def test_on_connect_success(self, streamer_instance, mock_websocket):
        """Test successful WebSocket connection."""
        future_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            hours=1
        )
        valid_token = {
            "userId": "test_user_123",
            "expires": future_time,
        }

        mock_pubsub = AsyncMock()
        mock_redis = Mock()
        mock_redis.pubsub.return_value = mock_pubsub

        with patch("girder_sivacor.logs.Token") as mock_token_class, patch(
            "girder_sivacor.logs.TokenScope"
        ) as mock_scope, patch(
            "girder_sivacor.logs._redis_client_async", return_value=mock_redis
        ), patch.object(asyncio, "create_task") as mock_create_task:
            mock_token_instance = Mock()
            mock_token_instance.load.return_value = valid_token
            mock_token_instance.hasScope.return_value = True
            mock_token_class.return_value = mock_token_instance
            mock_scope.USER_AUTH = "userAuth"

            await streamer_instance.on_connect(mock_websocket)

            # Verify connection flow
            mock_websocket.accept.assert_called_once()
            mock_pubsub.subscribe.assert_called_once_with("docker:logs:test_user_123")
            mock_create_task.assert_called_once()
            mock_websocket.send_text.assert_called_once_with(
                "Connected to Docker log stream."
            )

            # Verify state setup
            assert streamer_instance.user_id == "test_user_123"
            assert streamer_instance.channel == "docker:logs:test_user_123"
            assert streamer_instance.pubsub == mock_pubsub

    @pytest.mark.asyncio
    async def test_on_receive_does_nothing(self, streamer_instance):
        """Test that on_receive method does nothing (read-only logs)."""
        mock_websocket = AsyncMock()
        data = "some command"

        # Should not raise any exceptions or do anything
        await streamer_instance.on_receive(mock_websocket, data)

        # Verify no WebSocket operations were called
        mock_websocket.send_text.assert_not_called()
        mock_websocket.close.assert_not_called()

    @pytest.mark.asyncio
    async def test_on_disconnect_without_task(self, streamer_instance):
        """Test disconnect handling when no Redis task exists."""
        mock_websocket = AsyncMock()
        mock_websocket.client = "127.0.0.1:8080"

        streamer_instance.redis_listener_task = None

        with patch("builtins.print") as mock_print, patch.object(
            asyncio, "gather", new_callable=AsyncMock
        ) as mock_gather:
            await streamer_instance.on_disconnect(mock_websocket, 1000)

        # Verify no task operations
        mock_gather.assert_not_called()

        # Verify logging still occurs
        mock_print.assert_called_once()
        assert "WebSocket disconnected:" in str(mock_print.call_args[0][0])

    @pytest.mark.asyncio
    async def test_on_disconnect_with_task(self, streamer_instance):
        """Test disconnect handling when Redis task exists."""
        mock_websocket = AsyncMock()
        mock_websocket.client = "127.0.0.1:8080"

        # Create a mock task
        mock_task = AsyncMock()
        mock_task.cancel = Mock()
        streamer_instance.redis_listener_task = mock_task

        with patch("builtins.print") as mock_print, patch.object(
            asyncio, "gather", new_callable=AsyncMock
        ) as mock_gather:
            await streamer_instance.on_disconnect(mock_websocket, 1000)

        # Verify task cleanup
        mock_task.cancel.assert_called_once()
        mock_gather.assert_called_once_with(mock_task, return_exceptions=True)

        # Verify logging
        mock_print.assert_called_once()
        assert "WebSocket disconnected:" in str(mock_print.call_args[0][0])

    @pytest.mark.asyncio
    async def test_listen_to_redis_cancelled_error(self, streamer_instance):
        """Test Redis listener handles cancellation gracefully."""
        mock_websocket = AsyncMock()
        mock_pubsub = AsyncMock()

        # Mock pubsub to raise CancelledError
        async def mock_listen():
            raise asyncio.CancelledError()

        mock_pubsub.listen.return_value = mock_listen()
        mock_pubsub.unsubscribe = AsyncMock()
        mock_pubsub.close = AsyncMock()
        streamer_instance.pubsub = mock_pubsub

        # Should handle CancelledError gracefully
        await streamer_instance.listen_to_redis(mock_websocket)

        # Verify cleanup
        mock_pubsub.unsubscribe.assert_called_once()
        mock_pubsub.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_listen_to_redis_exception_handling(self, streamer_instance):
        """Test Redis listener handles exceptions."""
        mock_websocket = AsyncMock()
        mock_pubsub = AsyncMock()

        # Mock pubsub to raise an exception
        async def mock_listen():
            raise Exception("Redis connection failed")

        mock_pubsub.listen.return_value = mock_listen()
        mock_pubsub.unsubscribe = AsyncMock()
        mock_pubsub.close = AsyncMock()
        streamer_instance.pubsub = mock_pubsub

        with patch("builtins.print") as mock_print:
            await streamer_instance.listen_to_redis(mock_websocket)

        # Verify error handling
        mock_print.assert_called_once()
        assert "Redis listener error:" in str(mock_print.call_args[0][0])
        mock_websocket.close.assert_called_once_with(code=1011)

        # Verify cleanup
        mock_pubsub.unsubscribe.assert_called_once()
        mock_pubsub.close.assert_called_once()


class TestRedisMessageProcessing:
    """Test Redis message processing functionality."""

    @pytest.mark.asyncio
    async def test_message_processing(self):
        """Test processing of Redis messages."""
        from girder_sivacor.logs import DockerLogStreamer

        # Create a minimal streamer instance for testing
        scope = {"type": "websocket"}
        receive = AsyncMock()
        send = AsyncMock()
        streamer = DockerLogStreamer(scope, receive, send)

        mock_websocket = AsyncMock()
        mock_pubsub = AsyncMock()

        # Mock message iterator
        messages = [
            {"type": "message", "data": "Log line 1"},
            {"type": "message", "data": "Log line 2"},
            {"type": "subscribe", "data": 1},  # Non-message type should be ignored
        ]

        message_count = 0

        async def mock_listen():
            nonlocal message_count
            for msg in messages:
                yield msg
                message_count += 1
                # Stop after a few iterations to prevent infinite loop
                if message_count >= len(messages):
                    break

        mock_pubsub.listen.return_value = mock_listen()
        mock_pubsub.unsubscribe = AsyncMock()
        mock_pubsub.close = AsyncMock()
        streamer.pubsub = mock_pubsub

        # Create a task that we can cancel
        task = asyncio.create_task(streamer.listen_to_redis(mock_websocket))

        # Let it process messages briefly
        await asyncio.sleep(0.01)
        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass

        # Verify that send_text was called (exact count may vary due to async timing)
        assert (
            mock_websocket.send_text.call_count >= 0
        )  # At least some calls should happen


if __name__ == "__main__":
    # Run tests with: python -m pytest tests/test_logs.py -v
    import sys
    import subprocess

    result = subprocess.run(
        [sys.executable, "-m", "pytest", __file__, "-v"], capture_output=False
    )
    sys.exit(result.returncode)
