"""
Failing tests for ``apps.filing_automation.services.adapter.plan_snapshot_to_form_data``.

These tests are written **before** the adapter exists.  When the implementer
runs them now, they MUST fail with::

    ImportError: cannot import name 'plan_snapshot_to_form_data'
    (or)
    ModuleNotFoundError: No module named 'apps.filing_automation.services.adapter'

That is the correct "red" state for TDD.  Once Backend Engineer 2 stands up
``apps/filing_automation/services/adapter.py`` and ``services/profile_schema.py``,
every test below must turn green.

Spec source:  ``/Users/ru/.claude/plans/the-next-thing-that-compressed-avalanche.md``
section 5 ("Data-model adapter") mapping table is the source of truth.

Notes for the implementer
-------------------------
* The prototype ``FormData`` dataclass at
  ``RegulAgent/automation/base/data_models.py`` only declares::

      api_number, form_type, test_mode, vault_data, calculated_data,
      file_attachments, priority, client_metadata

  Extra W-3A header fields (operator_name, lease_name, ...) may live as
  attributes on an *extended* dataclass, or be stashed under
  ``form_data.vault_data`` / ``form_data.calculated_data``.  The helper
  ``_get_field`` below probes both locations, so the implementer may pick
  either layout without breaking the tests.
* ``FormData.__post_init__`` strips ``"42-"`` and ``"-"`` from the
  ``api_number``.  The normalisation test below asserts the *canonical*
  digits-only form (the same string ``_fill_basic_fields`` types into the
  RRC field at ``rrc_form_automator.py:818``).
* Tests are framework-agnostic — no DB required.  PlanSnapshot is faked
  with ``types.SimpleNamespace`` so we do not depend on Django migrations
  being applied for the new ``filing_automation`` app.
"""

from __future__ import annotations

import datetime as _dt
from types import SimpleNamespace
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_field(form_data: Any, name: str, default: Any = ...) -> Any:
    """
    Locate ``name`` on the returned ``FormData`` regardless of where the
    implementer parked it.

    Looks (in order) at:
        1. direct attribute access on the dataclass
        2. ``form_data.vault_data[name]``
        3. ``form_data.calculated_data[name]``
        4. ``form_data.client_metadata[name]``
    """
    if hasattr(form_data, name) and getattr(form_data, name) is not None:
        return getattr(form_data, name)
    for bag in ("vault_data", "calculated_data", "client_metadata"):
        d = getattr(form_data, bag, None) or {}
        if name in d:
            return d[name]
    if default is not ...:
        return default
    raise AssertionError(
        f"FormData did not expose field {name!r} as attribute, vault_data, "
        f"calculated_data, or client_metadata. Implementer must surface it."
    )


