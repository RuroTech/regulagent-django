import logging

from rest_framework import serializers
from apps.public_core.models import W3WizardSession

logger = logging.getLogger(__name__)


def _inject_dwr_cibp_tools(geometry: dict, parse_result: dict) -> None:
    """Inject CIBP (and packer/retainer) tool placements from DWR parse_result into
    geometry["existing_tools"] in-place.

    Mirrors the Step 7b logic in tasks_w3_wizard.py so that DWR-placed tools are
    visible when the AsPluggedWBD is first rendered (i.e. before W-3 generation runs).

    Deduplicates against tools already present using a ±5 ft depth tolerance.
    """
    if not parse_result or not geometry:
        return

    # Collect DWR tool events
    dwr_tools = []
    for day in parse_result.get("days", []):
        day_num = day.get("day_number", "?")
        for event in day.get("events", []):
            evt_type = event.get("event_type", "")
            depth = event.get("depth_top_ft") or event.get("depth_ft")
            if depth is None:
                continue
            if evt_type == "set_bridge_plug":
                dwr_tools.append({
                    "tool_type": "CIBP",
                    "type": "CIBP",
                    "depth_ft": depth,
                    "top_ft": depth,
                    "description": f"CIBP set at {depth} ft (from DWR)",
                    "source": "dwr_actuals",
                    "notes": f"Placed during plugging - Day {day_num}",
                })
            elif evt_type == "set_packer":
                dwr_tools.append({
                    "tool_type": "PACKER",
                    "type": "PACKER",
                    "depth_ft": depth,
                    "top_ft": depth,
                    "description": f"Packer set at {depth} ft (from DWR)",
                    "source": "dwr_actuals",
                    "notes": f"Placed during plugging - Day {day_num}",
                })
            elif evt_type == "set_retainer":
                dwr_tools.append({
                    "tool_type": "RETAINER",
                    "type": "RETAINER",
                    "depth_ft": depth,
                    "top_ft": depth,
                    "description": f"Retainer set at {depth} ft (from DWR)",
                    "source": "dwr_actuals",
                    "notes": f"Placed during plugging - Day {day_num}",
                })

    if not dwr_tools:
        return

    existing = geometry.get("existing_tools")
    if not isinstance(existing, list):
        existing = []

    added = 0
    for tool in dwr_tools:
        tool_depth = tool["depth_ft"]
        tool_type = tool["tool_type"].upper()
        # Deduplicate: skip if a same-type tool already exists within ±5 ft
        duplicate = any(
            (
                (t.get("tool_type") or t.get("type") or "").upper() == tool_type
                and abs(float(t.get("depth_ft") or t.get("top_ft") or 0) - float(tool_depth)) <= 5
            )
            for t in existing
        )
        if not duplicate:
            existing.append(tool)
            added += 1

    if added:
        geometry["existing_tools"] = existing
        logger.debug(
            "_inject_dwr_cibp_tools: added %d DWR tool(s) to well_geometry existing_tools",
            added,
        )


def _apply_cut_casing_events_to_geometry(geometry: dict, parse_result: dict) -> None:
    """Apply cut_casing events from parse_result to the geometry casing_strings in-place.

    For each event with event_type == "cut_casing", find the matching casing string
    by depth (the event's depth_top_ft falls within the casing's top_ft..bottom_ft range)
    and set removed_to_depth_ft on that casing entry.

    Uses apply_cut_casing() from w3_casing_engine if available, falling back to inline
    logic that sets removed_to_depth_ft on the innermost (smallest size_in) casing at
    the cut depth.
    """
    if not parse_result or not geometry:
        return

    casing_strings = geometry.get("casing_strings", [])
    if not casing_strings:
        return

    try:
        from apps.public_core.services.w3_casing_engine import apply_cut_casing
        from apps.public_core.models.w3_event import CasingStringState

        # Build a mutable CasingStringState list from the geometry casing dicts
        casing_state = []
        for c in casing_strings:
            try:
                size_in = float(c.get("size_in") or 0)
                top_ft = float(c.get("top_ft") or 0)
                bottom_ft = float(c.get("bottom_ft") or 0)
                if size_in <= 0 or bottom_ft <= 0:
                    continue
                casing_state.append(
                    CasingStringState(
                        name=c.get("string", "unknown"),
                        od_in=size_in,
                        top_ft=top_ft,
                        bottom_ft=bottom_ft,
                        hole_size_in=float(c["hole_size_in"]) if c.get("hole_size_in") is not None else None,
                        removed_to_depth_ft=(
                            float(c["removed_to_depth_ft"])
                            if c.get("removed_to_depth_ft") is not None
                            else None
                        ),
                    )
                )
            except (TypeError, ValueError):
                continue

        # Apply each cut_casing event
        for day in parse_result.get("days", []):
            for event in day.get("events", []):
                if event.get("event_type") != "cut_casing":
                    continue
                depth = event.get("depth_top_ft")
                if depth is None:
                    continue
                try:
                    apply_cut_casing(casing_state, float(depth))
                except Exception as e:
                    logger.warning("_apply_cut_casing_events_to_geometry: apply_cut_casing failed at depth %s: %s", depth, e)

        # Write results back to the geometry casing dicts
        for c in casing_strings:
            try:
                size_in = float(c.get("size_in") or 0)
                top_ft = float(c.get("top_ft") or 0)
                bottom_ft = float(c.get("bottom_ft") or 0)
            except (TypeError, ValueError):
                continue
            for cs in casing_state:
                if abs(cs.od_in - size_in) < 0.01 and abs(cs.top_ft - top_ft) < 1 and abs(cs.bottom_ft - bottom_ft) < 1:
                    c["removed_to_depth_ft"] = cs.removed_to_depth_ft
                    break

    except Exception as exc:
        logger.warning("_apply_cut_casing_events_to_geometry: failed (non-fatal): %s", exc, exc_info=True)


