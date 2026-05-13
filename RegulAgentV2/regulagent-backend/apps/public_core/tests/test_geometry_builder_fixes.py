"""
TDD Failing Tests — Well Geometry Builder Bug Fixes

These tests define expected behavior for two bugs in well_geometry_builder.py:

Bug 3 — Formation depths from user input ignored when component resolver runs:
    build_well_geometry() takes a payload with formation tops containing correct
    user-provided top_ft depths, but if WellComponent records exist, it calls
    build_well_geometry_from_components() and only falls back to payload formations
    when comp_geometry["formation_tops"] is EMPTY. If the resolver returns stale
    depths, the user-provided payload depths are silently discarded.
    Expected: payload formations with actual top_ft values override the resolver's
    formation tops when both are present.

Bug 4 — Liner missing top-of-string depth (no normalization):
    The legacy W-2 path in build_well_geometry() assigns liner_record directly to
    geometry["liner"] without running it through normalize_casing_for_frontend().
    If the extractor used non-canonical field names ("top" instead of "top_ft",
    "shoe_depth_ft" instead of "bottom_ft"), the liner data ends up with wrong
    or missing field names.
    Expected: liner records must always have canonical top_ft and bottom_ft keys.

These tests MUST fail on the current codebase (no implementation yet).
BE2 implements the fix ONLY after these tests are confirmed failing.
"""

import pytest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Bug 3 — Payload formation tops ignored when component resolver is present
# ---------------------------------------------------------------------------

class TestPayloadFormationTopsOverrideComponentResolver:
    """Bug 3: user-supplied payload depths must win over stale component resolver data."""

    def test_payload_formation_tops_override_component_resolver(self):
        """
        When WellComponent records exist for a well AND the component resolver returns
        stale depths, build_well_geometry() must still use the correct depths from the
        payload.

        Current buggy behaviour:
            if not comp_geometry.get("formation_tops"):
                ...
        This means: if the resolver returned ANY formation tops (even wrong depths),
        the payload's tops are ignored entirely.

        Expected fix: payload formations with actual top_ft values override the
        resolver's formation tops.

        CURRENTLY FAILS because the condition only fills in payload formations when
        comp_geometry["formation_tops"] is falsy (empty list), not when they exist
        with wrong depths.
        """
        from apps.public_core.services.well_geometry_builder import build_well_geometry

        # Component resolver returns "wrong" depths (stale public data)
        stale_geometry = {
            "casing_strings": [],
            "formation_tops": [
                {"formation": "Queen", "top_ft": 4950.0},    # WRONG depth
                {"formation": "Carlsbad", "top_ft": 4100.0}, # WRONG depth
            ],
            "liner": [],
            "perforations": [],
            "production_perforations": [],
            "tubing": [],
            "historic_cement_jobs": [],
            "mechanical_equipment": [],
            "existing_tools": [],
        }

        # User-provided payload with CORRECT depths
        payload = {
            "formations": [
                {"formation_name": "Queen", "top_ft": 2800.0},    # correct
                {"formation_name": "Carlsbad", "top_ft": 2500.0}, # correct
            ]
        }

        # Simulate: well has WellComponent records (count > 0).
        # build_well_geometry() does a local import inside a try block:
        #   from apps.public_core.models import WellComponent
        #   from apps.public_core.services.component_resolver import (
        #       build_well_geometry_from_components,
        #   )
        # Since these are local (inside-function) imports that run on every call,
        # patching the original module attribute is sufficient — Python's import
        # machinery will see the patched version when the local import executes.
        with patch(
            "apps.public_core.models.WellComponent"
        ) as MockWC, patch(
            "apps.public_core.services.component_resolver.build_well_geometry_from_components",
            return_value=stale_geometry,
        ):
            MockWC.objects.filter.return_value.count.return_value = 5  # has components

            result = build_well_geometry("30015288410000", payload=payload)

        tops = {f["formation"]: f["top_ft"] for f in result.get("formation_tops", [])}
        assert tops.get("Queen") == 2800.0, (
            f"Expected Queen top_ft=2800.0 from payload but got {tops.get('Queen')}. "
            "Payload formation depths must override stale component resolver data. "
            "Bug 3: the current code only fills in payload tops when the resolver "
            "returned an EMPTY list, so non-empty stale data is kept."
        )
        assert tops.get("Carlsbad") == 2500.0, (
            f"Expected Carlsbad top_ft=2500.0 from payload but got {tops.get('Carlsbad')}."
        )

    def test_payload_formation_tops_not_overridden_when_no_top_ft(self):
        """
        When the payload has formations WITHOUT top_ft values (None), the component
        resolver's depths should be kept — the override only applies when the payload
        has actual depth values.

        This test documents the edge case: payload formations with top_ft=None
        must NOT overwrite good resolver data.

        This test is expected to PASS both before and after the fix (documents
        existing correct behaviour for the None case). Included for contract clarity.
        """
        from apps.public_core.services.well_geometry_builder import build_well_geometry

        # Component resolver returns good depths
        good_geometry = {
            "casing_strings": [],
            "formation_tops": [
                {"formation": "Queen", "top_ft": 2800.0},
            ],
            "liner": [],
            "perforations": [],
            "production_perforations": [],
            "tubing": [],
            "historic_cement_jobs": [],
            "mechanical_equipment": [],
            "existing_tools": [],
        }

        # Payload has formation names but no depth values
        payload = {
            "formations": [
                {"formation_name": "Queen", "top_ft": None},  # no depth available
            ]
        }

        with patch(
            "apps.public_core.models.WellComponent"
        ) as MockWC, patch(
            "apps.public_core.services.component_resolver.build_well_geometry_from_components",
            return_value=good_geometry,
        ):
            MockWC.objects.filter.return_value.count.return_value = 3

            result = build_well_geometry("30015288410000", payload=payload)

        tops = {f["formation"]: f["top_ft"] for f in result.get("formation_tops", [])}
        # Resolver's good depth must be kept when payload has None
        assert tops.get("Queen") == 2800.0, (
            f"Resolver depth 2800.0 must be preserved when payload top_ft is None; "
            f"got {tops.get('Queen')}"
        )


