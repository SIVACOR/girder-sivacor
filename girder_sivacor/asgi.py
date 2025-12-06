import logging

from starlette.middleware.wsgi import WSGIMiddleware
from starlette.routing import Mount, WebSocketRoute
from starlette.applications import Starlette

from girder.wsgi import app as wsgi_app
from girder.notification import UserNotificationsSocket
from .logs import DockerLogStreamer

app = Starlette(
    routes=[
        WebSocketRoute("/notifications/me", UserNotificationsSocket),
        WebSocketRoute("/logs/docker", DockerLogStreamer),
        Mount("/", app=WSGIMiddleware(wsgi_app)),
    ]
)


@app.on_event("startup")
async def startup_event():
    logger = logging.getLogger(__name__)
    logger.info("Girder server running")
