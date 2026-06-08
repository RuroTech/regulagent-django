"""
W-3 Form Formatter Service

Transforms normalized W3Event instances and W-3A form data into complete, 
RRC-compliant W-3 form output ready for submission.

Handles:
- Grouping events into logical plugs
- Building plug rows for RRC W-3 form
- Formatting casing record
- Formatting perforations/open hole intervals
- Generating remarks section
- Building complete W3Form dictionary
"""

from __future__ import annotations
from typing import Any, Dict, List, Optional, Tuple
from datetime import date
import logging

from apps.public_core.models.w3_event import W3Event, Plug, CasingStringState, W3Form
from apps.public_core.services.w3_casing_engine import apply_cut_casing, get_active_casing_at_depth, get_plug_hole_size_at_depth

logger = logging.getLogger(__name__)


def get_cement_yield_cf_per_sack(cement_class: Optional[str]) -> float:
    """
    Get cement yield in cubic feet per sack based on cement class.
    
    Standard cement yields (API RP 65):
    - Class A: ~1.15 cf/sack
    - Class B: ~1.15 cf/sack
    - Class C: ~1.35 cf/sack
    - Class G: ~1.15 cf/sack
    - Class H: ~1.19 cf/sack
    
    Default to Class C (1.35 cf/sack) if not specified.
    """
    class_map = {
        "A": 1.15,
        "B": 1.15,
        "C": 1.35,
        "G": 1.15,
        "H": 1.19,
    }
    
    if cement_class and cement_class.upper() in class_map:
        return class_map[cement_class.upper()]
    
    return 1.35  # Default to Class C


def get_annular_capacity_cf_per_ft(hole_size_in: float) -> float:
    """
    Calculate annular capacity (volume) in cubic feet per foot for a given hole size.
    
    Formula: Area (sq in) = π × (d/2)² = π × (d²/4)
    Capacity (cf/ft) = Area / 144 sq in/sq ft × 1 ft
    
    Common hole sizes and their capacities:
    - 4.5" hole: ~0.111 cf/ft
    - 5.5" hole: ~0.165 cf/ft
    - 7" hole: ~0.267 cf/ft
    - 7.875" hole: ~0.339 cf/ft
    - 8.625" hole: ~0.408 cf/ft
    - 9.625" hole: ~0.507 cf/ft
    - 12.25" hole: ~0.818 cf/ft
    
    Args:
        hole_size_in: Hole size in inches
    
    Returns:
        Capacity in cubic feet per foot
    """
    import math
    
    # Area in square inches
    area_sq_in = math.pi * (hole_size_in / 2) ** 2
    
    # Convert to cubic feet per foot
    capacity_cf_per_ft = area_sq_in / 144.0
    
    return capacity_cf_per_ft


def calculate_top_of_plug(
    plug_bottom_depth_ft: float,
    sacks: Optional[float] = None,
    cement_class: Optional[str] = None,
    hole_size_in: Optional[float] = None,
    volume_bbl: Optional[float] = None
) -> Optional[float]:
    """
    Calculate top of plug depth using RRC/industry formula.
    
    Formula: TOC = BOTTOM_DEPTH − ((SACKS × YIELD_cf/sack) / ANNULAR_CAPACITY_cf/ft)
    
    This uses:
    - Actual cement yield based on cement class
    - Annular capacity based on hole size
    - Slurry volume if directly measured (preferred)
    
    Args:
        plug_bottom_depth_ft: Bottom depth of plug interval (where cement starts filling)
        sacks: Number of cement sacks
        cement_class: Cement class (A, B, C, G, H) - used to determine yield
        hole_size_in: Hole size in inches - used to determine annular capacity
        volume_bbl: Actual slurry volume in barrels (preferred if available)
    
    Returns:
        Calculated top of plug depth in feet, or None if insufficient data
    """
    
    # If we have measured volume, convert to cubic feet and use it directly
    if volume_bbl is not None and volume_bbl > 0:
        cf_per_bbl = 5.6146
        volume_cf = volume_bbl * cf_per_bbl
        
        # We need hole size to calculate annular capacity
        if hole_size_in is not None and hole_size_in > 0:
            annular_capacity = get_annular_capacity_cf_per_ft(hole_size_in)
            if annular_capacity > 0:
                plug_height_ft = volume_cf / annular_capacity
                calculated_toc = plug_bottom_depth_ft - plug_height_ft
                logger.debug(
                    f"Calculated TOC from measured volume: {volume_bbl} bbl ({volume_cf:.1f} cf) ÷ "
                    f"{annular_capacity:.4f} cf/ft = {plug_height_ft:.1f} ft height → TOC @ {calculated_toc:.1f} ft"
                )
                return calculated_toc
    
    # Fall back to sacks-based calculation
    if sacks is None or sacks <= 0:
        return None
    
    if cement_class is None:
        cement_class = "C"  # Default to Class C
    
    if hole_size_in is None or hole_size_in <= 0:
        # If hole size not provided, use 5.5" as conservative default
        hole_size_in = 5.5
    
    # Get cement yield for this class
    cement_yield_cf_per_sack = get_cement_yield_cf_per_sack(cement_class)
    
    # Calculate annular capacity based on hole size
    annular_capacity_cf_per_ft = get_annular_capacity_cf_per_ft(hole_size_in)
    
    # Apply formula: TOC = BOTTOM − ((SACKS × YIELD) / CAPACITY)
    plug_height_ft = (sacks * cement_yield_cf_per_sack) / annular_capacity_cf_per_ft
    calculated_toc = plug_bottom_depth_ft - plug_height_ft
    
    logger.debug(
        f"Calculated TOC from sacks: {sacks} sacks × {cement_yield_cf_per_sack} cf/sk ÷ "
        f"{annular_capacity_cf_per_ft:.4f} cf/ft ({hole_size_in}\" hole) = {plug_height_ft:.1f} ft height → TOC @ {calculated_toc:.1f} ft"
    )
    return calculated_toc


