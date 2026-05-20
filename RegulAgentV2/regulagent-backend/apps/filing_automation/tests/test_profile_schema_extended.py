"""
TDD (red phase) — Step 2 / A4: new optional profile keys + adapter wiring.

Tests 1-3: schema validation for the three new optional keys.
Tests 4-7: adapter emits / omits new keys in calculated_data.

All 7 tests MUST FAIL until Backend Engineer 2 implements:
  1. profile_schema.py — add the 3 new keys to RRC_W3A_OPTIONAL
  2. adapter.py — read the new keys and populate calculated_data

New optional keys (all str, all optional):
  - rrc.w3a.cementing_company_address
  - rrc.w3a.cementing_company_p5
  - rrc.w3a.contact_ext  (may be blank)

Design decisions (for Backend Engineer):
  - "optional" means: absent from profile → no BusinessProfileIncomplete raised.
  - Adapter pattern for missing optional keys: OMIT from calculated_data
    (do NOT store empty string).  Rationale: the filler reads these via
    .get() with a default of "" so absence and "" are equivalent at call
    time, but omitting avoids polluting the dict with sentinel strings and
    makes "key present?" a reliable signal in tests 10-11 (EXT skip).
    If the Backend Engineer prefers empty-string sentinel, update tests
    7, 10, 11 accordingly and document the decision here.
"""
from __future__ import annotations

import datetime as _dt
from types import SimpleNamespace

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_REQUIRED_PROFILE_DATA = {
    "cementing_company_name": "Acme Cementing Inc.",
    "contact_phone": "555-0100",
    "contact_email": "ops@example.com",
    "submitter_default_name": "Jane Doe",
    "submitter_default_title": "Operations Manager",
}

_NEW_OPTIONAL_DATA = {
    "cementing_company_address": "PO Box 13077, Odessa, TX 79768",
    "cementing_company_p5": "040196",
    "contact_ext": "555",
}

_FULL_W3A_DATA = {**_REQUIRED_PROFILE_DATA, **_NEW_OPTIONAL_DATA}


def _make_profile(w3a_data: dict) -> SimpleNamespace:
    """Return a SimpleNamespace(data=...) matching adapter's _profile_get shape."""
    return SimpleNamespace(data={"rrc": {"w3a": dict(w3a_data)}})


def _make_snap(*, api14="42501705750001") -> SimpleNamespace:
    """Minimal PlanSnapshot stand-in with a casing record (avoids PayloadIncomplete)."""
    return SimpleNamespace(
        id="snap-ext-1",
        plan_id="plan-ext-001",
        kind="post_edit",
        status="engineer_approved",
        tenant_id="tenant-ext-1",
        well=SimpleNamespace(
            api14=api14,
            operator_name="Ext Operator LLC",
            lease_name="Ext Lease #1",
            field_name="Spraberry",
            permit_number="PERMIT-EXT",
            district="7C",
        ),
        payload={
            "jurisdiction": "TX",
            "form": "W-3A",
            "district": "7C",
            "inputs_summary": {"api14": api14},
            "steps": [],
            "geometry": {
                "formation_tops": [],
                "mechanical_barriers": [],
                "casing_record": [
                    {
                        "grade": "L-80",
                        "weight_ppf": 29,
                        "top_ft": 0,
                        "bottom_ft": 7000,
                        "role": "production",
                        "size_in": 7.0,
                    }
                ],
            },
        },
    )


_ATTESTATION = {
    "submitter_name": "Sally Submitter",
    "submitter_title": "Compliance Lead",
    "certification_checked": True,
}


# ===========================================================================
# Schema tests (1-3)
# ===========================================================================

