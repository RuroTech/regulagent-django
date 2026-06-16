"""
TDD RED-PHASE tests for well_registry_enrichment bugs.

These tests define correct behavior for enrich_well_registry_from_documents() and
WellRegistrySerializer. They are written BEFORE the fix and are expected to FAIL
with current code.

Bug summary:
  1. FK join, not api_number string match — function requires caller to pass
     extracted_documents; it cannot self-fetch by FK. Missing `extracted_documents=None`
     default means the function won't auto-query by FK.
  2. county / district not populated from json_data well_info (not in field mapping).
  3. state not derived from api14 prefix when well.state is blank.
  4. NM c105 documents ignored because docs_by_type only accepts w2/w15/gau.
  5. WellRegistrySerializer has no `well_name` computed field.

Regression tests (expected to pass today — must stay green after the fix):
  - Existing non-empty fields must NOT be overwritten.
  - lat/lon populated within TX sanity range; out-of-range coords rejected.
  - TX w2/w15/gau fallback order (w2 wins over w15 wins over gau).
"""

import pytest
from decimal import Decimal
from unittest.mock import patch

from apps.public_core.models import WellRegistry, ExtractedDocument
from apps.public_core.services.well_registry_enrichment import (
    enrich_well_registry_from_documents,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def blank_tx_well(db):
    """TX WellRegistry with all enrichable fields blank."""
    return WellRegistry.objects.create(
        api14="42383396820000",
        state="TX",
        county="",
        district="",
        operator_name="",
        field_name="",
        lease_name="",
        well_number="",
    )


@pytest.fixture
def blank_nm_well(db):
    """NM WellRegistry with all enrichable fields blank."""
    return WellRegistry.objects.create(
        api14="30015288410000",
        state="NM",
        county="",
        district="",
        operator_name="",
        field_name="",
        lease_name="",
        well_number="",
    )


def _tx_json(
    operator="Sunset Petroleum LLC",
    county="Howard",
    district="8A",
    field="Spraberry (Trend Area)",
    lease="SMITH",
    well_no="1",
    lat=32.1,
    lon=-101.5,
):
    """Standard TX W-2 json_data shape from openai_extraction.py schema."""
    return {
        "operator_info": {
            "name": operator,
            "operator_number": "12345",
        },
        "well_info": {
            "api": "4238339682",
            "district": district,
            "county": county,
            "field": field,
            "lease": lease,
            "well_no": well_no,
            "location": {
                "lat": lat,
                "lon": lon,
            },
        },
    }


def _nm_json(
    operator="Desert Rose Energy",
    county="Eddy",
    lease="JONES",
    well_no="2",
):
    """Standard NM C-105 json_data shape from nm_extraction_mapper.py schema."""
    return {
        "operator_info": {
            "name": operator,
            "operator_number": "67890",
        },
        "well_info": {
            "api": "3001528841",
            "county": county,
            "field": "Permian",
            "lease": lease,
            "well_no": well_no,
            "well_name": f"{lease} #{well_no}",
            "location": {
                "lat": 32.3,
                "lon": -104.2,
            },
        },
    }


# ---------------------------------------------------------------------------
# 1. FK join — function auto-fetches documents via FK (not api_number match)
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_fk_join_auto_fetches_documents_when_not_passed(blank_tx_well):
    """
    enrich_well_registry_from_documents(well) with NO second argument must
    self-query ExtractedDocument.objects.filter(well=well, status='success')
    via the FK relationship, regardless of what api_number string is stored.

    Today the function signature is:
        enrich_well_registry_from_documents(well, extracted_documents: List[...]) -> bool
    There is no default=None, so calling it with one arg raises TypeError.
    Even if we add the default, the current code has no self-fetch path.

    The ED is linked via the `well` FK but has api_number in a DIFFERENT format
    (10-digit string vs 14-digit api14) — proving the lookup uses FK, not string.
    """
    ed = ExtractedDocument.objects.create(
        well=blank_tx_well,
        api_number="4238339682",          # 10-digit format — differs from api14
        document_type="w2",
        status="success",
        json_data=_tx_json(),
    )

    # Must work with a SINGLE argument — no document list passed
    result = enrich_well_registry_from_documents(blank_tx_well)

    blank_tx_well.refresh_from_db()
    assert result is True, "Should return True when fields were enriched"
    assert blank_tx_well.operator_name == "Sunset Petroleum LLC", (
        f"operator_name not populated via FK join; got {blank_tx_well.operator_name!r}"
    )
    assert blank_tx_well.lease_name == "SMITH", (
        f"lease_name not populated via FK join; got {blank_tx_well.lease_name!r}"
    )
    assert blank_tx_well.field_name == "Spraberry (Trend Area)", (
        f"field_name not populated; got {blank_tx_well.field_name!r}"
    )
    assert blank_tx_well.well_number == "1", (
        f"well_number not populated; got {blank_tx_well.well_number!r}"
    )


# ---------------------------------------------------------------------------
# 2. county populated from well_info.county
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_county_populated_from_well_info(blank_tx_well):
    """
    county must be read from json_data['well_info']['county'] and written to
    well.county.  Current _extract_with_fallback never reads the 'county' key.
    """
    ExtractedDocument.objects.create(
        well=blank_tx_well,
        api_number="4238339682",
        document_type="w2",
        status="success",
        json_data=_tx_json(county="Howard"),
    )

    enrich_well_registry_from_documents(blank_tx_well)

    blank_tx_well.refresh_from_db()
    assert blank_tx_well.county == "Howard", (
        f"county not populated from well_info.county; got {blank_tx_well.county!r}"
    )


# ---------------------------------------------------------------------------
# 3. district populated from well_info.district
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_district_populated_from_well_info(blank_tx_well):
    """
    district must be read from json_data['well_info']['district'] and written to
    well.district.  Current code never reads the 'district' key from json_data.
    """
    ExtractedDocument.objects.create(
        well=blank_tx_well,
        api_number="4238339682",
        document_type="w2",
        status="success",
        json_data=_tx_json(district="8A"),
    )

    enrich_well_registry_from_documents(blank_tx_well)

    blank_tx_well.refresh_from_db()
    assert blank_tx_well.district == "8A", (
        f"district not populated from well_info.district; got {blank_tx_well.district!r}"
    )


# ---------------------------------------------------------------------------
# 4. state derived from api14 prefix when well.state is blank
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_state_derived_from_api14_prefix_tx(db):
    """
    If well.state is blank, enrich should derive it from well.api14:
        prefix "42" → state "TX"
    """
    well = WellRegistry.objects.create(
        api14="42383396820000",
        state="",     # intentionally blank
        operator_name="",
        field_name="",
        lease_name="",
        well_number="",
    )
    ExtractedDocument.objects.create(
        well=well,
        api_number="4238339682",
        document_type="w2",
        status="success",
        json_data=_tx_json(),
    )

    enrich_well_registry_from_documents(well)

    well.refresh_from_db()
    assert well.state == "TX", (
        f"state should be derived as 'TX' from api14 prefix '42'; got {well.state!r}"
    )


@pytest.mark.django_db
def test_state_derived_from_api14_prefix_nm(db):
    """
    If well.state is blank, enrich should derive it from well.api14:
        prefix "30" → state "NM"
    """
    well = WellRegistry.objects.create(
        api14="30015288410000",
        state="",     # intentionally blank
        operator_name="",
        field_name="",
        lease_name="",
        well_number="",
    )
    ExtractedDocument.objects.create(
        well=well,
        api_number="3001528841",
        document_type="c105",
        status="success",
        json_data=_nm_json(),
    )

    enrich_well_registry_from_documents(well)

    well.refresh_from_db()
    assert well.state == "NM", (
        f"state should be derived as 'NM' from api14 prefix '30'; got {well.state!r}"
    )


# ---------------------------------------------------------------------------
# 5. NM c105 document enriches operator_name, lease_name, well_number, county
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_nm_c105_document_enriches_fields(blank_nm_well):
    """
    A document_type='c105' ED must be included in docs_by_type and used for
    enrichment.  Currently docs_by_type only accepts ['w2', 'w15', 'gau'],
    so c105 is silently dropped and no fields are populated.
    """
    ExtractedDocument.objects.create(
        well=blank_nm_well,
        api_number="3001528841",
        document_type="c105",
        status="success",
        json_data=_nm_json(
            operator="Desert Rose Energy",
            county="Eddy",
            lease="JONES",
            well_no="2",
        ),
    )

    result = enrich_well_registry_from_documents(blank_nm_well)

    blank_nm_well.refresh_from_db()
    assert result is True, "Should return True — c105 doc should have populated fields"
    assert blank_nm_well.operator_name == "Desert Rose Energy", (
        f"operator_name not enriched from c105; got {blank_nm_well.operator_name!r}"
    )
    assert blank_nm_well.lease_name == "JONES", (
        f"lease_name not enriched from c105; got {blank_nm_well.lease_name!r}"
    )
    assert blank_nm_well.well_number == "2", (
        f"well_number not enriched from c105; got {blank_nm_well.well_number!r}"
    )
    assert blank_nm_well.county == "Eddy", (
        f"county not enriched from c105; got {blank_nm_well.county!r}"
    )


# ---------------------------------------------------------------------------
# 6a. Regression: existing non-empty fields are NOT overwritten
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_existing_fields_not_overwritten(db):
    """
    Enrichment must only fill BLANK fields.  Pre-existing values must survive
    even when the document contains different data.
    """
    well = WellRegistry.objects.create(
        api14="42383396820000",
        state="TX",
        county="Existing County",
        district="7C",
        operator_name="Original Operator",
        field_name="Original Field",
        lease_name="Original Lease",
        well_number="99",
    )
    ExtractedDocument.objects.create(
        well=well,
        api_number="4238339682",
        document_type="w2",
        status="success",
        json_data=_tx_json(
            operator="New Operator",
            county="New County",
            district="8A",
            field="New Field",
            lease="NEW LEASE",
            well_no="1",
        ),
    )

    enrich_well_registry_from_documents(well)

    well.refresh_from_db()
    assert well.operator_name == "Original Operator", "operator_name must not be overwritten"
    assert well.county == "Existing County", "county must not be overwritten"
    assert well.district == "7C", "district must not be overwritten"
    assert well.field_name == "Original Field", "field_name must not be overwritten"
    assert well.lease_name == "Original Lease", "lease_name must not be overwritten"
    assert well.well_number == "99", "well_number must not be overwritten"


# ---------------------------------------------------------------------------
# 6b. Regression: lat/lon populated within TX sanity range; bad coords rejected
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_lat_lon_populated_within_tx_range(blank_tx_well):
    """
    Valid TX coordinates (lat ~32°N, lon ~-101°W) must be written to well.lat / well.lon.
    This must continue working after the fix.
    """
    ExtractedDocument.objects.create(
        well=blank_tx_well,
        api_number="4238339682",
        document_type="w2",
        status="success",
        json_data=_tx_json(lat=32.1, lon=-101.5),
    )

    enrich_well_registry_from_documents(blank_tx_well)

    blank_tx_well.refresh_from_db()
    assert blank_tx_well.lat is not None, "lat must be populated from valid TX coordinates"
    assert blank_tx_well.lon is not None, "lon must be populated from valid TX coordinates"
    assert abs(float(blank_tx_well.lat) - 32.1) < 0.001, (
        f"lat should be ~32.1, got {blank_tx_well.lat}"
    )
    assert abs(float(blank_tx_well.lon) - (-101.5)) < 0.001, (
        f"lon should be ~-101.5, got {blank_tx_well.lon}"
    )


@pytest.mark.django_db
def test_out_of_range_coords_rejected(blank_tx_well):
    """
    Coordinates outside TX sanity range (e.g. 0,0 or Europe) must be rejected.
    well.lat / well.lon must remain None.
    """
    ExtractedDocument.objects.create(
        well=blank_tx_well,
        api_number="4238339682",
        document_type="w2",
        status="success",
        json_data=_tx_json(lat=51.5, lon=-0.1),   # London
    )

    enrich_well_registry_from_documents(blank_tx_well)

    blank_tx_well.refresh_from_db()
    assert blank_tx_well.lat is None, "Out-of-range lat must NOT be stored"
    assert blank_tx_well.lon is None, "Out-of-range lon must NOT be stored"


# ---------------------------------------------------------------------------
# 6c. Regression: TX w2/w15/gau fallback order
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_w2_preferred_over_w15_over_gau(blank_tx_well):
    """
    When w2, w15, and gau docs all exist, w2 data wins for operator_name.
    Passing the list explicitly exercises the fallback without auto-fetch.
    """
    doc_gau = ExtractedDocument.objects.create(
        well=blank_tx_well,
        api_number="4238339682",
        document_type="gau",
        status="success",
        json_data=_tx_json(operator="GAU Operator", lease="GAU LEASE"),
    )
    doc_w15 = ExtractedDocument.objects.create(
        well=blank_tx_well,
        api_number="4238339682",
        document_type="w15",
        status="success",
        json_data=_tx_json(operator="W15 Operator", lease="W15 LEASE"),
    )
    doc_w2 = ExtractedDocument.objects.create(
        well=blank_tx_well,
        api_number="4238339682",
        document_type="w2",
        status="success",
        json_data=_tx_json(operator="W2 Operator", lease="W2 LEASE"),
    )

    # Explicit list — exercises fallback order regardless of FK auto-fetch
    enrich_well_registry_from_documents(blank_tx_well, [doc_gau, doc_w15, doc_w2])

    blank_tx_well.refresh_from_db()
    assert blank_tx_well.operator_name == "W2 Operator", (
        f"w2 should win over w15/gau; got {blank_tx_well.operator_name!r}"
    )
    assert blank_tx_well.lease_name == "W2 LEASE", (
        f"w2 lease_name should win; got {blank_tx_well.lease_name!r}"
    )


@pytest.mark.django_db
def test_w15_used_when_w2_absent(blank_tx_well):
    """When w2 is absent, w15 data is used."""
    doc_gau = ExtractedDocument.objects.create(
        well=blank_tx_well,
        api_number="4238339682",
        document_type="gau",
        status="success",
        json_data=_tx_json(operator="GAU Operator"),
    )
    doc_w15 = ExtractedDocument.objects.create(
        well=blank_tx_well,
        api_number="4238339682",
        document_type="w15",
        status="success",
        json_data=_tx_json(operator="W15 Operator"),
    )

    enrich_well_registry_from_documents(blank_tx_well, [doc_gau, doc_w15])

    blank_tx_well.refresh_from_db()
    assert blank_tx_well.operator_name == "W15 Operator", (
        f"w15 should win over gau when w2 absent; got {blank_tx_well.operator_name!r}"
    )


# ---------------------------------------------------------------------------
# 6d. Regression: only status='success' docs are used in auto-fetch
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_auto_fetch_excludes_non_success_documents(blank_tx_well):
    """
    When called with no document list, only status='success' EDs must be fetched.
    An 'error' status doc with good data must be ignored.
    """
    ExtractedDocument.objects.create(
        well=blank_tx_well,
        api_number="4238339682",
        document_type="w2",
        status="error",       # must be excluded
        json_data=_tx_json(operator="Error Doc Operator"),
    )

    result = enrich_well_registry_from_documents(blank_tx_well)

    blank_tx_well.refresh_from_db()
    assert result is False, "No fields enriched — only error doc present"
    assert blank_tx_well.operator_name == "", "Error-status doc must not supply data"


# ---------------------------------------------------------------------------
# 7. WellRegistrySerializer includes well_name computed from lease + well_number
# ---------------------------------------------------------------------------

@pytest.mark.django_db
def test_serializer_includes_well_name_field(db):
    """
    WellRegistrySerializer output must include a 'well_name' key composed from
    lease_name + well_number (e.g. lease_name='SMITH', well_number='1' → 'SMITH #1').

    Currently WellRegistrySerializer has no such field → the key is absent → FAILS.
    """
    from apps.public_core.serializers.well_registry import WellRegistrySerializer

    well = WellRegistry.objects.create(
        api14="42383396820001",
        state="TX",
        county="Howard",
        operator_name="Test Operator",
        field_name="Test Field",
        lease_name="SMITH",
        well_number="1",
    )

    data = WellRegistrySerializer(well).data

    assert "well_name" in data, (
        f"WellRegistrySerializer must include 'well_name'; keys present: {list(data.keys())}"
    )
    assert data["well_name"] == "SMITH #1", (
        f"well_name should be 'SMITH #1', got {data['well_name']!r}"
    )


@pytest.mark.django_db
def test_serializer_well_name_blank_when_no_lease(db):
    """
    When lease_name is blank, well_name must be blank (or at least not crash).
    """
    from apps.public_core.serializers.well_registry import WellRegistrySerializer

    well = WellRegistry.objects.create(
        api14="42383396820002",
        state="TX",
        county="Howard",
        operator_name="Test Operator",
        field_name="Test Field",
        lease_name="",
        well_number="",
    )

    data = WellRegistrySerializer(well).data

    assert "well_name" in data, (
        f"'well_name' key must exist even when lease blank; keys: {list(data.keys())}"
    )
    # When both blank, well_name should be blank (not "#" or "None")
    assert data["well_name"] in ("", None), (
        f"well_name should be blank when lease_name and well_number are empty; got {data['well_name']!r}"
    )


# ---------------------------------------------------------------------------
# 7. NM document types — the underscore/hyphen + all-types regression
#    (the original c105 test used 'c105' and would NOT have caught the prod
#     bug where the pipeline stores 'c_105' with an underscore)
# ---------------------------------------------------------------------------

@pytest.mark.django_db
@pytest.mark.parametrize("doc_type", ["c_105", "C-105", "c_103", "sundry"])
def test_nm_underscored_and_other_types_enrich(blank_nm_well, doc_type):
    """NM forms are stored with underscored/hyphenated types (c_105, C-105,
    c_103, sundry). All carry the operator_info/well_info schema and must
    enrich the registry — not just the bare 'c105' the whitelist used to allow."""
    ExtractedDocument.objects.create(
        well=blank_nm_well,
        api_number="3001528841",
        document_type=doc_type,
        status="success",
        json_data=_nm_json(operator="Desert Rose Energy", county="Eddy", lease="JONES", well_no="2"),
    )

    enrich_well_registry_from_documents(blank_nm_well)

    blank_nm_well.refresh_from_db()
    assert blank_nm_well.operator_name == "Desert Rose Energy", f"{doc_type} should set operator"
    assert blank_nm_well.county == "Eddy", f"{doc_type} should set county"
    assert blank_nm_well.lease_name == "JONES", f"{doc_type} should set lease"
    assert blank_nm_well.well_number == "2", f"{doc_type} should set well_number"


@pytest.mark.django_db
def test_nm_c101_flat_coordinates_populated(blank_nm_well):
    """NM C-101 stores coordinates as flat well_info.latitude/longitude rather
    than nested well_info.location.{lat,lon}. Those must still be picked up."""
    ExtractedDocument.objects.create(
        well=blank_nm_well,
        api_number="3001528841",
        document_type="c_101",
        status="success",
        json_data={
            "operator_info": {"name": "Desert Rose Energy"},
            "well_info": {
                "api": "3001528841",
                "county": "Eddy",
                "latitude": 32.4,
                "longitude": -104.1,
            },
        },
    )

    enrich_well_registry_from_documents(blank_nm_well)

    blank_nm_well.refresh_from_db()
    assert blank_nm_well.lat is not None and abs(float(blank_nm_well.lat) - 32.4) < 0.001
    assert blank_nm_well.lon is not None and abs(float(blank_nm_well.lon) - (-104.1)) < 0.001


@pytest.mark.django_db
def test_far_western_nm_coords_accepted(blank_nm_well):
    """San Juan basin (NW NM) longitudes (~-108°W) fall outside the old TX-only
    bound (-107.0) and were silently dropped. They must now be accepted."""
    ExtractedDocument.objects.create(
        well=blank_nm_well,
        api_number="3004500000",
        document_type="c_105",
        status="success",
        json_data={
            "operator_info": {"name": "San Juan Gas Co"},
            "well_info": {"api": "3004500000", "county": "San Juan",
                          "location": {"lat": 36.7, "lon": -108.2}},
        },
    )

    enrich_well_registry_from_documents(blank_nm_well)

    blank_nm_well.refresh_from_db()
    assert blank_nm_well.lon is not None and abs(float(blank_nm_well.lon) - (-108.2)) < 0.001


@pytest.mark.django_db
def test_raw_text_only_doc_ignored_gracefully(blank_nm_well):
    """A raw-text-only doc (e.g. c_104 with no extraction prompt) has no
    operator_info/well_info. Enrichment must no-op on it without error and
    still use a sibling structured doc."""
    ExtractedDocument.objects.create(
        well=blank_nm_well,
        api_number="3001528841",
        document_type="c_104",
        status="success",
        json_data={"_raw_text": "CHANGE OF OPERATOR ... noisy OCR text ..."},
    )
    ExtractedDocument.objects.create(
        well=blank_nm_well,
        api_number="3001528841",
        document_type="c_105",
        status="success",
        json_data=_nm_json(operator="Desert Rose Energy", county="Eddy"),
    )

    enrich_well_registry_from_documents(blank_nm_well)

    blank_nm_well.refresh_from_db()
    assert blank_nm_well.operator_name == "Desert Rose Energy"
    assert blank_nm_well.county == "Eddy"
