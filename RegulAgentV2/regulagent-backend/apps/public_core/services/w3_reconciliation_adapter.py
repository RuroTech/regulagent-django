"""W-3 Wizard reconciliation adapter.

Bridges the W3WizardSession / PlanSnapshot domain objects to the
PlugReconciliationEngine input format, then orchestrates the full
reconciliation run.
"""

import logging
import re
from dataclasses import asdict
from enum import Enum
from typing import List, Optional

logger = logging.getLogger(__name__)

# Plug-placement event types — must stay in sync with plug_reconciliation.py
_PLUG_PLACEMENT_TYPES = {
    "set_cement_plug",
    "set_surface_plug",
    "set_bridge_plug",
    "squeeze",
    "pump_cement",
    "circulate",
    "perforate",
}


def extract_planned_plugs_from_snapshot(snapshot) -> list:
    """Map PlanSnapshot.payload['steps'] to the planned plug list format
    expected by PlugReconciliationEngine.

    Args:
        snapshot: PlanSnapshot model instance.

    Returns:
        List of planned plug dicts.
    """
    try:
        payload = snapshot.payload or {}
        steps = payload.get("steps", [])
        if not steps:
            logger.warning(
                "extract_planned_plugs_from_snapshot: snapshot %s has no steps",
                getattr(snapshot, "pk", "unknown"),
            )
            return []

        plugs = []
        for step in steps:
            if not isinstance(step, dict):
                continue
            # Skip non-plug steps (milestones handled separately)
            from apps.public_core.services.operator_packet_importer import _categorize_step
            step_category = step.get("category") or _categorize_step(step.get("step_type", ""))
            if step_category != "plug":
                continue
            plugs.append(
                {
                    "plug_number": step.get("step_number"),
                    "plug_type": step.get("step_type"),
                    "top_ft": step.get("depth_top_ft") or step.get("top_depth_ft"),
                    "bottom_ft": step.get("depth_bottom_ft") or step.get("bottom_depth_ft"),
                    "sacks": step.get("sacks"),
                    "cement_class": step.get("cement_class"),
                    "formation": step.get("formation", ""),
                }
            )
        return plugs

    except Exception:
        logger.exception(
            "extract_planned_plugs_from_snapshot: failed for snapshot %s",
            getattr(snapshot, "pk", "unknown"),
        )
        return []


def extract_planned_milestones_from_snapshot(snapshot) -> list:
    """Extract non-plug (milestone) steps from PlanSnapshot for milestone reconciliation.

    Args:
        snapshot: PlanSnapshot model instance.

    Returns:
        List of milestone step dicts.
    """
    try:
        payload = snapshot.payload or {}
        steps = payload.get("steps", [])
        if not steps:
            return []

        from apps.public_core.services.operator_packet_importer import _categorize_step

        milestones = []
        for step in steps:
            if not isinstance(step, dict):
                continue
            step_category = step.get("category") or _categorize_step(step.get("step_type", ""))
            if step_category != "milestone":
                continue
            milestones.append({
                "step_number": step.get("step_number"),
                "step_type": step.get("step_type"),
                "description": step.get("description", ""),
                "depth_top_ft": step.get("depth_top_ft"),
                "depth_bottom_ft": step.get("depth_bottom_ft"),
            })
        return milestones

    except Exception:
        logger.exception(
            "extract_planned_milestones_from_snapshot: failed for snapshot %s",
            getattr(snapshot, "pk", "unknown"),
        )
        return []


