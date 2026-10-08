"""The TRS identity and run timestamps that reach a signed TRO declaration.

Two things a declaration has to get right, neither of which the submission
tests would notice going wrong, because a wrong-but-well-formed declaration
still signs and still uploads:

- **Who the TRS is.** ``@id`` identifies it in every declaration that mentions
  it, so tro-utils >=0.5.0 requires an absolute IRI (or a compact IRI whose
  prefix is not ``trov``) and refuses to save anything else. 0.4.x dropped a
  profile's ``@id`` and wrote a bare ``"trs"``, a reference that resolves only
  inside the one document holding it.
- **When the run happened.** The run is recorded on a worker and read back by
  whichever host runs the TRO step. An offset-less timestamp is read as local
  time, so a naive value recorded on one host and parsed on another lands in
  the declaration shifted by the difference between their zones.
"""

import datetime

import pytest
from girder.exceptions import ValidationException
from girder.models.setting import Setting
from girder.settings import SettingDefault
from girder_sivacor.settings import PluginSettings
from girder_sivacor.worker_plugin.run_submission import _run_instant
from girder_sivacor import is_conforming_capability_id
from tro_utils.models.trs import (
    UNIDENTIFIED_TRS_ID,
    TrustedResearchSystem,
    is_conforming_trs_id,
)

#: Identifiers an admin might plausibly type that cannot identify a TRS.
BAD_IDS = [
    "trs",  # the 0.4.x convention
    "trs/capability/1",
    "sivacor",
    "",
    "trov:sivacor",  # the vocabulary's own prefix
    "//sivacor.org/",  # no scheme
]


def _default_profile():
    return SettingDefault.defaults[PluginSettings.TRO_PROFILE]


class TestDefaultProfile:
    """The shipped profile must identify the TRS without relying on a fallback."""

    def test_default_profile_states_an_id(self):
        """Stated outright, not derived.

        tro-utils would reach the same string through schema:url, so this
        guards the identifier being ours rather than a side effect of that
        fallback's ordering.
        """
        assert _default_profile()["@id"] == "https://sivacor.org/"

    def test_default_profile_id_conforms(self):
        assert is_conforming_trs_id(_default_profile()["@id"])

    def test_default_profile_resolves_to_its_own_id(self):
        trs = TrustedResearchSystem.from_profile(_default_profile())
        assert trs.trs_id == "https://sivacor.org/"

    def test_profile_describes_the_trs_in_schema_org_terms(self):
        """The TRS is a schema:Organization, so schema: properties describe it.

        trov:owner, trov:description, trov:contact, trov:url and trov:name --
        all used here previously -- are not terms TROV defines for a TRS.
        """
        profile = _default_profile()
        assert profile["schema:name"] == "sivacor"
        assert profile["schema:url"] == "https://sivacor.org/"
        assert profile["schema:email"] == "admin@sivacor.org"
        assert profile["schema:owner"]["@type"] == "schema:Organization"
        assert profile["schema:owner"]["schema:name"] == "SIVACOR Team"
        assert "SIVACOR" in profile["schema:description"]
        # trov: is still right for the vocabulary's own terms, and only those.
        assert sorted(k for k in profile if k.startswith("trov:")) == [
            "trov:hasCapability"
        ]

    def test_schema_properties_reach_the_declaration(self):
        """Everything not typed by the model rides along verbatim."""
        trs = TrustedResearchSystem.from_profile(_default_profile())
        node = trs.to_jsonld()
        assert node["schema:name"] == "sivacor"
        assert node["schema:url"] == "https://sivacor.org/"
        assert node["schema:email"] == "admin@sivacor.org"
        assert node["schema:owner"]["schema:name"] == "SIVACOR Team"
        assert "schema:Organization" in node["@type"]

    def test_default_profile_declaration_can_be_saved(self, tmp_path):
        """A non-conforming TRS @id makes tro-utils refuse to save at all."""
        from tro_utils.models import TransparentResearchObject

        tro = TransparentResearchObject(
            trs=TrustedResearchSystem.from_profile(_default_profile())
        )
        out = tmp_path / "tro.jsonld"
        tro.save(out)  # raises ValueError if the @id does not conform
        assert out.exists()


class TestCapabilityIdentity:
    """Capabilities are named by their own term, not a relative id.

    trov:warrantedBy on every performance attribute points at the capability
    that justifies it. A relative id like "trs/capability/1" resolves against
    whichever document contains it, so an external validator rejects it --
    and because the warrant copies the profile's @id, one bad entry repeats
    through the whole declaration.
    """

    def test_profile_capabilities_use_their_own_term(self):
        capabilities = _default_profile()["trov:hasCapability"]
        assert capabilities, "the profile should declare capabilities"
        for capability in capabilities:
            assert capability["@id"] == capability["@type"]
            assert is_conforming_capability_id(capability["@id"])

    def test_warrants_conform(self):
        """What the external check actually inspects."""
        trs = TrustedResearchSystem.from_profile(_default_profile())
        for capability in trs.to_jsonld()["trov:hasCapability"]:
            assert is_conforming_capability_id(capability["@id"])

    @pytest.mark.plugin("sivacor")
    def test_validator_rejects_a_relative_capability_id(self, server):
        profile = dict(_default_profile())
        profile["trov:hasCapability"] = [
            {"@id": "trs/capability/1", "@type": "trov:CanIsolateEnvironment"}
        ]
        with pytest.raises(ValidationException, match="compact or absolute"):
            Setting().set(PluginSettings.TRO_PROFILE, profile)

    @pytest.mark.plugin("sivacor")
    def test_validator_rejects_a_capability_without_a_type(self, server):
        profile = dict(_default_profile())
        profile["trov:hasCapability"] = [{"@id": "trov:CanIsolateEnvironment"}]
        with pytest.raises(ValidationException, match="@type"):
            Setting().set(PluginSettings.TRO_PROFILE, profile)

    @pytest.mark.plugin("sivacor")
    def test_validator_accepts_the_shipped_capabilities(self, server):
        assert Setting().set(PluginSettings.TRO_PROFILE, _default_profile())