class PlanVerificationSerializer(serializers.Serializer):
    """Accepts corrected plan payload sections for verification."""
    well_header = serializers.DictField(required=False, default=dict)
    steps = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    formations = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    casing_record = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    existing_perforations = serializers.ListField(child=serializers.DictField(), required=False, default=list)


class W3WizardCreateSerializer(serializers.Serializer):
    """Create a new wizard session."""
    api_number = serializers.CharField(max_length=20)
    workspace_id = serializers.IntegerField(required=False, allow_null=True)


class W3WizardSessionSerializer(serializers.ModelSerializer):
    """Full session state for resume/display."""
    jurisdiction = serializers.CharField(read_only=True)
    form_type = serializers.CharField(read_only=True)
    plan_snapshot_well_geometry = serializers.SerializerMethodField()
    plan_snapshot_well_header = serializers.SerializerMethodField()
    plan_snapshot_payload = serializers.SerializerMethodField()

    def _apply_excel_overrides(self, geometry: dict, obj) -> None:
        """Merge excel_overrides from reconciliation_result into geometry in-place."""
        if not hasattr(obj, 'reconciliation_result') or not obj.reconciliation_result:
            return
        overrides = obj.reconciliation_result.get("excel_overrides", {})
        override_geom = overrides.get("well_geometry", {})
        if not override_geom:
            return
        for key in ["casing_strings", "formation_tops", "perforations", "tubing", "tools"]:
            if override_geom.get(key):
                geometry[key] = override_geom[key]

    def get_plan_snapshot_well_geometry(self, obj):
        from apps.public_core.services.well_geometry_builder import (
            build_well_geometry, normalize_casing_for_frontend
        )

        if not obj.plan_snapshot or not hasattr(obj.plan_snapshot, 'payload') or not obj.plan_snapshot.payload:
            return None

        payload = obj.plan_snapshot.payload

        # Case 1: payload already has well_geometry (in-system plan or vision-extracted)
        existing_geom = payload.get("well_geometry")
        if existing_geom and isinstance(existing_geom, dict) and existing_geom.get("casing_strings"):
            existing_geom["casing_strings"] = normalize_casing_for_frontend(existing_geom["casing_strings"])
            # If liner array is empty, try to recover liner from top-level casing_record
            if not existing_geom.get("liner"):
                liner_from_cr = []
                for cr in payload.get("casing_record", []):
                    if isinstance(cr, dict):
                        st = (cr.get("string_type") or cr.get("casing_type") or cr.get("string") or "").lower()
                        if "liner" in st:
                            liner_from_cr.append(cr)
                if liner_from_cr:
                    existing_geom["liner"] = liner_from_cr
            if existing_geom.get("liner"):
                existing_geom["liner"] = normalize_casing_for_frontend(existing_geom["liner"])

            # Merge full formation list from plan payload into geometry.
            # Vision extraction may only find a subset of formations from the
            # schematic image, while payload["formations"] has the complete
            # list with accurate depths from the P&A plan document.
            from apps.public_core.services.well_geometry_builder import extract_formations_from_payload
            plan_formations = extract_formations_from_payload(payload)
            if plan_formations:
                existing_ft = existing_geom.get("formation_tops", [])
                # Build a set of formation names already in geometry
                existing_names = set()
                for ft in existing_ft:
                    if isinstance(ft, dict):
                        existing_names.add((ft.get("formation") or "").lower())
                # Merge: use plan formations as base, supplement with any
                # vision-only formations not in the plan list
                plan_names = set()
                merged = []
                for ft in plan_formations:
                    if isinstance(ft, dict) and ft.get("formation"):
                        merged.append(ft)
                        plan_names.add(ft["formation"].lower())
                # Add vision-only formations that aren't in the plan list
                for ft in existing_ft:
                    if isinstance(ft, dict) and ft.get("formation"):
                        if ft["formation"].lower() not in plan_names:
                            merged.append(ft)
                existing_geom["formation_tops"] = merged

            _apply_cut_casing_events_to_geometry(existing_geom, obj.parse_result)
            _inject_dwr_cibp_tools(existing_geom, obj.parse_result)
            self._apply_excel_overrides(existing_geom, obj)
            return existing_geom

        # Case 2: Source from ExtractedDocuments (operator packet import path)
        api14 = obj.api_number
        if not api14:
            return None

        geometry = build_well_geometry(api14, payload, jurisdiction=obj.jurisdiction)
        geometry["casing_strings"] = normalize_casing_for_frontend(geometry.get("casing_strings", []))
        # If liner array is empty, try to recover liner from top-level casing_record
        if not geometry.get("liner"):
            liner_from_cr = []
            for cr in payload.get("casing_record", []):
                if isinstance(cr, dict):
                    st = (cr.get("string_type") or cr.get("casing_type") or cr.get("string") or "").lower()
                    if "liner" in st:
                        liner_from_cr.append(cr)
            if liner_from_cr:
                geometry["liner"] = liner_from_cr
        if geometry.get("liner"):
            geometry["liner"] = normalize_casing_for_frontend(geometry["liner"])
        _apply_cut_casing_events_to_geometry(geometry, obj.parse_result)
        _inject_dwr_cibp_tools(geometry, obj.parse_result)
        self._apply_excel_overrides(geometry, obj)
        return geometry

    def get_plan_snapshot_well_header(self, obj):
        if obj.plan_snapshot and hasattr(obj.plan_snapshot, 'payload') and obj.plan_snapshot.payload:
            return obj.plan_snapshot.payload.get("well_header", {})
        return None

    def get_plan_snapshot_payload(self, obj):
        """Return full plan snapshot payload sections for verification UI."""
        if not obj.plan_snapshot or not hasattr(obj.plan_snapshot, 'payload') or not obj.plan_snapshot.payload:
            return None
        payload = obj.plan_snapshot.payload
        return {
            "well_header": payload.get("well_header", {}),
            "steps": payload.get("steps", []),
            "formations": payload.get("formations", []),
            "casing_record": payload.get("casing_record", []),
            "existing_perforations": payload.get("existing_perforations", []),
        }

    class Meta:
        model = W3WizardSession
        fields = [
            "id", "well", "plan_snapshot", "w3_form",
            "tenant_id", "workspace", "api_number", "status",
            "current_step", "uploaded_documents", "parse_result",
            "reconciliation_result", "justifications",
            "w3_generation_result", "celery_task_id", "plan_import_task_id",
            "created_by", "created_at", "updated_at", "last_accessed_at",
            "jurisdiction", "form_type", "plan_snapshot_well_geometry",
            "plan_snapshot_well_header", "plan_snapshot_payload",
            "event_compliance_flags",
        ]
        read_only_fields = fields


class W3WizardListSerializer(serializers.ModelSerializer):
    """Lightweight list view."""
    class Meta:
        model = W3WizardSession
        fields = [
            "id", "api_number", "status", "current_step",
            "created_by", "created_at", "updated_at",
        ]
        read_only_fields = fields


class W3WizardJustificationsSerializer(serializers.Serializer):
    """Partial update for justifications."""
    justifications = serializers.DictField(
        child=serializers.DictField(),
        help_text="{plug_number: {note, resolved, resolved_by, resolved_at}}"
    )


class W3WizardUploadResponseSerializer(serializers.Serializer):
    """Response after file upload."""
    uploaded_count = serializers.IntegerField()
    documents = serializers.ListField(child=serializers.DictField())
    session_status = serializers.CharField()


class TaskStatusSerializer(serializers.Serializer):
    """Celery task polling response."""
    task_id = serializers.CharField()
    status = serializers.CharField()  # PENDING, STARTED, SUCCESS, FAILURE
    session_status = serializers.CharField()
    result = serializers.DictField(required=False)