def determine_plug_operation_type(
    event: W3Event,
    w3_events: List[W3Event],
    event_index: int
) -> str:
    """
    Determine if a plug event is a "spot" plug or a "squeeze" plug.
    
    Logic:
    - "spot" plug: Set Intermediate Plug (event_id=4) with "Spot" in detail, no perf context
    - "squeeze" plug: Set Surface Plug (event_id=3) or Squeeze (event_id=7) with "Squeezed" in detail,
                      typically preceded by Perforation event at similar depth
    
    Args:
        event: The plug creation event
        w3_events: Full list of events for context
        event_index: Index of this event in the list
        
    Returns:
        "spot" or "squeeze"
    """
    # Check event type and detail for signals
    event_detail_lower = (event.raw_event_detail or "").lower()
    
    logger.debug(f"🔍 SQUEEZE DETECTION for event_type={event.event_type}, plug#{event.plug_number}")
    logger.debug(f"   raw_event_detail: '{event.raw_event_detail}'")
    logger.debug(f"   event_detail_lower: '{event_detail_lower}'")
    
    # Set Intermediate Plug is typically a "spot" plug (unless squeezed)
    if event.event_type == "set_cement_plug":
        logger.debug(f"   → Event type is SET_CEMENT_PLUG")
        if "squeezed" in event_detail_lower:
            logger.debug(f"   → Contains 'squeezed' → returning SQUEEZE")
            return "squeeze"
        else:
            logger.debug(f"   → No 'squeezed' keyword → returning SPOT")
            return "spot"
    
    # Set Surface Plug or Squeeze with "squeezed" in detail indicates perf & squeeze
    if event.event_type in ("set_surface_plug", "squeeze"):
        logger.debug(f"   → Event type is SET_SURFACE_PLUG or SQUEEZE")
        if "squeezed" in event_detail_lower:
            logger.debug(f"   → Contains 'squeezed' → returning SQUEEZE")
            return "squeeze"
        # If it says "spot" it could be spot into an annulus, treat as squeeze
        logger.debug(f"   → Treating as SQUEEZE (default for surface plug)")
        return "squeeze"
    
    # Default to spot for bridge plugs and others
    logger.debug(f"   → Event type is other ({event.event_type}) → returning SPOT")
    return "spot"


