"""
TDD Failing Tests — NM C-103 _combine_nearby_plugs()

These tests define the expected behaviour for the new
``C103PluggingRules._combine_nearby_plugs()`` method.

All tests MUST fail on the current codebase (method does not exist yet).
The Backend Engineer implements the method ONLY after these tests are
confirmed failing.

Design contract:
  - Plugs that are "combinable" (surface_plug, shoe_plug, duqw_plug) are
    merged when the gap between them is <= threshold_ft.
  - Overlapping plugs (gap < 0) are ALWAYS merged regardless of threshold.
  - The merged plug spans from the minimum top_ft to the maximum bottom_ft
    of the contributing plugs.
  - The merged plug retains casing_size_in=None so that _calculate_volumes()
    uses _get_casing_od_at_depth(mid_depth) for correct multi-casing volumes.

NMAC 19.15.25 references throughout.
"""

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_plan(steps=None):
    """Return a C103PluggingPlan with the given steps (api_number + region required)."""
    from apps.kernel.services.c103_models import C103PluggingPlan

    plan = C103PluggingPlan(api_number="30015288410000", region="north")
    plan.steps = steps or []
    return plan


def _plug(step_type, top_ft, bottom_ft, casing_size_in=None):
    """Convenience factory for a C103PlugRow with sensible NM defaults."""
    from apps.kernel.services.c103_models import C103PlugRow

    return C103PlugRow(
        top_ft=top_ft,
        bottom_ft=bottom_ft,
        cement_class="C",
        step_type=step_type,
        operation_type="spot",
        hole_type="cased",
        sacks_required=25,
        casing_size_in=casing_size_in,
        regulatory_basis="NMAC 19.15.25",
    )


# ---------------------------------------------------------------------------
# Test 1 — overlapping plugs always combine (gap < 0)
# ---------------------------------------------------------------------------


def test_overlapping_plugs_always_combined():
    """Plugs that overlap (gap < 0) are always merged regardless of threshold."""
    from apps.kernel.services.c103_rules import C103PluggingRules

    rules = C103PluggingRules()
    surface = _plug("surface_plug", top_ft=0, bottom_ft=150)
    duqw = _plug("duqw_plug", top_ft=100, bottom_ft=200)  # gap = 100-150 = -50 (overlap)

    plan = _make_plan([surface, duqw])

    rules._combine_nearby_plugs(plan, threshold_ft=0)  # threshold=0 but overlap always merges

    combinable = [s for s in plan.steps if s.step_type in ("surface_plug", "duqw_plug")]
    # Should be merged into one plug
    assert len(combinable) <= 1, (
        f"Overlapping plugs should always merge, got: "
        f"{[(s.step_type, s.top_ft, s.bottom_ft) for s in plan.steps]}"
    )
    result = [s for s in plan.steps if s.top_ft == 0]
    assert len(result) == 1
    assert result[0].bottom_ft == 200


# ---------------------------------------------------------------------------
# Test 2 — gap exactly at threshold → combined
# ---------------------------------------------------------------------------


def test_gap_at_threshold_is_combined():
    """Gap exactly equal to threshold_ft triggers combination."""
    from apps.kernel.services.c103_rules import C103PluggingRules

    rules = C103PluggingRules()
    surface = _plug("surface_plug", top_ft=0, bottom_ft=150)
    shoe = _plug("shoe_plug", top_ft=350, bottom_ft=450)  # gap = 350-150 = 200 ft

    plan = _make_plan([surface, shoe])

    # Gap = 350 - 150 = 200 ft, threshold = 200 → should combine
    rules._combine_nearby_plugs(plan, threshold_ft=200)

    combinable = [s for s in plan.steps if s.step_type in ("surface_plug", "shoe_plug")]
    assert len(combinable) <= 1, (
        f"Gap=200 at threshold=200 should combine, steps: "
        f"{[(s.step_type, s.top_ft, s.bottom_ft) for s in plan.steps]}"
    )
    result_starts_at_0 = [s for s in plan.steps if s.top_ft == 0]
    assert result_starts_at_0[0].bottom_ft == 450


# ---------------------------------------------------------------------------
# Test 3 — gap above threshold → NOT combined
# ---------------------------------------------------------------------------


def test_gap_above_threshold_not_combined():
    """Gap exceeding threshold_ft leaves plugs separate."""
    from apps.kernel.services.c103_rules import C103PluggingRules

    rules = C103PluggingRules()
    surface = _plug("surface_plug", top_ft=0, bottom_ft=150)
    shoe = _plug("shoe_plug", top_ft=500, bottom_ft=600)  # gap = 500-150 = 350 ft

    plan = _make_plan([surface, shoe])

    # Gap = 500 - 150 = 350 ft, threshold = 200 → should NOT combine
    rules._combine_nearby_plugs(plan, threshold_ft=200)

    surface_plugs = [s for s in plan.steps if s.step_type == "surface_plug"]
    shoe_plugs = [s for s in plan.steps if s.step_type == "shoe_plug"]
    assert len(surface_plugs) == 1, "Surface plug should remain separate"
    assert len(shoe_plugs) == 1, "Shoe plug should remain separate"
    assert surface_plugs[0].bottom_ft == 150
    assert shoe_plugs[0].top_ft == 500


# ---------------------------------------------------------------------------
# Test 4 — surface + shoe → combined plug starts at 0 ft
# ---------------------------------------------------------------------------