def _enrich_event_from_description(event: dict) -> dict:
    """Use regex to populate structured fields from the ``description`` string.

    Only fills fields that are currently ``None`` — never overwrites existing
    non-null values.  Returns the (mutated) event dict for convenience.

    Patterns handled
    ----------------
    - ``"Plug (# 6) Squeezed (45sxs) class (C) cement from (2500') to (2400')"``
      → plug_number=6, sacks=45, cement_class="C", depth_top_ft=2400, depth_bottom_ft=2500
    - ``"Plug (#5) Set (100 sxs) class (A) cement from (6997') to (6800')"``
      → plug_number=5, sacks=100, cement_class="A", depth_top_ft=6800, depth_bottom_ft=6997
    - ``"Tagged top of plug at (6997')"``
      → tagged_depth_ft=6997
    - ``"Set 4-1/2\" CIBP at 7020'"``
      → depth_bottom_ft=7020, depth_top_ft=7020
    - ``"Bridge Plug, 4"``
      → nothing extracted (no useful numeric depth present)
    """
    desc = event.get("description") or ""
    if not desc:
        return event

    # --- plug number ---
    if event.get("plug_number") is None:
        m = re.search(r"[Pp]lug\s*\(?#?\s*(\d+)\)?", desc)
        if m:
            event["plug_number"] = int(m.group(1))

    # --- sacks ---
    if event.get("sacks") is None:
        m = re.search(r"(\d+)\s*(?:sxs|sacks|sx)\b", desc, re.IGNORECASE)
        if m:
            event["sacks"] = int(m.group(1))

    # --- cement class ---
    if event.get("cement_class") is None:
        m = re.search(r"[Cc]lass\s*\(?([A-Z])\)?", desc)
        if m:
            event["cement_class"] = m.group(1)

    # --- depth from / to (cement squeeze / set plug descriptions) ---
    # Convention: "from (X') to (Y')" where X is the shallower bottom and Y is
    # the deeper top — but DWR descriptions use "from <higher depth> to <lower
    # depth>" meaning bottom of interval first, top second.  We therefore map:
    #   from_val → depth_bottom_ft  (deeper / higher numeric value)
    #   to_val   → depth_top_ft     (shallower / lower numeric value)

    # --- "from (X') to surface" — surface = 0 ft ---
    # "to surface" means the cement interval goes from depth X down to surface (0').
    # Top of cement = 0 (surface), bottom of cement = X (deeper).
    m_surface = re.search(
        r"from\s*\(?(\d+)['′\u2018\u2019]?\)?\s*to\s+surface",
        desc, re.IGNORECASE
    )
    if m_surface:
        from_depth = int(m_surface.group(1))
        event["depth_top_ft"] = 0
        event["depth_bottom_ft"] = from_depth

    if event.get("depth_top_ft") is None or event.get("depth_bottom_ft") is None:
        m = re.search(
            r"from\s*\(?(\d+)['′\u2018\u2019]?\)?\s*to\s*\(?(\d+)['′\u2018\u2019]?\)?",
            desc,
            re.IGNORECASE,
        )
        if m:
            from_val = int(m.group(1))
            to_val = int(m.group(2))
            # Assign shallower (smaller) to top, deeper (larger) to bottom
            top_val = min(from_val, to_val)
            bot_val = max(from_val, to_val)
            if event.get("depth_top_ft") is None:
                event["depth_top_ft"] = top_val
            if event.get("depth_bottom_ft") is None:
                event["depth_bottom_ft"] = bot_val

    # --- "set at / @" single-depth (CIBP, bridge plug, etc.) ---
    if event.get("depth_top_ft") is None and event.get("depth_bottom_ft") is None:
        m = re.search(r"(?:at|@)\s*\(?(\d+)['′\u2018\u2019]?\)?", desc, re.IGNORECASE)
        if m:
            depth = int(m.group(1))
            event["depth_top_ft"] = depth
            event["depth_bottom_ft"] = depth

    # --- tagged depth ---
    if event.get("tagged_depth_ft") is None:
        m = re.search(
            r"[Tt]agged?\s+.*?(?:at|@)?\s*\(?(\d+)['′\u2018\u2019]?\)?",
            desc,
            re.IGNORECASE,
        )
        if m:
            event["tagged_depth_ft"] = int(m.group(1))

    # --- perf_and_circulate with only one depth → assume circulated to surface (0') ---
    if event.get("placement_method") == "perf_and_circulate":
        top = event.get("depth_top_ft")
        bot = event.get("depth_bottom_ft")
        if top is not None and bot is None:
            # "from 150 to <cut off>" → bottom is the deeper depth, top is surface
            event["depth_bottom_ft"] = top
            event["depth_top_ft"] = 0
        elif top is not None and bot is not None and top > 0 and bot == 0:
            # Already set but backwards (top=150, bot=0) → swap
            event["depth_top_ft"] = 0
            event["depth_bottom_ft"] = top

    return event


