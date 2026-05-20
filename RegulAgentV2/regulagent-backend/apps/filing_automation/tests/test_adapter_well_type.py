"""
TDD (red phase) — well_type pull-through in plan_snapshot_to_form_data.

These tests verify the adapter wires get_well_type(api14) into calculated_data:
  - When the helper returns a value, calculated_data["well_type"] is populated.
  - When the helper returns None, "well_type" must NOT be present in calculated_data
    (omit-on-absent pattern — same as cementing_company_address / contact_ext).

EXPECTED RED STATE:
    The adapter currently does NOT call get_well_type, so:
    - test_well_type_populated_when_helper_returns_value will FAIL
      (KeyError / AssertionError: "well_type" not in calculated_data)
    - test_well_type_omitted_when_helper_returns_none may PASS incidentally,
      but only because the key was never set — not because the omit-on-absent
      logic is implemented.  Once the helper is wired, this test enforces the
      intentional None-suppression branch.

Uses unittest.mock.patch — no DB required.  PlanSnapshot is faked with
types.SimpleNamespace, mirroring the existing test_adapter.py pattern.
"""
from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

os.environ.setdefault("ENCRYPTION_PEPPER", "test-pepper-for-adapter-well-type-tests")


# ---------------------------------------------------------------------------
# Minimal shared fixtures (mirrors test_adapter.py style)
# ---------------------------------------------------------------------------


@pytest.fixture
def payload() -> dict:
    """Minimal PlanSnapshot payload — only the fields adapter.py requires."""
    return {
        "jurisdiction": "TX",
        "form": "W-3A",
        "district": "7C",
        "inputs_summary": {"api14": "42-317-36134-00-00"},
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
                },
            ],
        },
    }


@pytest.fixture
def well() -> SimpleNamespace:
    """Stand-in for the related WellRegistry row."""
    return SimpleNamespace(
        api14="42-317-36134-00-00",
        operator_name="Test Operator LLC",
        lease_name="Test Lease #1",
        field_name="Test Field",
        permit_number="PERMIT-111222",
        state="TX",
        county="Midland",
        district="7C",
    )


@pytest.fixture
def snap(payload, well) -> SimpleNamespace:
    """Lightweight PlanSnapshot stand-in (no DB required)."""
    return SimpleNamespace(
        id="snap-well-type-1",
        plan_id="plan-wt-001",
        kind="post_edit",
        status="engineer_approved",
        payload=payload,
        well=well,
        tenant_id="tenant-uuid-wt-1",
    )


@pytest.fixture
def attestation() -> dict:
    return {
        "submitter_name": "Test Submitter",
        "submitter_title": "Test Title",
        "certification_checked": True,
    }


@pytest.fixture
def profile() -> SimpleNamespace:
    """Stand-in for TenantBusinessProfile with all required w3a fields."""
    return SimpleNamespace(
        data={
            "rrc": {
                "w3a": {
                    "cementing_company_name": "Test Cementing Co",
                    "contact_phone": "+1-555-0199",
                    "contact_email": "ops@test-example.com",
                    "submitter_default_name": "Default Name",
                    "submitter_default_title": "Default Title",
                    "default_plugging_date_offset_days": 30,
                }
            }
        }
    )


# ===========================================================================
# Test 1 — helper returns a value → calculated_data["well_type"] is populated
# ===========================================================================


class TestWellTypePopulatedWhenHelperReturnsValue:
    """When get_well_type returns 'Oil', calculated_data must include well_type='Oil'."""

    def test_well_type_populated_when_helper_returns_value(
        self, snap, attestation, profile
    ):
        from apps.filing_automation.services.adapter import plan_snapshot_to_form_data

        with patch(
            "apps.filing_automation.services.adapter.get_well_type",
            return_value="Oil",
        ):
            form_data, _ = plan_snapshot_to_form_data(snap, attestation, profile)

        assert "well_type" in form_data.calculated_data, (
            "Expected 'well_type' key in calculated_data when helper returns 'Oil'; "
            f"keys present: {list(form_data.calculated_data.keys())}"
        )
        assert form_data.calculated_data["well_type"] == "Oil", (
            f"Expected calculated_data['well_type'] == 'Oil'; "
            f"got {form_data.calculated_data['well_type']!r}"
        )

    def test_well_type_injection_populated_when_helper_returns_injection(
        self, snap, attestation, profile
    ):
        from apps.filing_automation.services.adapter import plan_snapshot_to_form_data

        with patch(
            "apps.filing_automation.services.adapter.get_well_type",
            return_value="Injection",
        ):
            form_data, _ = plan_snapshot_to_form_data(snap, attestation, profile)

        assert form_data.calculated_data.get("well_type") == "Injection", (
            f"Expected calculated_data['well_type'] == 'Injection'; "
            f"got {form_data.calculated_data.get('well_type')!r}"
        )


# ===========================================================================
# Test 2 — helper returns None → "well_type" key must NOT be in calculated_data
# ===========================================================================


class TestWellTypeOmittedWhenHelperReturnsNone:
    """When get_well_type returns None, 'well_type' must be absent from calculated_data.

    Uses assertNotIn / 'not in', NOT 'is None' — the omit-on-absent contract
    means the key must not exist at all (same as cementing_company_address when
    the profile omits it).
    """

    def test_well_type_omitted_when_helper_returns_none(
        self, snap, attestation, profile
    ):
        from apps.filing_automation.services.adapter import plan_snapshot_to_form_data

        with patch(
            "apps.filing_automation.services.adapter.get_well_type",
            return_value=None,
        ):
            form_data, _ = plan_snapshot_to_form_data(snap, attestation, profile)

        assert "well_type" not in form_data.calculated_data, (
            "Expected 'well_type' to be ABSENT from calculated_data when helper "
            "returns None (omit-on-absent pattern). Key must not exist at all — "
            f"not merely set to None. calculated_data keys: "
            f"{list(form_data.calculated_data.keys())}"
        )
