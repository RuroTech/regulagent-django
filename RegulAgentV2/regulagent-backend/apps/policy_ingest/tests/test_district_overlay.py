"""
Failing tests for DistrictOverlay / CountyOverlay models and API endpoints.

These tests are written BEFORE implementation (TDD mode). Every test in this
file is expected to FAIL when first run because:
  - DistrictOverlay model does not exist yet
  - CountyOverlay model does not exist yet
  - /api/policy/district-overlays/ endpoints do not exist yet
  - ingest_district_overlays management command does not exist yet

Tests must fail with ImportError / assertion errors — NOT syntax errors
in the test file itself.
"""

import pytest
from django.core.management import call_command

pytestmark = pytest.mark.django_db

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

YAML_DISTRICT_CODE = "7C"          # value from the district: key in the YAML
EXPECTED_API_CODE  = "7C"          # district_code stored in the DB (uppercase)
YAML_PATH = (
    "apps/policy/packs/tx/w3a/district_overlays/07c_county_procedures.yml"
)
KNOWN_COUNTY = "Coke County"       # first county listed in the YAML


# ---------------------------------------------------------------------------
# 1. DistrictOverlay model — basic creation and uniqueness constraint
# ---------------------------------------------------------------------------

class TestDistrictOverlayModel:
    """DistrictOverlay model can be created and queried by (jurisdiction, district_code)."""

    def test_create_district_overlay(self):
        """Creates a DistrictOverlay row and retrieves it by the composite key."""
        from apps.policy_ingest.models import DistrictOverlay  # noqa: F401 — will fail

        overlay = DistrictOverlay.objects.create(
            jurisdiction="TX",
            district_code="07C",
            source_file=YAML_PATH,
            requirements={"tagging_required_hint": {"value": True}},
            preferences={"operational": {"notice_hours_min": 4}},
            plugging_chart={},
        )
        assert overlay.pk is not None

        fetched = DistrictOverlay.objects.get(jurisdiction="TX", district_code="07C")
        assert fetched.pk == overlay.pk

    def test_district_code_and_jurisdiction_are_unique_together(self):
        """A second insert with the same (jurisdiction, district_code) must raise IntegrityError."""
        from apps.policy_ingest.models import DistrictOverlay
        from django.db import IntegrityError

        DistrictOverlay.objects.create(
            jurisdiction="TX",
            district_code="07C",
            source_file=YAML_PATH,
            requirements={},
            preferences={},
        )
        with pytest.raises(IntegrityError):
            DistrictOverlay.objects.create(
                jurisdiction="TX",
                district_code="07C",
                source_file=YAML_PATH,
                requirements={},
                preferences={},
            )

    def test_filter_by_jurisdiction(self):
        """Can filter DistrictOverlay rows by jurisdiction."""
        from apps.policy_ingest.models import DistrictOverlay

        DistrictOverlay.objects.create(
            jurisdiction="TX", district_code="07C", source_file=YAML_PATH,
            requirements={}, preferences={},
        )
        DistrictOverlay.objects.create(
            jurisdiction="NM", district_code="1", source_file="nm.yml",
            requirements={}, preferences={},
        )

        tx_overlays = DistrictOverlay.objects.filter(jurisdiction="TX")
        assert tx_overlays.count() == 1
        assert tx_overlays.first().district_code == "07C"

    def test_plugging_chart_defaults_to_empty_dict(self):
        """plugging_chart field defaults to an empty dict when not supplied."""
        from apps.policy_ingest.models import DistrictOverlay

        overlay = DistrictOverlay.objects.create(
            jurisdiction="TX",
            district_code="07C",
            source_file=YAML_PATH,
            requirements={},
            preferences={},
            # plugging_chart omitted — relies on default=dict
        )
        assert overlay.plugging_chart == {}

    def test_imported_at_auto_populated(self):
        """imported_at is set automatically on creation."""
        from apps.policy_ingest.models import DistrictOverlay
        from django.utils import timezone

        before = timezone.now()
        overlay = DistrictOverlay.objects.create(
            jurisdiction="TX", district_code="07C", source_file=YAML_PATH,
            requirements={}, preferences={},
        )
        after = timezone.now()

        assert overlay.imported_at is not None
        assert before <= overlay.imported_at <= after