class TestSchemaValidationNewOptionalKeys:
    """profile_schema.py must accept the 3 new optional keys."""

    # -----------------------------------------------------------------------
    # Test 1 — full profile with all new optional keys validates
    # -----------------------------------------------------------------------
    def test_profile_with_new_optional_keys_validates(self):
        """A profile carrying all 3 new keys must pass schema validation without error.

        FAILS until RRC_W3A_OPTIONAL in profile_schema.py lists the three new keys.
        Even without explicit validation logic, a call to get_schema must return
        a schema whose optional list includes the new key names — this test
        inspects that list directly.
        """
        from apps.filing_automation.services.profile_schema import (
            get_schema,
            RRC_W3A_OPTIONAL,
        )

        schema = get_schema("rrc", "w3a")
        assert schema is not None, "get_schema('rrc', 'w3a') must return a dict"

        # All three new optional keys must be declared in RRC_W3A_OPTIONAL.
        for key in (
            "rrc.w3a.cementing_company_address",
            "rrc.w3a.cementing_company_p5",
            "rrc.w3a.contact_ext",
        ):
            assert key in RRC_W3A_OPTIONAL, (
                f"New optional key {key!r} must be listed in RRC_W3A_OPTIONAL. "
                f"Current list: {RRC_W3A_OPTIONAL}"
            )

    # -----------------------------------------------------------------------
    # Test 2 — profile WITHOUT new optional keys still validates
    # -----------------------------------------------------------------------
    def test_profile_without_new_optional_keys_still_validates(self):
        """A profile with only the 5 required keys must not raise, AND the
        schema must already declare all 3 new keys as optional (so we know
        the implementer added them before marking this green).

        FAILS until RRC_W3A_OPTIONAL contains all 3 new keys (same gate as
        test 1, but from the "old profile still works" angle — both red until
        schema.py is updated).
        """
        from apps.filing_automation.services.adapter import plan_snapshot_to_form_data
        from apps.filing_automation.services.profile_schema import (
            BusinessProfileIncomplete,
            RRC_W3A_OPTIONAL,
        )

        # Gate 1: all three new optional keys must be declared in the schema.
        for key in (
            "rrc.w3a.cementing_company_address",
            "rrc.w3a.cementing_company_p5",
            "rrc.w3a.contact_ext",
        ):
            assert key in RRC_W3A_OPTIONAL, (
                f"New optional key {key!r} must be in RRC_W3A_OPTIONAL before this "
                f"test can be considered green. Current: {RRC_W3A_OPTIONAL}"
            )

        # Gate 2: profile with only required keys must not raise.
        profile = _make_profile(_REQUIRED_PROFILE_DATA)
        snap = _make_snap()

        try:
            plan_snapshot_to_form_data(snap, _ATTESTATION, profile)
        except BusinessProfileIncomplete as exc:
            pytest.fail(
                f"Profile with only required keys must not raise BusinessProfileIncomplete. "
                f"Got: {exc!r}"
            )

    # -----------------------------------------------------------------------
    # Test 3 — cementing_company_p5 must be a string; int must fail validation
    # -----------------------------------------------------------------------
    def test_profile_with_invalid_p5_type_fails(self):
        """Passing cementing_company_p5 as int (12345) must trigger a validation error.

        FAILS until profile_schema.py (or adapter.py) validates the type of
        cementing_company_p5.  The error must be a TypeError, ValueError, or
        BusinessProfileIncomplete — any of those is acceptable.

        Pattern: whichever module is responsible for type checking must NOT
        silently coerce 12345 to "12345" — a wrong type must raise loudly so
        mis-configured profiles are caught before submission.
        """
        from apps.filing_automation.services.profile_schema import (
            validate_profile_types,  # Backend Engineer must add this helper
        )

        # Pass an int for cementing_company_p5 — must raise.
        bad_data = {**_REQUIRED_PROFILE_DATA, "cementing_company_p5": 12345}

        with pytest.raises((TypeError, ValueError, Exception)) as exc_info:
            validate_profile_types("rrc", "w3a", bad_data)

        # The error must NOT be an AssertionError (test harness error).
        assert not isinstance(exc_info.value, AssertionError), (
            "validate_profile_types must raise TypeError/ValueError, not AssertionError"
        )
        # The message should hint at the offending field.
        error_text = str(exc_info.value).lower()
        assert any(kw in error_text for kw in ("p5", "cementing", "str", "type", "int")), (
            f"Error message must mention the offending field or type. Got: {exc_info.value!r}"
        )


# ===========================================================================
# Adapter tests (4-7)
# ===========================================================================