def group_events_into_plugs(
    w3_events: List[W3Event],
    casing_state: List[CasingStringState]
) -> List[Plug]:
    """
    Group related W3Events into logical Plug objects.
    
    Logic:
    - Cement plug events (set_cement_plug, set_surface_plug, squeeze) form plugs
    - Tag TOC events reference/validate the most recently set plug
    - Cut casing events update casing state and affect subsequent plugs
    - Bridge plugs are separate from cement plugs
    - Events are processed in chronological order
    - Determines plug operation type (spot vs squeeze) for accurate hole size selection
    
    Args:
        w3_events: Sorted list of W3Event instances (by date/time)
        casing_state: Current casing state from W-3A
    
    Returns:
        List of Plug objects grouped by plug_number
    """
    plugs: Dict[int, Plug] = {}
    plug_sequence = []
    last_plug_num = None  # Track the most recently created plug
    
    for event_idx, event in enumerate(w3_events):
        logger.debug(f"Processing event: {event.event_type} at depth {event.depth_bottom_ft}")
        
        # Handle casing cuts
        if event.event_type == "cut_casing":
            if event.depth_bottom_ft is not None:
                apply_cut_casing(casing_state, event.depth_bottom_ft)
                logger.info(f"Applied casing cut at {event.depth_bottom_ft} ft")
        
        # Handle plug events
        elif event.event_type in ("set_cement_plug", "set_surface_plug", "squeeze"):
            plug_num = event.plug_number or (len(plugs) + 1)
            logger.info(f"🔌 PLUG EVENT: plug#{plug_num}, depths={event.depth_top_ft}-{event.depth_bottom_ft}, sacks={event.sacks}, class={event.cement_class}")
            
            if plug_num not in plugs:
                # Determine operation type (spot vs squeeze) for hole size selection
                operation_type = determine_plug_operation_type(event, w3_events, event_idx)
                logger.debug(f"Created plug #{plug_num} with operation_type={operation_type}")
                
                plugs[plug_num] = Plug(
                    plug_number=plug_num,
                    depth_top_ft=event.depth_top_ft,  # Bottom of DP/tubing
                    depth_bottom_ft=event.depth_bottom_ft,  # Top of cement after set/squeeze
                    type="cement_plug" if event.event_type != "squeeze" else "squeeze",
                    plug_operation_type=operation_type,  # "spot" or "squeeze"
                    cement_class=event.cement_class,
                    sacks=event.sacks,
                    volume_bbl=event.volume_bbl,
                    # Calculated TOC should reflect user-provided "to" depth (or surface = 0)
                    calculated_top_of_plug_ft=event.depth_bottom_ft,
                )
                plug_sequence.append(plug_num)
                last_plug_num = plug_num  # Track this as most recent
            
            # Add event to plug
            if plug_num in plugs:
                plugs[plug_num].events.append(event)
                # Update remarks from event
                if event.raw_event_detail:
                    if not plugs[plug_num].remarks:
                        plugs[plug_num].remarks = event.raw_event_detail
                    else:
                        plugs[plug_num].remarks += f"\n{event.raw_event_detail}"
        
        # Handle bridge plugs (CIBP, cement retainers, etc.)
        # These are TOOLS, not plugs - they go in remarks, not plug rows
        elif event.event_type == "set_bridge_plug":
            # Don't create a plug entry for bridge plugs
            # They will be included in remarks as part of the operational narrative
            # Just add them to the last plug's events for remarks building
            if last_plug_num and last_plug_num in plugs:
                plugs[last_plug_num].events.append(event)
                logger.debug(f"Added bridge plug tool event to plug #{last_plug_num} remarks")
        
        # Handle tag TOC events
        elif event.event_type == "tag_toc":
            # Attach to the most recently set plug
            if last_plug_num and last_plug_num in plugs:
                plugs[last_plug_num].tag_required = True
                plugs[last_plug_num].events.append(event)
                
                # Store measured TOC from tag event
                if event.tagged_depth_ft is not None:
                    plugs[last_plug_num].measured_top_of_plug_ft = event.tagged_depth_ft
                    logger.info(f"Plug #{last_plug_num} measured TOC: {event.tagged_depth_ft} ft")
                
                if event.tagged_depth_ft:
                    plugs[last_plug_num].remarks = (
                        f"Tagged at {event.tagged_depth_ft} ft"
                        if not plugs[last_plug_num].remarks
                        else f"{plugs[last_plug_num].remarks}\nTagged at {event.tagged_depth_ft} ft"
                    )
        
        # Handle tag bridge plug events
        elif event.event_type == "tag_bridge_plug":
            # Similar logic to tag TOC
            if plugs:
                last_plug_num = plug_sequence[-1] if plug_sequence else None
                if last_plug_num and last_plug_num in plugs:
                    plugs[last_plug_num].events.append(event)
                    if event.tagged_depth_ft:
                        plugs[last_plug_num].remarks = (
                            f"Tagged bridge plug at {event.tagged_depth_ft} ft"
                            if not plugs[last_plug_num].remarks
                            else f"{plugs[last_plug_num].remarks}\nTagged at {event.tagged_depth_ft} ft"
                        )
        
        # Handle perforation events
        elif event.event_type == "perforate":
            # Perforations are tracked separately in formatter output
            pass
        
        # Handle admin events
        elif event.event_type in ("broke_circulation", "pressure_up", "rrc_approval"):
            # Add to remarks
            if plugs and plug_sequence:
                last_plug_num = plug_sequence[-1]
                if last_plug_num in plugs:
                    plugs[last_plug_num].events.append(event)
    
    # Return plugs in order
    return [plugs[num] for num in plug_sequence if num in plugs]


