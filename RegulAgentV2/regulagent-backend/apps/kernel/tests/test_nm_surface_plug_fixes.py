"""
TDD Failing Tests — NM Surface Plug Bug Fixes

These tests define expected behavior for two bugs in C103PluggingRules:

Bug 1 — Surface plug depth not configurable:
    _generate_surface_plug() always uses _SURFACE_PLUG_BOTTOM_FT = 50.0 regardless
    of options passed to generate_plugging_plan(). Expected: when
    options={"surface_plug_bottom_ft": 150} is passed, the generated surface plug
    must have bottom_ft == 150.

Bug 2 — Surface plug sack count wrong (wrong casing lookup + wrong volume formula):
    Root cause A: _generate_surface_plug() looks up c.get("type") == "surface" but
    casing records use the "string_type" field, so it always defaults to 13.375" OD.
    Root cause B: _calculate_volumes() for "circulate" operation adds annular volume
    behind casing (between casing OD and borehole wall), which is already cemented
    in P&A. Only inside-casing volume should be counted for surface plugs.

These tests MUST fail on the current codebase (no implementation yet).
BE2 implements the fix ONLY after these tests are confirmed failing.

NMAC 19.15.25 reference — surface plug is mandatory, min 25 sacks, 30-min static obs.
"""

import pytest

from apps.kernel.services.c103_rules import C103PluggingRules


# ---------------------------------------------------------------------------
# Minimal well fixture — surface-only casing, no perfs, no formations.
# Designed to produce exactly ONE plug: the surface plug.
# ---------------------------------------------------------------------------

def _minimal_well(size_in: float = 10.75, casing_depth_ft: float = 3000.0) -> dict:
    """Return a minimal NM well dict that exercises only the surface plug path."""
    return {
        "api_number": "30015288410000",
        # No county → region detection falls back to 'north', which has no mandatory
        # formation plugs for a well with no formation_tops, so only the surface
        # plug and the shoe plug for the surface casing will be generated.
        "casing_strings": [
            {
                # Correct field name per all existing tests and W-2 extraction:
                # "string_type", NOT "type"
                "string_type": "surface",
                "size_in": size_in,
                "depth_ft": casing_depth_ft,
                "shoe_depth_ft": casing_depth_ft,
            }
        ],
        "perforations": [],
        "formation_tops": [],
    }


# ---------------------------------------------------------------------------
# Bug 1 — surface_plug_bottom_ft option is ignored
# ---------------------------------------------------------------------------

class TestSurfacePlugDepthConfigurable:
    """Bug 1: generate_plugging_plan() must honour the surface_plug_bottom_ft option."""

    def test_surface_plug_depth_configurable(self):
        """
        When options={"surface_plug_bottom_ft": 150} is passed, the generated
        surface plug must have top_ft == 0 and bottom_ft == 150.

        CURRENTLY FAILS because _generate_surface_plug() always uses the module-level
        _SURFACE_PLUG_BOTTOM_FT = 50.0 constant and never reads the options dict.
        """
        rules = C103PluggingRules()
        well = _minimal_well()
        options = {"surface_plug_bottom_ft": 150}

        plan = rules.generate_plugging_plan(well, options)

        surface_plugs = [s for s in plan.steps if s.step_type == "surface_plug"]
        assert len(surface_plugs) == 1, (
            f"Expected exactly 1 surface plug, got {len(surface_plugs)}"
        )
        sp = surface_plugs[0]
        assert sp.top_ft == 0, f"Expected top_ft=0 but got {sp.top_ft}"
        assert sp.bottom_ft == 150, (
            f"Expected bottom_ft=150 (from options) but got {sp.bottom_ft}. "
            "Bug 1: _generate_surface_plug() ignores the surface_plug_bottom_ft option "
            "and always uses the _SURFACE_PLUG_BOTTOM_FT=50.0 module constant."
        )

    def test_surface_plug_depth_default_unchanged_without_option(self):
        """
        When no surface_plug_bottom_ft option is given, the surface plug should
        still use the default 50.0 ft depth. This sanity-checks that the fix
        doesn't break the default path.

        This test should PASS even before the fix (current behavior uses 50 ft).
        It is included here to document the contract: default must remain 50 ft.
        """
        rules = C103PluggingRules()
        well = _minimal_well()

        plan = rules.generate_plugging_plan(well, {})

        surface_plugs = [s for s in plan.steps if s.step_type == "surface_plug"]
        assert len(surface_plugs) == 1
        sp = surface_plugs[0]
        assert sp.top_ft == 0
        assert sp.bottom_ft == 50.0, (
            f"Default bottom_ft should be 50.0 ft; got {sp.bottom_ft}"
        )


# ---------------------------------------------------------------------------
# Bug 2 — wrong casing lookup + annular volume included for surface plug
# ---------------------------------------------------------------------------

