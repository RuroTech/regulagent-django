"""
Regression tests for POST /api/w3/build-from-pna/ hotfix (2026-06-08).

Three bugs were fixed that caused BuildW3FromPNAResponseSerializer.is_valid()
to return False and produce a 500 response:

  Bug 1 (casing key): format_casing_record() emitted `od_in` instead of
      `size_in`. CasingRowSerializer.size_in is a non-nullable FloatField so
      any payload without it always failed validation.

  Bug 2 (perforation date format): format_perforations() emitted dates as
      "%m/%d/%y" (e.g. "01/15/25"). PerforationRowSerializer.perforation_date
      is a DateField which only accepts ISO-8601 ("2025-01-15"), so every
      PNA-sourced perforation invalidated the response.

  Bug 3 (pdf_url field type): W3FormOutputSerializer.pdf_url was declared as
      URLField, which rejects relative paths like "/media/temp_pdfs/foo.pdf".
      Changed to CharField(allow_null=True, allow_blank=True, required=False).

Each test below is labelled with the bug it guards. Tests are pure unit tests
(serializer/formatter level) and do NOT require a database — no @pytest.mark.django_db.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest


# ---------------------------------------------------------------------------
# Helpers — minimal representative data shapes
# ---------------------------------------------------------------------------

def _minimal_header() -> dict:
    return {
        "api_number": "4250170575",
        "well_name": "Test Well",
        "operator": "Test Op",
        "county": "Reeves",
        "rrc_district": "7C",
        "field": "PERMIAN",
        "total_depth_ft": 10000.0,
    }


def _minimal_plug() -> dict:
    return {
        "plug_number": 1,
        "depth_top_ft": 200.0,
        "depth_bottom_ft": 500.0,
        "type": "cement_plug",
        "cement_class": "H",
        "sacks": 50.0,
        "slurry_weight_ppg": 14.8,
        "hole_size_in": 5.5,
        "top_of_plug_ft": 220.0,
        "measured_top_of_plug_ft": None,
        "calculated_top_of_plug_ft": 220.0,
        "toc_variance_ft": None,
        "remarks": "",
    }


def _minimal_casing() -> dict:
    """Casing dict as format_casing_record() now emits (post-fix)."""
    return {
        "string_type": "surface",
        "size_in": 13.375,       # Bug 1 fixed: was "od_in"
        "weight_ppf": 54.5,
        "hole_size_in": 17.5,
        "top_ft": 0.0,
        "bottom_ft": 1717.0,
        "shoe_depth_ft": 1717.0,
        "cement_top_ft": 930.0,
        "removed_to_depth_ft": None,
    }


def _minimal_perforation_iso() -> dict:
    """Perforation dict as format_perforations() now emits (post-fix)."""
    return {
        "interval_top_ft": 8110.0,
        "interval_bottom_ft": 8200.0,
        "formation": "Spraberry",
        "status": "perforated",
        "perforation_date": "2025-01-15",   # Bug 2 fixed: was "01/15/25"
    }


def _minimal_duqw() -> dict:
    return {
        "depth_ft": 400.0,
        "formation": "Ogallala",
        "determination_method": "log",
    }


def _minimal_validation() -> dict:
    return {"warnings": [], "errors": []}


def _minimal_metadata() -> dict:
    return {
        "api_number": "4250170575",
        "subproject_id": 1,
        "dwr_id": None,
        "events_processed": 1,
        "plugs_grouped": 1,
        "generated_at": "2025-01-15T12:00:00Z",
    }


def _minimal_w3_form(pdf_url=None) -> dict:
    return {
        "header": _minimal_header(),
        "plugs": [_minimal_plug()],
        "casing_record": [_minimal_casing()],
        "perforations": [_minimal_perforation_iso()],
        "duqw": _minimal_duqw(),
        "remarks": "",
        "pdf_url": pdf_url,
    }


def _minimal_response(pdf_url=None) -> dict:
    return {
        "success": True,
        "w3_form": _minimal_w3_form(pdf_url=pdf_url),
        "w3a_well_geometry": None,
        "error": "",
        "validation": _minimal_validation(),
        "metadata": _minimal_metadata(),
    }


# ---------------------------------------------------------------------------
# Bug 1 — Casing key: od_in → size_in
# ---------------------------------------------------------------------------

class TestCasingKeyRegression:
    """Guards Bug 1: format_casing_record emitted od_in; CasingRowSerializer needs size_in."""

    def test_casing_with_size_in_validates(self):
        """A casing dict with the correct `size_in` key must pass CasingRowSerializer."""
        from apps.public_core.serializers.w3_from_pna import CasingRowSerializer

        data = _minimal_casing()
        assert "size_in" in data, "Test fixture must use size_in (not od_in)"

        s = CasingRowSerializer(data=data)
        assert s.is_valid(), f"Expected valid but got errors: {s.errors}"

    def test_casing_with_od_in_only_fails(self):
        """A casing dict with od_in but NO size_in must FAIL CasingRowSerializer.
        This is the pre-fix behaviour — confirms the test would have caught Bug 1."""
        from apps.public_core.serializers.w3_from_pna import CasingRowSerializer

        data = _minimal_casing()
        # Simulate pre-fix formatter output: key was 'od_in', not 'size_in'
        data["od_in"] = data.pop("size_in")
        assert "size_in" not in data

        s = CasingRowSerializer(data=data)
        assert not s.is_valid(), "Should fail when size_in is missing (pre-fix scenario)"
        assert "size_in" in s.errors

    def test_format_casing_record_emits_size_in(self):
        """format_casing_record() must emit 'size_in' (not 'od_in') for each casing string."""
        from apps.public_core.services.w3_formatter import format_casing_record

        # Input uses the raw W-3A field name 'od_in' (what the extraction gives us)
        raw_casing = [
            {
                "string_type": "surface",
                "od_in": 13.375,
                "weight_ppf": 54.5,
                "hole_size_in": 17.5,
                "top_ft": 0.0,
                "bottom_ft": 1717.0,
                "shoe_depth_ft": 1717.0,
                "cement_top_ft": 930.0,
            }
        ]

        result = format_casing_record(raw_casing, casing_state=[])

        assert len(result) == 1
        row = result[0]
        assert "size_in" in row, "format_casing_record must emit 'size_in' key"
        assert row["size_in"] == 13.375


# ---------------------------------------------------------------------------
# Bug 2 — Perforation date format: MM/DD/YY → YYYY-MM-DD (ISO-8601)
# ---------------------------------------------------------------------------

class TestPerforationDateRegression:
    """Guards Bug 2: perforation_date was strftime('%m/%d/%y'), DateField needs ISO-8601."""

    def test_iso_date_validates(self):
        """ISO-8601 perforation_date must pass PerforationRowSerializer."""
        from apps.public_core.serializers.w3_from_pna import PerforationRowSerializer

        data = _minimal_perforation_iso()
        s = PerforationRowSerializer(data=data)
        assert s.is_valid(), f"Expected valid for ISO date but got: {s.errors}"

    def test_old_date_format_fails(self):
        """MM/DD/YY perforation_date must FAIL PerforationRowSerializer.
        This is the pre-fix behaviour — confirms the test would have caught Bug 2."""
        from apps.public_core.serializers.w3_from_pna import PerforationRowSerializer

        data = _minimal_perforation_iso()
        data["perforation_date"] = "01/15/25"   # pre-fix strftime("%m/%d/%y") output

        s = PerforationRowSerializer(data=data)
        assert not s.is_valid(), "Should fail for non-ISO date (pre-fix scenario)"
        assert "perforation_date" in s.errors

    def test_format_perforations_emits_iso_date(self):
        """format_perforations() must emit ISO-8601 dates from PNA perforation events."""
        from apps.public_core.services.w3_formatter import format_perforations
        from apps.public_core.serializers.w3_from_pna import PerforationRowSerializer

        # Use a minimal stub — format_perforations only reads these attributes
        perf_event = SimpleNamespace(
            event_type="perforate",
            date=date(2025, 1, 15),
            perf_depth_ft=8110.0,
            depth_top_ft=None,
            depth_bottom_ft=8200.0,
        )

        result = format_perforations(w3a_perforations=[], w3_events=[perf_event])

        assert len(result) == 1
        row = result[0]
        assert row["perforation_date"] == "2025-01-15", (
            f"Expected ISO-8601 date '2025-01-15', got '{row['perforation_date']}'"
        )

        # The emitted row must also pass the serializer (double-check)
        s = PerforationRowSerializer(data=row)
        assert s.is_valid(), f"format_perforations output failed serializer: {s.errors}"


# ---------------------------------------------------------------------------
# Bug 3 — pdf_url: URLField rejects relative paths → CharField
# ---------------------------------------------------------------------------

class TestPdfUrlRegression:
    """Guards Bug 3: pdf_url was URLField, rejecting relative /media/... paths."""

    def test_relative_pdf_url_validates(self):
        """A relative /media/... path must pass W3FormOutputSerializer."""
        from apps.public_core.serializers.w3_from_pna import W3FormOutputSerializer

        data = _minimal_w3_form(pdf_url="/media/temp_pdfs/w3_123.pdf")
        s = W3FormOutputSerializer(data=data)
        assert s.is_valid(), (
            f"Relative pdf_url should be valid but got errors: {s.errors}"
        )

    def test_null_pdf_url_validates(self):
        """A null pdf_url must also pass W3FormOutputSerializer (optional field)."""
        from apps.public_core.serializers.w3_from_pna import W3FormOutputSerializer

        data = _minimal_w3_form(pdf_url=None)
        s = W3FormOutputSerializer(data=data)
        assert s.is_valid(), f"Null pdf_url should be valid but got: {s.errors}"

    def test_blank_pdf_url_validates(self):
        """An empty-string pdf_url must pass W3FormOutputSerializer."""
        from apps.public_core.serializers.w3_from_pna import W3FormOutputSerializer

        data = _minimal_w3_form(pdf_url="")
        s = W3FormOutputSerializer(data=data)
        assert s.is_valid(), f"Blank pdf_url should be valid but got: {s.errors}"

    def test_absolute_url_still_validates(self):
        """A full https:// URL must also pass (regression-safe, was already working)."""
        from apps.public_core.serializers.w3_from_pna import W3FormOutputSerializer

        data = _minimal_w3_form(pdf_url="https://example.com/media/w3_123.pdf")
        s = W3FormOutputSerializer(data=data)
        assert s.is_valid(), f"Absolute URL should be valid but got: {s.errors}"