class TestAdapterEmitsNewOptionalKeys:
    """plan_snapshot_to_form_data must propagate new optional keys into calculated_data."""

    # -----------------------------------------------------------------------
    # Test 4 — cementing_company_address emitted in calculated_data
    # -----------------------------------------------------------------------
    def test_adapter_emits_cementing_address_in_calculated_data(self):
        """calculated_data['cementing_company_address'] must equal profile value.

        FAILS until adapter.py reads rrc.w3a.cementing_company_address from profile
        and stores it under the same key in calculated_data.
        """
        from apps.filing_automation.services.adapter import plan_snapshot_to_form_data

        profile = _make_profile(_FULL_W3A_DATA)
        snap = _make_snap()

        form_data, _ = plan_snapshot_to_form_data(snap, _ATTESTATION, profile)

        assert "cementing_company_address" in form_data.calculated_data, (
            "calculated_data must contain 'cementing_company_address' when profile supplies it."
        )
        assert form_data.calculated_data["cementing_company_address"] == (
            "PO Box 13077, Odessa, TX 79768"
        ), (
            f"Unexpected value: {form_data.calculated_data.get('cementing_company_address')!r}"
        )

    # -----------------------------------------------------------------------
    # Test 5 — cementing_company_p5 emitted in calculated_data
    # -----------------------------------------------------------------------
    def test_adapter_emits_cementing_p5_in_calculated_data(self):
        """calculated_data['cementing_company_p5'] must equal profile value.

        FAILS until adapter.py reads rrc.w3a.cementing_company_p5 from profile.
        """
        from apps.filing_automation.services.adapter import plan_snapshot_to_form_data

        profile = _make_profile(_FULL_W3A_DATA)
        snap = _make_snap()

        form_data, _ = plan_snapshot_to_form_data(snap, _ATTESTATION, profile)

        assert "cementing_company_p5" in form_data.calculated_data, (
            "calculated_data must contain 'cementing_company_p5' when profile supplies it."
        )
        assert form_data.calculated_data["cementing_company_p5"] == "040196", (
            f"Unexpected value: {form_data.calculated_data.get('cementing_company_p5')!r}"
        )

    # -----------------------------------------------------------------------
    # Test 6 — contact_ext emitted in calculated_data
    # -----------------------------------------------------------------------
    def test_adapter_emits_contact_ext_in_calculated_data(self):
        """calculated_data['contact_ext'] must equal profile value.

        FAILS until adapter.py reads rrc.w3a.contact_ext from profile.
        """
        from apps.filing_automation.services.adapter import plan_snapshot_to_form_data

        profile = _make_profile(_FULL_W3A_DATA)
        snap = _make_snap()

        form_data, _ = plan_snapshot_to_form_data(snap, _ATTESTATION, profile)

        assert "contact_ext" in form_data.calculated_data, (
            "calculated_data must contain 'contact_ext' when profile supplies it."
        )
        assert form_data.calculated_data["contact_ext"] == "555", (
            f"Unexpected value: {form_data.calculated_data.get('contact_ext')!r}"
        )

    # -----------------------------------------------------------------------
    # Test 7 — new keys absent from profile → omitted from calculated_data
    # -----------------------------------------------------------------------
    def test_adapter_omits_or_empties_new_keys_when_absent_from_profile(self):
        """When the profile has none of the 3 new optional keys, the adapter must:
          (a) NOT raise, AND
          (b) NOT put any of the 3 new keys into calculated_data with a non-empty
              value (absent is preferred; empty string is acceptable).

        DECISION: OMIT (not empty-string).  Makes "key present?" reliable in the
        EXT-skip filler test (test 11).  If the Backend Engineer prefers
        empty-string sentinel, update tests 7, 10, 11 and document the choice.

        This test FAILS until:
          1. The schema declares the new keys as optional (same gate as tests 1-2).
          2. The adapter reads the new keys and handles None/absent correctly.

        The schema-declaration gate makes this red before implementation even
        though the adapter currently omits the keys (because it doesn't know
        about them).  Once the schema is extended AND adapter wired, this turns
        green only if the omit-vs-empty behavior is correct.
        """
        from apps.filing_automation.services.adapter import plan_snapshot_to_form_data
        from apps.filing_automation.services.profile_schema import (
            BusinessProfileIncomplete,
            RRC_W3A_OPTIONAL,
        )

        # Gate: schema must already declare the new optional keys.
        for key in (
            "rrc.w3a.cementing_company_address",
            "rrc.w3a.cementing_company_p5",
            "rrc.w3a.contact_ext",
        ):
            assert key in RRC_W3A_OPTIONAL, (
                f"Schema gate: {key!r} must be in RRC_W3A_OPTIONAL. "
                f"Current: {RRC_W3A_OPTIONAL}"
            )

        profile = _make_profile(_REQUIRED_PROFILE_DATA)  # no new keys
        snap = _make_snap()

        # Must not raise
        try:
            form_data, _ = plan_snapshot_to_form_data(snap, _ATTESTATION, profile)
        except BusinessProfileIncomplete as exc:
            pytest.fail(
                f"Adapter must not raise BusinessProfileIncomplete for missing optional keys. "
                f"Got: {exc!r}"
            )

        cd = form_data.calculated_data

        for key in ("cementing_company_address", "cementing_company_p5", "contact_ext"):
            assert key not in cd or cd[key] in (None, ""), (
                f"Adapter must omit or empty-string absent optional key {key!r}. "
                f"Got: {cd.get(key)!r}"
            )
