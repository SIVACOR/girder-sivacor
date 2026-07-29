"""Girder access for remote celery workers.

SIVACOR tasks used to reach into ``girder.models`` directly, which pinned every
worker to the server's MongoDB and assetstore. Everything in here goes through
the REST API instead, so a worker only needs to be able to talk HTTP to Girder.

The API URL and the token ride along in the celery message headers that
``girder_worker`` attaches to every task (see
``girder_worker.app.girder_before_task_publish``); :func:`SIVACOR.submit_job`
seeds them with an admin-scoped token so the worker can act on the submission
collection.
"""

import io
import json
import logging
import os

from girder_client import GirderClient, HttpError

logger = logging.getLogger(__name__)

#: Read/write granularity when shuffling file contents around. Girder used to
#: hand us ``SettingKey.FILEHANDLE_MAX_SIZE`` for this; a remote worker has no
#: business asking the server how big its own buffers should be.
CHUNK_SIZE = 8 * 1024 * 1024


def _request_value(task, name):
    """Read a ``girder_worker`` header off a celery task request.

    Real workers get message headers promoted onto ``task.request`` by celery.
    Eager tasks (i.e. the test suite) never go through the publish path, so
    there the headers are only reachable as a plain dict.
    """
    value = getattr(task.request, name, None)
    if value is None:
        value = (getattr(task.request, "headers", None) or {}).get(name)
    return value