def format_casing_record(
    w3a_casing_record: List[Dict[str, Any]],
    casing_state: List[CasingStringState]
) -> List[Dict[str, Any]]:
    """
    Format casing record for RRC W-3 form.
    
    Takes W-3A casing data and applies any updates from casing state
    (e.g., casing cuts).
    
    Returns RRC-compliant casing row format:
    {
        "string_type": "surface|intermediate|production|liner",
        "size_in": 11.75,
        "weight_ppf": 47.0,
        "hole_size_in": 14.75,
        "top_ft": 0,
        "bottom_ft": 1717,
        "shoe_depth_ft": 1717,
        "cement_top_ft": 930,
        "removed_to_depth_ft": null  // If casing was cut
    }
    """
    formatted_casings = []
    
    for casing in w3a_casing_record:
        formatted_casing = {
            "string_type": casing.get("string_type"),
            "size_in": casing.get("od_in") or casing.get("size_in"),
            "weight_ppf": casing.get("weight_ppf"),
            "hole_size_in": casing.get("hole_size_in"),
            "top_ft": casing.get("top_ft"),
            "bottom_ft": casing.get("bottom_ft"),
            "shoe_depth_ft": casing.get("shoe_depth_ft"),
            "cement_top_ft": casing.get("cement_top_ft"),
            "removed_to_depth_ft": casing.get("removed_to_depth_ft"),  # From casing state if cut
        }
        formatted_casings.append(formatted_casing)
        logger.debug(f"Formatted casing: {formatted_casing['string_type']} {formatted_casing['size_in']}\"")
    
    return formatted_casings


def format_perforations(
    w3a_perforations: List[Dict[str, Any]],
    w3_events: List[W3Event]
) -> List[Dict[str, Any]]:
    """
    Format perforations/open hole intervals for RRC W-3 form.
    
    Takes W-3A perforation data and merges with any perforation events
    from pnaexchange.
    
    Returns RRC-compliant perforation row format:
    {
        "interval_top_ft": 8110,
        "interval_bottom_ft": 10914,
        "formation": "Spraberry",
        "status": "open|perforated|squeezed|plugged",
        "perforation_date": "2025-01-15"
    }
    """
    formatted_perfs = []
    
    # Start with W-3A perforations
    for perf in w3a_perforations:
        formatted_perf = {
            "interval_top_ft": perf.get("interval_top_ft"),
            "interval_bottom_ft": perf.get("interval_bottom_ft"),
            "formation": perf.get("formation"),
            "status": perf.get("status"),
            "perforation_date": perf.get("perforation_date"),
        }
        formatted_perfs.append(formatted_perf)
    
    # Add new perforations from PNA events
    new_perfs = [e for e in w3_events if e.event_type == "perforate"]
    for perf_event in new_perfs:
        perf_depth = perf_event.perf_depth_ft or perf_event.depth_top_ft or perf_event.depth_bottom_ft
        if perf_depth:
            # Add perforation from PNA event
            # Note: PNA typically only provides a single depth, so we use it for top
            # Bottom depth would need to be provided separately or defaulted
            formatted_perf = {
                "interval_top_ft": perf_depth,
                "interval_bottom_ft": perf_event.depth_bottom_ft or perf_depth,
                "formation": None,  # Not available from PNA events
                "status": "perforated",  # Status from perforation event
                "perforation_date": perf_event.date.strftime("%Y-%m-%d") if perf_event.date else None,
            }
            formatted_perfs.append(formatted_perf)
            logger.info(f"Added perforation from PNA event at {perf_depth} ft on {perf_event.date}")
    
    logger.info(f"Formatted {len(formatted_perfs)} perforation intervals ({len(w3a_perforations)} from W-3A, {len(new_perfs)} from PNA events)")
    return formatted_perfs