def test_surface_plus_shoe_combined_starts_at_zero():
    """Combined surface+shoe plug spans from 0 ft to shoe bottom."""
    from apps.kernel.services.c103_rules import C103PluggingRules

    rules = C103PluggingRules()
    surface = _plug("surface_plug", top_ft=0, bottom_ft=150)
    shoe = _plug("shoe_plug", top_ft=300, bottom_ft=400)  # gap = 300-150 = 150 ft

    plan = _make_plan([surface, shoe])

    # Gap = 300 - 150 = 150 ft < 200 ft threshold → combine
    rules._combine_nearby_plugs(plan, threshold_ft=200)

    top_level_combinable = [
        s for s in plan.steps
        if s.step_type in ("surface_plug", "shoe_plug")
    ]
    assert len(top_level_combinable) == 1, (
        f"Should be exactly one combined plug, got: "
        f"{[(s.step_type, s.top_ft, s.bottom_ft) for s in plan.steps]}"
    )
    merged = top_level_combinable[0]
    assert merged.top_ft == 0, f"Combined plug must start at 0 ft, got {merged.top_ft}"
    assert merged.bottom_ft == 400, (
        f"Combined plug must end at shoe bottom (400 ft), got {merged.bottom_ft}"
    )


# ---------------------------------------------------------------------------
# Test 5 — three-way chain combines in one pass
# ---------------------------------------------------------------------------


def test_three_way_chain_all_merged():
    """Three adjacent plugs (A-B-C) within threshold are all merged into one."""
    from apps.kernel.services.c103_rules import C103PluggingRules

    rules = C103PluggingRules()
    plug_a = _plug("surface_plug", top_ft=0, bottom_ft=150)   # A
    plug_b = _plug("duqw_plug", top_ft=200, bottom_ft=300)    # B  gap A-B = 50 ft
    plug_c = _plug("shoe_plug", top_ft=400, bottom_ft=500)    # C  gap B-C = 100 ft

    plan = _make_plan([plug_a, plug_b, plug_c])

    # A-B gap = 50 ft, B-C gap = 100 ft, both < 200 threshold → all three merge
    rules._combine_nearby_plugs(plan, threshold_ft=200)

    final_steps = [
        s for s in plan.steps
        if s.step_type in ("surface_plug", "duqw_plug", "shoe_plug")
    ]
    assert len(final_steps) == 1, (
        f"Three-way chain should collapse to one plug, got: "
        f"{[(s.step_type, s.top_ft, s.bottom_ft) for s in plan.steps]}"
    )
    assert final_steps[0].top_ft == 0
    assert final_steps[0].bottom_ft == 500


# ---------------------------------------------------------------------------
# Test 6 — combined plug has casing_size_in=None
# ---------------------------------------------------------------------------


def test_combined_plug_has_no_casing_size():
    """Combined plug must have casing_size_in=None so _calculate_volumes
    uses _get_casing_od_at_depth(mid_depth) for correct multi-casing volumes."""
    from apps.kernel.services.c103_rules import C103PluggingRules

    rules = C103PluggingRules()
    surface = _plug("surface_plug", top_ft=0, bottom_ft=150, casing_size_in=13.375)
    shoe = _plug("shoe_plug", top_ft=300, bottom_ft=400, casing_size_in=13.375)

    plan = _make_plan([surface, shoe])

    rules._combine_nearby_plugs(plan, threshold_ft=200)

    final_steps = [s for s in plan.steps if s.top_ft == 0]
    assert len(final_steps) == 1
    assert final_steps[0].casing_size_in is None, (
        f"Combined plug must have casing_size_in=None to force recalculation, "
        f"got: {final_steps[0].casing_size_in}"
    )


# ---------------------------------------------------------------------------
# Test 7 — integration: generate_plugging_plan with combine_nearby_plugs option
# ---------------------------------------------------------------------------


def test_generate_plan_with_combine_option():
    """generate_plugging_plan() respects combine_nearby_plugs option."""
    from apps.kernel.services.c103_rules import C103PluggingRules

    rules = C103PluggingRules()

    # Without combination — surface and shoe plugs should be separate
    well = {
        "api_number": "30015288410000",
        "casing_strings": [
            {
                "string_type": "surface",
                "size_in": 13.375,
                "depth_ft": 2000,
                "shoe_depth_ft": 2000,
            },
        ],
        "perforations": [],
        "formation_tops": [],
    }
    plan_no_combine = rules.generate_plugging_plan(well, {"surface_plug_bottom_ft": 150})
    no_combine_surface = [s for s in plan_no_combine.steps if s.step_type == "surface_plug"]
    assert no_combine_surface[0].bottom_ft == 150

    # With combination and large threshold — surface shoe at 350 ft to test combination.
    well_shallow = {
        "api_number": "30015288410000",
        "casing_strings": [
            {
                "string_type": "surface",
                "size_in": 13.375,
                "depth_ft": 350,
                "shoe_depth_ft": 350,
            },
        ],
        "perforations": [],
        "formation_tops": [],
    }
    plan_combine = rules.generate_plugging_plan(
        well_shallow,
        {
            "surface_plug_bottom_ft": 150,
            "combine_nearby_plugs": True,
            "combine_threshold_ft": 300,
        },
    )
    surface_plugs = [s for s in plan_combine.steps if s.step_type == "surface_plug"]
    shoe_plugs = [s for s in plan_combine.steps if s.step_type == "shoe_plug"]
    total_shallow = len(surface_plugs) + len(shoe_plugs)
    # At least one must be merged (total < 2) OR the merged plug spans from 0 to >= 350 ft
    assert total_shallow <= 1 or any(
        s.top_ft == 0 and s.bottom_ft >= 350 for s in plan_combine.steps
    ), (
        f"Expected surface/shoe to combine with threshold=300, got: "
        f"{[(s.step_type, s.top_ft, s.bottom_ft) for s in plan_combine.steps]}"
    )