class GirderApi:
    """The subset of Girder that the submission pipeline actually needs."""

    def __init__(self, api_url, token=None, api_key=None):
        self.client = GirderClient(apiUrl=api_url)
        if token:
            self.client.setToken(token)
        elif api_key:
            self.client.authenticate(apiKey=api_key)
        else:
            raise ValueError("No Girder token or API key available for this worker.")

    @classmethod
    def for_task(cls, task):
        """Build a client from the celery headers attached to ``task``."""
        api_url = _request_value(task, "girder_api_url") or os.environ.get(
            "GIRDER_API_URL"
        )
        if not api_url:
            raise RuntimeError(
                "No Girder API URL available; the task was published without a "
                "girder_api_url header and GIRDER_API_URL is unset."
            )
        return cls(
            api_url,
            token=_request_value(task, "girder_client_token"),
            api_key=os.environ.get("GIRDER_API_KEY"),
        )

    # -- settings ---------------------------------------------------------

    def settings(self, keys):
        """Fetch several system settings in one round trip."""
        return self.client.get("system/setting", parameters={"list": json.dumps(keys)})

    # -- jobs -------------------------------------------------------------

    def job(self, job_id, include_log=False):
        return self.client.get(
            f"job/{job_id}", parameters={"includeLog": include_log}
        )

    def update_job(self, job_id, log=None, status=None):
        params = {}
        if log is not None:
            params["log"] = log
        if status is not None:
            params["status"] = status
        return self.client.put(f"job/{job_id}", parameters=params)

    def heartbeat(self, job_id):
        """Tell the server this submission's worker is still alive.

        Best effort on purpose: a submission that is running fine should not be
        killed off because one heartbeat request lost a race with a Traefik
        restart. Missing several in a row is what the reaper acts on.
        """
        try:
            self.client.post(f"sivacor/heartbeat/{job_id}")
            return True
        except Exception:
            logger.warning("Heartbeat failed for job %s", job_id, exc_info=True)
            return False

    # -- collections, groups and access -----------------------------------

    def _find_collection(self, name):
        # The collection endpoint only offers a text search, which needs a text
        # index and matches loosely, so filter by exact name ourselves. There
        # are only ever a handful of collections.
        for collection in self.client.listCollection():
            if collection["name"] == name:
                return collection
        return None

    def find_or_create_collection(self, name):
        if collection := self._find_collection(name):
            return collection
        return self._create_unique(
            lambda: self.client.createCollection(name, public=True),
            lambda: self._find_collection(name),
        )

    def _find_group(self, name):
        groups = list(
            self.client.listResource("group", {"text": name, "exact": True}, limit=1)
        )
        return groups[0] if groups else None

    def find_or_create_group(self, name):
        if group := self._find_group(name):
            return group
        return self._create_unique(
            lambda: self.client.post(
                "group", parameters={"name": name, "public": True}
            ),
            lambda: self._find_group(name),
        )

    @staticmethod
    def _create_unique(create, find):
        """Create a uniquely-named resource, tolerating a concurrent creator.

        Two submissions starting at once both look, both find nothing, and both
        create; Girder rejects the second with a validation error. Whichever
        one landed first is the answer either way. The model-level calls this
        replaced took ``reuseExisting=True`` and had no such window.
        """
        try:
            return create()
        except HttpError as exc:
            if exc.status != 400:
                raise
            if existing := find():
                return existing
            raise

    def grant_group_access(self, collection_id, group_id, level):
        """Add a group to a collection's ACL, leaving existing entries alone."""
        access = self.client.get(f"collection/{collection_id}/access")
        groups = access.setdefault("groups", [])
        if any(str(entry["id"]) == str(group_id) for entry in groups):
            return
        groups.append({"id": str(group_id), "level": level})
        self.client.put(
            f"collection/{collection_id}/access",
            parameters={"access": json.dumps(access)},
        )

    def grant_user_access(self, folder_id, user_id, level):
        access = self.client.get(f"folder/{folder_id}/access")
        users = access.setdefault("users", [])
        for entry in users:
            if str(entry["id"]) == str(user_id):
                return
        users.append({"id": str(user_id), "level": level})
        self.client.put(
            f"folder/{folder_id}/access", parameters={"access": json.dumps(access)}
        )

    # -- folders and items -------------------------------------------------

    def folder(self, folder_id):
        return self.client.getFolder(folder_id)

    def create_folder(self, parent_id, name, parent_type="collection", public=False):
        return self.client.createFolder(
            parent_id, name, parentType=parent_type, public=public
        )

    def set_folder_metadata(self, folder_id, metadata):
        return self.client.addMetadataToFolder(folder_id, metadata)

    def item(self, item_id):
        return self.client.getItem(item_id)

    def move_item(self, item_id, folder_id):
        return self.client.put(
            f"item/{item_id}", parameters={"folderId": str(folder_id)}
        )

    def find_child_item(self, folder_id, name):
        items = list(self.client.listItem(folder_id, name=name, limit=1))
        return items[0] if items else None

    def item_files(self, item_id):
        return list(self.client.listFile(item_id))

    def annotate_item_type(self, file_obj, item_type):
        """Tag the item owning ``file_obj`` so editor tooling can find it."""
        item = self.item(file_obj["itemId"])
        if "type" not in item.get("meta", {}):
            self.client.addMetadataToItem(item["_id"], {"type": item_type})

    # -- files -------------------------------------------------------------

    def file(self, file_id):
        if not file_id:
            return None
        try:
            return self.client.getFile(file_id)
        except HttpError as exc:
            if exc.status == 404:
                return None
            raise

    def download_file(self, file_id, dest):
        """Download to a path or an open file-like object."""
        self.client.downloadFile(file_id, dest)

    def file_chunks(self, file_id):
        return self.client.downloadFileAsIterator(file_id, chunkSize=CHUNK_SIZE)

    def upload_file(self, folder_id, path, name=None, mime_type=None, item_type=None):
        file_obj = self.client.uploadFileToFolder(
            folder_id, path, filename=name, mimeType=mime_type
        )
        if item_type:
            self.annotate_item_type(file_obj, item_type)
        return file_obj

    def upload_bytes(self, folder_id, data, name, mime_type=None, item_type=None):
        file_obj = self.client.uploadStreamToFolder(
            folder_id, io.BytesIO(data), name, len(data), mimeType=mime_type
        )
        if item_type:
            self.annotate_item_type(file_obj, item_type)
        return file_obj

    def replace_file(self, file_id, path):
        """Overwrite an existing file's contents from a local path."""
        size = os.path.getsize(path)
        with open(path, "rb") as fp:
            return self.client.uploadFileContents(file_id, fp, size)


def dump_to_zip(chunks, zipf, arcname):
    """Stream an iterator of byte chunks into a zip archive member."""
    with zipf.open(arcname, "w") as dest:
        for chunk in chunks:
            dest.write(chunk)