def extract_actual_events_from_parse_result(parse_result: dict) -> list:
    """Map a DWRParseResult dict to the actual events list format expected
    by PlugReconciliationEngine.

    Only plug-placement events are included (set_cement_plug, set_surface_plug,
    set_bridge_plug, squeeze, pump_cement).

    Args:
        parse_result: Dict stored as JSON on the wizard session.

    Returns:
        List of actual event dicts.
    """
    if not parse_result or not isinstance(parse_result, dict):
        return []

    events: list = []
    days = parse_result.get("days", [])

    for day in days:
        if not isinstance(day, dict):
            continue
        for event in day.get("events", []):
            if not isinstance(event, dict):
                continue
            if event.get("event_type") not in _PLUG_PLACEMENT_TYPES:
                continue
            normalized = {
                "event_type": event.get("event_type"),
                "description": event.get("description"),
                "depth_top_ft": event.get("depth_top_ft"),
                "depth_bottom_ft": event.get("depth_bottom_ft"),
                "sacks": event.get("sacks"),
                "cement_class": event.get("cement_class"),
                "tagged_depth_ft": event.get("tagged_depth_ft"),
                "plug_number": event.get("plug_number"),
                "placement_method": event.get("placement_method"),
                "woc_hours": event.get("woc_hours"),
                "woc_tagged": event.get("woc_tagged"),
            }
            _enrich_event_from_description(normalized)

            # Skip cement removal operations (milling, drilling out) that the
            # DWR parser may have misclassified as set_cement_plug
            desc_lower = (normalized.get("description") or "").lower()
            if any(kw in desc_lower for kw in ("mill", "drill out", "clean out", "ream")):
                if any(kw in desc_lower for kw in ("cement", "plug", "cmt")):
                    continue

            # Skip bridge plug events with no depth (false positives from
            # tallying, pulling, or material listing mentions)
            if (
                normalized.get("event_type") == "set_bridge_plug"
                and normalized.get("depth_top_ft") is None
                and normalized.get("depth_bottom_ft") is None
            ):
                continue

            events.append(normalized)

    # Deduplicate events with same type and depths — keep the one with more data
    seen = {}
    deduped = []
    for ev in events:
        key = (ev.get("event_type"), ev.get("depth_top_ft"), ev.get("depth_bottom_ft"))
        if key in seen:
            # Keep the event with more populated fields
            existing = seen[key]
            existing_score = sum(1 for v in existing.values() if v is not None)
            new_score = sum(1 for v in ev.values() if v is not None)
            if new_score > existing_score:
                deduped[deduped.index(existing)] = ev
                seen[key] = ev
        else:
            seen[key] = ev
            deduped.append(ev)

    # --- Post-process: attach tag_toc and woc data to preceding plug events ---
    # Walk through all events chronologically. When we see a plug event that
    # matches one in our list, set it as "current". When we see tag_toc or woc
    # events, attach their data to "current".
    last_plug = None
    for day in days:
        if not isinstance(day, dict):
            continue
        for event in day.get("events", []):
            if not isinstance(event, dict):
                continue
            etype = event.get("event_type", "")

            # Track the last plug placement event
            if etype in _PLUG_PLACEMENT_TYPES:
                # Find matching event in our deduped list by depth
                top = event.get("depth_top_ft")
                bot = event.get("depth_bottom_ft")
                for ev in deduped:
                    if ev.get("depth_top_ft") == top and ev.get("depth_bottom_ft") == bot:
                        last_plug = ev
                        break

            # tag_toc: extract tagged depth and attach to last plug
            elif etype == "tag_toc" and last_plug is not None:
                desc = event.get("description", "")
                m = re.search(
                    r"(?:tag(?:ged)?|TOC)\s+.*?(?:at|@)?\s*\(?(\d[\d,]*)\s*['\u2032\u2018\u2019)']?",
                    desc,
                    re.IGNORECASE,
                )
                if m:
                    tagged_depth = int(m.group(1).replace(",", ""))
                    if last_plug.get("tagged_depth_ft") is None:
                        last_plug["tagged_depth_ft"] = tagged_depth
                    if last_plug.get("woc_tagged") is None:
                        last_plug["woc_tagged"] = True

            # woc: compute hours from timestamps if available, mark WOC occurred
            elif etype == "woc" and last_plug is not None:
                if last_plug.get("woc_tagged") is None:
                    last_plug["woc_tagged"] = True
                # Compute WOC hours from start_time/end_time
                if last_plug.get("woc_hours") is None:
                    start_t = event.get("start_time")
                    end_t = event.get("end_time")
                    if start_t and end_t:
                        try:
                            from datetime import datetime as _dt
                            def _to_dt(t):
                                if not isinstance(t, str):
                                    return _dt.combine(_dt.today(), t)
                                for fmt in ("%H:%M:%S", "%H:%M"):
                                    try:
                                        return _dt.strptime(t, fmt)
                                    except ValueError:
                                        continue
                                return None
                            s_dt = _to_dt(start_t)
                            e_dt = _to_dt(end_t)
                            if s_dt and e_dt and e_dt > s_dt:
                                hours = round((e_dt - s_dt).total_seconds() / 3600, 1)
                                last_plug["woc_hours"] = hours
                        except Exception:
                            pass
                # Fallback: try to extract hours from description text
                if last_plug.get("woc_hours") is None:
                    desc = event.get("description", "")
                    m = re.search(r"(\d+)\s*(?:hr|hour)", desc, re.IGNORECASE)
                    if m:
                        last_plug["woc_hours"] = int(m.group(1))

    return deduped