# ---------------------------------------------------------------------------
# 2. CountyOverlay model — FK link to DistrictOverlay
# ---------------------------------------------------------------------------

class TestCountyOverlayModel:
    """CountyOverlay links to DistrictOverlay via FK, unique on (district_overlay, county_name)."""

    def _make_district(self):
        from apps.policy_ingest.models import DistrictOverlay
        return DistrictOverlay.objects.create(
            jurisdiction="TX",
            district_code="07C",
            source_file=YAML_PATH,
            requirements={},
            preferences={},
        )

    def test_create_county_overlay(self):
        """Creates a CountyOverlay linked to a DistrictOverlay."""
        from apps.policy_ingest.models import CountyOverlay  # noqa: F401 — will fail

        district = self._make_district()
        county = CountyOverlay.objects.create(
            district_overlay=district,
            county_name=KNOWN_COUNTY,
            requirements={"cap_above_highest_perf_ft": {"value": 50}},
            preferences={},
            county_procedures={"cibp_required": {"offset_above_perf_ft": 50}},
            formation_data={},
        )
        assert county.pk is not None
        assert county.district_overlay_id == district.pk

    def test_counties_related_name(self):
        """DistrictOverlay.counties reverse relation returns associated CountyOverlay rows."""
        from apps.policy_ingest.models import CountyOverlay

        district = self._make_district()
        CountyOverlay.objects.create(
            district_overlay=district,
            county_name="Coke County",
            requirements={}, preferences={}, county_procedures={}, formation_data={},
        )
        CountyOverlay.objects.create(
            district_overlay=district,
            county_name="Concho County",
            requirements={}, preferences={}, county_procedures={}, formation_data={},
        )

        assert district.counties.count() == 2
        names = list(district.counties.values_list("county_name", flat=True))
        assert "Coke County" in names
        assert "Concho County" in names

    def test_county_unique_per_district(self):
        """Duplicate (district_overlay, county_name) must raise IntegrityError."""
        from apps.policy_ingest.models import CountyOverlay
        from django.db import IntegrityError

        district = self._make_district()
        CountyOverlay.objects.create(
            district_overlay=district,
            county_name=KNOWN_COUNTY,
            requirements={}, preferences={}, county_procedures={}, formation_data={},
        )
        with pytest.raises(IntegrityError):
            CountyOverlay.objects.create(
                district_overlay=district,
                county_name=KNOWN_COUNTY,
                requirements={}, preferences={}, county_procedures={}, formation_data={},
            )

    def test_county_defaults_to_empty_dicts(self):
        """JSONField defaults (requirements, preferences, county_procedures, formation_data) resolve to empty dict."""
        from apps.policy_ingest.models import CountyOverlay

        district = self._make_district()
        county = CountyOverlay.objects.create(
            district_overlay=district,
            county_name=KNOWN_COUNTY,
            # all JSON fields omitted — rely on default=dict
        )
        assert county.requirements == {}
        assert county.preferences == {}
        assert county.county_procedures == {}
        assert county.formation_data == {}


# ---------------------------------------------------------------------------
# 3. GET /api/policy/district-overlays/ — list endpoint
# ---------------------------------------------------------------------------

