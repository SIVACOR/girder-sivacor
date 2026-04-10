from girder.asgi import _WSGIBridge
from girder.notification import UserNotificationsSocket
from girder.wsgi import app as wsgi_app
from girder_async_routes import async_file_routes
from girder_async_routes.asgi import lifespan
from starlette.applications import Starlette
from starlette.routing import Mount, WebSocketRoute

from .logs import DockerLogStreamer


app = Starlette(
    lifespan=lifespan,
    routes=[
        WebSocketRoute("/notifications/me", UserNotificationsSocket),
        WebSocketRoute("/logs/docker", DockerLogStreamer),
        *async_file_routes,
        Mount("/", app=_WSGIBridge(wsgi_app)),
    ],
)