def format_plugs_for_rrc(
    plugs: List[Plug],
    casing_state: List[CasingStringState]
) -> List[Dict[str, Any]]:
    """
    Format Plug objects into RRC W-3 form row format.
    
    Each plug becomes one or more rows on the W-3 form, with:
    - Plug number
    - Depths (top/bottom)
    - Cement class and quantity
    - Hole size (from active casing at depth)
    - Top of plug (both measured and calculated)
    - Remarks with operational details
    
    Returns list of formatted plug dictionaries with all fields needed for RRC submission.
    """
    formatted_plugs = []
    
    for plug in plugs:
        # Determine plug hole size from W-3 Column 20 logic
        # This considers: inside innermost casing (spot) vs in annulus (squeeze)
        hole_size_in = None
        logger.debug(f"FORMAT PLUG #{plug.plug_number}: operation_type={plug.plug_operation_type}, depth_bottom={plug.depth_bottom_ft}")
        if plug.depth_bottom_ft is not None:
            hole_size_in = get_plug_hole_size_at_depth(
                casing_state, 
                plug.depth_bottom_ft,
                operation_type=plug.plug_operation_type
            )
            logger.debug(f"   → hole_size_in={hole_size_in}\"")
        else:
            logger.debug(f"   → SKIP: depth_bottom_ft is None!")
            hole_size_in = None
        
        # Calculate TOC if we have volume or sacks (and haven't already calculated)
        calculated_toc = plug.calculated_top_of_plug_ft
        # Use depth_top_ft (bottom of DP where cement starts) for TOC calculation, not depth_bottom_ft
        cement_start_depth_ft = plug.depth_top_ft or plug.depth_bottom_ft
        if calculated_toc is None and cement_start_depth_ft is not None:
            if plug.volume_bbl is not None or plug.sacks is not None:
                calculated_toc = calculate_top_of_plug(
                    cement_start_depth_ft,
                    sacks=plug.sacks,
                    cement_class=plug.cement_class,
                    hole_size_in=hole_size_in,
                    volume_bbl=plug.volume_bbl
                )
        
        # Calculate TOC variance if we have both values
        toc_variance_ft = None
        if plug.measured_top_of_plug_ft is not None and calculated_toc is not None:
            toc_variance_ft = plug.measured_top_of_plug_ft - calculated_toc
        
        # Determine which TOC to use for RRC (prefer measured, fall back to calculated)
        top_of_plug_ft = plug.measured_top_of_plug_ft or plug.calculated_top_of_plug_ft
        
        formatted_plug = {
            "plug_number": plug.plug_number,
            "depth_top_ft": plug.depth_top_ft,
            "depth_bottom_ft": plug.depth_bottom_ft,
            "type": plug.type or "cement_plug",
            "cement_class": plug.cement_class,
            "sacks": plug.sacks,
            "slurry_weight_ppg": plug.slurry_weight_ppg or 14.8,  # Default to 14.8
            "hole_size_in": hole_size_in,
            "top_of_plug_ft": top_of_plug_ft,  # Preferred: measured if available, else calculated
            "measured_top_of_plug_ft": plug.measured_top_of_plug_ft,
            "calculated_top_of_plug_ft": calculated_toc,
            "toc_variance_ft": toc_variance_ft,
            "remarks": plug.remarks or "",
            "cementing_date": plug.events[0].date.strftime("%m/%d/%y") if plug.events and plug.events[0].date else None,
        }

        # Build detailed remarks from events
        event_details = []
        for event in plug.events:
            if event.event_type not in ("tag_toc", "tag_bridge_plug", "broke_circulation"):
                event_details.append(event.raw_event_detail)
        
        if event_details:
            if formatted_plug["remarks"]:
                formatted_plug["remarks"] += "\n" + "\n".join(event_details)
            else:
                formatted_plug["remarks"] = "\n".join(event_details)
        
        # Log variance if present
        if toc_variance_ft is not None:
            logger.debug(f"Plug #{plug.plug_number} TOC variance: {toc_variance_ft:+.1f} ft (measured vs calculated)")
        
        formatted_plugs.append(formatted_plug)
        logger.debug(f"Formatted plug #{plug.plug_number}: {plug.depth_top_ft}-{plug.depth_bottom_ft} ft, hole: {hole_size_in}\"")
    
    return formatted_plugs