def _jsonable(obj):
    """Recursively convert Enum values to their .value for JSON serialization."""
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return type(obj)(_jsonable(v) for v in obj)
    if isinstance(obj, Enum):
        return obj.value
    return obj


def build_w3_reconciliation(session, plan_options: dict = None) -> dict:
    """Run full reconciliation for a wizard session.

    Returns ReconciliationResult as dict (for JSON storage).

    Args:
        session: W3WizardSession model instance.

    Raises:
        ValueError: If no approved plan snapshot is linked to the session.
    """
    from apps.public_core.models import PlanSnapshot  # noqa: F401 — imported for clarity
    from .plug_reconciliation import PlugReconciliationEngine

    # 1. Load approved plan snapshot
    snapshot = session.plan_snapshot
    if not snapshot:
        # No plan linked — skip reconciliation, return empty result
        return {
            "comparisons": [],
            "summary": {
                "total_planned": 0,
                "total_actual": 0,
                "matches": 0,
                "minor_deviations": 0,
                "major_deviations": 0,
                "added_plugs": 0,
                "missing_plugs": 0,
                "overall_status": "Skipped — no approved plan linked",
            },
            "skipped": True,
        }

    # 2. Extract planned plugs from snapshot
    planned_plugs = extract_planned_plugs_from_snapshot(snapshot)

    # 3. Extract actual events from parse result
    actual_events = extract_actual_events_from_parse_result(
        session.parse_result or {}
    )

    # 4. Run reconciliation
    engine = PlugReconciliationEngine()
    result = engine.reconcile(
        planned_plugs=planned_plugs,
        actual_events=actual_events,
        api_number=getattr(session, "api_number", ""),
    )

    # 5. Search variance approvals for each comparison with deviations
    well = getattr(session, "well", None)
    if well:
        for comp in result.comparisons:
            if comp.deviation_level.value in ("major", "minor"):
                engine.search_variance_approvals(well, comp)

    # 5b. AI-extract justifications from daily logs for non-MATCH comparisons
    try:
        from .justification_extractor import extract_justifications_from_daily_logs

        non_match = [
            _jsonable(asdict(c))
            for c in result.comparisons
            if c.deviation_level.value != "match"
        ]
        if non_match:
            ai_justifications = extract_justifications_from_daily_logs(
                comparisons=non_match,
                parse_result=session.parse_result or {},
                api_number=getattr(session, "api_number", ""),
            )
            # Write AI suggestions back onto PlugComparison objects
            for comp in result.comparisons:
                plug_key = str(comp.plug_number)
                if plug_key in ai_justifications:
                    info = ai_justifications[plug_key]
                    if not comp.justification_note:  # Don't overwrite existing
                        comp.justification_note = info.get("note", "")
                    comp.justification_source_type = info.get("source_type", "")
                    comp.justification_confidence = info.get("confidence", 0.0)
                    comp.justification_source_days = info.get("source_days", [])
    except Exception:
        logger.exception(
            "build_w3_reconciliation: non-fatal error in AI justification extraction"
        )

    # 6. Count unresolved/resolved divergences
    result.unresolved_divergences = sum(
        1
        for c in result.comparisons
        if c.deviation_level.value in ("major", "minor", "added", "missing")
        and not c.justification_resolved
    )
    result.resolved_divergences = sum(
        1 for c in result.comparisons if c.justification_resolved
    )

    logger.info(
        "build_w3_reconciliation: api=%s unresolved=%d resolved=%d status=%s",
        result.api_number,
        result.unresolved_divergences,
        result.resolved_divergences,
        result.overall_status,
    )

    # 7. Run milestone reconciliation (non-fatal)
    milestone_comparisons = []
    try:
        from .milestone_reconciler import reconcile as reconcile_milestones
        planned_milestones = extract_planned_milestones_from_snapshot(snapshot)
        if planned_milestones:
            milestone_results = reconcile_milestones(
                planned_milestones=planned_milestones,
                parse_result=session.parse_result or {},
            )
            milestone_comparisons = [
                _jsonable(asdict(m)) for m in milestone_results
            ]
    except Exception:
        logger.exception(
            "build_w3_reconciliation: non-fatal error in milestone reconciliation"
        )

    # 8. Convert to dict for JSON storage
    result_dict = _jsonable(asdict(result))
    result_dict["milestone_comparisons"] = milestone_comparisons
    result_dict["plan_options"] = plan_options or {}
    return result_dict