class TestSurfacePlugSacksCorrectCasingAndVolume:
    """Bug 2: surface plug sacks must use the correct casing ID and inside-only volume."""

    def test_surface_plug_sacks_uses_correct_casing_and_inside_only_volume(self):
        """
        For a 10.75" surface casing, a 50 ft surface plug should require
        approximately 25-40 sacks (inside-casing volume only, 50% excess).

        Math for 10.75" OD:
          ID ≈ 9.95" (wall = 0.400")
          inside_area = π/4 × (9.95/12)² ≈ 0.540 ft²
          vol = 0.540 × 50 × 1.50 (50% excess) ≈ 40.5 ft³
          sacks = 40.5 / 1.32 ≈ 30.7 → 31 sacks  (well above 25-sack minimum)

        CURRENTLY FAILS because two bugs combine to inflate the count to ~89 sacks:
          A) _generate_surface_plug() uses c.get("type") == "surface" instead of
             c.get("string_type") == "surface", so it finds NO surface casing and
             defaults to size_in = 13.375 (OD ~13.375", ID ~12.515").
          B) _calculate_volumes() for the "circulate" operation adds annular volume
             (between casing OD and borehole wall), which is already cemented and
             should NOT be pumped again for a surface plug.
        """
        rules = C103PluggingRules()
        # Use 10.75" casing with string_type (correct field name)
        well = _minimal_well(size_in=10.75)

        plan = rules.generate_plugging_plan(well, {})

        surface_plugs = [s for s in plan.steps if s.step_type == "surface_plug"]
        assert len(surface_plugs) == 1, (
            f"Expected exactly 1 surface plug, got {len(surface_plugs)}"
        )
        sp = surface_plugs[0]

        # Should be 25-40 sacks using 10.75" casing interior only
        # Current buggy behavior gives ~89 (13.375" default OD + annular volume)
        assert sp.sacks_required >= 25, (
            f"Must meet NM minimum 25 sacks per plug; got {sp.sacks_required}"
        )
        assert sp.sacks_required <= 40, (
            f"Expected ≤40 sacks for 10.75\" surface casing (inside-only, 50% excess), "
            f"got {sp.sacks_required}. "
            "If sacks ≈ 89, BOTH bugs are present: "
            "(A) wrong casing lookup defaulted to 13.375\" OD instead of 10.75\", AND "
            "(B) annular volume included in circulate calculation."
        )

    def test_surface_plug_casing_size_reflects_actual_surface_casing(self):
        """
        After generation, the surface plug's casing_size_in must reflect
        the actual surface casing OD (10.75"), not the 13.375" fallback.

        CURRENTLY FAILS because casing lookup uses the wrong field ("type" vs
        "string_type"), so surface_casing is None and size_in defaults to 13.375.
        """
        rules = C103PluggingRules()
        well = _minimal_well(size_in=10.75)

        plan = rules.generate_plugging_plan(well, {})

        surface_plugs = [s for s in plan.steps if s.step_type == "surface_plug"]
        assert len(surface_plugs) == 1
        sp = surface_plugs[0]

        assert sp.casing_size_in == 10.75, (
            f"Expected casing_size_in=10.75 for surface plug but got {sp.casing_size_in}. "
            "Bug: _generate_surface_plug() uses c.get('type') == 'surface' "
            "but casing records store the field as 'string_type', not 'type'."
        )

    def test_surface_plug_sacks_13375_casing_inside_only(self):
        """
        For a 13.375\" surface casing (the current fallback OD), 50 ft inside-only
        should be approximately 55-75 sacks.

        Math for 13.375\" OD:
          ID ≈ 12.515\" (wall = 0.430\")
          inside_area = π/4 × (12.515/12)² ≈ 0.856 ft²
          vol = 0.856 × 50 × 1.50 ≈ 64.2 ft³
          sacks = 64.2 / 1.32 ≈ 48.6 → 49 sacks

        This test verifies that even when 13.375\" OD IS correct, the inside-only
        volume gives ~49 sacks — NOT ~89 (which would require annular volume too).

        CURRENTLY FAILS because Bug B (annular volume) inflates the count.
        This test is independent of Bug A (wrong casing lookup).
        """
        rules = C103PluggingRules()
        # Pass 13.375\" so Bug A does not affect this test
        well = _minimal_well(size_in=13.375)

        plan = rules.generate_plugging_plan(well, {})

        surface_plugs = [s for s in plan.steps if s.step_type == "surface_plug"]
        assert len(surface_plugs) == 1
        sp = surface_plugs[0]

        # Inside-only for 13.375\" should be 49 sacks (above 25-sack minimum)
        # With annular volume (Bug B), count balloons to ~89
        assert sp.sacks_required >= 25, (
            f"Must meet NM minimum 25 sacks; got {sp.sacks_required}"
        )
        assert sp.sacks_required <= 70, (
            f"Expected ≤70 sacks for 13.375\" inside-only (50% excess), "
            f"got {sp.sacks_required}. "
            "If sacks ≈ 89, Bug B is present: annular volume is being added "
            "to circulate operations even though the annulus is already cemented."
        )
