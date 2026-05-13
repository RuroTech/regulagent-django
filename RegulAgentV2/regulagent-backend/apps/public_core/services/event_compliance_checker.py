"""
Event Compliance Checker

Validates per-event plugging operations against regulatory requirements from a
jurisdiction-specific policy pack. Produces granular per-event flags at three
severity levels: violation, warning, info.

No Django model dependencies — operates on pure dict inputs for easy unit testing.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(val: Any) -> Optional[float]:
    """Safely convert a value to float, returning None on failure."""
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _make_flag(
    day_number: int,
    event_index: int,
    event_type: str,
    severity: str,
    rule_id: str,
    rule_label: str,
    detail: str,
    field_name: str,
    citation: str = "",
) -> Dict[str, Any]:
    """Build a flag dict in the standard format."""
    return {
        "day_number": day_number,
        "event_index": event_index,
        "event_type": event_type,
        "severity": severity,
        "rule_id": rule_id,
        "rule_label": rule_label,
        "detail": detail,
        "citation": citation,
        "field_name": field_name,
    }


# ---------------------------------------------------------------------------
# Threshold extraction
# ---------------------------------------------------------------------------

def _extract_thresholds(policy: dict, jurisdiction: str) -> dict:
    """Extract numeric thresholds and config from a policy pack dict."""
    base = policy.get("base", {})
    reqs = base.get("requirements", {})
    cement = base.get("cement_class", {})

    def _req_val(key: str, default=None):
        entry = reqs.get(key, {})
        if isinstance(entry, dict):
            return entry.get("value", default)
        return entry if entry is not None else default

    def _req_citation(key: str) -> str:
        entry = reqs.get(key, {})
        if isinstance(entry, dict):
            return entry.get("text", "")
        return ""

    return {
        "cement_class_cutoff_ft": cement.get("cutoff_ft", 6500),
        "deep_classes": {"G", "H"},
        "min_plug_length_ft": _req_val(
            "surface_casing_shoe_plug_min_ft",
            100 if jurisdiction == "NM" else 50,
        ),
        "min_sacks": _req_val("surface_casing_shoe_plug_min_sacks")
        if jurisdiction == "NM"
        else None,
        "min_woc_hours": _req_val(
            "woc_time_hours",
            4 if jurisdiction == "NM" else 8,
        ),
        "cement_above_cibp_ft": _req_val(
            "cement_above_cibp_min_ft",
            100 if jurisdiction == "NM" else 20,
        ),
        # Citations for use in flag messages
        "_citation_cement_class": _req_citation("surface_casing_shoe_plug_min_ft"),
        "_citation_min_plug": _req_citation("surface_casing_shoe_plug_min_ft"),
        "_citation_min_sacks": _req_citation("surface_casing_shoe_plug_min_sacks"),
        "_citation_woc": _req_citation("woc_time_hours"),
        "_citation_cibp": _req_citation("cement_above_cibp_min_ft"),
    }


# ---------------------------------------------------------------------------
# Rule implementations
# ---------------------------------------------------------------------------

def _r_cement_class_depth(
    event: dict,
    day_number: int,
    event_index: int,
    thresholds: dict,
    jurisdiction: str,
    all_events: list,
) -> List[Dict[str, Any]]:
    """Rule evt_cement_class_depth: cement class must match depth requirements."""
    flags = []
    cutoff = _safe_float(thresholds.get("cement_class_cutoff_ft", 6500))
    deep_classes = thresholds.get("deep_classes", {"G", "H"})
    depth = _safe_float(event.get("depth_bottom_ft"))
    cement_class = event.get("cement_class")

    if depth is None or cement_class is None:
        return flags

    cc = str(cement_class).upper().strip()
    if depth >= (cutoff or 6500) and cc not in deep_classes:
        flags.append(_make_flag(
            day_number=day_number,
            event_index=event_index,
            event_type=event.get("event_type", ""),
            severity="violation",
            rule_id="evt_cement_class_depth",
            rule_label="Cement Class vs Depth",
            detail=(
                f"Class {cc} cement used at {depth:.0f} ft — "
                f"Class G or H required below {cutoff:.0f} ft"
            ),
            field_name="cement_class",
            citation=thresholds.get("_citation_cement_class", ""),
        ))
    return flags


def _r_min_plug_length(
    event: dict,
    day_number: int,
    event_index: int,
    thresholds: dict,
    jurisdiction: str,
    all_events: list,
) -> List[Dict[str, Any]]:
    """Rule evt_min_plug_length: plug must meet minimum length requirement."""
    flags = []
    top = _safe_float(event.get("depth_top_ft"))
    bottom = _safe_float(event.get("depth_bottom_ft"))

    if top is None or bottom is None:
        return flags

    length = abs(bottom - top)
    min_length = _safe_float(thresholds.get("min_plug_length_ft")) or 50.0

    if length < min_length:
        flags.append(_make_flag(
            day_number=day_number,
            event_index=event_index,
            event_type=event.get("event_type", ""),
            severity="warning",
            rule_id="evt_min_plug_length",
            rule_label="Minimum Plug Length",
            detail=(
                f"Plug length {length:.0f} ft is below minimum {min_length:.0f} ft"
            ),
            field_name="depth_top_ft",
            citation=thresholds.get("_citation_min_plug", ""),
        ))
    return flags


def _r_min_sacks(
    event: dict,
    day_number: int,
    event_index: int,
    thresholds: dict,
    jurisdiction: str,
    all_events: list,
) -> List[Dict[str, Any]]:
    """Rule evt_min_sacks: NM only — minimum sack count for cement plugs."""
    flags = []
    if jurisdiction != "NM":
        return flags

    min_sacks = _safe_float(thresholds.get("min_sacks"))
    if min_sacks is None:
        return flags

    sacks = _safe_float(event.get("sacks"))
    if sacks is None:
        return flags

    if sacks < min_sacks:
        flags.append(_make_flag(
            day_number=day_number,
            event_index=event_index,
            event_type=event.get("event_type", ""),
            severity="warning",
            rule_id="evt_min_sacks",
            rule_label="Minimum Cement Sacks (NM)",
            detail=(
                f"Only {sacks:.0f} sacks used — minimum {min_sacks:.0f} sacks required"
            ),
            field_name="sacks",
            citation=thresholds.get("_citation_min_sacks", ""),
        ))
    return flags


def _r_woc_duration(
    event: dict,
    day_number: int,
    event_index: int,
    thresholds: dict,
    jurisdiction: str,
    all_events: list,
) -> List[Dict[str, Any]]:
    """Rule evt_woc_duration: wait-on-cement time must meet minimum."""
    flags = []
    woc_hours = _safe_float(event.get("woc_hours"))
    if woc_hours is None:
        return flags

    min_woc = _safe_float(thresholds.get("min_woc_hours")) or 4.0

    if woc_hours < min_woc:
        flags.append(_make_flag(
            day_number=day_number,
            event_index=event_index,
            event_type=event.get("event_type", ""),
            severity="violation",
            rule_id="evt_woc_duration",
            rule_label="Wait-on-Cement Duration",
            detail=(
                f"Wait-on-cement {woc_hours:.1f} hours is below minimum {min_woc:.0f} hours"
            ),
            field_name="woc_hours",
            citation=thresholds.get("_citation_woc", ""),
        ))
    return flags


def _r_cibp_cap(
    event: dict,
    day_number: int,
    event_index: int,
    thresholds: dict,
    jurisdiction: str,
    all_events: list,
) -> List[Dict[str, Any]]:
    """Rule evt_cibp_cap: a cement cap must follow each bridge plug."""
    flags = []
    cibp_depth = _safe_float(event.get("depth_top_ft"))
    if cibp_depth is None:
        return flags

    min_cement = _safe_float(thresholds.get("cement_above_cibp_ft")) or 100.0
    cibp_day = event.get("day_number", day_number)
    # Resolve position key — all_events entries include day_number and event_index
    cibp_event_index = event_index

    # Look ahead for a subsequent set_cement_plug that covers this CIBP
    covering_found = False
    for later in all_events:
        if later.get("event_type") != "set_cement_plug":
            continue

        later_day = later.get("day_number", 0)
        later_idx = later.get("event_index", 0)

        # Must be at the same position or later in chronological order
        is_later = (later_day > day_number) or (
            later_day == day_number and later_idx >= cibp_event_index
        )
        if not is_later:
            continue

        cement_bottom = _safe_float(later.get("depth_bottom_ft"))
        if cement_bottom is None:
            continue

        # "Covers" = cement bottom is within ±50 ft of the CIBP top depth
        if abs(cement_bottom - cibp_depth) <= 50:
            covering_found = True
            break

    if not covering_found:
        flags.append(_make_flag(
            day_number=day_number,
            event_index=event_index,
            event_type=event.get("event_type", ""),
            severity="warning",
            rule_id="evt_cibp_cap",
            rule_label="Cement Cap Above CIBP",
            detail=(
                f"No cement cap found after bridge plug at {cibp_depth:.0f} ft — "
                f"minimum {min_cement:.0f} ft of cement required above CIBP"
            ),
            field_name="depth_top_ft",
            citation=thresholds.get("_citation_cibp", ""),
        ))
    return flags


def _r_missing_depths(
    event: dict,
    day_number: int,
    event_index: int,
    thresholds: dict,
    jurisdiction: str,
    all_events: list,
) -> List[Dict[str, Any]]:
    """Rule evt_missing_depths: operational events must have depth information."""
    flags = []
    top = _safe_float(event.get("depth_top_ft"))
    bottom = _safe_float(event.get("depth_bottom_ft"))

    if top is None and bottom is None:
        event_type = event.get("event_type", "unknown")
        flags.append(_make_flag(
            day_number=day_number,
            event_index=event_index,
            event_type=event_type,
            severity="warning",
            rule_id="evt_missing_depths",
            rule_label="Missing Depth Information",
            detail=f"No depth information recorded for {event_type} event",
            field_name="depth_top_ft",
        ))
    return flags


def _r_missing_sacks(
    event: dict,
    day_number: int,
    event_index: int,
    thresholds: dict,
    jurisdiction: str,
    all_events: list,
) -> List[Dict[str, Any]]:
    """Rule evt_missing_sacks: cement operations should record sack counts."""
    flags = []
    sacks = event.get("sacks")
    if sacks is None:
        flags.append(_make_flag(
            day_number=day_number,
            event_index=event_index,
            event_type=event.get("event_type", ""),
            severity="info",
            rule_id="evt_missing_sacks",
            rule_label="Missing Sack Count",
            detail="No sacks value recorded for cement operation",
            field_name="sacks",
        ))
    return flags


def _r_missing_cement_class(
    event: dict,
    day_number: int,
    event_index: int,
    thresholds: dict,
    jurisdiction: str,
    all_events: list,
) -> List[Dict[str, Any]]:
    """Rule evt_missing_cement_class: cement plugs should record cement class."""
    flags = []
    cement_class = event.get("cement_class")
    if cement_class is None:
        flags.append(_make_flag(
            day_number=day_number,
            event_index=event_index,
            event_type=event.get("event_type", ""),
            severity="info",
            rule_id="evt_missing_cement_class",
            rule_label="Missing Cement Class",
            detail="No cement class recorded for cement plug",
            field_name="cement_class",
        ))
    return flags


def _r_missing_woc(
    event: dict,
    day_number: int,
    event_index: int,
    thresholds: dict,
    jurisdiction: str,
    all_events: list,
) -> List[Dict[str, Any]]:
    """Rule evt_missing_woc: WOC and tag events must record wait time."""
    flags = []
    woc_hours = event.get("woc_hours")
    if woc_hours is None:
        flags.append(_make_flag(
            day_number=day_number,
            event_index=event_index,
            event_type=event.get("event_type", ""),
            severity="warning",
            rule_id="evt_missing_woc",
            rule_label="Missing Wait-on-Cement Time",
            detail="No wait-on-cement time recorded",
            field_name="woc_hours",
        ))
    return flags


def _r_missing_pressure(
    event: dict,
    day_number: int,
    event_index: int,
    thresholds: dict,
    jurisdiction: str,
    all_events: list,
) -> List[Dict[str, Any]]:
    """Rule evt_missing_pressure: pressure test events should record pressure."""
    flags = []
    pressure_psi = event.get("pressure_psi")
    if pressure_psi is None:
        flags.append(_make_flag(
            day_number=day_number,
            event_index=event_index,
            event_type=event.get("event_type", ""),
            severity="info",
            rule_id="evt_missing_pressure",
            rule_label="Missing Pressure Value",
            detail="No pressure value recorded for pressure test",
            field_name="pressure_psi",
        ))
    return flags


# ---------------------------------------------------------------------------
# Rule registry
# ---------------------------------------------------------------------------

_RULE_REGISTRY: Dict[str, list] = {
    "set_cement_plug": [
        _r_cement_class_depth,
        _r_min_plug_length,
        _r_min_sacks,
        _r_missing_sacks,
        _r_missing_cement_class,
    ],
    "set_bridge_plug": [_r_cibp_cap],
    "woc": [_r_woc_duration, _r_missing_woc],
    "tag_toc": [_r_woc_duration, _r_missing_woc],
    "pressure_test": [_r_missing_pressure],
    "squeeze": [_r_missing_sacks],
}

# _r_missing_depths applies to all operational event types
_OPERATIONAL_TYPES = {
    "set_cement_plug",
    "set_bridge_plug",
    "set_surface_plug",
    "squeeze",
    "circulate",
    "pump_cement",
    "perforate",
    "cut_casing",
    "pull_tubing",
}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def check_events(parse_result: dict, policy: dict, jurisdiction: str) -> dict:
    """Run per-event regulatory compliance checks against a policy pack.

    Args:
        parse_result: DWR parse result dict with a ``days`` list, each day
            containing an ``events`` list.
        policy: Policy pack dict (loaded from YAML) keyed on ``base``,
            ``citations``, etc.
        jurisdiction: Two-letter state code, e.g. ``"NM"`` or ``"TX"``.

    Returns:
        Result dict with ``summary`` and ``flags`` keys.
    """
    thresholds = _extract_thresholds(policy, jurisdiction)

    # Flatten all events with day_number and event_index for look-ahead rules
    all_events: List[Dict[str, Any]] = []
    for day in parse_result.get("days", []):
        day_num = day.get("day_number", 0)
        for idx, event in enumerate(day.get("events", [])):
            all_events.append({
                "day_number": day_num,
                "event_index": idx,
                **event,
            })

    flags: List[Dict[str, Any]] = []
    total_checked = 0

    for day in parse_result.get("days", []):
        day_num = day.get("day_number", 0)
        for idx, event in enumerate(day.get("events", [])):
            event_type = event.get("event_type", "other")
            total_checked += 1

            # Type-specific rules
            for rule_fn in _RULE_REGISTRY.get(event_type, []):
                try:
                    flags.extend(
                        rule_fn(event, day_num, idx, thresholds, jurisdiction, all_events)
                    )
                except Exception:
                    logger.exception(
                        "event_compliance_checker: rule %s failed for event_type=%s day=%s idx=%s",
                        rule_fn.__name__,
                        event_type,
                        day_num,
                        idx,
                    )

            # Generic missing depths check (operational types only)
            if event_type in _OPERATIONAL_TYPES:
                try:
                    flags.extend(
                        _r_missing_depths(event, day_num, idx, thresholds, jurisdiction, all_events)
                    )
                except Exception:
                    logger.exception(
                        "event_compliance_checker: _r_missing_depths failed for day=%s idx=%s",
                        day_num,
                        idx,
                    )

    violations = sum(1 for f in flags if f["severity"] == "violation")
    warnings = sum(1 for f in flags if f["severity"] == "warning")
    info = sum(1 for f in flags if f["severity"] == "info")

    result = {
        "jurisdiction": jurisdiction,
        "policy_id": policy.get("policy_id", ""),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_events_checked": total_checked,
            "violations": violations,
            "warnings": warnings,
            "info": info,
        },
        "flags": flags,
    }

    logger.info(
        "event_compliance_checker: jurisdiction=%s policy_id=%s events=%d violations=%d warnings=%d info=%d",
        jurisdiction,
        policy.get("policy_id", ""),
        total_checked,
        violations,
        warnings,
        info,
    )

    return result
