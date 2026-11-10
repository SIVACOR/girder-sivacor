"""Who this deployment says it is in every TRO it signs (issue #153).

``sivacor.tro_profile`` defaults to SIVACOR's own profile. A deployment that
never sets it signs TROs, with its own GPG key, that name SIVACOR as the
Trusted Research System -- a misstatement of who certified the run, made in an
artifact designed to be relied on later. So the profile is editable on the
plugin's config page, and a deployment still on the built-in one is told so,
there and in the log at startup.

Defaults are not stored, so "still on the built-in profile" is detectable: no
setting document, or one holding exactly the default (a seed that copied it).
"""

import copy
import logging
from urllib.parse import urlparse

from girder.models.setting import Setting
from girder.settings import SettingDefault, SettingKey

from .settings import REQUIRED_CAPABILITIES, PluginSettings

logger = logging.getLogger(__name__)

#: The deployment the built-in profile describes.
BUILTIN_DOMAIN = "sivacor.org"


def builtin():
    return copy.deepcopy(SettingDefault.defaults[PluginSettings.TRO_PROFILE])


def is_builtin():
    doc = Setting().findOne({"key": PluginSettings.TRO_PROFILE})
    return doc is None or doc.get("value") == builtin()


def _server_host():
    root = Setting().get(SettingKey.SERVER_ROOT) or ""
    return urlparse(root).hostname


def warning():
    """Why this deployment's TROs may misstate their signer, or ``None``.

    ``None`` when the profile has been set, or when the built-in one is in use
    on SIVACOR's own domain. Without a server root the domain is unknown, and
    that is warned about too: a quiet answer there would be a guess.
    """
    if not is_builtin():
        return None
    host = _server_host()
    if host and (host == BUILTIN_DOMAIN or host.endswith("." + BUILTIN_DOMAIN)):
        return None
    where = (
        f"this deployment ({host}) is not {BUILTIN_DOMAIN}"
        if host
        else f"if this deployment is not {BUILTIN_DOMAIN}"
    )
    return (
        "This deployment signs TROs with the built-in SIVACOR TRS profile, so "
        "every declaration it signs names SIVACOR as the Trusted Research "
        f"System. That misstates who certified the run {where}. Set this "
        "deployment's own profile on the SIVACOR plugin's config page."
    )


def warn_if_builtin():
    message = warning()
    if message:
        logger.warning(message)


def status():
    """The profile in force and what the config page needs to edit it."""
    return {
        "profile": copy.deepcopy(Setting().get(PluginSettings.TRO_PROFILE)),
        "builtin": is_builtin(),
        "warning": warning(),
        "required_capabilities": [cap.value for cap in REQUIRED_CAPABILITIES],
    }


def set_profile(profile):
    Setting().set(PluginSettings.TRO_PROFILE, profile)