def build_remarks_section(
    w3a_remarks: Optional[str],
    w3_events: List[W3Event],
    plugs: List[Plug]
) -> str:
    """
    Build complete remarks section for W-3 form.
    
    Chronological narrative including:
    - W-3A baseline remarks
    - Tool placements (CIBP, cement retainers, etc.)
    - Perforation operations and depths
    - Squeeze/circulation operations
    - Pressures and other operational details
    
    Returns formatted remarks text
    """
    remarks_parts = []
    
    # Start with W-3A remarks if present
    if w3a_remarks:
        remarks_parts.append(w3a_remarks)
    
    # Build chronological narrative from events (sorted by date)
    # Include tools, perfs, squeezes, circs in the narrative
    narrative_events = []
    for event in w3_events:
        # Include: tools, perfs, squeezes, circs, pressure_up, rrc_approval, tag_toc
        if event.event_type in (
            "set_bridge_plug",  # CIBP, cement retainers, etc.
            "perforate",
            "squeeze",
            "pressure_up",
            "broke_circulation",
            "rrc_approval",
            "tag_toc",
            "tag_bridge_plug"
        ):
            if event.raw_event_detail:
                # Format with date if available
                date_str = event.date.strftime("%m/%d/%y") if event.date else ""
                if date_str:
                    narrative_events.append(f"{date_str} {event.raw_event_detail}")
                else:
                    narrative_events.append(event.raw_event_detail)
    
    # Add chronological narrative
    if narrative_events:
        remarks_parts.append("\n".join(narrative_events))
    
    # Join all remarks with line breaks
    final_remarks = "\n".join(remarks_parts)
    
    logger.debug(f"Built remarks section ({len(final_remarks)} chars)")
    return final_remarks


def build_w3_form(
    w3a_form: Dict[str, Any],
    w3_events: List[W3Event],
    casing_state: List[CasingStringState]
) -> W3Form:
    """
    Build complete W3Form from W-3A data, normalized events, and casing state.
    
    This is the main orchestrator for formatting - it ties together all the
    individual formatting functions.
    
    Args:
        w3a_form: Extracted W-3A form data (from w3_extraction.py)
        w3_events: List of normalized W3Event instances (from w3_mapper.py)
        casing_state: Current casing state (from w3_casing_engine.py)
    
    Returns:
        Complete W3Form ready for API response or further processing
    """
    logger.info("🏗️ Building W-3 form from components...")
    
    # Group events into plugs
    plugs = group_events_into_plugs(w3_events, casing_state)
    logger.info(f"✅ Grouped {len(w3_events)} events into {len(plugs)} plugs")
    
    # Format casing record
    formatted_casing = format_casing_record(
        w3a_form.get("casing_record", []),
        casing_state
    )
    logger.info(f"✅ Formatted {len(formatted_casing)} casing strings")
    
    # Format perforations
    formatted_perfs = format_perforations(
        w3a_form.get("perforations", []),
        w3_events
    )
    logger.info(f"✅ Formatted {len(formatted_perfs)} perforations")
    
    # Format plugs for RRC (pass casing_state for hole size determination)
    formatted_plugs = format_plugs_for_rrc(plugs, casing_state)
    logger.info(f"✅ Formatted {len(formatted_plugs)} plugs for RRC")
    
    # Build remarks
    remarks = build_remarks_section(
        w3a_form.get("remarks"),
        w3_events,
        plugs
    )
    logger.info(f"✅ Built remarks section")
    
    # Build final W3Form
    w3_form = W3Form(
        header=w3a_form.get("header", {}),
        plugs=formatted_plugs,
        casing_record=formatted_casing,
        perforations=formatted_perfs,
        duqw=w3a_form.get("duqw", {}),
        remarks=remarks,
        pdf_url=w3a_form.get("pdf_url"),
    )
    
    logger.info("✅ W-3 form build complete!")
    return w3_form


def w3_form_to_dict(w3_form: W3Form) -> Dict[str, Any]:
    """
    Convert W3Form dataclass to dictionary for JSON serialization.
    
    Args:
        w3_form: W3Form instance
    
    Returns:
        Dictionary representation suitable for API response
    """
    return {
        "header": w3_form.header,
        "plugs": w3_form.plugs,
        "casing_record": w3_form.casing_record,
        "perforations": w3_form.perforations,
        "duqw": w3_form.duqw,
        "remarks": w3_form.remarks,
        "pdf_url": w3_form.pdf_url,
    }

