"""C-103 step generator — bridges policy kernel to C103PluggingRules."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from apps.kernel.services.c103_rules import STANDARD_HOLE_SIZES

logger = logging.getLogger(__name__)


def _extract_fact_value(facts: Dict[str, Any], key: str) -> Any:
    """Extract a fact value from the kernel resolved_facts dict.

    Kernel facts may be plain values or dicts with a "value" key.
    Plain dicts (like formation_tops_map) are returned as-is.
    """
    raw = facts.get(key)
    if isinstance(raw, dict) and "value" in raw:
        return raw.get("value")
    return raw


def _build_well_dict(resolved_facts: Dict[str, Any]) -> Dict[str, Any]:
    """Map kernel resolved_facts to the well dict expected by C103PluggingRules."""
    well: Dict[str, Any] = {}

    # Well identification
    well["api_number"] = _extract_fact_value(resolved_facts, "api14") or _extract_fact_value(resolved_facts, "api_number")
    well["operator"] = _extract_fact_value(resolved_facts, "operator")
    well["lease_name"] = _extract_fact_value(resolved_facts, "lease_name")
    well["lease_type"] = _extract_fact_value(resolved_facts, "lease_type")
    well["field_name"] = _extract_fact_value(resolved_facts, "field")

    # Location
    well["county"] = _extract_fact_value(resolved_facts, "county")
    well["township"] = _extract_fact_value(resolved_facts, "township")
    well["range"] = _extract_fact_value(resolved_facts, "range")
    well["state"] = _extract_fact_value(resolved_facts, "state")

    # Well geometry
    total_depth = _extract_fact_value(resolved_facts, "total_depth_ft")
    if total_depth is not None:
        try:
            well["total_depth_ft"] = float(total_depth)
        except (ValueError, TypeError):
            pass

    duqw = _extract_fact_value(resolved_facts, "duqw_ft")
    if duqw is not None:
        try:
            well["duqw_ft"] = float(duqw)
        except (ValueError, TypeError):
            pass

    # Formation tops — accept both dict map and list formats
    formation_tops_map = _extract_fact_value(resolved_facts, "formation_tops_map")
    formation_tops = _extract_fact_value(resolved_facts, "formation_tops")
    if formation_tops_map and isinstance(formation_tops_map, dict):
        well["formation_tops"] = formation_tops_map
    elif formation_tops:
        well["formation_tops"] = formation_tops

    # Normalize formation_tops: convert dict format to list format if needed
    ft = well.get("formation_tops")
    if isinstance(ft, dict):
        well["formation_tops"] = [{"name": k, "depth_ft": v} for k, v in ft.items()]

    # Casing strings (facts may use "casing_strings" or "casing_record")
    casing_strings = _extract_fact_value(resolved_facts, "casing_strings") or _extract_fact_value(resolved_facts, "casing_record")
    if casing_strings and isinstance(casing_strings, list):
        well["casing_strings"] = casing_strings

    # Perforations
    perforations = resolved_facts.get("perforations")
    if isinstance(perforations, list):
        well["perforations"] = perforations
    elif perforations is not None:
        perf_val = _extract_fact_value(resolved_facts, "perforations")
        if perf_val:
            well["perforations"] = perf_val

    # CBL data (optional — drives operation type classification)
    cbl_data = _extract_fact_value(resolved_facts, "cbl_data")
    if cbl_data:
        well["cbl_data"] = cbl_data

    # Existing mechanical barriers (CIBP, packer, etc.)
    barriers = _extract_fact_value(resolved_facts, "existing_mechanical_barriers")
    if barriers and isinstance(barriers, list):
        well["existing_mechanical_barriers"] = barriers

    # Historic cement jobs
    historic_cement = _extract_fact_value(resolved_facts, "historic_cement_jobs")
    if historic_cement and isinstance(historic_cement, list):
        well["historic_cement_jobs"] = historic_cement

    # Production perforations (separate from general perforations)
    prod_perfs = _extract_fact_value(resolved_facts, "production_perforations")
    if prod_perfs and isinstance(prod_perfs, list):
        well["production_perforations"] = prod_perfs

    # Fix 1B: normalize alternate field names to canonical depth_ft / size_in
    for cs in well.get("casing_strings", []):
        if not cs.get("depth_ft"):
            cs["depth_ft"] = cs.get("shoe_depth_ft") or cs.get("bottom") or 0
        if not cs.get("size_in"):
            cs["size_in"] = cs.get("od_in") or cs.get("diameter") or 0

    return well


def _c103_plug_row_to_kernel_step(plug_row: Any, casing_strings: list = None) -> Dict[str, Any]:
    """Convert a C103PlugRow dataclass instance to a kernel step dict.

    Maps C103PlugRow fields to the step schema the frontend expects,
    mirroring the dict structure used by w3a_rules.generate_steps().
    """
    step: Dict[str, Any] = {
        "type": plug_row.step_type,
        "top_ft": plug_row.top_ft,
        "bottom_ft": plug_row.bottom_ft,
        "regulatory_basis": [plug_row.regulatory_basis] if plug_row.regulatory_basis else ["nmac.19.15.25"],
        "operation_type": plug_row.operation_type,
        "hole_type": plug_row.hole_type,
        "tag_required": plug_row.tag_required,
    }

    # Top-level sacks for frontend rendering
    step["sacks"] = int(plug_row.sacks_required) if plug_row.sacks_required else None

    # Formation name for formation_plug and shoe_plug steps
    if plug_row.formation_name:
        step["formation"] = plug_row.formation_name

    # Casing size
    if plug_row.casing_size_in is not None:
        step["casing_id_in"] = plug_row.casing_size_in

    # Details sub-dict — carries NM-specific fields
    details: Dict[str, Any] = {
        "cement_class": plug_row.cement_class,
        "sacks_required": plug_row.sacks_required,
        "excess_factor": plug_row.excess_factor,
        "wait_hours": plug_row.wait_hours,
        "nmac_compliant": plug_row.nmac_compliant,
    }

    if plug_row.inside_sacks is not None:
        details["inside_sacks"] = plug_row.inside_sacks
    if plug_row.outside_sacks is not None:
        details["outside_sacks"] = plug_row.outside_sacks
    if plug_row.procedure_narrative:
        details["procedure_narrative"] = plug_row.procedure_narrative
    if plug_row.region_requirements:
        details["region_requirements"] = plug_row.region_requirements
    if plug_row.special_instructions:
        details["special_instructions"] = plug_row.special_instructions

    step["details"] = details

    # Perforate-and-squeeze: override step type and add WBD rendering fields
    if plug_row.operation_type == "perforate_and_squeeze":
        step["type"] = "perforate_and_squeeze_plug"
        step["plug_type"] = "perf_and_squeeze_plug"
        step["requires_perforation"] = True

        perf_top = plug_row.perf_top_ft if plug_row.perf_top_ft is not None else plug_row.top_ft
        perf_bottom = plug_row.perf_bottom_ft if plug_row.perf_bottom_ft is not None else plug_row.bottom_ft
        mid = (perf_top + perf_bottom) / 2

        step["details"]["perforation_interval"] = {
            "top_ft": mid,
            "bottom_ft": perf_bottom,
            "length_ft": perf_bottom - mid,
            "description": "Perforations for squeeze behind pipe",
        }
        step["details"]["cement_cap_inside_casing"] = {
            "top_ft": perf_top,
            "bottom_ft": mid,
            "height_ft": mid - perf_top,
            "description": "Cement cap above perforations",
        }
        step["details"]["squeeze_behind_pipe"] = True
        step["details"]["perforation_required_reason"] = (
            "No cement behind production casing above TOC — "
            "perforation required to squeeze cement into annulus"
        )

        # Annuli data for WBD rendering (cement extends through perforations into annulus)
        # Find the production casing OD and the outer casing/hole at this depth
        prod_od = plug_row.casing_size_in or 5.5
        outer_id = None
        outer_label = "openhole"

        if casing_strings:
            plug_mid = (plug_row.top_ft + plug_row.bottom_ft) / 2
            # Sort casings by size (largest = outermost)
            sorted_cs = sorted(
                [cs for cs in casing_strings if cs.get("bottom") or cs.get("depth_ft") or cs.get("shoe_depth_ft")],
                key=lambda c: c.get("diameter") or c.get("size_in") or 0,
            )
            # Find casings that cover this depth
            for cs in sorted_cs:
                cs_bottom = cs.get("bottom") or cs.get("depth_ft") or cs.get("shoe_depth_ft") or 0
                cs_size = cs.get("diameter") or cs.get("size_in") or 0
                cs_type = (cs.get("casing_type") or cs.get("string") or "").lower()
                if float(cs_bottom) >= plug_mid and float(cs_size) > prod_od:
                    # This casing is larger than production and covers this depth → outer casing
                    outer_id = float(cs_size) * 0.94  # ID ≈ 94% of OD for intermediate
                    outer_label = cs_type or "intermediate"
                    # Also check hole_size if available
                    hole = cs.get("hole_size_in") or cs.get("hole_size")
                    if hole:
                        outer_id = float(hole)
                    break

        if not outer_id:
            # Use standard hole size lookup for accurate annular geometry
            best = min(STANDARD_HOLE_SIZES.keys(), key=lambda k: abs(k - prod_od))
            if abs(best - prod_od) < 1.0:
                outer_id = STANDARD_HOLE_SIZES[best]
            else:
                outer_id = prod_od * 1.45  # Conservative fallback

        step["details"]["annuli"] = [{
            "inner": "production_casing",
            "outer": outer_label,
            "inner_od_in": prod_od,
            "outer_id_in": outer_id,
        }]
        step["details"]["geometry_used"] = {
            "casing_id_in": prod_od * 0.87,
            "annulus": f"production_to_{outer_label}",
        }

    # Add recipe for materials computation
    cement_class = plug_row.cement_class or "H"
    yield_map = {"A": 1.18, "B": 1.18, "C": 1.32, "G": 1.15, "H": 1.15}
    density_map = {"A": 15.6, "B": 15.6, "C": 14.8, "G": 15.8, "H": 16.4}
    water_map = {"A": 5.2, "B": 5.2, "C": 6.3, "G": 5.0, "H": 4.3}
    step["recipe"] = {
        "id": f"nm_class_{cement_class.lower()}",
        "class": cement_class,
        "density_ppg": density_map.get(cement_class, 16.4),
        "yield_ft3_per_sk": yield_map.get(cement_class, 1.15),
        "water_gal_per_sk": water_map.get(cement_class, 4.3),
        "additives": [],
    }

    return step


def generate_c103_steps(
    resolved_facts: Dict[str, Any],
    effective_policy: Dict[str, Any],
    formula_engine: Any = None,
) -> Dict[str, Any]:
    """Generate C-103 plugging plan steps from resolved kernel facts.

    Translates kernel fact format into well dict expected by C103PluggingRules,
    runs the rules engine, and converts output back to kernel step format.

    Args:
        resolved_facts: Kernel resolved facts dict with well data.
        effective_policy: Policy configuration from nm.c103 pack.
        formula_engine: NM formula engine instance (optional, unused currently).

    Returns:
        dict with "steps" list in kernel format and optional "violations" list.
    """
    from apps.kernel.services.c103_rules import C103PluggingRules
    from apps.policy.services.nm_region_rules import NMRegionRulesEngine

    result: Dict[str, Any] = {"steps": [], "violations": []}

    try:
        well_data = _build_well_dict(resolved_facts)

        county = well_data.get("county") or ""
        township = well_data.get("township") or ""
        range_ = well_data.get("range") or ""

        region_engine = NMRegionRulesEngine(
            county=county or None,
            township=township or None,
            range_=range_ or None,
        )

        # Build options from effective_policy (bailer_method, narrative, etc.)
        options: Dict[str, Any] = {}
        if effective_policy:
            prefs = effective_policy.get("preferences") or {}
            ops = (prefs.get("operational") or {}) if isinstance(prefs, dict) else {}
            bailer = ops.get("bailer_method")
            if bailer is not None:
                options["bailer_method"] = bool(bailer)
            combine = ops.get("combine_nearby_plugs")
            if combine is not None:
                options["combine_nearby_plugs"] = bool(combine)
            threshold = ops.get("combine_threshold_ft")
            if threshold is not None:
                options["combine_threshold_ft"] = float(threshold)

        rules = C103PluggingRules(region_engine=region_engine)
        plan = rules.generate_plugging_plan(well_data, options)

        steps: List[Dict[str, Any]] = []

        # Add existing cement steps (documentation of what's already in the hole)
        # These are prepended so they appear before new plugs in chronological order
        existing_steps: List[Dict[str, Any]] = []
        for cement_job in well_data.get("historic_cement_jobs", []):
            top = cement_job.get("cement_top_ft") or cement_job.get("top_ft")
            bottom = cement_job.get("interval_bottom_ft") or cement_job.get("bottom_ft")
            if top is None or bottom is None:
                continue
            existing_steps.append({
                "type": "existing_cement",
                "top_ft": top,
                "bottom_ft": bottom,
                "formation": cement_job.get("formation", ""),
                "sacks": cement_job.get("sacks"),
                "details": {
                    "cement_class": cement_job.get("cement_class", ""),
                    "source": cement_job.get("description", "Historic cement"),
                    "is_existing": True,
                },
            })

        # Check for packer cement in existing mechanical barriers
        for b in well_data.get("existing_mechanical_barriers", []):
            if isinstance(b, str):
                continue
            barrier_data = b.get("value", b) if isinstance(b, dict) else {}
            if not isinstance(barrier_data, dict):
                continue
            b_type = (barrier_data.get("type") or "").upper()
            if b_type == "PACKER" and "cement" in str(barrier_data.get("description", "")).lower():
                packer_depth = barrier_data.get("depth_ft")
                if packer_depth:
                    # Use user-edited cement_top_ft if available, otherwise default 50 ft
                    cement_height = barrier_data.get("cement_top_ft") or 50
                    cement_height = float(cement_height)
                    cement_sacks = barrier_data.get("sacks")
                    cement_sacks = int(cement_sacks) if cement_sacks else None
                    existing_steps.append({
                        "type": "existing_cement",
                        "top_ft": float(packer_depth) - cement_height,
                        "bottom_ft": float(packer_depth),
                        "formation": "",
                        "sacks": cement_sacks,
                        "details": {
                            "cement_class": "",
                            "source": f"Cement on top of packer at {packer_depth} ft ({cement_height:.0f} ft)",
                            "is_existing": True,
                        },
                    })

        steps.extend(existing_steps)

        for plug_row in plan.steps:
            try:
                step = _c103_plug_row_to_kernel_step(plug_row, casing_strings=well_data.get("casing_strings"))
                steps.append(step)
            except Exception:
                logger.exception("c103_step_generator: failed to convert plug row %r", plug_row)

        result["steps"] = steps

        logger.info(
            "c103_step_generator: generated %d steps for API %s (region=%s)",
            len(steps),
            well_data.get("api_number", "unknown"),
            plan.region,
        )

    except Exception:
        logger.exception("c103_step_generator: plan generation failed")
        result["violations"].append({
            "code": "c103_generation_failed",
            "severity": "error",
            "message": "C-103 step generation failed — check logs for details.",
        })

    return result
