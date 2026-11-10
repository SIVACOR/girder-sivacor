"""Who this deployment says it is in every TRO it signs (issue #153).

``sivacor.tro_profile`` defaults to SIVACOR's own profile. A deployment that
never sets it signs TROs, with its own GPG key, that name SIVACOR as the
Trusted Research System -- a misstatement of who certified the run, made in an
artifact designed to be relied on later. So the profile is editable on the
plugin's config page, and a deployment whose profile names SIVACOR is told so,
there and in the log at startup.

"Names SIVACOR" is read from what lands in the declaration -- the identifier's
host -- rather than by comparing against today's default. Copies of older
defaults are stored on real deployments (production's own among them), and
they are the ones most likely to be signing as SIVACOR unnoticed.
"""

import logging
from urllib.parse import urlparse

from girder.models.setting import Setting
from girder.settings import SettingKey
from tro_utils import TROVCapability

from .settings import CLAIMED_ON_REQUEST, REQUIRED_CAPABILITIES, PluginSettings

logger = logging.getLogger(__name__)

#: The deployment the built-in profile describes.
BUILTIN_DOMAIN = "sivacor.org"

#: Where a profile identifies its TRS, in the order tro-utils prefers them.
#: ``trov:url`` is the pre-schema.org spelling, still in older stored copies.
_IDENTIFYING_KEYS = ("@id", "schema:url", "trov:url")


def _host(iri):
    if not isinstance(iri, str):
        return None
    return urlparse(iri).hostname


def _on_sivacor(host):
    return bool(host) and (host == BUILTIN_DOMAIN or host.endswith("." + BUILTIN_DOMAIN))


def identified_host(profile):
    """The host of the first identifier in ``profile`` that has one."""
    for key in _IDENTIFYING_KEYS:
        if host := _host(profile.get(key)):
            return host
    return None


def names_sivacor(profile):
    return _on_sivacor(identified_host(profile))


def _declared(profile):
    capabilities = profile.get("trov:hasCapability", [])
    if not isinstance(capabilities, list):
        return set()
    return {cap.get("@type") for cap in capabilities if isinstance(cap, dict)}


def missing_capabilities(profile):
    """Capabilities every run claims that ``profile`` does not declare."""
    declared = _declared(profile)
    return [cap.value for cap in REQUIRED_CAPABILITIES if cap.value not in declared]


def undeclared_requests(profile):
    """``{stage field: capability}`` for each opt-in claim ``profile`` cannot back."""
    declared = _declared(profile)
    undeclared = {}
    for field, attr in CLAIMED_ON_REQUEST.items():
        capability = TROVCapability.translate(attr).value
        if capability not in declared:
            undeclared[field] = capability
    return undeclared


def warnings():
    """What is wrong with the stored profile, as sentences for an operator.

    A profile naming SIVACOR is fine on SIVACOR's own domain. Without a server
    root the domain is unknown, and that is warned about too: a quiet answer
    there would be a guess.
    """
    profile = Setting().get(PluginSettings.TRO_PROFILE) or {}
    found = []
    server_host = _host(Setting().get(SettingKey.SERVER_ROOT))
    if names_sivacor(profile) and not _on_sivacor(server_host):
        where = (
            f"this deployment ({server_host}) is not {BUILTIN_DOMAIN}"
            if server_host
            else f"if this deployment is not {BUILTIN_DOMAIN}"
        )
        found.append(
            f"The TRS profile identifies the system as {identified_host(profile)}, "
            "so every TRO this deployment signs names SIVACOR as the Trusted "
            f"Research System. That misstates who certified the run {where}. "
            "Set this deployment's own profile on the SIVACOR plugin's config page."
        )
    if missing := missing_capabilities(profile):
        found.append(
            f"The TRS profile does not declare {', '.join(missing)}, which every "
            "run claims, so every submission will fail when its run is recorded, "
            "after its analysis has run."
        )
    return found


def warn_if_misconfigured():
    for message in warnings():
        logger.warning(message)


def status():
    """The profile in force and what the config page needs to edit it."""
    return {
        "profile": Setting().get(PluginSettings.TRO_PROFILE),
        "warnings": warnings(),
        "required_capabilities": [cap.value for cap in REQUIRED_CAPABILITIES],
    }


def set_profile(profile):
    Setting().set(PluginSettings.TRO_PROFILE, profile)