class TestDistrictOverlayListAPI:
    """GET /api/policy/district-overlays/ returns district overlays, filterable by jurisdiction."""

    def _make_overlays(self):
        """Create sample TX and NM overlays. Deferred import so failure occurs in test body."""
        from apps.policy_ingest.models import DistrictOverlay
        tx = DistrictOverlay.objects.create(
            jurisdiction="TX", district_code="07C", source_file=YAML_PATH,
            requirements={}, preferences={},
        )
        nm = DistrictOverlay.objects.create(
            jurisdiction="NM", district_code="1", source_file="nm.yml",
            requirements={}, preferences={},
        )
        return tx, nm

    def test_list_endpoint_returns_200(self):
        """GET /api/policy/district-overlays/ returns HTTP 200."""
        from rest_framework.test import APIClient
        self._make_overlays()
        client = APIClient()
        response = client.get("/api/policy/district-overlays/")
        assert response.status_code == 200

    def test_list_endpoint_no_auth_required(self):
        """Endpoint is accessible without any authentication token."""
        from rest_framework.test import APIClient
        self._make_overlays()
        client = APIClient()  # no credentials set
        response = client.get("/api/policy/district-overlays/")
        # Must NOT be 401 or 403
        assert response.status_code not in (401, 403)

    def test_list_returns_all_overlays_when_no_filter(self):
        """Without a filter, all districts are returned."""
        from rest_framework.test import APIClient
        self._make_overlays()
        client = APIClient()
        response = client.get("/api/policy/district-overlays/")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2

    def test_filter_by_jurisdiction_tx(self):
        """?jurisdiction=TX returns only TX districts."""
        from rest_framework.test import APIClient
        self._make_overlays()
        client = APIClient()
        response = client.get("/api/policy/district-overlays/?jurisdiction=TX")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["jurisdiction"] == "TX"
        assert data[0]["district_code"] == "07C"

    def test_filter_by_jurisdiction_nm(self):
        """?jurisdiction=NM returns only NM districts."""
        from rest_framework.test import APIClient
        self._make_overlays()
        client = APIClient()
        response = client.get("/api/policy/district-overlays/?jurisdiction=NM")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["jurisdiction"] == "NM"

    def test_response_contains_expected_fields(self):
        """Each item in the list response contains the key fields."""
        from rest_framework.test import APIClient
        self._make_overlays()
        client = APIClient()
        response = client.get("/api/policy/district-overlays/?jurisdiction=TX")
        assert response.status_code == 200
        item = response.json()[0]
        for field in ("jurisdiction", "district_code", "requirements", "preferences", "plugging_chart"):
            assert field in item, f"Missing field: {field}"


# ---------------------------------------------------------------------------
# 4. GET /api/policy/district-overlays/<district_code>/counties/ — county endpoint
# ---------------------------------------------------------------------------

class TestDistrictOverlayCountiesAPI:
    """GET /api/policy/district-overlays/<district_code>/counties/ returns county overlays."""

    def _make_district_with_counties(self):
        """Create district + two counties. Deferred import so failure occurs in test body."""
        from apps.policy_ingest.models import DistrictOverlay, CountyOverlay

        district = DistrictOverlay.objects.create(
            jurisdiction="TX", district_code="07C", source_file=YAML_PATH,
            requirements={}, preferences={},
        )
        CountyOverlay.objects.create(
            district_overlay=district,
            county_name="Coke County",
            requirements={"cap_above_highest_perf_ft": {"value": 50}},
            preferences={},
            county_procedures={"cibp_required": {"offset_above_perf_ft": 50}},
            formation_data={},
        )
        CountyOverlay.objects.create(
            district_overlay=district,
            county_name="Concho County",
            requirements={},
            preferences={},
            county_procedures={},
            formation_data={},
        )
        return district

    def test_counties_endpoint_returns_200(self):
        """GET /api/policy/district-overlays/07C/counties/ returns HTTP 200."""
        from rest_framework.test import APIClient
        self._make_district_with_counties()
        client = APIClient()
        response = client.get("/api/policy/district-overlays/07C/counties/")
        assert response.status_code == 200

    def test_counties_endpoint_no_auth_required(self):
        """Counties endpoint is accessible without authentication."""
        from rest_framework.test import APIClient
        self._make_district_with_counties()
        client = APIClient()
        response = client.get("/api/policy/district-overlays/07C/counties/")
        assert response.status_code not in (401, 403)

    def test_counties_returns_all_counties_for_district(self):
        """Returns all county overlays for the requested district code."""
        from rest_framework.test import APIClient
        self._make_district_with_counties()
        client = APIClient()
        response = client.get("/api/policy/district-overlays/07C/counties/")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        county_names = [item["county_name"] for item in data]
        assert "Coke County" in county_names
        assert "Concho County" in county_names

    def test_counties_response_contains_expected_fields(self):
        """Each county item contains the key fields."""
        from rest_framework.test import APIClient
        self._make_district_with_counties()
        client = APIClient()
        response = client.get("/api/policy/district-overlays/07C/counties/")
        assert response.status_code == 200
        item = response.json()[0]
        for field in ("county_name", "requirements", "preferences", "county_procedures", "formation_data"):
            assert field in item, f"Missing field: {field}"

    def test_counties_404_for_unknown_district(self):
        """Returns 404 when the endpoint exists but the district_code has no DB row."""
        from rest_framework.test import APIClient
        # Ensure at least one district exists so the URL is registered but ZZZZ won't be found
        from apps.policy_ingest.models import DistrictOverlay
        DistrictOverlay.objects.create(
            jurisdiction="TX", district_code="07C", source_file=YAML_PATH,
            requirements={}, preferences={},
        )
        client = APIClient()
        response = client.get("/api/policy/district-overlays/ZZZZ/counties/")
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# 5. Management command: ingest_district_overlays
# ---------------------------------------------------------------------------