class TestProfileValidator:
    """A bad @id must be caught while editing the setting.

    Not hygiene: the alternative is a submission that fails at its first TRO
    step, several stages in, after the workers have already done the work.
    """

    @pytest.mark.plugin("sivacor")
    @pytest.mark.parametrize("bad", BAD_IDS)
    def test_validator_rejects_nonconforming_id(self, server, bad):
        profile = dict(_default_profile(), **{"@id": bad})
        with pytest.raises(ValidationException, match="absolute IRI"):
            Setting().set(PluginSettings.TRO_PROFILE, profile)

    @pytest.mark.plugin("sivacor")
    @pytest.mark.parametrize(
        "good",
        [
            "https://sivacor.org/",
            "https://aea.sivacor.org/trs/1",
            "urn:uuid:6fa459ea-ee8a-3ca4-894e-db77e160355e",
            "ex:trs",
        ],
    )
    def test_validator_accepts_conforming_id(self, server, good):
        profile = dict(_default_profile(), **{"@id": good})
        assert Setting().set(PluginSettings.TRO_PROFILE, profile)["value"]["@id"] == good

    @pytest.mark.plugin("sivacor")
    def test_validator_allows_an_absent_id(self, server):
        """The validator rejects a wrong identifier, not a missing one.

        With no @id, tro-utils derives one from schema:url -- which for this
        profile is the same IRI the @id states.
        """
        profile = {k: v for k, v in _default_profile().items() if k != "@id"}
        Setting().set(PluginSettings.TRO_PROFILE, profile)
        assert (
            TrustedResearchSystem.from_profile(profile).trs_id == "https://sivacor.org/"
        )

    @pytest.mark.plugin("sivacor")
    def test_profile_without_id_or_url_is_unidentified(self, server):
        """Nothing to derive from, so the TRS is recorded as unidentified.

        Still allowed -- a missing identifier is not a wrong one -- but it is
        why the shipped profile carries both.
        """
        profile = {
            k: v
            for k, v in _default_profile().items()
            if k not in ("@id", "schema:url")
        }
        Setting().set(PluginSettings.TRO_PROFILE, profile)
        assert TrustedResearchSystem.from_profile(profile).trs_id == UNIDENTIFIED_TRS_ID

    @pytest.mark.plugin("sivacor")
    def test_validator_still_rejects_a_non_dict(self, server):
        with pytest.raises(ValidationException, match="dictionary"):
            Setting().set(PluginSettings.TRO_PROFILE, "not a profile")


class TestRunInstant:
    """Stored run timestamps must mean the same instant on every host."""

    def test_offset_is_preserved(self):
        parsed = _run_instant("2026-07-31T10:00:00+02:00")
        assert parsed.utcoffset() == datetime.timedelta(hours=2)
        assert parsed == datetime.datetime(
            2026, 7, 31, 8, 0, tzinfo=datetime.timezone.utc
        )

    def test_naive_is_read_as_utc(self):
        """A run recorded before timestamps carried an offset.

        Pinned to UTC rather than left naive, because tro-utils would otherwise
        read it as the TRO step host's local time -- which is not the host that
        recorded the run.
        """
        parsed = _run_instant("2026-07-31T10:00:00")
        assert parsed.tzinfo is not None
        assert parsed == datetime.datetime(
            2026, 7, 31, 10, 0, tzinfo=datetime.timezone.utc
        )

    def test_utc_input_round_trips(self):
        assert _run_instant("2026-07-31T10:00:00+00:00") == datetime.datetime(
            2026, 7, 31, 10, 0, tzinfo=datetime.timezone.utc
        )

    def test_result_is_always_aware(self):
        for value in (
            "2026-07-31T10:00:00",
            "2026-07-31T10:00:00+00:00",
            "2026-07-31T10:00:00-05:00",
            "2026-07-31T10:00:00.123456",
        ):
            assert _run_instant(value).tzinfo is not None


class TestRecordedRunTimestamps:
    """The timestamps the workflow tasks record carry an offset."""

    def test_execute_workflow_records_aware_timestamps(self):
        """Guards the `datetime.datetime.now()` that used to be here.

        A bare now() is naive, and nothing downstream would complain -- the
        declaration would just be wrong by the host's UTC offset.
        """
        import inspect

        from girder_sivacor.worker_plugin import run_submission

        for task in (run_submission.execute_workflow, run_submission.prune_workspace):
            source = inspect.getsource(task)
            assert "datetime.datetime.now()" not in source, (
                f"{task.__name__} records a naive timestamp; it is serialised "
                "into submission['runs'] and parsed on another host"
            )
