import logging
import re
from typing import Optional, Any, Dict, List
from apps.public_core.models import ExtractedDocument

logger = logging.getLogger(__name__)


def extract_formations_from_payload(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Extract formation tops from plan payload.

    The payload contains:
    - formation_tops_detected: list of formation names ["Spraberry", "Dean", "Clearfork"]
    - steps: list of plug steps with top_ft depth and regulatory_basis

    We combine these to create formation_tops with name and depth.

    Returns list like:
    [
        {"formation": "Spraberry", "top_ft": 6750},
        {"formation": "Dean", "top_ft": 6750},
        {"formation": "Clearfork", "top_ft": 5650},
        ...
    ]
    """
    formation_tops: List[Dict[str, Any]] = []

    try:
        # Priority 1: Direct formations list from operator packet / plan payload
        # Format: [{"formation_name": "Morrow", "top_ft": 13619}, ...]
        direct_formations = payload.get("formations", [])
        if isinstance(direct_formations, list) and direct_formations:
            for f in direct_formations:
                if isinstance(f, dict) and f.get("formation_name"):
                    top = f.get("top_ft")
                    formation_tops.append({
                        "formation": f["formation_name"],
                        "top_ft": float(top) if top is not None else None,
                    })
            if formation_tops:
                logger.info(f"Extracted {len(formation_tops)} formation tops from direct formations list")
                return formation_tops

        # Get list of detected formations
        formations_detected = payload.get('formation_tops_detected', [])
        if not formations_detected:
            formations_detected = payload.get('formations_targeted', [])

        # If we have formations, extract their depths from steps
        if formations_detected and isinstance(formations_detected, list):
            steps = payload.get('steps', [])

            # Build a map of formation name to depths from regulatory_basis codes
            formation_depths: Dict[str, float] = {}

            if isinstance(steps, list):
                for step in steps:
                    if isinstance(step, dict):
                        top_ft = step.get('top_ft')

                        # Method 1: Direct formation field on step (NM C-103 format)
                        step_formation = step.get('formation')
                        if step_formation and top_ft is not None and step_formation not in formation_depths:
                            formation_depths[step_formation] = float(top_ft)

                        # Method 2: Parse regulatory_basis for TX W-3A format
                        # Format: "rrc.district.XX.county:formation_top:FormationName"
                        regulatory_basis = step.get('regulatory_basis', [])
                        if isinstance(regulatory_basis, list):
                            for basis in regulatory_basis:
                                if isinstance(basis, str) and 'formation_top:' in basis:
                                    try:
                                        formation_name = basis.split('formation_top:')[-1].strip()
                                        if formation_name and top_ft is not None:
                                            formation_depths[formation_name] = float(top_ft)
                                    except Exception:
                                        pass

            # Create formation_tops list from detected formations with their depths
            for formation_name in formations_detected:
                if formation_name in formation_depths:
                    formation_tops.append({
                        "formation": formation_name,
                        "top_ft": formation_depths[formation_name],
                    })
                else:
                    # Include even if depth not found (may be in W-2 later)
                    formation_tops.append({
                        "formation": formation_name,
                        "top_ft": None,
                    })

        if formation_tops:
            logger.info(f"Extracted {len(formation_tops)} formation tops from payload")

    except Exception as e:
        logger.warning(f"Failed to extract formations from payload: {e}")

    return formation_tops


def extract_historic_cement_jobs(api14: str) -> List[Dict[str, Any]]:
    """
    Extract all historic cement jobs from W-15 document.
    Store all cement jobs without filtering to preserve complete historical data.
    """
    historic_cement_jobs: List[Dict[str, Any]] = []
    try:
        w15_doc = ExtractedDocument.objects.filter(
            api_number=api14,
            document_type='w15'
        ).order_by('-created_at').first()

        if w15_doc and isinstance(w15_doc.json_data, dict):
            w15 = w15_doc.json_data
            cementing_data = w15.get("cementing_data") or []

            if isinstance(cementing_data, list):
                for cement_job in cementing_data:
                    if isinstance(cement_job, dict):
                        try:
                            # Include all available fields from the cement job
                            slurries = cement_job.get("slurries")
                            sacks = cement_job.get("sacks")

                            # Compute sacks from slurries if not explicitly provided
                            if sacks is None and slurries:
                                sacks = sum(
                                    s.get("sacks", 0) or 0
                                    for s in slurries
                                    if isinstance(s, dict)
                                )

                            job_entry: Dict[str, Any] = {
                                "job_type": cement_job.get("job"),
                                "interval_top_ft": cement_job.get("interval_top_ft"),
                                "interval_bottom_ft": cement_job.get("interval_bottom_ft"),
                                "cement_top_ft": cement_job.get("cement_top_ft"),
                                "sacks": sacks,
                                "slurry_density_ppg": cement_job.get("slurry_density_ppg"),
                                "additives": cement_job.get("additives"),
                                "yield_ft3_per_sk": cement_job.get("yield_ft3_per_sk"),
                            }

                            # Preserve per-slurry detail when available
                            if slurries:
                                job_entry["slurries"] = slurries

                            # Store all cement jobs as-is, preserving complete historical data
                            historic_cement_jobs.append(job_entry)
                        except Exception:
                            pass

            if historic_cement_jobs:
                logger.info(f"Extracted {len(historic_cement_jobs)} historic cement jobs from W-15 for API {api14}")
    except Exception as e:
        logger.warning(f"Failed to extract historic cement jobs from W-15 for API {api14}: {e}")

    return historic_cement_jobs


def extract_mechanical_equipment(api14: str) -> List[Dict[str, Any]]:
    """
    Extract mechanical equipment (CIBPs, bridge plugs, packers) from W-15 document.
    Store all equipment with complete specifications.
    """
    mechanical_equipment: List[Dict[str, Any]] = []
    try:
        w15_doc = ExtractedDocument.objects.filter(
            api_number=api14,
            document_type='w15'
        ).order_by('-created_at').first()

        if w15_doc and isinstance(w15_doc.json_data, dict):
            w15 = w15_doc.json_data
            equipment_data = w15.get("mechanical_equipment") or []

            if isinstance(equipment_data, list):
                for equipment in equipment_data:
                    if isinstance(equipment, dict):
                        try:
                            # Include all available fields from the equipment entry
                            equipment_entry: Dict[str, Any] = {
                                "equipment_type": equipment.get("equipment_type"),  # CIBP|bridge_plug|packer
                                "size_in": equipment.get("size_in"),
                                "depth_ft": equipment.get("depth_ft"),
                                "sacks": equipment.get("sacks"),
                                "notes": equipment.get("notes"),
                            }
                            # Store all equipment as-is, preserving complete specifications
                            mechanical_equipment.append(equipment_entry)
                        except Exception:
                            pass

            if mechanical_equipment:
                logger.info(f"Extracted {len(mechanical_equipment)} mechanical equipment items from W-15 for API {api14}")
    except Exception as e:
        logger.warning(f"Failed to extract mechanical equipment from W-15 for API {api14}: {e}")

    return mechanical_equipment


def build_well_geometry(api14: str, payload: Optional[Dict[str, Any]] = None, jurisdiction: str = None) -> dict:
    """
    Extract well geometry from ExtractedDocuments for a given API.
    Returns casing strings, formation tops, perforations, production intervals, mechanical equipment, and tubing.

    Args:
        api14: The API number
        payload: Optional plan payload containing formation_tops_detected and steps
        jurisdiction: Optional jurisdiction code (e.g. "TX", "NM"). When "NM", the W-2
                      query is skipped to prevent TX form data from polluting NM wells.
    """
    # ── Component-based resolver (Phase 3) ──────────────────────────
    # If WellComponent records exist for this well, use the new resolver.
    # Falls back to legacy parsing below if no components or on error.
    try:
        from apps.public_core.models import WellComponent
        _comp_count = WellComponent.objects.filter(
            well__api14=api14, is_archived=False
        ).count()
        if _comp_count > 0:
            from apps.public_core.services.component_resolver import (
                build_well_geometry_from_components,
            )
            logger.info(
                "build_well_geometry: using component resolver for %s (%d components)",
                api14, _comp_count,
            )
            comp_geometry = build_well_geometry_from_components(well=api14)

            # Merge payload data for fields the component resolver left empty
            if payload and isinstance(payload, dict):
                if not comp_geometry.get("casing_strings"):
                    casing_from_payload = payload.get("casing_strings") or payload.get("casing_record", [])
                    if casing_from_payload:
                        comp_geometry["casing_strings"] = casing_from_payload
                        logger.info("build_well_geometry: supplemented %d casing_strings from payload", len(casing_from_payload))

                # Always prefer payload formation tops if they contain actual depth values
                formation_tops_from_payload = extract_formations_from_payload(payload)
                payload_has_depths = any(f.get("top_ft") is not None for f in formation_tops_from_payload)
                if payload_has_depths:
                    comp_geometry["formation_tops"] = formation_tops_from_payload
                    logger.info("build_well_geometry: used %d formation_tops from payload (overrides component resolver)", len(formation_tops_from_payload))
                elif not comp_geometry.get("formation_tops") and formation_tops_from_payload:
                    comp_geometry["formation_tops"] = formation_tops_from_payload
                    logger.info("build_well_geometry: supplemented %d formation_tops from payload", len(formation_tops_from_payload))

                if not comp_geometry.get("production_perforations"):
                    perfs = payload.get("production_perforations", [])
                    if perfs:
                        comp_geometry["production_perforations"] = perfs
                if not comp_geometry.get("tubing"):
                    tubing = payload.get("tubing") or payload.get("tubing_record", [])
                    if tubing:
                        comp_geometry["tubing"] = tubing
                if not comp_geometry.get("existing_tools"):
                    tools = payload.get("existing_tools", [])
                    if tools:
                        comp_geometry["existing_tools"] = tools
                if not comp_geometry.get("historic_cement_jobs"):
                    hcj = extract_historic_cement_jobs(api14)
                    if hcj:
                        comp_geometry["historic_cement_jobs"] = hcj
                if not comp_geometry.get("mechanical_equipment"):
                    me = extract_mechanical_equipment(api14)
                    if me:
                        comp_geometry["mechanical_equipment"] = me

            return comp_geometry
    except Exception:
        logger.warning(
            "build_well_geometry: component resolver failed for %s, falling back to legacy",
            api14,
            exc_info=True,
        )
    # ── End component-based resolver ─────────────────────────────────

    geometry = {
        "casing_strings": [],
        "formation_tops": [],
        "perforations": [],
        "production_perforations": [],
        "tubing": [],
        "liner": [],
        "historic_cement_jobs": [],
        "mechanical_equipment": [],
        "existing_tools": [],
    }

    # First, try to extract formations from payload if provided
    # This includes formation names and depths from the plan
    if payload and isinstance(payload, dict):
        formation_tops_from_payload = extract_formations_from_payload(payload)
        if formation_tops_from_payload:
            geometry['formation_tops'] = formation_tops_from_payload

        # Also check for casing_strings in payload (from segmented W3A flow)
        casing_strings_from_payload = payload.get('casing_strings') or payload.get('casing_record', [])
        logger.info(f"🔍 _build_well_geometry: Found {len(casing_strings_from_payload)} casing_strings in payload")
        if casing_strings_from_payload:
            geometry['casing_strings'] = casing_strings_from_payload
            logger.info(f"✅ Using {len(casing_strings_from_payload)} casing_strings from payload")
        else:
            logger.info(f"⚠️ No casing_strings in payload, will try ExtractedDocument")

    # Get W-2 document for casing and formation data (TX only - skip for NM wells)
    if jurisdiction != "NM":
        w2 = ExtractedDocument.objects.filter(
            api_number=api14,
            document_type='w2'
        ).first()

        if w2:
            # Extract casing strings (only if not already loaded from payload)
            if not geometry['casing_strings']:
                casing_record = w2.json_data.get('casing_record', [])
                if casing_record:
                    geometry['casing_strings'] = casing_record

            # Extract formation tops from W-2 (fallback if not in payload)
            if not geometry['formation_tops']:
                formation_record = w2.json_data.get('formation_record', [])
                if formation_record:
                    geometry['formation_tops'] = formation_record

            # Extract tubing if available
            tubing_record = w2.json_data.get('tubing_record', [])
            if tubing_record:
                geometry['tubing'] = tubing_record

            # Extract liner if available
            liner_record = w2.json_data.get('liner_record', [])
            if liner_record:
                geometry['liner'] = normalize_casing_for_frontend(liner_record)

            # Extract production/injection/disposal intervals as production perforations
            pidi_record = w2.json_data.get('producing_injection_disposal_interval', [])
            if pidi_record:
                production_perfs = []
                for interval in pidi_record:
                    if isinstance(interval, dict):
                        perf_entry = {
                            "top_ft": interval.get("from_ft"),
                            "bottom_ft": interval.get("to_ft"),
                            "open_hole": interval.get("open_hole", False),
                        }
                        production_perfs.append(perf_entry)
                geometry['production_perforations'] = production_perfs

            # Extract existing tools (CIBP, bridge plugs, packers, DV tools, retainers) from multiple sources
            existing_tools = []

            # 1. From acid_fracture_operations (mechanical_plug, retainer, bridge plug)
            afo_record = w2.json_data.get('acid_fracture_operations', [])
            if afo_record:
                for operation in afo_record:
                    if isinstance(operation, dict):
                        op_type = operation.get("operation_type", "").lower()
                        # Filter for mechanical plugs and barriers
                        if "mechanical" in op_type or "cibp" in op_type or "bridge" in op_type or "retainer" in op_type:
                            tool_entry = {
                                "source": "acid_fracture_operations",
                                "tool_type": operation.get("operation_type"),
                                "material_description": operation.get("amount_and_kind_of_material_used"),
                                "top_ft": operation.get("from_ft"),
                                "bottom_ft": operation.get("to_ft"),
                                "open_hole": operation.get("open_hole", False),
                                "notes": operation.get("notes"),
                            }
                            existing_tools.append(tool_entry)

            # 2. From remarks - extract CIBP, Packer, DV Tool, Retainer depths using regex
            try:
                remarks_txt = str(w2.json_data.get("remarks") or "")
                rrc_remarks_obj = w2.json_data.get("rrc_remarks") or {}
                rrc_remarks_txt = ""
                if isinstance(rrc_remarks_obj, dict):
                    for key, val in rrc_remarks_obj.items():
                        if val:
                            rrc_remarks_txt += f" {val}"
                elif isinstance(rrc_remarks_obj, str):
                    rrc_remarks_txt = rrc_remarks_obj

                combined_remarks = f"{remarks_txt} {rrc_remarks_txt}"

                # Extract CIBP depth
                for pattern in [r"CIBP\s*(?:at|@)?\s*(\d{3,5})", r"cast\s*iron\s*bridge\s*plug\s*(?:at|@)?\s*(\d{3,5})", r"\bBP\b\s*(?:at|@)?\s*(\d{3,5})"]:
                    match = re.search(pattern, combined_remarks, flags=re.IGNORECASE)
                    if match:
                        try:
                            depth = float(match.group(1))
                            # Check if already in existing_tools (from acid_fracture_operations)
                            if not any(t.get("tool_type", "").lower() == "cibp" and t.get("top_ft") == depth for t in existing_tools):
                                existing_tools.append({
                                    "source": "remarks",
                                    "tool_type": "CIBP",
                                    "depth_ft": depth,
                                })
                            break
                        except Exception:
                            pass

                # Extract Packer depth
                packer_match = re.search(r"packer\s*(?:at|@|set\s+at)?\s*(\d{3,5})", combined_remarks, flags=re.IGNORECASE)
                if packer_match:
                    try:
                        depth = float(packer_match.group(1))
                        if not any(t.get("tool_type", "").lower() == "packer" and t.get("depth_ft") == depth for t in existing_tools):
                            existing_tools.append({
                                "source": "remarks",
                                "tool_type": "Packer",
                                "depth_ft": depth,
                            })
                    except Exception:
                        pass

                # Extract DV Tool depth
                for pattern in [r"DV[- ]?(?:stage)?\s*tool\s*(?:at|@)?\s*(\d{3,5})", r"DV[- ]?tool\s*(?:at|@)?\s*(\d{3,5})"]:
                    dv_match = re.search(pattern, combined_remarks, flags=re.IGNORECASE)
                    if dv_match:
                        try:
                            depth = float(dv_match.group(1))
                            if not any(t.get("tool_type", "").lower() == "dv_tool" and t.get("depth_ft") == depth for t in existing_tools):
                                existing_tools.append({
                                    "source": "remarks",
                                    "tool_type": "DV_Tool",
                                    "depth_ft": depth,
                                })
                            break
                        except Exception:
                            pass

                # Extract Retainer depth
                for pattern in [r"retainer\s*(?:at|@)?\s*(\d{3,5})", r"retainer\s+(?:packer\s+)?(?:at|@)?\s*(\d{3,5})"]:
                    retainer_matches = re.finditer(pattern, combined_remarks, flags=re.IGNORECASE)
                    for match in retainer_matches:
                        try:
                            depth = float(match.group(1))
                            if not any(t.get("tool_type", "").lower() == "retainer" and t.get("depth_ft") == depth for t in existing_tools):
                                existing_tools.append({
                                    "source": "remarks",
                                    "tool_type": "Retainer",
                                    "depth_ft": depth,
                                })
                        except Exception:
                            pass

                # Extract Straddle Packer depth
                for pattern in [r"straddle\s*(?:packer\s+)?(?:at|@)?\s*(\d{3,5})", r"straddle\s*(?:at|@)?\s*(\d{3,5})"]:
                    straddle_matches = re.finditer(pattern, combined_remarks, flags=re.IGNORECASE)
                    for match in straddle_matches:
                        try:
                            depth = float(match.group(1))
                            if not any(t.get("tool_type", "").lower() == "straddle_packer" and t.get("depth_ft") == depth for t in existing_tools):
                                existing_tools.append({
                                    "source": "remarks",
                                    "tool_type": "Straddle_Packer",
                                    "depth_ft": depth,
                                })
                        except Exception:
                            pass
            except Exception:
                pass

            geometry['existing_tools'] = existing_tools

    # Get W-15 document for additional formation tops or perforations
    w15 = ExtractedDocument.objects.filter(
        api_number=api14,
        document_type='w15'
    ).first()

    if w15:
        # Check for perforations
        perforations = w15.json_data.get('perforations', [])
        if perforations:
            geometry['perforations'] = perforations

        # Check for formation tops (if not already in W-2)
        formation_tops = w15.json_data.get('formation_tops', [])
        if formation_tops and not geometry['formation_tops']:
            geometry['formation_tops'] = formation_tops

    # Get C-105 document for NM wells (NM equivalent of W-2)
    # Always check C-105 for NM wells even if some data came from payload;
    # for other jurisdictions only fall back if casing/formation data is still missing.
    if jurisdiction == "NM" or (not geometry['casing_strings'] or not geometry['formation_tops']):
        c105 = ExtractedDocument.objects.filter(
            api_number=api14, document_type='c105'
        ).order_by('-created_at').first()

        if c105:
            if not geometry['casing_strings']:
                geometry['casing_strings'] = c105.json_data.get('casing_record', [])
            if not geometry['formation_tops']:
                geometry['formation_tops'] = c105.json_data.get('formation_record', [])
            if not geometry['production_perforations']:
                pidi = c105.json_data.get('producing_injection_disposal_interval', [])
                if pidi:
                    geometry['production_perforations'] = [
                        {"top_ft": p.get("top_md"), "bottom_ft": p.get("bottom_md")}
                        for p in pidi if isinstance(p, dict)
                    ]

    # Extract historic cement jobs from W-15 (or payload if available)
    if payload and isinstance(payload, dict) and payload.get('historic_cement_jobs'):
        geometry['historic_cement_jobs'] = payload['historic_cement_jobs']
        logger.info(f"Using historic_cement_jobs from payload for API {api14}")
    else:
        geometry['historic_cement_jobs'] = extract_historic_cement_jobs(api14)

    # Extract mechanical equipment from payload (user-added tools) or W-15
    if payload and isinstance(payload, dict) and payload.get('mechanical_equipment'):
        geometry['mechanical_equipment'] = payload['mechanical_equipment']
        logger.info(f"Using mechanical_equipment from payload for API {api14} ({len(payload['mechanical_equipment'])} tools)")
    else:
        geometry['mechanical_equipment'] = extract_mechanical_equipment(api14)

    # Also add existing_tools alias if present in payload
    if payload and isinstance(payload, dict) and payload.get('existing_tools'):
        geometry['existing_tools'].extend([
            tool for tool in payload['existing_tools']
            if tool not in geometry['existing_tools']
        ])
        logger.info(f"Added {len(payload['existing_tools'])} existing_tools from payload for API {api14}")

    # Extract production perforations from payload if available
    if payload and isinstance(payload, dict) and payload.get('production_perforations'):
        # Only override if payload has perforations and we don't have them from W-2
        if not geometry['production_perforations']:
            geometry['production_perforations'] = payload['production_perforations']
            logger.info(f"Using production_perforations from payload for API {api14}")

    # Separate liner entries from casing_strings into liner array for frontend
    # Frontend renders liners with dashed lines from well_geometry.liner[]
    if not geometry['liner'] and geometry['casing_strings']:
        liner_entries = []
        non_liner_entries = []
        for cs in geometry['casing_strings']:
            if isinstance(cs, dict) and (cs.get('string_type') or cs.get('casing_type') or cs.get('string', '')).lower() == 'liner':
                liner_entries.append(cs)
            else:
                non_liner_entries.append(cs)
        if liner_entries:
            geometry['liner'] = liner_entries
            geometry['casing_strings'] = non_liner_entries
            logger.info(f"Separated {len(liner_entries)} liner entries from casing_strings")

    return geometry


# Standard bit sizes for common casing ODs (inches)
# Used to infer hole_size_in when not provided in extraction data
STANDARD_BIT_SIZES = {
    20.0: 26.0,
    16.0: 20.0,
    13.375: 17.5,
    10.75: 14.0,
    9.625: 12.25,
    8.625: 11.0,
    7.625: 9.875,
    7.0: 8.75,
    5.5: 7.875,
    4.5: 6.125,
    4.0: 5.875,
    2.875: 3.875,
    2.375: 3.5,
}


def _infer_hole_size(casing_od: float) -> float:
    """Infer hole size from casing OD using standard bit sizes.
    Falls back to casing OD * 1.3 if no standard match found.
    """
    if not casing_od:
        return 0
    # Try exact match first
    if casing_od in STANDARD_BIT_SIZES:
        return STANDARD_BIT_SIZES[casing_od]
    # Try closest match within 0.25"
    closest = min(STANDARD_BIT_SIZES.keys(), key=lambda x: abs(x - casing_od))
    if abs(closest - casing_od) <= 0.25:
        return STANDARD_BIT_SIZES[closest]
    # Fallback: casing OD * 1.3
    return round(casing_od * 1.3, 3)


def normalize_casing_for_frontend(casing_list: list) -> list:
    """Map W-2 / C-105 extraction field names to frontend CasingString expectations.
    W-2 uses: string_type, shoe_depth_ft
    C-105 uses: casing_type, diameter, top, bottom, cement_top
    Frontend expects: string, bottom_ft

    Uses explicit None checks (not `or`) so that 0 values are preserved.
    Infers hole_size_in from standard bit sizes when not provided.
    """
    def _first(*values):
        """Return the first value that is not None."""
        for v in values:
            if v is not None:
                return v
        return None

    normalized = []
    for c in casing_list:
        if not isinstance(c, dict):
            continue
        size_in = _first(c.get("size_in"), c.get("od_in"), c.get("diameter"))
        hole_size = _first(c.get("hole_size_in"), c.get("bit_size_in"))
        if hole_size is None and size_in:
            hole_size = _infer_hole_size(float(size_in))
        string_name = c.get("string_type") or c.get("casing_type") or c.get("string", "")
        normalized.append({
            "string": string_name,
            "string_type": string_name,          # alias for component-path consumers
            "size_in": size_in,
            "outside_dia_in": size_in,           # alias for component-path consumers
            "top_ft": _first(c.get("top_ft"), c.get("top")),
            "bottom_ft": _first(c.get("shoe_depth_ft"), c.get("bottom_ft"), c.get("bottom")),
            "hole_size_in": hole_size,
            "cement_top_ft": _first(c.get("cement_top_ft"), c.get("top_cmt_ft"), c.get("cement_top")),
            "id_in": c.get("id_in"),
            "weight_ppf": c.get("weight_ppf") or c.get("weight_per_ft"),
            "removed_to_depth_ft": c.get("removed_to_depth_ft"),
        })
    return normalized


def _classify_schematic_type(client, data_url: str) -> str:
    """Classify a schematic as 'current_state' or 'proposed_plugging'.

    Returns: 'current_state', 'proposed_plugging', 'not_schematic', or 'unknown'
    """
    from .openai_config import DEFAULT_CHAT_MODEL as MODEL_VISION

    prompt = (
        "This image is from an oil/gas P&A (plug and abandonment) plan. "
        "Classify it as exactly one of:\n"
        "- current_state: shows the EXISTING wellbore as-built (casing, cement, formations, existing tools) WITHOUT proposed cement plugs\n"
        "- proposed_plugging: shows PROPOSED plugging operations with planned cement plugs (labeled Plug 1, Plug 2, etc.), plug depths, and planned cement fill sections\n"
        "- not_schematic: not a wellbore diagram at all (text page, map, permit, photo, etc.)\n\n"
        "Respond with exactly one phrase: current_state, proposed_plugging, or not_schematic"
    )
    try:
        resp = client.chat.completions.create(
            model=MODEL_VISION,
            messages=[{"role": "user", "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]}],
            max_tokens=20, temperature=0,
        )
        label = (resp.choices[0].message.content or "").strip().lower().replace(" ", "_")
        valid = {"current_state", "proposed_plugging", "not_schematic"}
        return label if label in valid else "unknown"
    except Exception as e:
        logger.warning(f"_classify_schematic_type error: {e}")
        return "unknown"


def extract_geometry_from_plan_pdf(file_path: str, w2_data: Optional[Dict] = None) -> Optional[Dict]:
    """
    Scan plan PDF pages for the CURRENT STATE wellbore diagram and extract geometry.

    1. Open PDF with fitz, render each page to PNG
    2. Classify each page: current_state / proposed_plugging / not_schematic
    3. For the current_state page, run full extraction via extract_schematic_from_image
    4. Return extracted geometry dict or None
    """
    import fitz
    import base64
    import tempfile
    from pathlib import Path
    from .schematic_extraction import extract_schematic_from_image
    from .openai_config import get_openai_client

    try:
        doc = fitz.open(file_path)
    except Exception as e:
        logger.warning(f"Could not open plan PDF for vision: {e}")
        return None

    client = get_openai_client(operation="wbd_classify")

    for page_num in range(min(len(doc), 10)):  # cap at 10 pages
        page = doc[page_num]
        pix = page.get_pixmap(dpi=200)

        with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmp:
            pix.save(tmp.name)
            tmp_path = Path(tmp.name)

        try:
            image_data = base64.b64encode(open(tmp_path, 'rb').read()).decode()
            data_url = f"data:image/png;base64,{image_data}"

            # Classify: current_state vs proposed_plugging vs not_schematic
            classification = _classify_schematic_type(client, data_url)
            logger.info(f"Page {page_num + 1} classified as: {classification}")

            if classification == "current_state":
                logger.info(f"Found current-state WBD on page {page_num + 1}, extracting geometry")
                result = extract_schematic_from_image(tmp_path, w2_data=w2_data)
                tmp_path.unlink(missing_ok=True)
                doc.close()
                return result
        except Exception as e:
            logger.warning(f"Vision extraction failed on page {page_num + 1}: {e}")
        finally:
            tmp_path.unlink(missing_ok=True)

    doc.close()
    logger.warning(f"No current-state WBD found in plan PDF: {file_path}")
    return None


def normalize_vision_to_well_geometry(vision_data: Dict) -> Dict:
    """Convert schematic_extraction output to well_geometry format."""
    casing_strings = []
    for cs in vision_data.get("casing_strings", []):
        casing_strings.append({
            "string_type": cs.get("string_type", ""),
            "size_in": cs.get("size_in"),
            "top_ft": cs.get("top_md_ft", 0),
            "shoe_depth_ft": cs.get("bottom_md_ft") or cs.get("shoe_md_ft"),
            "hole_size_in": cs.get("hole_size_in"),
            "cement_top_ft": (cs.get("cement_job") or {}).get("cement_top_md_ft"),
            "id_in": None,
        })

    formation_tops = [
        {"formation": f.get("name", ""), "top_ft": f.get("top_md_ft")}
        for f in vision_data.get("formations", [])
    ]

    production_perforations = [
        {"top_ft": p.get("top_md_ft"), "bottom_ft": p.get("bottom_md_ft")}
        for p in vision_data.get("producing_intervals", [])
    ]

    perforations = [
        {"top_ft": p.get("top_md_ft"), "bottom_ft": p.get("bottom_md_ft")}
        for p in vision_data.get("perforations", [])
    ]

    # Map historical_interventions to existing_tools
    existing_tools = []
    for h in vision_data.get("historical_interventions", []):
        tool_type_map = {
            "cibp": "CIBP", "bridge_plug": "Bridge_Plug",
            "packer": "Packer", "squeeze": "Squeeze",
        }
        existing_tools.append({
            "source": "vision_extraction",
            "tool_type": tool_type_map.get(h.get("type", ""), h.get("type", "")),
            "top_ft": h.get("top_md_ft"),
            "bottom_ft": h.get("bottom_md_ft"),
            "depth_ft": h.get("top_md_ft"),
            "notes": h.get("notes"),
        })

    # Map cement jobs to historic_cement_jobs
    historic_cement_jobs = []
    for cs in vision_data.get("casing_strings", []):
        cj = cs.get("cement_job") or {}
        if cj.get("cement_top_md_ft") is not None or cj.get("cement_bottom_md_ft") is not None:
            historic_cement_jobs.append({
                "job_type": f"{cs.get('string_type', '')} cement",
                "interval_top_ft": cs.get("top_md_ft", 0),           # Casing string top (0 for surface)
                "interval_bottom_ft": cs.get("bottom_md_ft"),         # Casing shoe depth
                "cement_top_ft": cj.get("cement_top_md_ft"),          # Where cement reached (unchanged)
                "sacks": cj.get("sacks"),
            })

    return {
        "casing_strings": casing_strings,
        "formation_tops": formation_tops,
        "production_perforations": production_perforations,
        "perforations": perforations,
        "existing_tools": existing_tools,
        "historic_cement_jobs": historic_cement_jobs,
        "tubing": [],
        "liner": [],
        "mechanical_equipment": [],
    }