class TestIngestDistrictOverlaysCommand:
    """ingest_district_overlays management command creates DistrictOverlay + CountyOverlay rows."""

    def test_command_creates_district_overlay(self):
        """Running the command creates a DistrictOverlay row for district 7C."""
        from apps.policy_ingest.models import DistrictOverlay

        call_command("ingest_district_overlays", jurisdiction="TX")

        assert DistrictOverlay.objects.filter(jurisdiction="TX").exists()
        overlay = DistrictOverlay.objects.get(jurisdiction="TX", district_code=EXPECTED_API_CODE)
        assert overlay.source_file != ""

    def test_command_creates_county_overlays(self):
        """Running the command creates CountyOverlay rows for counties in the YAML."""
        from apps.policy_ingest.models import DistrictOverlay, CountyOverlay

        call_command("ingest_district_overlays", jurisdiction="TX")

        district = DistrictOverlay.objects.get(jurisdiction="TX", district_code=EXPECTED_API_CODE)
        assert district.counties.exists()

        # The YAML has Coke County as its first county
        assert district.counties.filter(county_name=KNOWN_COUNTY).exists()

    def test_command_stores_requirements_on_county(self):
        """County overlay has requirements data ingested from the YAML."""
        from apps.policy_ingest.models import DistrictOverlay

        call_command("ingest_district_overlays", jurisdiction="TX")

        district = DistrictOverlay.objects.get(jurisdiction="TX", district_code=EXPECTED_API_CODE)
        coke = district.counties.get(county_name=KNOWN_COUNTY)

        # Coke County has cap_above_highest_perf_ft: 50 in the YAML
        assert "cap_above_highest_perf_ft" in coke.requirements

    def test_command_stores_county_procedures(self):
        """County overlay has county_procedures data ingested from the YAML."""
        from apps.policy_ingest.models import DistrictOverlay

        call_command("ingest_district_overlays", jurisdiction="TX")

        district = DistrictOverlay.objects.get(jurisdiction="TX", district_code=EXPECTED_API_CODE)
        coke = district.counties.get(county_name=KNOWN_COUNTY)

        # Coke County has cibp_required in county_procedures
        assert "cibp_required" in coke.county_procedures

    def test_command_stores_plugging_chart_on_district(self):
        """DistrictOverlay has plugging_chart populated from preferences.plugging_chart in YAML."""
        from apps.policy_ingest.models import DistrictOverlay

        call_command("ingest_district_overlays", jurisdiction="TX")

        district = DistrictOverlay.objects.get(jurisdiction="TX", district_code=EXPECTED_API_CODE)
        # The 07c YAML has openHole, casing, casingOpenHole sections in plugging_chart
        assert "openHole" in district.plugging_chart or len(district.plugging_chart) > 0

    def test_command_is_idempotent(self):
        """Running the command twice does not create duplicate rows."""
        from apps.policy_ingest.models import DistrictOverlay, CountyOverlay

        call_command("ingest_district_overlays", jurisdiction="TX")
        district_count_after_first = DistrictOverlay.objects.filter(jurisdiction="TX").count()
        county_count_after_first = CountyOverlay.objects.filter(
            district_overlay__jurisdiction="TX"
        ).count()

        # Run again — must not raise and must not create duplicates
        call_command("ingest_district_overlays", jurisdiction="TX")
        assert DistrictOverlay.objects.filter(jurisdiction="TX").count() == district_count_after_first
        assert CountyOverlay.objects.filter(
            district_overlay__jurisdiction="TX"
        ).count() == county_count_after_first

    def test_jurisdiction_flag_limits_scope(self):
        """--jurisdiction TX only ingests TX districts, not NM."""
        from apps.policy_ingest.models import DistrictOverlay

        call_command("ingest_district_overlays", jurisdiction="TX")

        # No NM rows should have been created
        assert not DistrictOverlay.objects.filter(jurisdiction="NM").exists()
