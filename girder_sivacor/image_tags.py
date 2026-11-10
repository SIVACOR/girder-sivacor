"""The analysis-image allow-list, and where it comes from (issue #151).

``sivacor.image_tags`` is the list. ``sivacor.image_tags_url``, when set, names
a YAML file that replaces it, refreshed every :data:`REFRESH`; the setting is
then only the fallback for a fetch that fails.

One shape is enforced everywhere a list can come from -- the setting's
validator and the fetched file alike -- because every consumer does ``tag in
tags[image]``, and the ways a list can be wrong are not cosmetic: a bare string
for a list allow-lists every substring of it, and an empty document makes that
expression raise inside ``submit_job``.
"""

import datetime
import json
import logging
import os
import tempfile
from urllib.parse import urlparse

import requests
import yaml
from girder.exceptions import ValidationException
from girder.models.setting import Setting

from .settings import PluginSettings

logger = logging.getLogger(__name__)

#: Cache of the most recent fetch from ``sivacor.image_tags_url``, shared by
#: every gunicorn worker on the host.
CACHE = "/tmp/sivacor_image_tags.json"

#: How long a successful fetch is served before the URL is read again.
REFRESH = datetime.timedelta(hours=4)

#: How long a *failed* fetch is remembered before it is retried. Short, because
#: until then the deployment serves its fallback list; not zero, because
#: ``submit_job`` reads the allow-list and an unreachable URL would otherwise
#: add a 10 s timeout to every submission.
RETRY = datetime.timedelta(minutes=5)

#: Cache documents carry this, so a cache written by an older release -- a bare
#: tag dict, or one with no ``checked_at`` -- is treated as absent.
CACHE_VERSION = 2


def check_image_tags(value):
    """Raise ``ValidationException`` unless ``value`` is a usable allow-list."""
    if not isinstance(value, dict):
        raise ValidationException(
            "The image list must be a mapping of image name to a list of tags."
        )
    if not value:
        raise ValidationException("The image list must name at least one image.")
    for image, tags in value.items():
        if not isinstance(image, str) or not image.strip():
            raise ValidationException("Image names must be non-empty strings.")
        if not isinstance(tags, list) or not tags or not all(
            isinstance(tag, str) and tag.strip() for tag in tags
        ):
            raise ValidationException(
                f"Tags for {image} must be a non-empty list of non-empty strings."
            )


def check_url(value):
    """The stripped URL, or ``""`` for "never fetch". Raises if unusable."""
    if not isinstance(value, str):
        raise ValidationException("Image tags URL must be a string.")
    value = value.strip()
    if value:
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValidationException(
                "Image tags URL must be an http(s) URL with a host, or empty to "
                "never fetch."
            )
    return value


def fetch(url):
    """The allow-list at ``url``, checked. Raises on any problem.

    Parsed with ``BaseLoader``, which reads every scalar as a string: a safe
    load turns an unquoted ``2026-08-12`` into a ``date`` (which the JSON cache
    cannot hold) and an unquoted ``4.10`` into the float ``4.1`` -- silently
    allow-listing a tag the file never named. Tags are always text.
    """
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    tags = yaml.load(response.text, Loader=yaml.BaseLoader)
    check_image_tags(tags)
    return tags


def _read_cache(url):
    try:
        with open(CACHE) as f:
            cached = json.load(f)
    except (OSError, ValueError):
        return None
    if (
        not isinstance(cached, dict)
        or cached.get("version") != CACHE_VERSION
        or cached.get("source_url") != url
    ):
        # Changing the setting takes effect now, not up to REFRESH later.
        return None
    try:
        checked = datetime.datetime.fromisoformat(cached["checked_at"])
    except (KeyError, TypeError, ValueError):
        return None
    ttl = REFRESH if cached.get("tags") is not None else RETRY
    if datetime.datetime.now(datetime.UTC) - checked > ttl:
        return None
    return cached


def _write_cache(record):
    """Atomically, and never fatally: the cache is an optimisation."""
    directory = os.path.dirname(CACHE) or "."
    try:
        # A unique name per writer: two threads of one process refreshing at
        # once would otherwise share a pid-derived name and clobber each other.
        with tempfile.NamedTemporaryFile(
            "w", dir=directory, prefix=".sivacor_image_tags.", delete=False
        ) as f:
            json.dump(record, f)
        os.replace(f.name, CACHE)
    except OSError:
        logger.warning("Could not write the image tags cache", exc_info=True)
        try:
            os.unlink(f.name)
        except (NameError, OSError):
            pass


def status():
    """What the allow-list is right now, and where it came from.

    ``source`` is ``"setting"`` (no URL configured), ``"url"`` (fetched) or
    ``"fallback"`` (a URL is configured but its last fetch failed; ``error``
    says why). In the last case the list is read from the setting on every
    call, so an edit to the fallback applies at once.
    """
    url = Setting().get(PluginSettings.IMAGE_TAGS_URL)
    if not url:
        return {
            "source": "setting",
            "url": None,
            "checked_at": None,
            "error": None,
            "tags": Setting().get(PluginSettings.IMAGE_TAGS),
        }

    cached = _read_cache(url)
    if cached is None:
        cached = {
            "version": CACHE_VERSION,
            "source_url": url,
            "checked_at": datetime.datetime.now(datetime.UTC).isoformat(),
            "tags": None,
            "error": None,
        }
        try:
            cached["tags"] = fetch(url)
        except Exception as exc:
            # Kept short and free of the response body: it is shown to admins
            # and logged, and a hostile URL controls the body.
            cached["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
            logger.warning("Fetching image tags from %s failed: %s", url, exc)
        _write_cache(cached)

    if cached["tags"] is None:
        return {
            "source": "fallback",
            "url": url,
            "checked_at": cached["checked_at"],
            "error": cached["error"],
            "tags": Setting().get(PluginSettings.IMAGE_TAGS),
        }
    return {
        "source": "url",
        "url": url,
        "checked_at": cached["checked_at"],
        "error": None,
        "tags": cached["tags"],
    }


def get_tags():
    return status()["tags"]


def set_config(url, tags):
    """Save both settings, or neither.

    ``PUT /system/setting`` with a list saves its entries one at a time, so a
    refused second entry leaves the first saved -- the URL live while the page
    reports a failure. Validating both before writing either closes that.
    """
    docs = [
        {"key": PluginSettings.IMAGE_TAGS_URL, "value": url},
        {"key": PluginSettings.IMAGE_TAGS, "value": tags},
    ]
    for doc in docs:
        Setting().validate(doc)
    for doc in docs:
        Setting().set(doc["key"], doc["value"])