# ---------------------------------------------------------------------------
# Full round-trip — the exact guard for the 500
# ---------------------------------------------------------------------------

class TestBuildW3FromPNAResponseRoundTrip:
    """Guards all three bugs via the exact serializer that was raising is_valid()==False."""

    def test_complete_response_validates(self):
        """BuildW3FromPNAResponseSerializer must accept a fully-formed success response.

        This is the highest-value regression guard: it mirrors exactly what
        w3_from_pna.py does with `BuildW3FromPNAResponseSerializer(data=result).is_valid()`.
        Before the hotfix this returned False (and the view raised a 500).
        """
        from apps.public_core.serializers.w3_from_pna import BuildW3FromPNAResponseSerializer

        result = _minimal_response(pdf_url="/media/temp_pdfs/w3_test.pdf")
        s = BuildW3FromPNAResponseSerializer(data=result)
        assert s.is_valid(), (
            f"BuildW3FromPNAResponseSerializer.is_valid() returned False — "
            f"this is the 500 guard. Errors: {s.errors}"
        )

    def test_response_with_null_pdf_url_validates(self):
        """Round-trip also valid when pdf_url is absent/null (common case)."""
        from apps.public_core.serializers.w3_from_pna import BuildW3FromPNAResponseSerializer

        result = _minimal_response(pdf_url=None)
        s = BuildW3FromPNAResponseSerializer(data=result)
        assert s.is_valid(), (
            f"Null pdf_url round-trip failed: {s.errors}"
        )

    def test_response_with_multiple_casings_and_perfs_validates(self):
        """Round-trip with multiple casing strings and perforations (realistic payload)."""
        from apps.public_core.serializers.w3_from_pna import BuildW3FromPNAResponseSerializer

        result = _minimal_response(pdf_url="/media/temp_pdfs/w3_test.pdf")
        # Add a second casing string and an extra perforation
        result["w3_form"]["casing_record"].append({
            "string_type": "production",
            "size_in": 5.5,
            "weight_ppf": 17.0,
            "hole_size_in": 7.875,
            "top_ft": 0.0,
            "bottom_ft": 9500.0,
            "shoe_depth_ft": 9500.0,
            "cement_top_ft": 4200.0,
            "removed_to_depth_ft": None,
        })
        result["w3_form"]["perforations"].append({
            "interval_top_ft": 9100.0,
            "interval_bottom_ft": 9200.0,
            "formation": None,
            "status": "squeezed",
            "perforation_date": "2024-11-03",
        })

        s = BuildW3FromPNAResponseSerializer(data=result)
        assert s.is_valid(), (
            f"Multi-casing/perf round-trip failed: {s.errors}"
        )