# ---------------------------------------------------------------------------
# Bug 4 — Liner record not normalised (missing top_ft / bottom_ft)
# ---------------------------------------------------------------------------

class TestLinerRecordNormalization:
    """Bug 4: liner records from W-2 must go through normalize_casing_for_frontend()."""

    def test_normalize_casing_for_frontend_maps_top_and_shoe_depth(self):
        """
        normalize_casing_for_frontend() must map:
          "top"           → "top_ft"
          "shoe_depth_ft" → "bottom_ft"

        This tests the helper directly. Looking at the current implementation,
        normalize_casing_for_frontend() does map these fields:
          top_ft  = _first(c.get("top_ft"), c.get("top"))
          bottom_ft = _first(c.get("shoe_depth_ft"), c.get("bottom_ft"), c.get("bottom"))

        So this particular test may already PASS. Its purpose is to pin the contract
        so that any regression to the normalize helper is caught immediately.
        If this test passes but test_build_well_geometry_normalizes_liner_record fails,
        it confirms the bug is in the build_well_geometry call path (liner_record
        is NOT routed through normalize_casing_for_frontend).
        """
        from apps.public_core.services.well_geometry_builder import normalize_casing_for_frontend

        # Simulate W-2 extraction output with non-canonical field names
        liner_raw = [
            {
                "string_type": "liner",
                "size_in": 7.0,
                "top": 3500.0,            # uses "top" not "top_ft"
                "shoe_depth_ft": 4800.0,  # uses "shoe_depth_ft" not "bottom_ft"
                "cement_top_ft": 3500.0,
            }
        ]

        normalized = normalize_casing_for_frontend(liner_raw)
        assert len(normalized) == 1, f"Expected 1 normalized record, got {len(normalized)}"
        liner = normalized[0]
        assert liner["top_ft"] == 3500.0, (
            f"Expected top_ft=3500.0 (mapped from 'top') but got {liner.get('top_ft')}"
        )
        assert liner["bottom_ft"] == 4800.0, (
            f"Expected bottom_ft=4800.0 (mapped from 'shoe_depth_ft') but got {liner.get('bottom_ft')}"
        )

    def test_build_well_geometry_normalizes_liner_record(self):
        """
        build_well_geometry() must route the liner_record from W-2 through
        normalize_casing_for_frontend() so that canonical top_ft / bottom_ft
        keys are always present.

        CURRENTLY FAILS because the legacy W-2 path does:
            liner_record = w2.json_data.get('liner_record', [])
            if liner_record:
                geometry['liner'] = liner_record   # <-- raw, not normalized!

        The liner data is stored directly without normalization, so if the extractor
        produced "top" and "shoe_depth_ft" instead of "top_ft" and "bottom_ft",
        the frontend receives records with wrong / missing field names.
        """
        from apps.public_core.services.well_geometry_builder import build_well_geometry

        # Mock W-2 ExtractedDocument with a liner that has non-canonical field names
        mock_w2 = MagicMock()
        mock_w2.json_data = {
            "liner_record": [
                {
                    "string_type": "liner",
                    "size_in": 7.0,
                    "top": 3500.0,           # non-canonical: "top" not "top_ft"
                    "shoe_depth_ft": 4800.0, # non-canonical: "shoe_depth_ft" not "bottom_ft"
                }
            ],
            "casing_record": [],
            "formation_record": [],
        }

        # WellComponent is imported locally inside build_well_geometry(), so patch
        # at the source model location. ExtractedDocument is imported at module level.
        with patch(
            "apps.public_core.models.WellComponent"
        ) as MockWC, patch(
            "apps.public_core.services.well_geometry_builder.ExtractedDocument"
        ) as MockDoc:
            # No WellComponent records → falls back to legacy path
            MockWC.objects.filter.return_value.count.return_value = 0

            # W-2 lookup returns our mock; W-15 and C-105 lookups return None
            def _doc_filter_side_effect(*args, **kwargs):
                mock_qs = MagicMock()
                # Use the document_type kwarg to decide what to return
                doc_type = kwargs.get("document_type", "")
                if doc_type == "w2":
                    mock_qs.first.return_value = mock_w2
                else:
                    mock_qs.first.return_value = None
                    mock_qs.order_by.return_value.first.return_value = None
                return mock_qs

            MockDoc.objects.filter.side_effect = _doc_filter_side_effect

            result = build_well_geometry("30015288410000")

        liners = result.get("liner", [])
        assert len(liners) == 1, (
            f"Expected 1 liner record, got {len(liners)}. "
            "The liner_record must not be filtered out or lost during geometry build."
        )
        liner = liners[0]
        assert liner.get("top_ft") == 3500.0, (
            f"Expected liner top_ft=3500.0 after normalization but got {liner.get('top_ft')}. "
            "Bug 4: liner_record is assigned to geometry['liner'] WITHOUT being run "
            "through normalize_casing_for_frontend(). The 'top' field is never mapped "
            "to 'top_ft', so the frontend receives a record with no top depth."
        )
        assert liner.get("bottom_ft") == 4800.0, (
            f"Expected liner bottom_ft=4800.0 (mapped from shoe_depth_ft) "
            f"but got {liner.get('bottom_ft')}. "
            "normalize_casing_for_frontend() maps shoe_depth_ft → bottom_ft, "
            "but this mapping is never applied to liner_record in the legacy path."
        )

    def test_build_well_geometry_liner_from_casing_strings_is_normalized(self):
        """
        When liners are embedded in casing_strings (not liner_record), the existing
        code separates them and they should already have canonical field names because
        they come from the payload or component path. This test confirms that path
        does NOT regress after the Bug 4 fix.

        Expected to PASS both before and after fix.
        """
        from apps.public_core.services.well_geometry_builder import build_well_geometry

        # Payload supplies a liner via casing_strings with canonical names
        payload = {
            "casing_strings": [
                {
                    "string_type": "surface",
                    "size_in": 13.375,
                    "top_ft": 0.0,
                    "bottom_ft": 2000.0,
                },
                {
                    "string_type": "liner",
                    "size_in": 7.0,
                    "top_ft": 3500.0,      # canonical field name
                    "bottom_ft": 4800.0,   # canonical field name
                },
            ]
        }

        with patch(
            "apps.public_core.models.WellComponent"
        ) as MockWC, patch(
            "apps.public_core.services.well_geometry_builder.ExtractedDocument"
        ) as MockDoc:
            MockWC.objects.filter.return_value.count.return_value = 0
            MockDoc.objects.filter.return_value.first.return_value = None
            MockDoc.objects.filter.return_value.order_by.return_value.first.return_value = None

            result = build_well_geometry("30015288410000", payload=payload)

        liners = result.get("liner", [])
        assert len(liners) == 1, (
            f"Expected the liner from casing_strings to be separated into liner[]; "
            f"got {len(liners)} liner(s)"
        )
        liner = liners[0]
        # Already canonical — should pass before and after fix
        assert liner.get("top_ft") == 3500.0 or liner.get("top_ft") is not None, (
            f"Liner top_ft should be preserved; got {liner.get('top_ft')}"
        )