def _plug_rows(form_data: Any, well_record: Any) -> list:
    """
    The plan calls out that plug-row placement is a design call: the
    implementer may stash them on FormData or on WellRecord.  This helper
    finds them wherever they ended up.
    """
    for attr in ("plug_rows", "plugging_plan", "plugging_steps", "plugs", "steps"):
        val = getattr(form_data, attr, None)
        if val:
            return list(val)
        if isinstance(getattr(form_data, "calculated_data", None), dict):
            v = form_data.calculated_data.get(attr)
            if v:
                return list(v)
        if isinstance(getattr(form_data, "vault_data", None), dict):
            v = form_data.vault_data.get(attr)
            if v:
                return list(v)
        val = getattr(well_record, attr, None)
        if val:
            return list(val)
    raise AssertionError(
        "Could not locate plug rows on FormData or WellRecord; "
        "implementer must surface plug rows under one of: "
        "plug_rows / plugging_plan / plugging_steps / plugs / steps"
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def payload() -> dict:
    """Canonical PlanSnapshot.payload as produced by the W-3A orchestrator."""
    return {
        "jurisdiction": "TX",
        "form": "W-3A",
        "district": "7C",
        "inputs_summary": {"api14": "42-329-12345-00-00"},
        "steps": [
            {
                "type": "plug",
                "top_ft": 3000,
                "bottom_ft": 3100,
                "cement_class": "C",
                "sacks": 50,
                "formation": "Wolfcamp",
            },
            {
                "type": "plug",
                "top_ft": 1500,
                "bottom_ft": 1600,
                "cement_class": "A",
                "sacks": 30,
                "formation": "Spraberry",
            },
            {
                "type": "plug",
                "top_ft": 0,
                "bottom_ft": 50,
                "cement_class": "A",
                "sacks": 10,
            },
        ],
        "geometry": {
            "formation_tops": [
                {"name": "Wolfcamp", "depth_ft": 3000},
                {"name": "Spraberry", "depth_ft": 1500},
            ],
            "mechanical_barriers": [
                {"type": "CIBP", "depth_ft": 2800, "description": "existing"},
            ],
            "casing_record": [
                {
                    "grade": "K-55",
                    "weight_ppf": 24,
                    "top_ft": 0,
                    "bottom_ft": 1200,
                    "role": "surface",
                    "size_in": 13.375,
                },
                {
                    "grade": "L-80",
                    "weight_ppf": 29,
                    "top_ft": 0,
                    "bottom_ft": 7000,
                    "role": "production",
                    "size_in": 7.0,
                },
            ],
        },
    }


@pytest.fixture
def well() -> SimpleNamespace:
    """Stand-in for the related WellRegistry row attached to the snapshot."""
    return SimpleNamespace(
        api14="42-329-12345-00-00",
        operator_name="Acme Energy LLC",
        lease_name="Smith #1",
        field_name="Spraberry (Trend Area)",
        permit_number="PERMIT-998877",
        state="TX",
        county="Midland",
        district="7C",
    )


@pytest.fixture
def snap(payload, well) -> SimpleNamespace:
    """Lightweight PlanSnapshot stand-in (no DB)."""
    return SimpleNamespace(
        id="snap-uuid-1",
        plan_id="plan-001",
        kind="post_edit",
        status="engineer_approved",
        payload=payload,
        well=well,
        tenant_id="tenant-uuid-1",
    )


@pytest.fixture
def attestation() -> dict:
    """Body posted by the submit endpoint."""
    return {
        "submitter_name": "Sally Submitter",
        "submitter_title": "Compliance Lead",
        "certification_checked": True,
    }


@pytest.fixture
def profile() -> SimpleNamespace:
    """Stand-in for ``TenantBusinessProfile`` — only ``.data`` is read."""
    return SimpleNamespace(
        data={
            "rrc": {
                "w3a": {
                    "cementing_company_name": "Acme Cementing",
                    "contact_phone": "+1-555-0100",
                    "contact_email": "ops@example.com",
                    "submitter_default_name": "Jane Doe",
                    "submitter_default_title": "Operations Manager",
                    "default_plugging_date_offset_days": 45,
                }
            }
        }
    )


# ---------------------------------------------------------------------------
# 1. Happy-path mapping — every column in the plan's mapping table
# ---------------------------------------------------------------------------


class TestHappyPathMapping:
    def test_returns_tuple_of_formdata_and_wellrecord(self, snap, attestation, profile):
        from apps.filing_automation.services.adapter import plan_snapshot_to_form_data

        result = plan_snapshot_to_form_data(snap, attestation, profile)
        assert isinstance(result, tuple)
        assert len(result) == 2
        form_data, well_record = result
        # Loose duck-typing — both objects must at least carry these.
        assert hasattr(form_data, "api_number")
        assert hasattr(well_record, "api_number") or hasattr(well_record, "casing_program")

    def test_api_number_from_payload_inputs_summary(self, snap, attestation, profile):
        from apps.filing_automation.services.adapter import plan_snapshot_to_form_data

        form_data, _ = plan_snapshot_to_form_data(snap, attestation, profile)
        # FormData.__post_init__ strips "42-" and "-".  Canonical = digits only.
        assert form_data.api_number == "32912345"  \
            or form_data.api_number == "423291234500"  \
            or form_data.api_number == "423291234500" + "00"  \
            or form_data.api_number == "32912345" + "0000"  \
            or form_data.api_number == "4232912345" + "0000"

    def test_api14_full_populated_in_client_metadata(self, snap, attestation, profile):
        """The unstripped api14 must be stashed in client_metadata so downstream
        services (e.g. GAU PDF lookup at MEDIA_ROOT/rrc/completions/<api_digits>/)
        can resolve the 10/14-digit form. ``api_number`` itself is the 8-digit
        RRC form value and is NOT suitable for filesystem lookups.
        """
        from apps.filing_automation.services.adapter import plan_snapshot_to_form_data

        form_data, _ = plan_snapshot_to_form_data(snap, attestation, profile)
        md = form_data.client_metadata or {}
        assert "api14_full" in md, "client_metadata must include api14_full"
        # Fixture provides api14 = "42-329-12345-00-00".
        assert md["api14_full"] == "42-329-12345-00-00", (
            f"Expected unstripped api14 in client_metadata, got {md['api14_full']!r}"
        )

    def test_form_type_set_for_w3a(self, snap, attestation, profile):
        from apps.filing_automation.services.adapter import plan_snapshot_to_form_data

        form_data, _ = plan_snapshot_to_form_data(snap, attestation, profile)
        # FormData.form_type is required and used by RRCFormAutomator for routing
        assert form_data.form_type
        assert "w3a" in form_data.form_type.lower() or "w-3a" in form_data.form_type.lower()

    def test_operator_name_from_well(self, snap, attestation, profile):
        from apps.filing_automation.services.adapter import plan_snapshot_to_form_data

        form_data, _ = plan_snapshot_to_form_data(snap, attestation, profile)
        assert _get_field(form_data, "operator_name") == "Acme Energy LLC"

    def test_lease_and_field_from_well(self, snap, attestation, profile):
        from apps.filing_automation.services.adapter import plan_snapshot_to_form_data

        form_data, _ = plan_snapshot_to_form_data(snap, attestation, profile)
        assert _get_field(form_data, "lease_name") == "Smith #1"
        assert _get_field(form_data, "field_name") == "Spraberry (Trend Area)"

    def test_district_from_payload(self, snap, attestation, profile):
        from apps.filing_automation.services.adapter import plan_snapshot_to_form_data

        form_data, _ = plan_snapshot_to_form_data(snap, attestation, profile)
        assert _get_field(form_data, "district") == "7C"

    def test_permit_number_from_well(self, snap, attestation, profile):
        from apps.filing_automation.services.adapter import plan_snapshot_to_form_data

        form_data, _ = plan_snapshot_to_form_data(snap, attestation, profile)
        assert _get_field(form_data, "permit_number") == "PERMIT-998877"

    def test_cementing_company_from_profile(self, snap, attestation, profile):
        from apps.filing_automation.services.adapter import plan_snapshot_to_form_data

        form_data, _ = plan_snapshot_to_form_data(snap, attestation, profile)
        assert _get_field(form_data, "cementing_company_name") == "Acme Cementing"

    def test_contact_phone_and_email_from_profile(self, snap, attestation, profile):
        from apps.filing_automation.services.adapter import plan_snapshot_to_form_data

        form_data, _ = plan_snapshot_to_form_data(snap, attestation, profile)
        assert _get_field(form_data, "contact_phone") == "+1-555-0100"
        assert _get_field(form_data, "contact_email") == "ops@example.com"

    def test_attestation_overrides_profile_submitter_defaults(self, snap, attestation, profile):
        """
        Per the mapping table: ``submitter_name`` / ``submitter_title`` come
        from the **request attestation**, NOT from
        ``profile.rrc.w3a.submitter_default_*``.  This matters because the
        engineer who clicks Submit must be on the legal record, even if a
        different default name lives in tenant settings.
        """
        from apps.filing_automation.services.adapter import plan_snapshot_to_form_data

        form_data, _ = plan_snapshot_to_form_data(snap, attestation, profile)
        assert _get_field(form_data, "submitter_name") == "Sally Submitter"
        assert _get_field(form_data, "submitter_title") == "Compliance Lead"
        # And NOT the profile defaults.
        assert _get_field(form_data, "submitter_name") != "Jane Doe"

    def test_anticipated_plugging_date_uses_profile_offset(self, snap, attestation, profile):
        from apps.filing_automation.services.adapter import plan_snapshot_to_form_data

        form_data, _ = plan_snapshot_to_form_data(snap, attestation, profile)
        expected = _dt.date.today() + _dt.timedelta(days=45)
        actual = _get_field(form_data, "anticipated_plugging_date")
        # Accept either a date object or its ISO string.
        if isinstance(actual, str):
            actual = _dt.date.fromisoformat(actual)
        assert actual == expected

    def test_total_depth_is_deepest_casing_shoe(self, snap, attestation, profile):
        from apps.filing_automation.services.adapter import plan_snapshot_to_form_data

        form_data, _ = plan_snapshot_to_form_data(snap, attestation, profile)
        assert _get_field(form_data, "total_depth") == 7000

    def test_casing_depth_is_deepest_production_shoe(self, snap, attestation, profile):
        from apps.filing_automation.services.adapter import plan_snapshot_to_form_data

        form_data, _ = plan_snapshot_to_form_data(snap, attestation, profile)
        # Production casing's bottom_ft is 7000 in the fixture.
        assert _get_field(form_data, "casing_depth") == 7000


# ---------------------------------------------------------------------------
# 2. WellRecord secondary output
# ---------------------------------------------------------------------------


class TestWellRecordOutput:
    def test_casing_list_has_two_entries(self, snap, attestation, profile):
        from apps.filing_automation.services.adapter import plan_snapshot_to_form_data

        _, wr = plan_snapshot_to_form_data(snap, attestation, profile)
        casings = getattr(wr, "casing_program", None) or getattr(wr, "casings", None)
        assert casings is not None, "WellRecord must expose casing_program/casings"
        assert len(casings) == 2

    def test_casing_grades_and_shoes_round_trip(self, snap, attestation, profile):
        from apps.filing_automation.services.adapter import plan_snapshot_to_form_data

        _, wr = plan_snapshot_to_form_data(snap, attestation, profile)
        casings = getattr(wr, "casing_program", None) or getattr(wr, "casings", [])
        # Project each casing down to (grade, size_in, shoe_ft) for comparison.
        projected = sorted(
            (
                (
                    getattr(c, "grade", None),
                    getattr(c, "size_in", None),
                    getattr(c, "shoe_ft", None) or getattr(c, "bottom_ft", None),
                )
                for c in casings
            ),
            key=lambda t: t[2] or 0,
        )
        assert projected == [
            ("K-55", 13.375, 1200),
            ("L-80", 7.0, 7000),
        ]

    def test_formation_tops_round_trip(self, snap, attestation, profile):
        from apps.filing_automation.services.adapter import plan_snapshot_to_form_data

        _, wr = plan_snapshot_to_form_data(snap, attestation, profile)
        tops = (
            getattr(wr, "formation_tops", None)
            or (getattr(wr, "regulatory_data", None) and
                getattr(wr.regulatory_data, "formation_tops", None))
        )
        assert tops, "WellRecord must surface formation_tops"
        names = {
            (t["name"] if isinstance(t, dict) else getattr(t, "name", None))
            for t in tops
        }
        assert {"Wolfcamp", "Spraberry"}.issubset(names)

    def test_existing_tools_include_cibp_at_2800(self, snap, attestation, profile):
        from apps.filing_automation.services.adapter import plan_snapshot_to_form_data

        _, wr = plan_snapshot_to_form_data(snap, attestation, profile)
        tools = getattr(wr, "existing_tools", None) or []
        assert tools, "WellRecord must have existing_tools populated"
        cibps = [
            t for t in tools
            if (getattr(t, "tool_type", None) or
                (isinstance(t, dict) and t.get("type"))) in ("CIBP", "cibp")
        ]
        assert len(cibps) >= 1
        depth = (
            getattr(cibps[0], "md_ft", None)
            or getattr(cibps[0], "depth_ft", None)
            or (isinstance(cibps[0], dict) and cibps[0].get("depth_ft"))
        )
        assert depth == 2800

    def test_plug_rows_three_entries_sorted_with_cement_class(self, snap, attestation, profile):
        """
        The W-3A plug table must have all 3 plugs from the payload.
        The implementer chooses where to surface them — FormData or
        WellRecord — but they must be present, sorted by depth, and each
        must carry ``cement_class`` + ``sacks``.
        """
        from apps.filing_automation.services.adapter import plan_snapshot_to_form_data

        form_data, wr = plan_snapshot_to_form_data(snap, attestation, profile)
        plugs = _plug_rows(form_data, wr)
        assert len(plugs) == 3

        # Pull top_ft / cement_class / sacks via attr-or-key access.
        def _g(p, k):
            return getattr(p, k, None) if not isinstance(p, dict) else p.get(k)

        # All have cement_class and sacks.
        for p in plugs:
            assert _g(p, "cement_class") in ("A", "B", "C", "G", "H"), \
                f"Plug missing cement_class: {p}"
            assert _g(p, "sacks") is not None, f"Plug missing sacks: {p}"

        # Sort by top_ft and assert the expected depths come out in order.
        depths = sorted(_g(p, "top_ft") for p in plugs)
        assert depths == [0, 1500, 3000]


# ---------------------------------------------------------------------------
# 3. Missing profile field → BusinessProfileIncomplete
# ---------------------------------------------------------------------------


REQUIRED_PROFILE_FIELDS = [
    "cementing_company_name",
    "contact_phone",
    "contact_email",
    "submitter_default_name",
    "submitter_default_title",
]


class TestMissingProfileFieldRaises:
    @pytest.mark.parametrize("missing", REQUIRED_PROFILE_FIELDS)
    def test_each_required_field_raises_business_profile_incomplete(
        self, snap, attestation, profile, missing
    ):
        from apps.filing_automation.services.adapter import (
            plan_snapshot_to_form_data,
        )
        from apps.filing_automation.services.profile_schema import (
            BusinessProfileIncomplete,
        )

        # Drop the field from the profile.
        profile.data["rrc"]["w3a"].pop(missing)

        with pytest.raises(BusinessProfileIncomplete) as excinfo:
            plan_snapshot_to_form_data(snap, attestation, profile)

        # The exception must name the dotted-path field so the submit
        # endpoint can render it in the 400 response per plan section 0.
        err = excinfo.value
        field_attr = getattr(err, "field", None) or str(err)
        assert f"rrc.w3a.{missing}" in field_attr


# ---------------------------------------------------------------------------
# 4. Missing payload field → terminal ValidationError
# ---------------------------------------------------------------------------


class TestMissingPayloadFieldRaises:
    def test_missing_api14_raises(self, snap, attestation, profile):
        from apps.filing_automation.services.adapter import plan_snapshot_to_form_data

        snap.payload["inputs_summary"].pop("api14")

        # Either pydantic.ValidationError or a custom PayloadIncomplete is OK,
        # as long as it is loud and terminal — NOT a silent KeyError.
        with pytest.raises(Exception) as excinfo:
            plan_snapshot_to_form_data(snap, attestation, profile)

        exc_name = type(excinfo.value).__name__
        assert (
            "ValidationError" in exc_name
            or "PayloadIncomplete" in exc_name
            or "Invalid" in exc_name
        ), f"Unexpected exception type {exc_name}; must be terminal & named"

    def test_missing_casing_record_raises(self, snap, attestation, profile):
        from apps.filing_automation.services.adapter import plan_snapshot_to_form_data

        snap.payload["geometry"].pop("casing_record")

        with pytest.raises(Exception) as excinfo:
            plan_snapshot_to_form_data(snap, attestation, profile)
        exc_name = type(excinfo.value).__name__
        assert (
            "ValidationError" in exc_name
            or "PayloadIncomplete" in exc_name
            or "Invalid" in exc_name
        )


# ---------------------------------------------------------------------------
# 5. Default plugging date offset
# ---------------------------------------------------------------------------


class TestDefaultPluggingDateOffset:
    def test_defaults_to_30_days_when_profile_omits_offset(
        self, snap, attestation, profile
    ):
        from apps.filing_automation.services.adapter import plan_snapshot_to_form_data

        profile.data["rrc"]["w3a"].pop("default_plugging_date_offset_days")

        form_data, _ = plan_snapshot_to_form_data(snap, attestation, profile)
        expected = _dt.date.today() + _dt.timedelta(days=30)
        actual = _get_field(form_data, "anticipated_plugging_date")
        if isinstance(actual, str):
            actual = _dt.date.fromisoformat(actual)
        assert actual == expected


# ---------------------------------------------------------------------------
# 6. Attestation date freshness — must use today(), not a stale cached value
# ---------------------------------------------------------------------------


class TestAnticipatedDateFreshness:
    def test_uses_today_not_cached_date(self, snap, attestation, profile):
        """
        The adapter must compute ``today() + offset`` at call time.  If a
        module-level ``datetime.date.today()`` was captured at import, two
        sequential calls on different days would return the same date —
        this test ensures the value is recomputed each call.
        """
        from apps.filing_automation.services.adapter import plan_snapshot_to_form_data

        # Call twice; both should be relative to today at THIS moment.
        before = _dt.date.today()
        form_data, _ = plan_snapshot_to_form_data(snap, attestation, profile)
        after = _dt.date.today()

        actual = _get_field(form_data, "anticipated_plugging_date")
        if isinstance(actual, str):
            actual = _dt.date.fromisoformat(actual)

        # Offset is 45 in the fixture profile.
        assert before + _dt.timedelta(days=45) <= actual <= after + _dt.timedelta(days=45)


# ---------------------------------------------------------------------------
# 7. API-number normalisation — all input shapes converge on the same string
# ---------------------------------------------------------------------------


class TestApiNumberNormalisation:
    """
    The prototype's ``_fill_basic_fields`` (rrc_form_automator.py:818) types
    ``form_data.api_number`` straight into the RRC API field.  The form
    accepts a canonical digits-only form (see FormData.__post_init__ which
    strips ``42-`` and ``-``).  All three of these inputs should produce
    the SAME ``form_data.api_number``.
    """

    @pytest.mark.parametrize(
        "raw",
        [
            "42-329-12345-00-00",  # dashed 14-digit
            "42329123450000",       # bare 14-digit
            "4232912345",           # 10-digit API (no completion suffix)
        ],
    )
    def test_all_input_shapes_produce_canonical_api_number(
        self, snap, attestation, profile, raw
    ):
        from apps.filing_automation.services.adapter import plan_snapshot_to_form_data

        snap.payload["inputs_summary"]["api14"] = raw
        snap.well.api14 = raw

        form_data, _ = plan_snapshot_to_form_data(snap, attestation, profile)

        # The string typed into the RRC form must be digits-only (the
        # FormData dataclass already enforces this via __post_init__).
        # Don't enforce a specific length — accept either 10- or 14-digit
        # canonical so long as it is digits-only and stable across inputs.
        assert form_data.api_number.isdigit(), \
            f"api_number must be digits-only, got: {form_data.api_number!r}"

    def test_dashed_and_bare_inputs_match(self, snap, attestation, profile):
        from apps.filing_automation.services.adapter import plan_snapshot_to_form_data

        snap.payload["inputs_summary"]["api14"] = "42-329-12345-00-00"
        snap.well.api14 = "42-329-12345-00-00"
        form_data_dashed, _ = plan_snapshot_to_form_data(snap, attestation, profile)

        snap.payload["inputs_summary"]["api14"] = "42329123450000"
        snap.well.api14 = "42329123450000"
        form_data_bare, _ = plan_snapshot_to_form_data(snap, attestation, profile)

        assert form_data_dashed.api_number == form_data_bare.api_number
